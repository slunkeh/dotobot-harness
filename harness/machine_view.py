"""Host-side view into a bot machine (machines backend).

The agent's computer driver and the harness server both route screen capture,
input sessions, and app launches into the right machine container over
`docker exec` — the machine itself has no harness code running and no harness
state mounted; the exec channel is the only doorway, and it exists only on
the trusted host side.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

from harness.paths import HarnessPaths
from isolation.base import IsolationUnavailable
from isolation.engine import resolve_engine

#: every machine runs its own Xvfb on :0
MACHINE_DISPLAY = ":0"
MACHINE_UPLOADS = "/home/agent/Downloads/Uploads"


def stage_upload(paths: HarnessPaths, machine: str, source: Path) -> str | None:
    """Copy a current upload into its bot's machine, without mounting host state.

    Only direct, regular upload files qualify. Keep the host copy for vision,
    connector attachments and transcript replay. Bytes travel on stdin, never
    through a shell; an atomic replace avoids presenting a partial upload.
    """
    script = """import os, pathlib, shutil, sys, tempfile
target = pathlib.Path(sys.argv[1])
target.parent.mkdir(parents=True, exist_ok=True)
fd, name = tempfile.mkstemp(dir=target.parent)
try:
    with os.fdopen(fd, 'wb') as output:
        shutil.copyfileobj(sys.stdin.buffer, output)
    os.replace(name, target)
finally:
    pathlib.Path(name).unlink(missing_ok=True)
"""
    try:
        root = paths.uploads.resolve()
        if source.resolve().parent != root or source.is_symlink():
            return None
        target = str(Path(MACHINE_UPLOADS) / source.name)
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 256 << 20:
                return None
            result = subprocess.run(
                [*exec_prefix(machine, interactive=True), "python3", "-c", script, target],
                stdin=stream,
                capture_output=True,
                timeout=60,
            )
        return target if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError, IsolationUnavailable):
        return None


_TIMEOUT = 10
_GEOM_TTL = 2.0
#: geometry results by machine, successes AND failures — a down machine must
#: not cost the WS read loop a blocking `docker exec` per mouse event.
_geom_cache: dict[str, tuple[float, tuple[int, int] | None]] = {}


#: run-file (mtime_ns, size) -> machine per bot: input events resolve the
#: machine inline on the WS read loop, and re-reading + re-parsing the run
#: file per pointer move added up. A bot moving machines rewrites the file,
#: which changes the stamp.
_machine_cache: dict[str, tuple[tuple[int, int], str | None]] = {}


def machine_for_bot(paths: HarnessPaths, bot: str) -> str | None:
    """The bot's machine container name, or None when it isn't on a machine."""
    rf = paths.run_file(bot)
    try:
        st = rf.stat()
    except OSError:
        # missing / mid-unlink (bot stopping): not on a machine
        _machine_cache.pop(bot, None)
        return None
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _machine_cache.get(bot)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    try:
        data = json.loads(rf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # mid-unlink or corrupt: not on a machine (not cached — transient)
        return None
    machine = None
    if data.get("backend") == "machines":
        machine = data.get("machine") or None
    _machine_cache[bot] = (stamp, machine)
    return machine


def exec_prefix(machine: str, *, interactive: bool = False) -> list[str]:
    argv = [resolve_engine(), "exec"]
    if interactive:
        argv.append("-i")
    argv += ["-e", f"DISPLAY={MACHINE_DISPLAY}", machine]
    return argv


def capture_png(machine: str) -> bytes | None:
    """One-shot screenshot of the machine's display (PNG bytes)."""
    try:
        proc = subprocess.run(
            [*exec_prefix(machine), "import", "-silent", "-window", "root", "png:-"],
            capture_output=True,
            timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError, IsolationUnavailable):
        return None
    if proc.returncode == 0 and proc.stdout:
        return proc.stdout
    return None


def capture_for_model(machine: str) -> tuple[bytes, str] | None:
    """Screenshot resized for a vision model (JPEG when convert is in the jail)."""
    wrapper = "import -silent -window root png:- | convert - -resize '1280x800>' -quality 70 jpeg:-"
    try:
        proc = subprocess.run(
            [*exec_prefix(machine), "sh", "-c", wrapper],
            capture_output=True,
            timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError, IsolationUnavailable):
        proc = None
    if proc is not None and proc.returncode == 0 and proc.stdout[:2] == b"\xff\xd8":
        return proc.stdout, "image/jpeg"
    png = capture_png(machine)
    if png:
        return png, "image/png"
    return None


def input_argv(machine: str) -> list[str]:
    """argv for a long-lived xdotool session inside the machine."""
    return [*exec_prefix(machine, interactive=True), "xdotool", "-"]


def launch(machine: str, argv: list[str]) -> bool:
    """Start a desktop app inside the machine (detached exec)."""
    try:
        from harness import cdp

        # launch-chrome reads this at exec time; pass the host kill switch
        # so a flip is visible without rewriting the wrapper or recreating
        # the container.
        cmd = [
            resolve_engine(),
            "exec",
            "-d",
            "-e",
            f"DISPLAY={MACHINE_DISPLAY}",
            "-e",
            f"HARNESS_CHROME_CDP={'1' if cdp.enabled() else '0'}",
            machine,
            *argv,
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError, IsolationUnavailable):
        return False
    if proc.returncode != 0:
        return False
    # docker exec -d returns 0 even if Chrome SIGTRAPs a moment later
    # (root-owned profile, jail). Treat a missing process as a failed launch.
    needle = os.path.basename(argv[0]) if argv else ""
    if "chrome" in needle or "chromium" in needle:
        time.sleep(0.8)
        return _running(machine, "chrome")
    return True


def _running(machine: str, needle: str) -> bool:
    try:
        proc = subprocess.run(
            [*exec_prefix(machine), "pgrep", "-f", needle],
            capture_output=True,
            timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError, IsolationUnavailable):
        return False
    return proc.returncode == 0


_geom_lock = threading.Lock()
_geom_refreshing: set[str] = set()


def geometry(machine: str) -> tuple[int, int] | None:
    """The machine display's WxH (TTL-cached), for pixel/0..1 coordinate math.

    Failures are cached for the same TTL: input events resolve geometry
    inline in the WS read loop, and hammering a dead container with 10s
    `docker exec` timeouts would freeze every other message on the socket.
    Once a value exists, a stale entry is served immediately and refreshed by
    a single-flight background thread — the value was already up to a TTL
    stale by design, and this keeps the periodic probe off the event path.
    Only the very first probe for a machine blocks.
    """
    cached = _geom_cache.get(machine)
    now = time.monotonic()
    if cached and now - cached[0] < _GEOM_TTL:
        return cached[1]
    if cached:
        _refresh_geometry_async(machine)
        return cached[1]
    size = _probe_geometry(machine)
    _geom_cache[machine] = (time.monotonic(), size)
    return size


def _refresh_geometry_async(machine: str) -> None:
    with _geom_lock:
        if machine in _geom_refreshing:
            return
        _geom_refreshing.add(machine)

    def _run() -> None:
        try:
            size = _probe_geometry(machine)
            _geom_cache[machine] = (time.monotonic(), size)
        finally:
            with _geom_lock:
                _geom_refreshing.discard(machine)

    threading.Thread(target=_run, daemon=True, name=f"geom-{machine}").start()


def _probe_geometry(machine: str) -> tuple[int, int] | None:
    try:
        proc = subprocess.run(
            [*exec_prefix(machine), "xdotool", "getdisplaygeometry"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError, IsolationUnavailable):
        return None
    if proc.returncode != 0:
        return None
    try:
        w, h = proc.stdout.split()
        return (int(w), int(h))
    except ValueError:
        return None


def screen_source(machine: str):
    """A ScreenSource that grabs this machine's display (see harness.screen)."""
    from .screen import ScreenSource

    def _argv(fps: int) -> list[str] | None:
        try:
            return stream_argv(machine, fps, geometry(machine))
        except IsolationUnavailable:
            return None

    return ScreenSource(stream_argv=_argv, capture=lambda: capture_png(machine))


def stream_argv(machine: str, fps: int, size: tuple[int, int] | None) -> list[str]:
    """ffmpeg x11grab MJPEG argv running INSIDE the machine, frames on stdout
    (the server's existing MJPEG parser consumes them unchanged).

    Killing a `docker exec` client does NOT kill the exec'd process, so a bare
    ffmpeg outlives every stream stop/restart. Orphans pile up against the
    machine's --pids-limit (16 threads each) until in-machine forks fail —
    at which point the dock stops launching apps while the surviving stream
    keeps the screen looking alive. The sh wrapper ties ffmpeg's life to the
    exec channel instead: the harness holds stdin open (screen._mjpeg_pipe),
    and when the client goes away `cat` sees EOF and reaps ffmpeg; when ffmpeg
    exits on its own, `wait` returns and the reader sees EOF.
    """
    ffmpeg = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-f",
        "x11grab",
        "-framerate",
        str(fps),
        "-draw_mouse",
        "0",
    ]
    if size:
        ffmpeg += ["-video_size", f"{size[0]}x{size[1]}"]
    ffmpeg += ["-i", MACHINE_DISPLAY, "-f", "mjpeg", "-q:v", "7", "-"]
    # fd 3 keeps the real stdin: POSIX gives background jobs /dev/null as
    # stdin, which would feed `cat` an instant EOF and kill the stream at birth.
    wrapper = (
        "exec 3<&0; "
        f"{' '.join(ffmpeg)} & p=$!; "
        "( cat <&3 >/dev/null 2>&1; kill $p 2>/dev/null ) >/dev/null 2>&1 & "
        "wait $p"
    )
    return [*exec_prefix(machine, interactive=True), "sh", "-c", wrapper]
