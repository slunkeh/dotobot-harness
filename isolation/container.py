"""Container isolation backend.

One Docker/Podman container per bot, driven through the engine CLI so the
runtime stays stdlib-only. The shared harness home is bind-mounted at the SAME
path inside the container, so the agent argv the orchestrator builds for the
process backend works unmodified; the roster file gets its own read-only mount
when it lives outside the home.

Engine selection: $HARNESS_CONTAINER_ENGINE, else docker, else podman.
Image: $HARNESS_CONTAINER_IMAGE or the `image` backend option, default
`dotobot` (built from deploy/Dockerfile). Per-bot images are a follow-on.
"""

from __future__ import annotations

import json
import os
import subprocess

from harness.paths import HarnessPaths

from . import engine
from .base import BotHandle, IsolationBackend, IsolationUnavailable, Status

_DEFAULT_IMAGE = "dotobot"


class ContainerBackend(IsolationBackend):
    id = "container"

    def __init__(self, paths: HarnessPaths, **options) -> None:
        self.paths = paths
        self.image = (
            os.environ.get("HARNESS_CONTAINER_IMAGE")
            or str(options.get("image") or "")
            or _DEFAULT_IMAGE
        )

    # -- engine plumbing (shared with the machines backend) ----------------
    def _engine(self) -> str:
        return engine.resolve_engine()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return engine.run(*args)

    @staticmethod
    def _container_name(bot: str) -> str:
        return f"harness-{bot}"

    # -- run-file persistence (mirrors ProcessBackend) ---------------------
    def _record(self, handle: BotHandle) -> None:
        self.paths.run.mkdir(parents=True, exist_ok=True)
        data = {
            "bot": handle.bot,
            "backend": handle.backend,
            "pid": None,
            "container": handle.meta.get("container"),
            "name": handle.meta.get("name"),
        }
        self.paths.run_file(handle.bot).write_text(json.dumps(data), encoding="utf-8")

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
            meta={
                "container": data.get("container"),
                "name": data.get("name") or self._container_name(bot),
            },
        )
        handle.status = self.status(handle)
        return handle

    # -- lifecycle ---------------------------------------------------------
    def spawn(self, bot: str, argv: list[str]) -> BotHandle:
        existing = self.load(bot)
        if existing and existing.status == Status.RUNNING:
            return existing

        name = self._container_name(bot)
        home = str(self.paths.home)
        # clear any stopped leftover claiming the name (ignore failure)
        self._run("rm", "-f", name)

        run_args = [
            "run",
            "-d",
            "--name",
            name,
            "-v",
            f"{home}:{home}",
            "-e",
            f"HARNESS_HOME={home}",
            "-e",
            f"HARNESS_BOT={bot}",
            # per-bot environment: this bot does not drive the host display,
            # so a takeover of another bot must not pause it
            "-e",
            "HARNESS_SHARED_DISPLAY=0",
        ]
        roster = self._argv_value(argv, "--roster")
        if roster and not roster.startswith(home.rstrip("/") + "/"):
            run_args += ["-v", f"{roster}:{roster}:ro"]
        run_args += [self.image, "python", *argv]

        proc = self._run(*run_args)
        if proc.returncode != 0:
            raise IsolationUnavailable(
                f"container spawn for {bot!r} failed "
                f"(is the daemon running and image {self.image!r} built?): "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        container_id = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        handle = BotHandle(
            bot=bot,
            backend=self.id,
            status=Status.RUNNING,
            meta={"container": container_id, "name": name},
        )
        self._record(handle)
        return handle

    @staticmethod
    def _argv_value(argv: list[str], flag: str) -> str:
        try:
            return argv[argv.index(flag) + 1]
        except (ValueError, IndexError):
            return ""

    def status(self, handle: BotHandle) -> Status:
        name = handle.meta.get("name") or self._container_name(handle.bot)
        try:
            proc = self._run("inspect", "-f", "{{.State.Status}}", name)
        except IsolationUnavailable:
            return Status.UNKNOWN
        if proc.returncode != 0:
            return Status.STOPPED
        state = proc.stdout.strip().lower()
        if state == "running":
            return Status.RUNNING
        if state in ("exited", "dead"):
            return Status.DEAD
        return Status.UNKNOWN

    def stop(self, handle: BotHandle) -> None:
        name = handle.meta.get("name") or self._container_name(handle.bot)
        try:
            self._run("rm", "-f", name)
        except IsolationUnavailable:
            pass
        rf = self.paths.run_file(handle.bot)
        if rf.is_file():
            rf.unlink()
