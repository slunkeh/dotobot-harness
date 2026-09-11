"""Inject mouse/keyboard into the harness host display (live control).

Screen-control lessons from RustDesk (libs/enigo + input_service), without
file transfer, NAT/relay, clipboard, audio, or Wayland uinput:

* One long-lived input session so button-down survives across moves.
* Integer framebuffer pixels (client `px`/`py`), not only 0..1 floats.
* Skip a motion event when the pointer is already there — Chromium treats
  MotionNotify between ButtonPress and ButtonRelease as a drag even if the
  coordinates did not change (crbug.com/138075).
* Activate + focus the window under the pointer before click/key/type.
  XTEST mouse events are delivered by coordinates (Chrome sees the click,
  the DOM input may focus) but KeyPress goes to the X input-focus window,
  which XTEST clicks do not always give to Chrome on openbox/Xvfb. Without
  this, clicks look fine and typed text vanishes — bot `computer_type` and
  the iOS/Mac keyboards share this path.
* Local cursor on the client; the grab does not draw the remote one.

Commands go to a long-lived `xdotool -` stdin (libxdo, same library RustDesk
uses on X11). 0..1 `x`/`y` still work for bots that do not send pixels.

Every public entry point takes an optional `machine=` (a bot machine container
name, machines backend): the same long-lived-session semantics run over
`docker exec -i <machine> xdotool -` against that machine's own display, so
takeover behaves identically on a machine and on the host. Default (None)
stays the host display, byte-for-byte the old behavior.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import threading
import time

_TIMEOUT = 5
MAX_SCROLL_TICKS = 20
MAX_DRAG_POINTS = 50
_SIZE_TTL = 2.0
#: after spawning a session process, how long to wait before checking it
#: survived — a doomed `docker exec` (stopped container, dead engine, missing
#: xdotool) accepts one buffered line and exits, which used to look like
#: success for every single event, forever.
_SPAWN_GRACE = 0.05

#: Bytes that would end an `xdotool -` command line (or a C string) inside
#: an argument. Checked on every line `_Session` writes.
_LINE_BREAK = re.compile(r"[\r\n\x00]")

#: One keysym chord, or chords separated by single spaces (see valid_key_name).
_KEY_RE = re.compile(r"[A-Za-z0-9_]+(\+[A-Za-z0-9_]+)*(?: [A-Za-z0-9_]+(\+[A-Za-z0-9_]+)*)*")
_size_cache: tuple[float, tuple[int, int]] | None = None
#: tint2 dock rects (wid, x, y, w, h) per machine. XTEST clicks pass through
#: the panel to the wallpaper, so a Chrome-icon click must be sent --window.
_DOCK_TTL = 2.0
_dock_cache: dict[str, tuple[float, list[tuple[str, int, int, int, int]]]] = {}
#: last window we activated per machine — skip WM restack + 50ms sleep
#: when click/type stay on the same Chrome window.
_focused_wid: dict[str, str] = {}
#: last model-facing screenshot size per machine. computer_click often
#: sends JPEG pixels; those must scale onto the display, not be treated
#: as 0..1 fractions (which clamp to the far corner).
_shot_size: dict[str, tuple[int, int]] = {}


def available() -> bool:
    return bool(shutil.which("xdotool") and os.environ.get("DISPLAY"))


class _Session:
    """One xdotool process; mousedown stays held across mousemove.

    `argv` defaults to the host `xdotool -`; a machine session runs the same
    protocol over `docker exec -i <machine> xdotool -`.
    """

    def __init__(self, argv: tuple[str, ...] = ("xdotool", "-")) -> None:
        self._argv = argv
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._last: tuple[int, int] | None = None

    def close(self) -> None:
        with self._lock:
            self._stop()

    def flush(self) -> bool:
        """Finish queued commands before observing their display."""
        with self._lock:
            return self._drain()

    def _drain(self) -> bool:
        """Wait for queued input without truncating it on an observation timeout.

        A closed stdin prevents new commands from overtaking input that is
        still draining. Keep the process so the next observation can wait
        again; only explicit session shutdown may terminate it.
        """
        proc = self._proc
        if proc is None:
            return True
        try:
            if proc.stdin:
                proc.stdin.close()
            status = proc.wait(timeout=_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            return False
        self._proc = None
        self._last = None
        return status == 0

    def send(self, *parts: str) -> bool:
        line = " ".join(str(p) for p in parts)
        # `xdotool -` is one command per line, and `exec` is a command: an
        # argument carrying a line break would run a program. Refuse the
        # whole line here (never trim it) so no caller can emit a partial
        # command by accident — this is the one place lines are written.
        if _LINE_BREAK.search(line):
            return False
        with self._lock:
            return self._write(line)

    def move_to(self, x: int, y: int) -> bool:
        """Absolute move. No-op when already at (x, y)."""
        with self._lock:
            if self._last == (x, y):
                return True
            if not self._write(f"mousemove {x} {y}"):
                return False
            self._last = (x, y)
            return True

    def drag(self, points: list[tuple[int, int]], button: int) -> bool:
        """Finish a bounded drag before a caller captures its result.

        Drain preceding input, then use a synchronous xdotool invocation on
        the same display. The normal stdin session has no completion reply;
        queueing a longer drag there would race the next screenshot.
        """
        x, y = points[0]
        parts = ["mousemove", str(x), str(y), "mousedown", str(button), "sleep", "0.05"]
        for x, y in points[1:]:
            parts.extend(("mousemove", str(x), str(y), "sleep", "0.05"))
        parts.extend(("mouseup", str(button)))
        return self.run_sync(*parts, position=points[-1], release_button=button)

    def run_sync(
        self,
        *parts: str,
        position: tuple[int, int] | None = None,
        release_button: int | None = None,
    ) -> bool:
        """Drain queued input and wait for an observation-producing action."""
        argv = self._argv[:-1]  # replace stdin-mode '-' with one bounded gesture
        with self._lock:
            if not self._drain():
                return False
            try:
                ok = (
                    subprocess.run(
                        [*argv, *parts], capture_output=True, timeout=_TIMEOUT, check=False
                    ).returncode
                    == 0
                )
            except (OSError, subprocess.SubprocessError):
                ok = False
            self._last = position if ok else None
            if not ok and release_button is not None:
                try:
                    subprocess.run(
                        [*argv, "mouseup", str(release_button)],
                        capture_output=True,
                        timeout=_TIMEOUT,
                        check=False,
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
            return bool(ok)

    def _start(self) -> subprocess.Popen | None:
        if not shutil.which(self._argv[0]):
            return None
        try:
            return subprocess.Popen(
                list(self._argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError:
            return None

    def _stop(self) -> None:
        self._last = None
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        # EOF first so xdotool drains queued commands; only then escalate.
        try:
            proc.wait(timeout=2)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass

    def _write(self, line: str) -> bool:
        proc = self._proc
        fresh = False
        if proc is None or proc.poll() is not None:
            self._last = None
            proc = self._start()
            self._proc = proc
            fresh = True
        if proc is None or proc.stdin is None:
            return False
        if proc.stdin.closed:
            # An observation timed out waiting for this queue. Do not
            # replace or overtake it; let its remaining input finish.
            return False
        try:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self._stop()
            return False
        if fresh:
            # The first write lands in the pipe buffer even when the process
            # is already dying, so a freshly spawned session gets a beat and
            # a liveness check — otherwise every event "succeeds" into a dead
            # exec and the user's clicks vanish without a trace.
            time.sleep(_SPAWN_GRACE)
            if proc.poll() is not None:
                self._stop()
                return False
        return True


_session = _Session()
_machine_sessions: dict[str, _Session] = {}
_machine_lock = threading.Lock()


def session_for(machine: str | None) -> _Session:
    """The host session, or the long-lived exec session for one machine."""
    if not machine:
        return _session
    with _machine_lock:
        session = _machine_sessions.get(machine)
        if session is None:
            from .machine_view import input_argv

            session = _Session(tuple(input_argv(machine)))
            _machine_sessions[machine] = session
        return session


def flush(machine: str | None = None) -> bool:
    """Drain an existing input session without creating one just to observe."""
    if not machine:
        session = _session
    else:
        with _machine_lock:
            session = _machine_sessions.get(machine)
    return session.flush() if session is not None else True


def display_size(machine: str | None = None) -> tuple[int, int] | None:
    if machine:
        from .machine_view import geometry

        return geometry(machine)
    global _size_cache
    now = time.monotonic()
    if _size_cache and now - _size_cache[0] < _SIZE_TTL:
        return _size_cache[1]
    if not shutil.which("xdotool"):
        return None
    try:
        out = subprocess.run(
            ["xdotool", "getdisplaygeometry"], capture_output=True, timeout=_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        w, h = out.stdout.decode().split()
        size = (int(w), int(h))
    except ValueError:
        return None
    _size_cache = (now, size)
    return size


def remember_shot(size: tuple[int, int], machine: str | None = None) -> None:
    """Record the screenshot the model just saw, for click coordinate mapping."""
    w, h = size
    if w > 0 and h > 0:
        _shot_size[machine or ""] = (int(w), int(h))


def shot_size(machine: str | None = None) -> tuple[int, int] | None:
    return _shot_size.get(machine or "")


def _clamp_px(x: int, y: int, w: int, h: int) -> tuple[int, int]:
    return max(0, min(x, w - 1)), max(0, min(y, h - 1))


def _dock_rects(machine: str | None = None) -> list[tuple[str, int, int, int, int]]:
    """Cached tint2 geometries. Empty when there is no panel."""
    key = machine or ""
    now = time.monotonic()
    hit = _dock_cache.get(key)
    if hit is not None and now - hit[0] < _DOCK_TTL:
        return hit[1]
    try:
        prefix = _machine_prefix(machine)
    except Exception:
        prefix = []
    rects: list[tuple[str, int, int, int, int]] = []
    try:
        found = subprocess.run(
            [*prefix, "xdotool", "search", "--class", "tint2"],
            capture_output=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _dock_cache[key] = (now, [])
        return []
    for line in found.stdout.decode("utf-8", "replace").splitlines():
        wid = line.strip()
        if not wid.isdigit():
            continue
        try:
            geo = subprocess.run(
                [*prefix, "xdotool", "getwindowgeometry", "--shell", wid],
                capture_output=True,
                timeout=_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        fields: dict[str, int] = {}
        for row in geo.stdout.decode("utf-8", "replace").splitlines():
            if "=" not in row:
                continue
            name, _, raw = row.partition("=")
            try:
                fields[name.strip()] = int(raw.strip())
            except ValueError:
                continue
        if {"X", "Y", "WIDTH", "HEIGHT"} <= fields.keys():
            rects.append((wid, fields["X"], fields["Y"], fields["WIDTH"], fields["HEIGHT"]))
    _dock_cache[key] = (now, rects)
    return rects


def _dock_hit(x: int, y: int, machine: str | None = None) -> tuple[str, int, int] | None:
    """If (x, y) is on a tint2 dock, (window id, rel_x, rel_y)."""
    for wid, dx, dy, dw, dh in _dock_rects(machine):
        if dx <= x < dx + dw and dy <= y < dy + dh:
            return wid, x - dx, y - dy
    return None


def _press(
    session: _Session,
    pos: tuple[int, int],
    button: int,
    machine: str | None,
    *,
    up: bool,
) -> bool:
    """mousedown/mouseup, targeting tint2 with --window when the point is on it."""
    if not session.move_to(*pos):
        return False
    verb = "mouseup" if up else "mousedown"
    hit = _dock_hit(*pos, machine)
    if hit:
        wid, rx, ry = hit
        if not session.send("mousemove", "--window", wid, str(rx), str(ry)):
            return False
        return session.send(verb, "--window", wid, str(button))
    # Chrome's render widget has no _NET_WM_DESKTOP; activate --sync hangs
    # and the click never lands. Focus without --sync on press (not move).
    if not up:
        _focus_pointer_window(machine)
    return session.send(verb, str(button))


def _abs(nx: float, ny: float, machine: str | None = None) -> tuple[int, int] | None:
    size = display_size(machine)
    if not size:
        return None
    w, h = size
    x = max(0, min(round(nx * (w - 1)), w - 1))
    y = max(0, min(round(ny * (h - 1)), h - 1))
    return x, y


def _pos(
    nx: float,
    ny: float,
    px: int | None = None,
    py: int | None = None,
    machine: str | None = None,
) -> tuple[int, int] | None:
    if px is not None and py is not None:
        size = display_size(machine)
        if not size:
            return None
        w, h = size
        pix = _clamp_px(int(px), int(py), w, h)
        # App clients send 0..1 x/y plus JPEG px/py. On a retina Mac, px was
        # 2× the display and won, so dock clicks landed on the wallpaper.
        # If the fractions and pixels disagree, trust the fractions.
        if 0 <= nx <= 1 and 0 <= ny <= 1 and (nx, ny) != (0.0, 0.0):
            frac = _abs(nx, ny, machine)
            if frac is not None:
                slop = max(16, w // 50, h // 50)
                if abs(frac[0] - pix[0]) > slop or abs(frac[1] - pix[1]) > slop:
                    return frac
        return pix
    # Pixel values from the last screenshot (or the display, if none yet).
    # Treating them as 0..1 fractions used to slam the pointer to the corner.
    if nx > 1 or ny > 1:
        size = display_size(machine)
        if not size:
            return None
        w, h = size
        src = shot_size(machine) or size
        sw, sh = src
        x = round(nx / max(sw - 1, 1) * (w - 1))
        y = round(ny / max(sh - 1, 1) * (h - 1))
        return _clamp_px(x, y, w, h)
    return _abs(nx, ny, machine)


def move(
    nx: float,
    ny: float,
    px: int | None = None,
    py: int | None = None,
    machine: str | None = None,
) -> bool:
    pos = _pos(nx, ny, px, py, machine)
    return session_for(machine).move_to(*pos) if pos else False


def down(
    nx: float,
    ny: float,
    button: int = 1,
    px: int | None = None,
    py: int | None = None,
    machine: str | None = None,
) -> bool:
    pos = _pos(nx, ny, px, py, machine)
    if not pos:
        return False
    # Clicks are coordinate-based and must stay cheap. Keyboard focus is
    # applied on type/key, not on every press (docker exec + WM restack
    # on each click made the Mac computer view lag and miss).
    return _press(session_for(machine), pos, button, machine, up=False)


def up(
    nx: float,
    ny: float,
    button: int = 1,
    px: int | None = None,
    py: int | None = None,
    machine: str | None = None,
) -> bool:
    pos = _pos(nx, ny, px, py, machine)
    if not pos:
        return False
    # Skip mousemove when already there (Chromium crbug.com/138075).
    return _press(session_for(machine), pos, button, machine, up=True)


def click(
    nx: float,
    ny: float,
    button: int = 1,
    px: int | None = None,
    py: int | None = None,
    machine: str | None = None,
    *,
    clicks: int = 1,
) -> bool:
    if (
        type(clicks) is not int
        or type(button) is not int
        or clicks not in (1, 2)
        or button not in (1, 2, 3)
    ):
        return False
    pos = _pos(nx, ny, px, py, machine)
    if not pos:
        return False
    # Tiny sleep so Chrome/GTK sees a real press/release, not a zero-width click.
    session = session_for(machine)
    if not session.move_to(*pos):
        return False
    hit = _dock_hit(*pos, machine)
    repeat = ("--repeat", "2", "--delay", "100") if clicks == 2 else ()
    if hit:
        wid, rx, ry = hit
        if not session.send("mousemove", "--window", wid, str(rx), str(ry)):
            return False
        return session.send("sleep", "0.03", "click", "--window", wid, *repeat, str(button))
    _focus_pointer_window(machine)
    return session.send("sleep", "0.03", "click", *repeat, str(button))


def drag(path: list[tuple[float, float]], button: int = 1, machine: str | None = None) -> bool:
    """Drag through screenshot coordinates, fully validated before pressing."""
    if not isinstance(path, list) or not 2 <= len(path) <= MAX_DRAG_POINTS:
        return False
    if type(button) is not int or button not in (1, 2, 3):
        return False
    points: list[tuple[int, int]] = []
    try:
        for x, y in path:
            if not math.isfinite(x) or not math.isfinite(y) or x < 0 or y < 0:
                return False
        for x, y in path:
            pos = _pos(x, y, machine=machine)
            if not pos:
                return False
            points.append(pos)
    except (TypeError, ValueError):
        return False
    return session_for(machine).drag(points, button)


def scroll(
    amount: int,
    machine: str | None = None,
    *,
    nx: float | None = None,
    ny: float | None = None,
    axis: str = "vertical",
    wait: bool = False,
) -> bool:
    """Scroll exact wheel ticks at a target (legacy default: the page body)."""
    if type(amount) is not int or not -MAX_SCROLL_TICKS <= amount <= MAX_SCROLL_TICKS:
        return False
    if axis not in ("vertical", "horizontal") or (nx is None) != (ny is None):
        return False
    target = None
    if nx is not None and ny is not None:
        try:
            if not math.isfinite(nx) or not math.isfinite(ny) or nx < 0 or ny < 0:
                return False
            target = _pos(nx, ny, machine=machine)
        except (TypeError, ValueError):
            return False
        if target is None:
            return False
    if amount == 0:
        return True
    # Wheel events go to the window under the pointer. After typing a URL
    # the pointer is still in the address bar, so Reddit's feed does not
    # move and the model clicks a post to "focus" it. Park on the page.
    session = session_for(machine)
    if target is None:
        size = display_size(machine)
        if size:
            target = size[0] // 2, int(size[1] * 0.62)
    up, down = ("4", "5") if axis == "vertical" else ("6", "7")
    wheel = ("click", "--repeat", str(abs(amount)), up if amount < 0 else down)
    if wait:
        # Bot observations must wait for all requested ticks. Native client
        # input keeps its existing low-latency stdin queue.
        motion = ("mousemove", str(target[0]), str(target[1])) if target else ()
        return session.run_sync(*motion, *wheel, position=target)
    if target:
        if not session.move_to(*target):
            return False
    return session.send(*wheel)


def _pointer_window(machine: str | None = None) -> str | None:
    """X window id under the pointer, or None if it cannot be resolved.

    One-shot (needs stdout). The long-lived `xdotool -` session discards
    stdout, so it cannot answer this.
    """
    try:
        prefix = _machine_prefix(machine)
    except Exception as exc:
        from isolation.base import IsolationUnavailable

        if not isinstance(exc, IsolationUnavailable):
            raise
        return None
    try:
        out = subprocess.run(
            [*prefix, "xdotool", "getmouselocation", "--shell"],
            capture_output=True,
            timeout=0.6,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("WINDOW="):
            wid = line.split("=", 1)[1].strip()
            # 0 is the root window — activating it does not give keys to Chrome.
            if wid and wid != "0":
                return wid
            return None
    return None


def _focus_pointer_window(machine: str | None = None) -> bool:
    """Give the window under the pointer X keyboard focus.

    Best-effort: a missing id (headless tests, empty desktop) is not a
    failure of the click/key/type that follows.
    """
    wid = _pointer_window(machine)
    if not wid:
        return False
    key = machine or ""
    if _focused_wid.get(key) == wid:
        return True
    session = session_for(machine)
    # --sync waits on _NET_WM_DESKTOP; Chrome's inner widget and tint2
    # never set it, so activate --sync hangs until timeout and eats the click.
    if not session.send("windowactivate", wid):
        return False
    if not session.send("windowfocus", wid):
        return False
    _focused_wid[key] = wid
    # Chrome needs a beat after the WM restacks before it accepts keys.
    return session.send("sleep", "0.03")


def _emit_type(text: str, machine: str | None = None) -> bool:
    """xdotool `type` with no focus/clipboard policy — one command, one line."""
    return session_for(machine).send("type", "--clearmodifiers", "--", text)


def valid_key_name(name: str) -> bool:
    """True for an xdotool keysym chord or a space-separated sequence of them.

    Keysyms are word characters (`Return`, `Page_Down`, `XF86AudioPlay`)
    joined by `+` for modifiers (`ctrl+shift+t`, `alt+F4`); a sequence
    (`ctrl+a ctrl+c`) is chords separated by single spaces. Anything else
    — option-shaped names (`--delay`), `$` (script mode expands variables),
    quotes, control characters — is refused, so a key name a web page
    talked the model into can only ever press keys.
    """
    return bool(name) and _KEY_RE.fullmatch(name) is not None


def key(name: str, machine: str | None = None, *, focus: bool = True) -> bool:
    if not valid_key_name(name):
        return False
    if focus:
        _focus_pointer_window(machine)
    return session_for(machine).send("key", "--clearmodifiers", name)


def type_text(text: str, machine: str | None = None) -> bool:
    if not text:
        return False
    # xdotool - is line-oriented; keep the payload on one command.
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    if not text:
        return False
    _focus_pointer_window(machine)
    # A whole word/email is more reliable on the clipboard: `type` maps each
    # glyph through the X keymap and can silently drop `@` / capitals when
    # the layout and --clearmodifiers disagree. Single characters (the iOS
    # per-keystroke path) stay on `type` so we do not clobber the clipboard
    # on every letter.
    if len(text) > 1 and _clip_tool(machine):
        if clipboard_write(text, machine) and key("ctrl+v", machine, focus=False):
            return True
    return _emit_type(text, machine)


def _clip_tool(machine: str | None = None) -> tuple[str, str] | None:
    if machine:
        return "xclip", "xclip"  # the machine image ships xclip
    if shutil.which("xclip"):
        return "xclip", "xclip"
    if shutil.which("xsel"):
        return "xsel", "xsel"
    return None


def _machine_prefix(machine: str | None, *, interactive: bool = False) -> list[str]:
    if not machine:
        return []
    from .machine_view import exec_prefix

    return exec_prefix(machine, interactive=interactive)


def clipboard_read(machine: str | None = None) -> str | None:
    """CLIPBOARD, then PRIMARY. None if no xclip/xsel; '' if empty."""
    tool = _clip_tool(machine)
    if not tool:
        return None
    kind, _ = tool
    for selection in ("clipboard", "primary"):
        text = _selection_read(kind, selection, machine)
        if text:
            return text
    return "" if kind else None


def clipboard_write(text: str, machine: str | None = None) -> bool:
    tool = _clip_tool(machine)
    if not tool:
        return False
    kind, _ = tool
    return _selection_write(kind, "clipboard", text, machine)


def _selection_read(kind: str, selection: str, machine: str | None = None) -> str:
    try:
        if kind == "xclip":
            cmd = ["xclip", "-selection", selection, "-o"]
        else:
            cmd = ["xsel", "--clipboard" if selection == "clipboard" else "--primary", "-o"]
        out = subprocess.run(
            [*_machine_prefix(machine), *cmd], capture_output=True, timeout=_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.decode("utf-8", "replace")


def _selection_write(kind: str, selection: str, text: str, machine: str | None = None) -> bool:
    try:
        if kind == "xclip":
            # Do not wait: xclip holds the clipboard until replaced.
            proc = subprocess.Popen(
                [
                    *_machine_prefix(machine, interactive=True),
                    "xclip",
                    "-selection",
                    selection,
                    "-in",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if proc.stdin:
                proc.stdin.write(text.encode("utf-8"))
                proc.stdin.close()
            return True
        proc = subprocess.run(
            ["xsel", "--clipboard" if selection == "clipboard" else "--primary", "-i"],
            input=text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_TIMEOUT,
            check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def copy_from_display(machine: str | None = None) -> str | None:
    """Ctrl+C on the focused window, then read the X clipboard."""
    if not _clip_tool(machine):
        return None
    if not key("ctrl+c", machine):
        return None
    time.sleep(0.08)
    return clipboard_read(machine)


def paste_to_display(text: str, machine: str | None = None) -> bool:
    """Put text on the X clipboard and paste into the focused field."""
    if not text:
        return False
    if clipboard_write(text, machine):
        time.sleep(0.03)
        if key("ctrl+v", machine):
            return True
    return type_text(text, machine)


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def dispatch(event: dict, machine: str | None = None) -> bool:
    """Apply one input event dict (from the client)."""
    action = event.get("action")
    px = _int_or_none(event.get("px"))
    py = _int_or_none(event.get("py"))
    nx = float(event.get("x", 0))
    ny = float(event.get("y", 0))
    if action == "move":
        return move(nx, ny, px, py, machine)
    if action in {"down", "mousedown"}:
        return down(nx, ny, int(event.get("button", 1)), px, py, machine)
    if action in {"up", "mouseup"}:
        return up(nx, ny, int(event.get("button", 1)), px, py, machine)
    if action == "click":
        return click(nx, ny, int(event.get("button", 1)), px, py, machine)
    if action == "scroll":
        amt = event.get("amount", event.get("button", 0))
        return scroll(int(amt), machine)
    if action == "key":
        return key(str(event.get("key", "")), machine)
    if action == "type":
        return type_text(str(event.get("text", "")), machine)
    if action == "paste":
        return paste_to_display(str(event.get("text", "")), machine)
    return False
