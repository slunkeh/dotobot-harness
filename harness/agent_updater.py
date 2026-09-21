"""Drive durable, acknowledged one-bot-at-a-time updates.

HARNESS_AUTO_ROLL=0 disables implicit rolls, not an owner's requested operation.
HARNESS_ROLL_SPACING optionally adds a delay after a healthy bot (default zero).
The legacy idle-only helpers remain available to command-line callers; the
server loop uses the durable operation coordinator.
"""

from __future__ import annotations

import os
import threading
import time

from agent import messaging

from .version import __version__


def _is_stale(handle) -> bool:
    """Running with a different (or unrecorded, i.e. pre-versioning) code version."""
    if handle is None or handle.status is None:
        return False
    if handle.status.value != "running":
        return False
    return handle.meta.get("version") != __version__


def find_rollable(orch) -> str | None:
    """Name of one running stale-code bot that is safe to restart right now."""
    ctrl = orch.control
    for name in orch.roster.names():
        if orch.is_starting(name):
            continue
        if not _is_stale(orch._handle(name)):
            continue
        if ctrl.is_busy(name):
            continue
        if messaging.pending(orch.paths, name):
            continue
        if ctrl.state(name).mode != "bot":
            continue
        return name
    return None


def roll_once(orch, log=print) -> str | None:
    """Restart at most one idle stale bot; return its name (None = nothing to do)."""
    name = find_rollable(orch)
    if name is None:
        return None
    handle = orch._handle(name)
    old = (handle.meta.get("version") if handle else None) or "unversioned"
    orch.restart(name, updating=True)
    log(f"agent-updater: rolled {name} {old} -> {__version__}")
    return name


def start_agent_updater(orch, *, interval: float = 1.0) -> None:
    """Background thread driving the roll loop; a no-op when disabled."""
    try:
        spacing = float(os.environ.get("HARNESS_ROLL_SPACING", "0"))
    except ValueError:
        spacing = 0.0

    def _loop():
        last = 0.0
        observed = None
        while True:
            time.sleep(interval)
            if time.time() - last < spacing:
                continue
            try:
                from . import updates
                operation = updates.snapshot(orch)
                if operation.get("updated_at") != observed:
                    observed = operation.get("updated_at")
                    hub = getattr(orch, "ws_hub", None)
                    if hub:
                        hub.broadcast({"type": "harness_update"})
                if (os.environ.get("HARNESS_AUTO_ROLL", "1") != "0"
                        and operation.get("stage") in {"idle", "complete", "cancelled"}):
                    target = {"version": __version__, "identity": updates.current_identity()}
                    if updates.affected(orch, target):
                        updates.begin(orch, target)
                if updates.tick(orch):
                    last = time.time()
            except Exception as exc:  # noqa: BLE001 - keep the controller serving
                from .redaction import scrub
                print(f"agent-updater: {scrub(str(exc))}", flush=True)

    threading.Thread(target=_loop, daemon=True, name="agent-updater").start()
