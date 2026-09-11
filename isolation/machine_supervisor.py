"""In-machine supervisor: PID 1 of a bot machine (machines backend).

Boots the machine's own display and desktop, then idles reaping children:

    Xvfb :0  ->  computer_env.bringup (openbox + centered 3-icon dock +
    Chromium on the machine's real profile)  ->  wait

There is deliberately NOTHING harness-shaped in here at runtime: no shared
home, no credentials, no message bus. HARNESS_HOME points at a container-local
throwaway (`~/.harness-local`, excluded from state sync) used only for desktop
config files. The agent process lives on the host and reaches this machine
over `docker exec`; state moves via `docker cp` (see machines.py/state_sync).

On SIGTERM (docker stop) Chrome is asked to exit so the profile quiesces —
belt-and-braces with the host-side quiesce the backend already does — then the
supervisor exits 0.

Verified startup/shutdown: the display socket is the one resource
this supervisor binds per machine. Startup refuses to proceed when something
already answers on :0 ("refusing contaminated startup") — a live X server
there means another supervisor generation, and stacking a second desktop on
it would interleave two sessions. A stale, unanswering socket file left by an
unclean death is removed and boot continues. On shutdown the socket must
actually release; a display still accepting connections after Xvfb was
terminated raises instead of reporting a clean stop.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket as socketlib
import subprocess
import sys
import time
from pathlib import Path

DISPLAY = ":0"
_X_SOCKET = Path("/tmp/.X11-unix/X0")
_DEFAULT_GEOMETRY = "1280x800x24"
_RELEASE_TIMEOUT = 5.0


def display_bound(socket_path: Path = _X_SOCKET, *, timeout: float = 0.5) -> bool:
    """True when a live server already accepts connections on the display socket."""
    if not socket_path.exists():
        return False
    sock = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(socket_path))
    except (TimeoutError, BlockingIOError, InterruptedError):
        return True  # a listener exists but is slow/backlogged: still bound
    except OSError:
        return False  # ECONNREFUSED etc.: a stale file, nothing behind it
    finally:
        sock.close()
    return True


def verify_display_released(
    socket_path: Path = _X_SOCKET, *, timeout: float = _RELEASE_TIMEOUT
) -> None:
    """Raise loudly when the display socket stays bound after shutdown."""
    deadline = time.time() + timeout
    while display_bound(socket_path):
        if time.time() >= deadline:
            raise RuntimeError(
                f"machine shutdown left display {DISPLAY} bound: {socket_path} "
                "still accepts connections after Xvfb was terminated"
            )
        time.sleep(0.1)


def start_xvfb(*, spawn=subprocess.Popen, socket_path=_X_SOCKET) -> subprocess.Popen | None:
    """Start Xvfb on :0 and wait for its socket (None when unavailable).

    Refuses a contaminated startup: something already answering on the
    display socket means this machine's one display is taken.
    """
    if display_bound(socket_path):
        raise RuntimeError(
            f"refusing contaminated startup: display {DISPLAY} "
            f"({socket_path}) is already bound by a live server"
        )
    if socket_path.exists():
        # stale socket from an unclean death — nothing answers; clear it so
        # Xvfb does not mistake the leftover for an active display
        with contextlib.suppress(OSError):
            socket_path.unlink()
    geometry = os.environ.get("HARNESS_MACHINE_GEOMETRY") or _DEFAULT_GEOMETRY
    try:
        proc = spawn(
            [
                "Xvfb",
                DISPLAY,
                "-screen",
                "0",
                geometry,
                "-nolisten",
                "tcp",
                "+extension",
                "COMPOSITE",
                "+extension",
                "RENDER",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
    deadline = time.time() + 10
    while time.time() < deadline:
        if socket_path.exists():
            return proc
        if proc.poll() is not None:
            return None
        time.sleep(0.1)
    return proc


def quiesce_chrome(*, run=subprocess.run, timeout: float = 10.0) -> None:
    """Ask Chrome to exit and wait so the profile settles before shutdown."""
    run(["pkill", "-f", "chrom"], capture_output=True, check=False)
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = run(["pgrep", "-f", "chrom"], capture_output=True, check=False)
        if alive.returncode != 0:
            return
        time.sleep(0.5)


def _reap() -> None:
    """PID-1 duty: collect exited desktop children so they don't zombify."""
    try:
        while os.waitpid(-1, os.WNOHANG) != (0, 0):
            pass
    except ChildProcessError:
        pass


#: the shared project directory (isolation.machines.MACHINE_WORKSPACE; the
#: supervisor must not import isolation.machines — it runs inside the jail).
WORKSPACE = "/workspace"


def workspace_status(path: str = WORKSPACE) -> str:
    """How the shared project directory looks from inside this machine.

    'ok' (present and writable), 'disabled' (the backend opted out with
    HARNESS_MACHINE_WORKSPACE=0, so the directory is the image's private one
    and nothing another machine can see), 'missing' (a container from before
    the mount, or an image without the directory), or
    'read-only' (the mount exists but this uid cannot write it: the image
    predates the `chown 1000:1000 /workspace` step, so the volume was
    initialised root-owned — rebuild the image and recreate the volume).
    Printed at boot so the failure is in the machine log, not a mystery
    inside a bot's shell.
    """
    raw = (os.environ.get("HARNESS_MACHINE_WORKSPACE") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return "disabled"
    if not os.path.isdir(path):
        return "missing"
    return "ok" if os.access(path, os.W_OK) else "read-only"


def run_session(
    *,
    bringup,
    start_x=start_xvfb,
    quiesce=quiesce_chrome,
    idle=time.sleep,
    reap=_reap,
    socket_path: Path = _X_SOCKET,
    release_timeout: float = _RELEASE_TIMEOUT,
) -> int:
    """Boot display + desktop, then idle until SIGTERM. Injectable for tests."""
    from harness.paths import HarnessPaths

    home = os.environ.get("HARNESS_HOME") or str(Path.home() / ".harness-local")
    paths = HarnessPaths.resolve(home)
    paths.home.mkdir(parents=True, exist_ok=True)

    def boot():
        try:
            proc = start_x()
        except RuntimeError as exc:  # contaminated startup: refuse, loudly
            print(f"machine supervisor: {exc}", file=sys.stderr)
            return None
        if proc is None:
            return None
        os.environ["DISPLAY"] = DISPLAY
        report = bringup(paths, display=DISPLAY, no_sandbox=True)
        print(f"machine desktop up: {report}", flush=True)
        ws = workspace_status(WORKSPACE)
        if ws != "ok":
            print(f"machine supervisor: shared workspace {WORKSPACE} is {ws}", file=sys.stderr)
        return proc

    xvfb = boot()
    if xvfb is None:
        print("machine supervisor: could not start Xvfb", file=sys.stderr)
        return 1

    terminated: list[int] = []
    signal.signal(signal.SIGTERM, lambda signum, frame: terminated.append(signum))
    signal.signal(signal.SIGINT, lambda signum, frame: terminated.append(signum))
    while not terminated:
        reap()
        if xvfb is None or xvfb.poll() is not None:
            print("machine supervisor: Xvfb exited, restarting", flush=True)
            xvfb = boot()
            if xvfb is None:
                idle(2.0)
                continue
        idle(1.0)

    quiesce()
    try:
        if xvfb is not None:
            xvfb.terminate()
    except OSError:
        pass
    # verified shutdown: the display we bound must actually be released
    verify_display_released(socket_path, timeout=release_timeout)
    return 0


def main() -> int:
    from harness.computer_env import bringup

    return run_session(bringup=bringup)


if __name__ == "__main__":
    raise SystemExit(main())
