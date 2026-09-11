"""Isolation backend interface.

The orchestrator drives bots through a small, backend-agnostic interface so the
laptop (process -> container -> VM) and later the Pi (containers/processes) and
home server (KVM/QEMU VMs) can differ without changing the control plane.

Implemented backends: `process` (host subprocesses), `container` (ephemeral
one-container-per-bot), and `machines` (default: persistent sandboxed
computers). `vm` raises IsolationUnavailable until that backend lands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class IsolationUnavailable(RuntimeError):
    """A backend cannot run here (missing engine/daemon, or not implemented)."""


class Status(StrEnum):
    STARTING = "starting"
    RESTARTING = "restarting"
    UPDATING = "updating"
    RUNNING = "running"
    STOPPED = "stopped"
    DEAD = "dead"
    UNKNOWN = "unknown"


@dataclass
class BotHandle:
    """A running (or recorded) bot instance."""

    bot: str
    backend: str
    pid: int | None = None
    status: Status = Status.UNKNOWN
    meta: dict = field(default_factory=dict)


class IsolationBackend:
    """Spawn/stop/status for one isolation strategy."""

    id: str = "base"

    def spawn(self, bot: str, argv: list[str]) -> BotHandle:
        raise NotImplementedError

    def stop(self, handle: BotHandle) -> None:
        raise NotImplementedError

    def status(self, handle: BotHandle) -> Status:
        raise NotImplementedError
