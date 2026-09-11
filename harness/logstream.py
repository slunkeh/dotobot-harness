"""Per-server log stream: the harness's own log, and every bot's, tailed live.

Debugging a hosted harness used to mean SSH plus `journalctl` (the server only
ever printed to stdout) or `harness logs <bot>` cat-ing `run/<bot>.log` in
full. This module gives every log one read/follow contract so the API, the
WebSocket and the CLI can all stream it:

* **Capture.** `install_server_log(paths)` wraps `sys.stdout` / `sys.stderr`
  in a tee: every write still reaches the original stream (the systemd
  journal keeps working), and every complete line is also appended to
  `$HARNESS_HOME/run/server/serve.log` as `[YYYY-MM-DD HH:MM:SS] <line>` after the
  redaction registry has scrubbed it. The file is size-capped
  (`HARNESS_SERVER_LOG_MAX_BYTES`, default 5 MiB): past the cap it rotates
  once to `serve.log.1`. Capture never raises — a log that cannot be written
  is dropped, never a reason for serve to fail.
* **Sources.** `server` is the harness's own log (`run/server/serve.log`,
  its own directory so no bot's `run/<bot>.log` can ever be the same file);
  a bot's log is addressed by its roster name (or `bot:<name>` explicitly,
  which is also how a bot literally named `server` is reached). Ids follow
  the roster's own name rule (one safe path component) and always resolve
  to a regular file inside `run/` — never through a symlink, never a FIFO.
* **Reading.** `tail()` returns the last N complete lines plus a byte
  `offset` and the file's identity (`gen`, its inode); `read_from(offset)`
  returns only the complete lines appended since. A partial trailing line
  is held back until its newline lands; a file that shrank below the offset
  (truncated) or whose identity changed (rotated) restarts from 0 with
  `reset=True`. `LogFollower` holds the file open, so when it rotates the
  old generation is drained to its end before the new one starts. Lines are
  scrubbed again at read time with what the reading process's registry
  knows (in serve: the link key and code, plus any secret stored or
  resolved during its lifetime).

Only Python-level writes are captured: a child process that inherits fd 1
writes to the journal alone, as it always did. Bot logs are what they always
were — the bot process's redirected stdout, already scrubbed line by line by
`Agent._log` — this module only reads them.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from .paths import HarnessPaths
from .redaction import scrub as scrub_secrets
from .roster import valid_bot_name

SERVER_SOURCE = "server"
#: The server's own log lives in its own directory under run/: a bot's log is
#: run/<bot>.log and a bot may be called anything one path component allows
#: (`serve` included), so a sibling file could be claimed by a bot.
SERVER_LOG_DIR = "server"
SERVER_LOG_NAME = "serve.log"
BOT_PREFIX = "bot:"

DEFAULT_LINES = 200
MAX_LINES = 2000
#: Most bytes one page reads (a tail stops here; an offset page reports `truncated`).
DEFAULT_PAGE_BYTES = 256 * 1024
DEFAULT_ROTATE_BYTES = 5 * 1024 * 1024
#: A line with no newline for this long is written anyway (a stuck partial write).
_PARTIAL_CAP = 64 * 1024
_BLOCK = 8192


def poll_interval() -> float:
    try:
        return max(0.05, float(os.environ.get("HARNESS_LOG_POLL_INTERVAL", "0.25")))
    except ValueError:
        return 0.25


def heartbeat_interval() -> float:
    try:
        return max(1.0, float(os.environ.get("HARNESS_LOG_HEARTBEAT", "15")))
    except ValueError:
        return 15.0


def rotate_bytes() -> int:
    raw = os.environ.get("HARNESS_SERVER_LOG_MAX_BYTES", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_ROTATE_BYTES
    except ValueError:
        return DEFAULT_ROTATE_BYTES
    return max(64 * 1024, value)


# -- sources ---------------------------------------------------------------


def server_log_file(paths: HarnessPaths) -> Path:
    return paths.run / SERVER_LOG_DIR / SERVER_LOG_NAME


def _not_a_link(path: Path) -> bool:
    """Neither the file nor its directory is a symlink: a link planted under
    run/ must never redirect a read (or the capture's append) elsewhere."""
    return not path.is_symlink() and not path.parent.is_symlink()


def previous_generation(path: Path) -> Path | None:
    """The rotated predecessor of a log (`<name>.1`), when one exists."""
    rotated = path.with_name(path.name + ".1")
    return rotated if rotated.is_file() and not rotated.is_symlink() else None


def _bot_log_path(paths: HarnessPaths, name: str) -> Path | None:
    """run/<name>.log for a name the roster itself would accept (one safe
    path component: `valid_bot_name`), sitting directly inside `run/` and
    not a symlink — `run/` also holds pid files and staged machine secrets,
    and a link named like a log must never read one of those."""
    if not valid_bot_name(name):
        return None
    path = paths.log_file(name)
    root = paths.run.resolve()
    try:
        path.resolve().relative_to(root)
    except (ValueError, OSError):
        return None
    if path.parent.resolve() != root or not _not_a_link(path):
        return None
    return path


def log_path_for(paths: HarnessPaths, source: str) -> Path | None:
    """The file a source id names, validated by shape only (no roster check).

    `server` -> run/server/serve.log; `bot:<name>` or a bare name ->
    run/<name>.log. (The CLI reads a departed bot's log with this; the API
    adds the roster check in `resolve_source`.)
    """
    raw = (source or "").strip()
    if raw == SERVER_SOURCE:
        path = server_log_file(paths)
        return path if _not_a_link(path) else None
    if raw.startswith(BOT_PREFIX):
        raw = raw[len(BOT_PREFIX) :]
    return _bot_log_path(paths, raw)


def resolve_source(paths: HarnessPaths, source: str, bots: Iterable[str]) -> Path | None:
    """The file behind a source id, refusing any bot not in the roster. A
    roster name that literally is the id wins over the `bot:` prefix, so a
    hand-edited roster with a bot called `bot:atlas` still round-trips."""
    raw = (source or "").strip()
    if raw == SERVER_SOURCE:
        return log_path_for(paths, SERVER_SOURCE)
    known = set(bots)
    if raw in known:
        return _bot_log_path(paths, raw)
    if raw.startswith(BOT_PREFIX) and raw[len(BOT_PREFIX) :] in known:
        return _bot_log_path(paths, raw[len(BOT_PREFIX) :])
    return None


def _describe(source: str, kind: str, name: str, path: Path) -> dict:
    try:
        st = path.stat()
        size, modified, exists = st.st_size, st.st_mtime, True
    except OSError:
        size, modified, exists = 0, None, False
    # No `path`: clients address a log by id; the host layout stays the
    # operator's (`harness paths`, `harness logs`).
    return {
        "id": source,
        "kind": kind,
        "name": name,
        "exists": exists,
        "size": size,
        "modified": modified,
    }


def list_sources(paths: HarnessPaths, bots: Iterable[str]) -> list[dict]:
    """Every log a client may tail: the server first, then the roster's bots."""
    server = _describe(SERVER_SOURCE, "server", "harness", server_log_file(paths))
    capture = installed()
    # Lines the capture could not write (permissions, disk full): a gap a
    # client would otherwise take for silence.
    server["dropped"] = capture.dropped if capture is not None else 0
    out = [server]
    for name in bots:
        path = _bot_log_path(paths, name)
        if path is None:
            continue  # a roster name that is not addressable as a source id
        source = BOT_PREFIX + name if name == SERVER_SOURCE else name
        out.append(_describe(source, "bot", name, path))
    return out


# -- reading -----------------------------------------------------------------


@dataclass
class LogPage:
    """One read of a log: complete lines, the byte offset after them, the
    file's size at read time and its identity (`gen`: the inode, so a
    offset can be told apart from an offset into a rotated predecessor).
    `truncated` says not everything available was returned (older lines
    beyond a tail's byte budget, or more lines past `offset` on an offset
    read — read again). `reset` says the file was truncated or replaced, so
    a client's view should be cleared first: the lines are the new file
    from byte 0. `exists=False` says the file is not there: a tail or
    offset read of a missing log (the page agrees with the listing), or a
    follower's page when the file went away — a deleted bot, a purged log —
    and the view is empty until it comes back."""

    lines: list[str] = field(default_factory=list)
    offset: int = 0
    size: int = 0
    truncated: bool = False
    reset: bool = False
    gen: int = 0
    exists: bool = True

    def to_dict(self) -> dict:
        return {
            "lines": list(self.lines),
            "offset": self.offset,
            "size": self.size,
            "truncated": self.truncated,
            "reset": self.reset,
            "gen": self.gen,
            "exists": self.exists,
        }


def _decode_lines(data: bytes) -> list[str]:
    text = data.decode("utf-8", errors="replace")
    return [scrub_secrets(line.rstrip("\r")) for line in text.split("\n")]


def _open_log(path: Path) -> BinaryIO | None:
    """Open a log for reading without following a symlink, refusing anything
    that is not a regular file (a FIFO would block a request thread for
    ever). None when it cannot be opened that way."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        return os.fdopen(fd, "rb")
    except OSError:
        os.close(fd)
        return None


def read_whole(path: Path) -> str | None:
    """The entire log as text (the plain `harness logs <bot>`), opened the
    same guarded way as every other read; None when there is no such file."""
    fh = _open_log(path)
    if fh is None:
        return None
    with fh:
        return fh.read().decode("utf-8", errors="replace")


def _identity(fh: BinaryIO) -> tuple[int, int]:
    """(inode, size) of an open log."""
    st = os.fstat(fh.fileno())
    return st.st_ino, st.st_size


def _inode(path: Path) -> int | None:
    try:
        return os.stat(path, follow_symlinks=False).st_ino
    except OSError:
        return None


def tail(
    path: Path,
    *,
    lines: int = DEFAULT_LINES,
    max_bytes: int = DEFAULT_PAGE_BYTES,
    previous: Path | None = None,
) -> LogPage:
    """The last `lines` complete lines of `path` and the offset to follow from.

    With `previous` (the rotated generation), a live file that is shorter
    than the request is topped up from the end of that one — right after a
    rotation the burst that caused it is exactly what an operator wants to
    read. `offset`/`gen` always describe the live file."""
    page = _tail_one(path, lines=lines, max_bytes=max_bytes)
    if previous is not None and lines and len(page.lines) < lines and not page.truncated:
        older = _tail_one(previous, lines=lines - len(page.lines), max_bytes=max_bytes)
        if older.lines:
            page.lines = older.lines + page.lines
            page.truncated = older.truncated
    return page


def _tail_one(path: Path, *, lines: int, max_bytes: int) -> LogPage:
    lines = max(0, lines)
    fh = _open_log(path)
    if fh is None:
        return LogPage(exists=False)
    with fh:
        gen, size = _identity(fh)
        chunks: list[bytes] = []
        pos = size
        newlines = 0
        read_total = 0
        while pos > 0 and newlines <= lines and read_total < max_bytes:
            step = min(_BLOCK, pos, max_bytes - read_total)
            pos -= step
            fh.seek(pos)
            chunk = fh.read(step)
            chunks.append(chunk)
            newlines += chunk.count(b"\n")
            read_total += step
    data = b"".join(reversed(chunks))
    end = data.rfind(b"\n")
    if end == -1:
        if pos > 0 and data:
            # A line longer than the byte budget, still unfinished: show its
            # cut tail (as `read_from` would) and follow from the end, rather
            # than dropping it and letting a follower start past it.
            return LogPage(_decode_lines(data), size, size, truncated=True, gen=gen)
        # A short partial line: held back until its newline lands.
        return LogPage([], size - len(data), size, gen=gen)
    partial = len(data) - (end + 1)
    offset = size - partial
    complete = _decode_lines(data[: end + 1])[:-1]  # drop the empty tail after the last \n
    if pos > 0 and len(complete) > 1:
        complete = complete[1:]  # the first piece may start mid-line
    # (a lone piece is a finished line longer than the budget: kept, cut)
    dropped = len(complete) - lines if lines else len(complete)
    if dropped > 0:
        complete = complete[-lines:] if lines else []
    return LogPage(complete, offset, size, truncated=pos > 0 or dropped > 0, gen=gen)


def _read_after(fh: BinaryIO, offset: int, *, max_bytes: int) -> LogPage:
    """Complete lines after byte `offset` of an open log. A file that shrank
    below the offset restarts at 0 with `reset`. A line longer than the
    window is emitted cut (and `truncated`) rather than stalling the reader
    until a newline lands inside the window."""
    gen, size = _identity(fh)
    reset = False
    if offset > size:
        offset, reset = 0, True
    fh.seek(offset)
    data = fh.read(max_bytes)
    remaining = size - offset - len(data)
    end = data.rfind(b"\n")
    if end == -1:
        if data and len(data) >= max_bytes:
            return LogPage(
                _decode_lines(data), offset + len(data), size, truncated=True, reset=reset, gen=gen
            )
        return LogPage([], offset, size, truncated=False, reset=reset, gen=gen)
    complete = data[: end + 1]
    return LogPage(
        _decode_lines(complete)[:-1],
        offset + len(complete),
        size,
        truncated=remaining > 0,
        reset=reset,
        gen=gen,
    )


def read_from(
    path: Path, offset: int, *, max_bytes: int = DEFAULT_PAGE_BYTES, gen: int | None = None
) -> LogPage:
    """Complete lines appended after byte `offset`; restarts at 0 (saying so
    with `reset`) when the file shrank below it or, given the `gen` the
    offset came from, when the file is no longer that one (rotated and
    regrown past the offset — a stateless poll's only tell)."""
    offset = max(0, offset)
    fh = _open_log(path)
    if fh is None:
        return LogPage([], 0, 0, reset=offset > 0, exists=False)
    with fh:
        current, _size = _identity(fh)
        if gen is not None and gen != current and offset > 0:
            page = _read_after(fh, 0, max_bytes=max_bytes)
            page.reset = True
            return page
        return _read_after(fh, offset, max_bytes=max_bytes)


class LogFollower:
    """Poll a log from an offset, holding it open: when the path is replaced
    (rotation) the old generation is read to its end first, then the new
    file is followed from 0 with `reset`. `gen` is the identity the offset
    belongs to (from a tail); a different file under that name resets. A
    follower that starts after the file has gone, or announces it gone,
    forgets its cursor: the file's return is read from byte 0."""

    def __init__(self, path: Path, offset: int = 0, *, gen: int | None = None) -> None:
        self.path = path
        self.offset = max(0, offset)
        self.size = 0
        if not gen and self.offset == 0:
            # Gen 0 is "no file when this was taken"; with nothing read there
            # is no view to reset, so the file's birth is plain new lines.
            gen = None
        self.gen = gen
        self._fh: BinaryIO | None = None
        self._rotated = False
        self._pending_reset = False
        self._gone_announced = False
        self._missing = 0  # consecutive failed opens
        # Hold the file from the start: a rotation before the first poll
        # must still find the old generation open, or its tail is lost.
        if not self._open() and (self.offset > 0 or gen):
            # The client had a view of this file and it is already gone:
            # whatever comes back under the name is read from its start.
            self.offset = 0
            self._pending_reset = True

    def _open(self) -> bool:
        fh = _open_log(self.path)
        if fh is None:
            self._missing += 1
            return False
        self._missing = 0
        self._fh = fh
        current, _size = _identity(fh)
        if self.gen is not None and current != self.gen:
            # Not the file the offset was taken from: it rotated between
            # the tail and now (or before a reconnect). Gen 0 is what a
            # heartbeat carries while the file is gone; no real file has it.
            self.offset = 0
            self._pending_reset = True
        self.gen = current
        return True

    def close(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass

    def _switch(self) -> None:
        """The held generation is drained: start over on whatever the path is now."""
        self.close()
        self._rotated = False
        self.gen = None
        self.offset = 0
        self._pending_reset = True

    def poll(self, *, max_bytes: int = DEFAULT_PAGE_BYTES) -> LogPage | None:
        """The next page, or None when nothing changed."""
        if self._fh is not None and not self._rotated:
            try:
                held, _size = _identity(self._fh)
            except OSError:
                held = None
            if _inode(self.path) != held:
                self._rotated = True
        if self._fh is not None and self._rotated:
            page = _read_after(self._fh, self.offset, max_bytes=max_bytes)
            if page.lines:
                self.offset = page.offset
                page.reset = False  # still the old generation
                return page
            self._switch()
        if self._fh is None and not self._open():
            # Missing across two polls is gone (a deleted bot, a purged log):
            # say so once, so a client empties its view instead of hearing
            # heartbeats. One miss is the instant between a rotation's
            # rename and its reopen, not worth a cleared view.
            if self._gone_announced or not self._pending_reset or self._missing < 2:
                return None
            self._gone_announced = True
            # The view is empty now: a heartbeat says {offset: 0, gen: 0},
            # and the file's return is read from its start.
            self.offset, self.size, self.gen = 0, 0, None
            return LogPage([], 0, 0, reset=True, exists=False)
        self._gone_announced = False
        page = _read_after(self._fh, self.offset, max_bytes=max_bytes)
        reset = self._pending_reset or page.reset
        self._pending_reset = False
        self.offset = page.offset
        self.size = page.size
        if not page.lines and not reset:
            return None
        page.reset = reset
        return page


def is_heartbeat(page: LogPage) -> bool:
    """A follow() tick with nothing new: no lines, no reset (offset only)."""
    return not page.lines and not page.reset


def follow(
    path: Path,
    offset: int,
    stop: threading.Event,
    *,
    gen: int | None = None,
    interval: float | None = None,
    heartbeat: float | None = None,
) -> Iterator[LogPage]:
    """Yield a page whenever lines land (or the file was replaced). With
    `heartbeat` set, an empty page carrying the offset is yielded after that
    many quiet seconds, so a writer can probe a client that may be gone
    (`is_heartbeat`). Ends when `stop` is set."""
    interval = poll_interval() if interval is None else interval
    follower = LogFollower(path, offset, gen=gen)
    last_activity = time.monotonic()
    try:
        while not stop.is_set():
            page = follower.poll()
            if page is not None:
                last_activity = time.monotonic()
                yield page
                if page.truncated:
                    continue  # more is waiting; do not sleep on it
            elif heartbeat is not None and time.monotonic() - last_activity >= heartbeat:
                last_activity = time.monotonic()
                yield LogPage([], follower.offset, follower.size, gen=follower.gen or 0)
            stop.wait(interval)
    finally:
        follower.close()


def stream_pages(
    path: Path,
    *,
    lines: int,
    stop: threading.Event,
    write: Callable[[LogPage, str], bool],
    offset: int | None = None,
    gen: int | None = None,
    interval: float | None = None,
    heartbeat: float | None = None,
) -> None:
    """Drive `write(page, mutation)`: a `snapshot` tail first, then an
    `appended` page per batch of new lines, `heartbeat` for a quiet tick. A
    page with `reset` (the file was truncated or replaced) goes out as a
    `snapshot` again: its lines are the new file from byte 0; a file that
    went away is one `cleared` page (`exists=False`). With `offset` (a
    reconnecting client's last one, and the `gen` it belongs to) there is
    no opening snapshot: the first page is whatever landed since. Ends when
    `stop` is set or `write` returns False (the client is gone)."""
    if offset is None:
        first = tail(path, lines=lines, previous=previous_generation(path))
        if write(first, "snapshot") is False:
            return
        offset, gen = first.offset, first.gen
    for page in follow(path, offset, stop, gen=gen, interval=interval, heartbeat=heartbeat):
        if not page.exists:
            mutation = "cleared"
        elif is_heartbeat(page):
            mutation = "heartbeat"
        elif page.reset:
            mutation = "snapshot"
        else:
            mutation = "appended"
        if write(page, mutation) is False:
            return


# -- capture -----------------------------------------------------------------


class ServerLog:
    """Line sink behind the stdout/stderr tee: timestamps, scrubs, appends,
    rotates. Every method swallows OSError — logging never fails serve."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int | None = None,
        alert: Callable[[str], None] | None = None,
    ) -> None:
        self.path = path
        self.max_bytes = max_bytes or rotate_bytes()
        #: told once, the first time a line cannot be written (the journal
        #: still has the line; the file has a gap a client cannot see).
        self._alert = alert
        self._alerted = False
        # Re-entrant: serve's SIGTERM handler prints, and it runs on the main
        # thread wherever that thread happens to be — including inside feed().
        self._lock = threading.RLock()
        self._fh = None
        self._buffers: dict[int, str] = {}
        self._tags: dict[int, str] = {}  # the tag each key's partial line carries
        self.dropped = 0

    # -- tee side --
    def feed(self, key: int, text: str, *, tag: str = "") -> None:
        if not text:
            return
        with self._lock:
            self._tags[key] = tag
            buf = self._buffers.get(key, "") + text
            while True:
                nl = buf.find("\n")
                if nl == -1:
                    break
                self._write_line(buf[:nl], tag)
                buf = buf[nl + 1 :]
            if len(buf) > _PARTIAL_CAP:
                self._write_line(buf, tag)
                buf = ""
            self._buffers[key] = buf

    def write_line(self, line: str, *, tag: str = "") -> None:
        """Append one line directly (no tee): serve's own notes."""
        with self._lock:
            self._write_line(line, tag)

    def flush_partial(self) -> None:
        """Write out buffered partial lines (shutdown)."""
        with self._lock:
            for key, buf in list(self._buffers.items()):
                if buf:
                    self._write_line(buf, self._tags.get(key, ""))
                self._buffers[key] = ""

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    # -- file side (lock held) --
    def _close_locked(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass

    def _open_locked(self):
        if self._fh is None:
            if self.path.parent.is_symlink():
                raise OSError(f"{self.path.parent} is a symlink")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Never through a link: a symlink planted at the log's name must
            # not turn the capture into an append onto some other file.
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.path, flags, 0o644)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError(f"{self.path} is not a regular file")
            except OSError:
                os.close(fd)
                raise
            self._fh = os.fdopen(fd, "a", encoding="utf-8", errors="replace")
        return self._fh

    def _write_line(self, line: str, tag: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        prefix = f"[{stamp}] " + (f"[{tag}] " if tag else "")
        try:
            fh = self._open_locked()
            fh.write(prefix + scrub_secrets(line.rstrip("\r")) + "\n")
            fh.flush()
            if fh.tell() >= self.max_bytes:
                self._rotate_locked()
        except (OSError, ValueError) as exc:
            self.dropped += 1
            self._close_locked()
            self._warn_once(exc)

    def _warn_once(self, exc: Exception) -> None:
        if self._alerted or self._alert is None:
            return
        self._alerted = True
        try:
            self._alert(
                f"warning: server log {self.path} is not being written ({exc}); "
                "lines still reach the journal but GET /api/logs/server will have a gap\n"
            )
        except Exception:  # noqa: BLE001 - the alert must not fail a print either
            pass

    def _rotate_locked(self) -> None:
        self._close_locked()
        try:
            os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        except OSError:
            pass
        # Start the fresh file now, so `serve.log` never goes missing between
        # a rotation and the next line (a tail sees an empty file, a follower
        # sees one inode change).
        try:
            self._open_locked()
        except OSError as exc:
            self.dropped += 1
            self._warn_once(exc)


class _Tee:
    """A text stream that forwards to the original and feeds a ServerLog.

    Anything the wrapped stream offers that this class does not (`buffer`,
    `encoding`, `fileno`, `isatty`, …) is proxied, so `sys.stdout.isatty()`
    and `subprocess.Popen(stdout=sys.stdout)` behave exactly as before. Note
    what that means: a child writing to the inherited fd reaches the journal
    only, never the file — output that must be in serve.log is captured and
    printed by serve.
    """

    def __init__(self, stream, sink: ServerLog, tag: str = "") -> None:
        self._stream = stream
        self._sink = sink
        self._tag = tag

    @property
    def wrapped(self):
        return self._stream

    @property
    def sink(self) -> ServerLog:
        return self._sink

    def write(self, text) -> int:
        if not isinstance(text, str):
            text = str(text)
        # Sink first: a broken original (closed stdout, dead journal pipe)
        # must not cost the file its copy of the line.
        try:
            self._sink.feed(id(self), text, tag=self._tag)
        except Exception:  # noqa: BLE001 - capture never fails a print
            pass
        return self._stream.write(text)

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        self._stream.flush()

    def isatty(self) -> bool:
        try:
            return bool(self._stream.isatty())
        except (AttributeError, ValueError):
            return False

    def fileno(self) -> int:
        return self._stream.fileno()

    def __getattr__(self, name):
        return getattr(self._stream, name)


_install_lock = threading.Lock()


def installed() -> ServerLog | None:
    """The active capture, if `install_server_log` ran in this process."""
    out = sys.stdout
    return out.sink if isinstance(out, _Tee) else None


def install_server_log(paths: HarnessPaths, *, max_bytes: int | None = None) -> ServerLog:
    """Tee this process's stdout/stderr into run/server/serve.log (idempotent)."""
    with _install_lock:
        current = installed()
        if current is not None:
            return current
        original_err = sys.stderr

        def _alert(message: str) -> None:
            # Straight to the wrapped stream (the journal / terminal), not
            # through the tee: this is about the file being unwritable.
            original_err.write(message)
            original_err.flush()

        sink = ServerLog(server_log_file(paths), max_bytes=max_bytes, alert=_alert)
        sys.stdout = _Tee(sys.stdout, sink)
        sys.stderr = _Tee(original_err, sink, tag="stderr")
        return sink


def uninstall_server_log() -> None:
    """Restore the original streams (tests; symmetric with install)."""
    with _install_lock:
        sink = None
        for attr in ("stdout", "stderr"):
            stream = getattr(sys, attr)
            if isinstance(stream, _Tee):
                sink = stream.sink
                setattr(sys, attr, stream.wrapped)
        if sink is not None:
            sink.flush_partial()
            sink.close()
