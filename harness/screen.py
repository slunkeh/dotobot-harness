"""Capture the harness host's screen for the app's computer/VM view.

Pluggable and dependency-free: it shells out to whatever screenshot tool is
available on the host, or a user-provided command. Returns PNG bytes, or None
when there is no display / no tool (the UI then shows an honest "no screen"
state rather than a fake placeholder).

Capture order:
    1. $HARNESS_SCREENSHOT_CMD   (a shell command that writes PNG bytes to stdout)
    2. ImageMagick `import -window root png:-`   (X11)
    3. `scrot -o <file>`                          (X11)
    4. `grim -`                                   (Wayland)

Today this streams the single host display (one shared "computer"). Per-bot
isolated displays arrive with the container/VM isolation work.
"""

from __future__ import annotations

import hashlib
import os
import select
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

_TIMEOUT = 10
#: ImageMagick resource ceilings for host-side recodes (see agent/postimage).
_CONVERT_LIMITS = (
    "-limit",
    "memory",
    "256MiB",
    "-limit",
    "map",
    "512MiB",
    "-limit",
    "disk",
    "512MiB",
    "-limit",
    "area",
    "64MP",
    "-limit",
    "width",
    "16000",
    "-limit",
    "height",
    "16000",
    "-limit",
    "time",
    "20",
)
_SOI = b"\xff\xd8"  # JPEG start-of-image
_EOI = b"\xff\xd9"  # JPEG end-of-image


def _run_stdout(cmd, *, shell=False) -> bytes | None:
    try:
        proc = subprocess.run(cmd, shell=shell, capture_output=True, timeout=_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0 and proc.stdout:
        return proc.stdout
    return None


def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def capture_png() -> bytes | None:
    """Return a PNG screenshot of the host display, or None if unavailable."""
    custom = os.environ.get("HARNESS_SCREENSHOT_CMD")
    if custom:
        data = _run_stdout(custom, shell=True)
        if data:
            return data

    if not _has_display():
        return None

    if shutil.which("import"):  # ImageMagick -> PNG on stdout
        data = _run_stdout(["import", "-silent", "-window", "root", "png:-"])
        if data:
            return data

    if shutil.which("scrot"):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "s.png"
            proc = subprocess.run(
                ["scrot", "-o", str(out)], capture_output=True, timeout=_TIMEOUT, check=False
            )
            if proc.returncode == 0 and out.is_file():
                return out.read_bytes()

    if shutil.which("grim"):  # Wayland
        data = _run_stdout(["grim", "-"])
        if data:
            return data

    return None


def screen_available() -> bool:
    if os.environ.get("HARNESS_SCREENSHOT_CMD"):
        return True
    if not _has_display():
        return False
    return any(shutil.which(t) for t in ("import", "scrot", "grim"))


def unavailable_reason() -> str:
    if not _has_display() and not os.environ.get("HARNESS_SCREENSHOT_CMD"):
        return "no display on the harness host (headless)"
    return "no screenshot tool on the harness host (install imagemagick/scrot/grim)"


def image_size(data: bytes) -> tuple[int, int] | None:
    """Width, height of a PNG or JPEG, or None if the header is unreadable."""
    if not data:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        if w > 0 and h > 0:
            return w, h
        return None
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    end = len(data) - 8
    while i < end:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            h = int.from_bytes(data[i + 5 : i + 7], "big")
            w = int.from_bytes(data[i + 7 : i + 9], "big")
            if w > 0 and h > 0:
                return w, h
            return None
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD9:
            i += 2
            continue
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        if length < 2:
            return None
        i += 2 + length
    return None


def compress_for_model(
    png: bytes, *, max_w: int = 1280, max_h: int = 800, quality: int = 70
) -> tuple[bytes, str]:
    """Downscale a PNG to JPEG for vision APIs. Falls back to the original PNG."""
    if not png:
        return png, "image/png"
    convert = shutil.which("convert")
    if convert:
        try:
            proc = subprocess.run(
                [
                    convert,
                    # A frame captured inside a bot machine is bytes the
                    # machine controls; cap what decoding it may cost.
                    *_CONVERT_LIMITS,
                    "png:-",
                    "-resize",
                    f"{max_w}x{max_h}>",
                    "-quality",
                    str(quality),
                    "jpeg:-",
                ],
                input=png,
                capture_output=True,
                timeout=_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None and proc.returncode == 0 and proc.stdout[:2] == b"\xff\xd8":
            return proc.stdout, "image/jpeg"
    return png, "image/png"


# -- real-time streaming --------------------------------------------------


def ffmpeg_stream_available() -> bool:
    return bool(shutil.which("ffmpeg") and os.environ.get("DISPLAY"))


@dataclass
class ScreenSource:
    """Where frames come from: the host display (default) or a bot machine.

    `stream_argv(fps)` returns an MJPEG-on-stdout ffmpeg argv, or None to use
    the periodic `capture()` fallback. The MJPEG parsing is shared either way.
    """

    stream_argv: Callable[[int], list[str] | None]
    capture: Callable[[], bytes | None]


def host_source() -> ScreenSource:
    """Today's behavior: host $DISPLAY, $HARNESS_SCREENSHOT_CMD honored first."""
    return ScreenSource(stream_argv=_host_ffmpeg_argv, capture=capture_png)


def _host_ffmpeg_argv(fps: int) -> list[str] | None:
    if not ffmpeg_stream_available():
        return None
    display = os.environ.get("DISPLAY", ":0")
    cmd = [
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
    # Pin grab size to the same geometry xdotool uses so 0..1 coords line up.
    try:
        from .hostinput import display_size

        size = display_size()
    except Exception:
        size = None
    if size:
        cmd.extend(["-video_size", f"{size[0]}x{size[1]}"])
    cmd.extend(
        [
            "-i",
            display,
            "-f",
            "mjpeg",
            "-q:v",
            "7",
            "-",
        ]
    )
    return cmd


def stream_frames(
    fps: int, stop: threading.Event, *, source: ScreenSource | None = None
) -> Iterator[tuple[bytes, str]]:
    """Yield (frame_bytes, mime) in real time until `stop` is set.

    Prefers a single persistent ffmpeg MJPEG pipe (low latency, one process) —
    on the host display by default, or inside a bot machine when `source` says
    so. Falls back to periodic screenshots when ffmpeg/X is unavailable.
    """
    fps = max(1, min(fps, 30))
    src = source or host_source()
    argv = src.stream_argv(fps)
    if argv:
        # Deduped here rather than inside `_mjpeg_pipe` so the parser stays a
        # parser: one job each, and the fallback path below gets the same
        # treatment from the same wrapper.
        yield from dedupe_frames(_mjpeg_pipe(argv, stop))
        if stop.is_set():
            return
        # The pipe died mid-session (ffmpeg missing or crashed inside a
        # machine, X hiccup): stay live on periodic screenshots instead of
        # freezing the view on the last frame with no signal to the user.
    # Fallback: periodic PNG capture (capped — screenshots are heavyweight).
    interval = 1.0 / max(1, min(fps, 4))

    def _polled() -> Iterator[tuple[bytes, str]]:
        while not stop.is_set():
            frame = src.capture()
            if frame:
                yield frame, "image/png"
            time.sleep(interval)

    yield from dedupe_frames(_polled())


_IDLE_TIMEOUT = 10.0  # healthy x11grab emits frames continuously; silence = dead

#: Send an unchanged frame at least this often even when nothing moves, so a
#: client that joins mid-stall still gets a picture and a proxy that drops idle
#: sockets sees traffic. Long enough that an idle desktop costs ~1 frame every
#: two seconds instead of `fps` of them.
_KEEPALIVE_SECS = 2.0


def dedupe_frames(
    frames: Iterator[tuple[bytes, str]],
    *,
    keepalive: float = _KEEPALIVE_SECS,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[tuple[bytes, str]]:
    """Drop frames identical to the one already sent.

    ffmpeg's x11grab produces `fps` frames a second whether or not the screen
    changed, and every one of them used to cross the WebSocket — to the iOS
    client too, over someone's LAN, with the desktop sitting still. Chrome's own
    screencast (which openbot uses) pushes only on change; this is the same
    saving without giving up grabbing a whole X display rather than one page.

    Compared by digest rather than by bytes so the check is cheap and constant
    in frame size. `keepalive` still lets one identical frame through
    periodically: a viewer that connects while nothing is moving must not wait
    for the user to jiggle the mouse before it sees anything.
    """
    last_digest: str | None = None
    last_sent = 0.0
    for frame, mime in frames:
        digest = hashlib.blake2b(frame, digest_size=16).hexdigest()
        now = clock()
        if digest == last_digest and (now - last_sent) < keepalive:
            continue
        last_digest = digest
        last_sent = now
        yield frame, mime


def _mjpeg_pipe(cmd: list[str], stop: threading.Event) -> Iterator[tuple[bytes, str]]:
    # stdin=PIPE and held open: a machine stream's sh wrapper (see
    # machine_view.stream_argv) kills its ffmpeg when this stdin closes, so
    # ffmpeg dies with the exec channel instead of orphaning in the machine.
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return
    fd = proc.stdout.fileno()
    buf = bytearray()
    last_data = time.monotonic()
    try:
        while not stop.is_set():
            # select tick so `stop` (and a wedged pipe) can't hang this thread
            # in a blocking read while the socket keeps a dead stream open.
            ready, _, _ = select.select([fd], [], [], 0.5)
            if not ready:
                if proc.poll() is not None:
                    break
                if time.monotonic() - last_data > _IDLE_TIMEOUT:
                    break
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            last_data = time.monotonic()
            buf.extend(chunk)
            # extract complete JPEG frames
            while True:
                start = buf.find(_SOI)
                if start < 0:
                    break
                end = buf.find(_EOI, start + 2)
                if end < 0:
                    if start > 0:
                        del buf[:start]  # drop junk before SOI
                    break
                frame = bytes(buf[start : end + 2])
                del buf[: end + 2]
                yield frame, "image/jpeg"
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
