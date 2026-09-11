"""Record a human demonstration on a bot's computer.

Teach a task starts an ffmpeg x11grab of the bot's display (host $DISPLAY, or
inside the bot's machine). Stop SIGINTs ffmpeg — never SIGKILL — extracts
stills into `workspace/uploads`, and returns attachments the host attaches to
a `/learn-from-demonstration` chat. The bot does not own capture or the
recording queue.

If ffmpeg cannot start, stills are sampled from the existing screenshot path
(`HARNESS_SCREENSHOT_CMD` / machine `import` / host capture).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from harness.control import Control
from harness.paths import HarnessPaths
from isolation.base import IsolationUnavailable

from . import machine_view

_LEASE_WAIT = 15.0
_SAMPLE_DEFAULT = 1.0
_MAX_STILLS = 8
_MIN_DURATION = 0.5


class TeachBusy(RuntimeError):
    """A recording is already running for this bot."""


class TeachIdle(RuntimeError):
    """Stop/status was called with no live recording."""


class TeachUnavailable(RuntimeError):
    """No display / ffmpeg / screenshot source to record."""


@dataclass
class StopResult:
    bot: str
    session_id: str
    duration: float
    attachments: list[dict[str, Any]]
    unusable: bool
    reason: str | None = None


@dataclass
class _Live:
    bot: str
    session_id: str
    session_dir: Path
    video: Path
    proc: subprocess.Popen | None = None
    pid: int | None = None
    machine: str | None = None
    inner_video: str | None = None
    started_at: float = 0.0
    sampler_stop: threading.Event | None = None
    samples: list[Path] = field(default_factory=list)
    inputs: list[dict[str, Any]] = field(default_factory=list)


_lock = threading.Lock()
_live: dict[str, _Live] = {}


def _key(paths: HarnessPaths, bot: str) -> str:
    return f"{paths.home}:{bot}"


def sessions_root(paths: HarnessPaths) -> Path:
    return paths.home / "teach-sessions"


def _sample_interval() -> float:
    raw = os.environ.get("HARNESS_TEACH_SAMPLE_SECS", "")
    try:
        return max(0.05, float(raw)) if raw else _SAMPLE_DEFAULT
    except ValueError:
        return _SAMPLE_DEFAULT


def _capture(paths: HarnessPaths, bot: str) -> bytes | None:
    machine = machine_view.machine_for_bot(paths, bot)
    if machine:
        return machine_view.capture_png(machine)
    from harness.screen import capture_png

    return capture_png()


def _display_size(bot: str, paths: HarnessPaths, machine: str | None) -> tuple[int, int] | None:
    try:
        from harness.hostinput import display_size

        return display_size(machine)
    except Exception:
        if machine:
            return machine_view.geometry(machine)
        return None


def _ffmpeg_cmd(display: str, size: tuple[int, int] | None, dest: Path) -> list[str]:
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "x11grab",
        "-framerate",
        "8",
        "-draw_mouse",
        "1",
    ]
    if size:
        cmd.extend(["-video_size", f"{size[0]}x{size[1]}"])
    cmd.extend(
        [
            "-i",
            display,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ]
    )
    return cmd


def _start_ffmpeg(live: _Live, paths: HarnessPaths) -> None:
    """Best-effort: leave live.proc/pid unset when ffmpeg cannot run."""
    if live.machine:
        _start_machine_ffmpeg(live)
        return
    ffmpeg = shutil.which("ffmpeg")
    display = os.environ.get("DISPLAY")
    if not ffmpeg or not display:
        return
    size = _display_size(live.bot, paths, None)
    cmd = _ffmpeg_cmd(display, size, live.video)
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return
    live.proc = proc
    live.pid = proc.pid


def _start_machine_ffmpeg(live: _Live) -> None:
    assert live.machine
    inner_dir = "/tmp/harness-teach"
    inner = f"{inner_dir}/demo.mp4"
    size = machine_view.geometry(live.machine)
    size_args = f"-video_size {size[0]}x{size[1]} " if size else ""
    display = machine_view.MACHINE_DISPLAY
    script = (
        f"mkdir -p {inner_dir}; "
        "ffmpeg -y -loglevel error -f x11grab -framerate 8 -draw_mouse 1 "
        f"{size_args}-i {display} -an -c:v libx264 -preset ultrafast "
        f"-pix_fmt yuv420p {inner} </dev/null >{inner_dir}/ffmpeg.log 2>&1 & "
        "echo $!"
    )
    try:
        proc = subprocess.run(
            [*machine_view.exec_prefix(live.machine), "sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, IsolationUnavailable):
        return
    out = (proc.stdout or "").strip().splitlines()
    pid_s = out[-1] if out else ""
    if proc.returncode != 0 or not pid_s.isdigit():
        return
    live.pid = int(pid_s)
    live.inner_video = inner


def _start_sampler(live: _Live, paths: HarnessPaths) -> None:
    stop = threading.Event()
    live.sampler_stop = stop
    interval = _sample_interval()

    def _run() -> None:
        n = 0
        while not stop.wait(interval):
            frame = _capture(paths, live.bot)
            if not frame:
                continue
            dest = live.session_dir / f"sample_{n:04d}.png"
            try:
                dest.write_bytes(frame)
            except OSError:
                continue
            live.samples.append(dest)
            n += 1
            if n >= 40:
                return

    threading.Thread(target=_run, name=f"teach-sample-{live.bot}", daemon=True).start()


def _pid_alive(pid: int, machine: str | None) -> bool:
    if machine:
        try:
            proc = subprocess.run(
                [*machine_view.exec_prefix(machine), "sh", "-c", f"kill -0 {pid}"],
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError, IsolationUnavailable):
            return False
        return proc.returncode == 0
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _interrupt(live: _Live) -> None:
    if live.sampler_stop is not None:
        live.sampler_stop.set()
    if live.proc is not None:
        try:
            live.proc.send_signal(2)  # SIGINT — never SIGKILL
        except OSError:
            pass
        return
    if live.pid and live.machine:
        try:
            subprocess.run(
                [
                    *machine_view.exec_prefix(live.machine),
                    "sh",
                    "-c",
                    f"kill -INT {live.pid} 2>/dev/null",
                ],
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError, IsolationUnavailable):
            pass
        return
    if live.pid:
        try:
            os.kill(live.pid, 2)
        except OSError:
            pass


def _wait_stopped(live: _Live, timeout: float = _LEASE_WAIT) -> None:
    deadline = time.monotonic() + timeout
    if live.proc is not None:
        try:
            live.proc.wait(timeout=max(0.1, timeout))
        except subprocess.TimeoutExpired:
            pass
        return
    pid = live.pid
    if not pid:
        return
    while time.monotonic() < deadline:
        if not _pid_alive(pid, live.machine):
            return
        time.sleep(0.2)


def _copy_machine_video(live: _Live) -> None:
    """Bring the recording out of the machine as a tar stream and keep only
    a regular file under the sync size cap.

    /tmp/harness-teach/demo.mp4 inside the machine is a path anything in
    the jail can pre-create: a symlink there used to be reproduced on the
    host by `docker cp` and then followed by ffprobe/ffmpeg, and a sparse
    multi-GB file was materialised on the host every stop with no cap.
    """
    if not (live.machine and live.inner_video):
        return
    try:
        from isolation import engine, state_sync
        from isolation.machines import _tar_output_limit

        live.video.parent.mkdir(parents=True, exist_ok=True)
        tar_path = live.video.with_name(f".{live.video.name}.cp.tar")
        try:
            with tar_path.open("wb") as out:
                proc = engine.run_raw(
                    ["cp", f"{live.machine}:{live.inner_video}", "-"],
                    stdout=out,
                    preexec_fn=_tar_output_limit(),
                )
            if proc.returncode != 0:
                return
            with tar_path.open("rb") as fh:
                _extract_single_regular(fh, live.video, state_sync.max_file_bytes())
        finally:
            tar_path.unlink(missing_ok=True)
    except Exception:
        return


def _extract_single_regular(fileobj, dest: Path, max_bytes: int) -> bool:
    """Write the first regular member of a `docker cp -` tar stream to dest.

    Symlinks, hardlinks, devices and directories are skipped, a member
    over max_bytes (0 = uncapped) is skipped, and the destination is
    replaced atomically so a half-written file is never probed.
    """
    import tarfile
    import tempfile

    with tarfile.open(fileobj=fileobj, mode="r|*") as tar:
        for member in tar:
            if not member.isreg():
                continue
            if max_bytes and member.size > max_bytes:
                return False
            src = tar.extractfile(member)
            if src is None:
                continue
            fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.")
            try:
                with os.fdopen(fd, "wb") as out:
                    while chunk := src.read(1 << 20):
                        out.write(chunk)
                os.replace(tmp, dest)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return True
    return False


def _regular_file(path: Path) -> bool:
    """True for a real file at `path` itself — never a symlink to one."""
    import stat

    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _probe_duration(video: Path, fallback: float) -> float:
    if not _regular_file(video) or video.stat().st_size < 32:
        return fallback
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return fallback
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(video),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return fallback
    try:
        value = float((proc.stdout or "").strip().split(",")[0])
    except ValueError:
        return fallback
    return value if value > 0 else fallback


def _extract_still(video: Path, offset: float, dest: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not _regular_file(video):
        return False
    try:
        proc = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-ss",
                f"{offset:.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-q:v",
                "5",
                str(dest),
            ],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 0


def _stage_uploads(paths: HarnessPaths, session_id: str, files: list[Path]) -> list[dict[str, Any]]:
    paths.uploads.mkdir(parents=True, exist_ok=True)
    out: list[dict[str, Any]] = []
    for i, src in enumerate(files):
        if not src.is_file() or src.stat().st_size <= 0:
            continue
        dest = paths.uploads / f"teach-{session_id}-{i:02d}{src.suffix.lower() or '.png'}"
        try:
            shutil.copy2(src, dest)
        except OSError:
            continue
        out.append({"name": dest.name, "path": str(dest), "size": dest.stat().st_size})
        if len(out) >= _MAX_STILLS:
            break
    return out


def _write_session(live: _Live, extra: dict[str, Any] | None = None) -> None:
    payload = {
        "bot": live.bot,
        "sessionId": live.session_id,
        "sessionDir": str(live.session_dir),
        "videoPath": str(live.video),
        "ffmpegPid": live.pid,
        "machine": live.machine,
        "startedAt": live.started_at,
    }
    if extra:
        payload.update(extra)
    (live.session_dir / "session.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def _drop(key: str, live: _Live) -> None:
    if live.sampler_stop is not None:
        live.sampler_stop.set()
    _live.pop(key, None)


def is_recording(paths: HarnessPaths, bot: str) -> bool:
    with _lock:
        return _key(paths, bot) in _live


def status(paths: HarnessPaths, bot: str) -> dict[str, Any] | None:
    with _lock:
        live = _live.get(_key(paths, bot))
        if live is None:
            return None
        return {
            "bot": bot,
            "session_id": live.session_id,
            "session_dir": str(live.session_dir),
            "started_at": live.started_at,
            "recording": True,
        }


def note_input(paths: HarnessPaths, bot: str, event: dict[str, Any]) -> None:
    """Append a pointer/key event to the live session (best-effort)."""
    with _lock:
        live = _live.get(_key(paths, bot))
        if live is None:
            return
        row = {"ts": time.time()}
        for key in ("action", "x", "y", "px", "py", "key", "text", "button", "amount"):
            if key in event and event[key] is not None:
                row[key] = event[key]
        live.inputs.append(row)
        try:
            with (live.session_dir / "input.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError:
            pass


def start(paths: HarnessPaths, bot: str) -> dict[str, Any]:
    """Begin recording. Returns the new control state (asdict)."""
    bot = (bot or "").strip()
    if not bot:
        raise TeachUnavailable("teach record needs a bot")
    key = _key(paths, bot)
    with _lock:
        existing = _live.get(key)
        if existing is not None:
            alive = existing.proc is not None and existing.proc.poll() is None
            alive = alive or (
                existing.pid is not None and _pid_alive(existing.pid, existing.machine)
            )
            if alive or (existing.sampler_stop is not None and not existing.sampler_stop.is_set()):
                raise TeachBusy(f"{bot} is already recording a demonstration")
            _drop(key, existing)
        session_id = uuid.uuid4().hex[:12]
        session_dir = sessions_root(paths) / bot / f"teach-{session_id}"
        session_dir.mkdir(parents=True, exist_ok=True)
        machine = machine_view.machine_for_bot(paths, bot)
        live = _Live(
            bot=bot,
            session_id=session_id,
            session_dir=session_dir,
            video=session_dir / "demo.mp4",
            machine=machine,
            started_at=time.time(),
        )
        _start_ffmpeg(live, paths)
        first = _capture(paths, bot)
        if first:
            dest = session_dir / "start.png"
            dest.write_bytes(first)
            live.samples.append(dest)
        if live.proc is None and live.pid is None:
            if first is None and not os.environ.get("HARNESS_SCREENSHOT_CMD"):
                session_dir_cleanup = session_dir
                try:
                    shutil.rmtree(session_dir_cleanup, ignore_errors=True)
                except OSError:
                    pass
                raise TeachUnavailable("no display to record (no ffmpeg capture, no screenshot)")
            _start_sampler(live, paths)
        _write_session(live)
        _live[key] = live
    state = Control(paths).start_teach(bot, recording=True)
    payload = asdict(state)
    payload["session_id"] = session_id
    return payload


def abort(paths: HarnessPaths, bot: str) -> None:
    """Stop capture without learning. Safe if nothing is live."""
    key = _key(paths, bot)
    with _lock:
        live = _live.get(key)
        if live is None:
            return
        _interrupt(live)
        _wait_stopped(live, timeout=5.0)
        try:
            live.video.unlink(missing_ok=True)
        except OSError:
            pass
        _drop(key, live)


def stop(paths: HarnessPaths, bot: str) -> StopResult:
    """Finalize capture, stage stills, exit teach mode. Does not dispatch chat."""
    key = _key(paths, bot)
    with _lock:
        live = _live.get(key)
        if live is None:
            raise TeachIdle(f"no demonstration is recording for {bot}")
        _live.pop(key, None)
    _interrupt(live)
    _wait_stopped(live)
    _copy_machine_video(live)
    last = _capture(paths, bot)
    if last:
        dest = live.session_dir / "stop.png"
        dest.write_bytes(last)
        live.samples.append(dest)
    elapsed = max(0.0, time.time() - live.started_at)
    duration = _probe_duration(live.video, elapsed)

    stills: list[Path] = []
    if _regular_file(live.video) and live.video.stat().st_size > 32 and duration >= _MIN_DURATION:
        offsets = [duration * 0.2, duration * 0.5, duration * 0.7]
        for i, ss in enumerate(offsets):
            dest = live.session_dir / f"frame_{i}.jpg"
            if _extract_still(live.video, ss, dest):
                stills.append(dest)
    if not stills:
        stills = list(live.samples)

    if live.inputs:
        log = live.session_dir / "input.json"
        try:
            log.write_text(json.dumps(live.inputs, indent=2) + "\n", encoding="utf-8")
            stills.append(log)
        except OSError:
            pass

    attachments = _stage_uploads(paths, live.session_id, stills)
    image_atts = [
        a
        for a in attachments
        if Path(str(a.get("name") or "")).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
    ]
    unusable = not image_atts
    reason = None
    if unusable:
        reason = f"capture was {duration:.2f}s" if duration < _MIN_DURATION else "no usable frames"

    _write_session(
        live,
        {
            "endedAt": time.time(),
            "duration": duration,
            "unusable": unusable,
            "reason": reason,
        },
    )
    try:
        live.video.unlink(missing_ok=True)
        for part in live.session_dir.glob("demo_part_*.mp4"):
            part.unlink(missing_ok=True)
    except OSError:
        pass

    Control(paths).cancel_teach(bot)
    return StopResult(
        bot=bot,
        session_id=live.session_id,
        duration=duration,
        attachments=attachments,
        unusable=unusable,
        reason=reason,
    )
