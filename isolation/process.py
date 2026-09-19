"""Process isolation backend.

Each bot runs as its own OS process sharing the host directory layout. This is
the laptop-first prototype: real isolation of process state (separate PIDs, cwd,
signals) with the shared volume providing shared credentials/browser/workspace.

Pain points this leaves open (handed to containers/VMs): shared env,
no filesystem namespace, kill blast radius, browser SingletonLock contention.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from harness.fsutil import pid_alive as _pid_alive
from harness.fsutil import write_atomic
from harness.paths import HarnessPaths
from harness.version import __version__

from .base import BotHandle, IsolationBackend, Status


def stop_grace() -> float:
    """Seconds to wait after SIGTERM before SIGKILL. Generous by default so an
    agent that just started a turn can finish it (HARNESS_STOP_GRACE)."""
    try:
        return max(0.5, float(os.environ.get("HARNESS_STOP_GRACE", "10")))
    except ValueError:
        return 10.0


class ProcessBackend(IsolationBackend):
    id = "process"

    def __init__(self, paths: HarnessPaths) -> None:
        self.paths = paths

    def _record(self, handle: BotHandle) -> None:
        self.paths.run.mkdir(parents=True, exist_ok=True)
        from harness.runtime_identity import current_identity
        data = {
            "identity": current_identity(),
            "release_root": str(Path(__file__).resolve().parents[1]),
            "bot": handle.bot,
            "backend": handle.backend,
            "pid": handle.pid,
            # Code version the agent was spawned with: lets a newer server
            # detect adopted agents still running old code and roll them.
            "version": handle.meta.get("version", __version__),
            "started": handle.meta.get("started", time.time()),
        }
        write_atomic(self.paths.run_file(handle.bot), json.dumps(data))

    def load(self, bot: str) -> BotHandle | None:
        rf = self.paths.run_file(bot)
        if not rf.is_file():
            return None
        try:
            data = json.loads(rf.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        handle = BotHandle(
            bot=bot,
            backend=self.id,
            pid=data.get("pid"),
            meta={"version": data.get("version"), "started": data.get("started"), "identity": data.get("identity"), "release_root": data.get("release_root")},
        )
        handle.status = self.status(handle)
        return handle

    def spawn(self, bot: str, argv: list[str]) -> BotHandle:
        existing = self.load(bot)
        if existing and existing.status == Status.RUNNING:
            return existing

        self.paths.run.mkdir(parents=True, exist_ok=True)
        log_path: Path = self.paths.log_file(bot)
        log_fh = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 - lives with the child
        # Pin lazy imports to this immutable release, never the current symlink.
        release_root = str(Path(__file__).resolve().parents[1])
        env = {**os.environ, "PYTHONPATH": release_root}
        proc = subprocess.Popen(
            [sys.executable, *argv],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group; scoped signals
            cwd=release_root,
            env=env,
        )
        handle = BotHandle(bot=bot, backend=self.id, pid=proc.pid, status=Status.RUNNING)
        self._record(handle)
        return handle

    def status(self, handle: BotHandle) -> Status:
        if not handle.pid:
            return Status.STOPPED
        return Status.RUNNING if _pid_alive(handle.pid) else Status.DEAD

    def stop(self, handle: BotHandle) -> None:
        if not handle.pid:
            return
        try:
            os.killpg(os.getpgid(handle.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(handle.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.time() + stop_grace()
        while time.time() < deadline:
            if not _pid_alive(handle.pid):
                break
            time.sleep(0.1)
        else:
            try:
                os.killpg(os.getpgid(handle.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        rf = self.paths.run_file(handle.bot)
        if rf.is_file():
            rf.unlink()
