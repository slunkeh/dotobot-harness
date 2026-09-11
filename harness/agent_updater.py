"""Rolling agent updates: restart stale-code bots when they go idle.

After a harness update the new server adopts agent processes still running
the previous release (run files record the code version each agent was
spawned with — see the isolation backends' `_record`). This background
thread rolls them onto the new code **one at a time**, and only when a bot
is genuinely idle:

* not busy on a turn (pid-validated busy flag),
* nothing pending in its inbox,
* not under human takeover or teach.

A message that races the restart is safe either way: the agent loop only
marks a message processed after its turn completes, so the fresh agent
picks it up from the inbox.

On the machines backend a restart runs the full quiesce -> state sync ->
container stop path, and `_ensure_container` recreates a stopped machine
whose image was rebuilt — so machine-image updates ride this same idle
cycle with no extra code.

Env: HARNESS_AUTO_ROLL=0 disables; HARNESS_ROLL_SPACING seconds between
restarts (default 30).
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


def start_agent_updater(orch, *, interval: float = 15.0) -> None:
    """Background thread driving the roll loop; a no-op when disabled."""
    if os.environ.get("HARNESS_AUTO_ROLL", "1") == "0":
        return
    try:
        spacing = float(os.environ.get("HARNESS_ROLL_SPACING", "30"))
    except ValueError:
        spacing = 30.0

    def _loop():
        last = 0.0
        while True:
            time.sleep(interval)
            if time.time() - last < spacing:
                continue
            try:
                if roll_once(orch):
                    last = time.time()
            except Exception:  # noqa: BLE001 - the updater must never kill serve
                pass

    threading.Thread(target=_loop, daemon=True, name="agent-updater").start()
