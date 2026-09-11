"""Isolation backend registry."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import BotHandle, IsolationBackend, IsolationUnavailable, Status

if TYPE_CHECKING:
    from harness.paths import HarnessPaths

# Backends import harness.paths. isolation.machine_supervisor is PID 1 of a
# bot machine (`python -m isolation.machine_supervisor`) and must be able to
# load this package *before* harness — a top-level backend import is
# isolation → harness → isolation and the container exits on boot.
_BACKENDS: dict[str, type[IsolationBackend]] | None = None


def _registry() -> dict[str, type[IsolationBackend]]:
    global _BACKENDS
    if _BACKENDS is None:
        from .container import ContainerBackend
        from .machines import MachineBackend
        from .process import ProcessBackend
        from .vm import VMBackend

        _BACKENDS = {
            "machines": MachineBackend,
            "process": ProcessBackend,
            "container": ContainerBackend,
            "vm": VMBackend,
        }
    return _BACKENDS


def available_backends() -> list[str]:
    return sorted(_registry())


def get_backend(name: str, paths: HarnessPaths) -> IsolationBackend:
    try:
        cls = _registry()[name.lower()]
    except KeyError as exc:
        raise ValueError(
            f"Unknown isolation backend {name!r}. Known: {', '.join(available_backends())}"
        ) from exc
    return cls(paths)


def __getattr__(name: str):
    aliases = {
        "MachineBackend": "machines",
        "ProcessBackend": "process",
        "ContainerBackend": "container",
        "VMBackend": "vm",
    }
    key = aliases.get(name)
    if key is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return _registry()[key]


__all__ = [
    "BotHandle",
    "ContainerBackend",
    "IsolationBackend",
    "IsolationUnavailable",
    "MachineBackend",
    "ProcessBackend",
    "Status",
    "VMBackend",
    "available_backends",
    "get_backend",
]
