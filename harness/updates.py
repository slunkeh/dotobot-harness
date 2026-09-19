"""One durable fleet operation. No inferred success from controller readiness."""

from __future__ import annotations

import time
import uuid
from contextlib import ExitStack

from agent import messaging
from harness import update_state as state
from harness.runtime_identity import compatible, current_identity
from harness.version import __version__

TERMINAL = {"complete", "cancelled"}


class UpdateError(RuntimeError):
    pass


def snapshot(orch):
    return state.read(orch.paths) or {"stage": "idle", "bots": []}


def affected(orch, target):
    result = []
    for name in orch.roster.names():
        handle = orch._handle(name)
        if not handle or handle.status.value != "running":
            continue
        if handle.meta.get("version") == target["version"]:
            continue
        if compatible(handle.meta.get("identity"), target.get("identity")):
            continue
        result.append(
            {
                "name": name,
                "previous_version": handle.meta.get("version"),
                "previous_identity": handle.meta.get("identity"),
                "stage": "pending",
                "previous_pid": handle.pid,
                "previous_release": handle.meta.get("release_root"),
            }
        )
    return result


def begin(orch, target=None):
    target = target or {"version": __version__, "identity": current_identity()}
    with state.operation_lock(orch.paths):
        existing = state.read(orch.paths)
        if existing and existing.get("stage") not in TERMINAL:
            return existing
        operation = {
            "id": uuid.uuid4().hex,
            "target": target,
            "previous_version": __version__,
            "stage": "rolling",
            "started_at": time.time(),
            "bots": affected(orch, target),
            "running_before": [
                n
                for n in orch.roster.names()
                if (h := orch._handle(n)) and h.status.value == "running"
            ],
        }
        if not operation["bots"]:
            operation["stage"] = "complete"
        state.save(orch.paths, operation)
        return operation


def retry(orch):
    with state.operation_lock(orch.paths):
        operation = state.read(orch.paths)
        if operation.get("stage") != "needs_attention":
            return operation
        if operation["target"]["version"] != __version__:
            operation["stage"] = "preparing"
            operation.pop("error", None)
            state.save(orch.paths, operation)
            return operation
        # A failed restart can have left a healthy replacement. Reconcile it
        # before another restart; never blindly replay destructive stages.
        for bot in operation["bots"]:
            if bot["stage"] == "needs_attention":
                handle = orch._handle(bot["name"])
                bot["stage"] = (
                    "waiting"
                    if handle
                    and handle.status.value == "running"
                    and handle.meta.get("version") != operation["target"]["version"]
                    else "checking_health"
                )
                bot["check_started"] = time.time()
                bot.pop("error", None)
        operation["stage"] = "rolling"
        operation.pop("error", None)
        state.save(orch.paths, operation)
        return operation


def _save(orch, op):
    state.save(orch.paths, op)
    hub = getattr(orch, "ws_hub", None)
    if hub:
        hub.broadcast({"type": "harness_update"})


def _has_approval(orch, name):
    from agent.streaming import list_prompts

    return bool(list_prompts(orch.paths, name))


def tick(orch):
    """Advance at most one bot. Acknowledgement follows all turn settlement."""
    with state.operation_lock(orch.paths):
        op = state.read(orch.paths)
        if not op or op.get("stage") not in {"rolling", "recovering"}:
            return None
        if op["target"]["version"] != __version__:
            return None  # host updater has not installed this controller yet
        row = next((b for b in op["bots"] if b["stage"] not in {"complete", "stopped"}), None)
        if row is None:
            # Bots added during preparation must also be accounted for.
            known = {b["name"] for b in op["bots"]}
            remaining = [b for b in affected(orch, op["target"]) if b["name"] not in known]
            if remaining:
                op["bots"].extend(remaining)
            else:
                op["stage"] = "complete"
            _save(orch, op)
            return None
        name = row["name"]
        try:
            with orch._lifecycle_lock(name), ExitStack() as admission:
                if name not in orch.roster.names() or (orch.paths.run / f"{name}.stopped").exists():
                    row["stage"] = "stopped"
                    state.release_hold(orch.paths, name, op["id"])
                    _save(orch, op)
                    return None
                handle = orch._handle(name)
                if row["stage"] == "recovering":
                    proof = state.ready(orch.paths, name, handle, op["id"])
                    if proof and proof.get("version") == row.get("previous_version"):
                        check = getattr(orch.backend, "update_healthy", None)
                        if not check or check(handle):
                            state.release_hold(orch.paths, name, op["id"])
                            row["rollback"] = "previous_agent_restored"
                            row["stage"] = op["stage"] = "needs_attention"
                    if time.time() - row.get("check_started", 0) > 120:
                        row["stage"] = op["stage"] = "needs_attention"
                        row["error"] = (
                            "Previous agent did not confirm readiness. State retained; manual recovery required."
                        )
                    _save(orch, op)
                    return None
                if row["stage"] in {"saving_state", "restarting"}:
                    # An interrupted step has an uncertain outcome. Verify it,
                    # never repeat stop/spawn merely because the journal says so.
                    row["stage"] = "checking_health"
                    row["check_started"] = time.time()
                if row["stage"] == "checking_health":
                    proof = state.ready(orch.paths, name, handle, op["id"])
                    if proof and proof.get("version") == op["target"]["version"]:
                        check = getattr(orch.backend, "update_healthy", None)
                        if check and not check(handle):
                            proof = {}
                    if proof and proof.get("version") == op["target"]["version"]:
                        row["stage"] = "complete"
                        row["downtime_seconds"] = time.time() - row.get(
                            "restart_started", time.time()
                        )
                        state.release_hold(orch.paths, name, op["id"])
                    elif time.time() - row.get("check_started", 0) > 120:
                        raise UpdateError(
                            "Bot did not confirm the target runtime. State retained; inspect the bot and retry after recovery."
                        )
                    _save(orch, op)
                    return name if row["stage"] == "complete" else None
                if not handle or handle.status.value != "running":
                    row["stage"] = "stopped"
                    state.release_hold(orch.paths, name, op["id"])
                    _save(orch, op)
                    return None
                row["stage"] = "waiting"
                admission.enter_context(messaging.queue_lock(orch.paths, name))
                state.set_hold(orch.paths, name, op["id"])
                proof = state.ready(orch.paths, name, handle, op["id"])
                control = orch.control.state(name)
                if control.mode != "bot" or control.teach_recording:
                    row["reason"] = "Waiting for human control or teaching to finish"
                elif _has_approval(orch, name):
                    row["reason"] = "Waiting for an approval to be resolved"
                elif orch.control.is_busy(name):
                    row["reason"] = "Waiting for current task to finish"
                elif not proof and not (
                    handle.meta.get("version") == "0.2.110"
                    and not handle.meta.get("identity")
                    and not messaging.pending(orch.paths, name)
                ):
                    row["reason"] = (
                        "Waiting for queue-hold acknowledgement; bridge agents must finish their queue first"
                    )
                else:
                    row.pop("reason", None)
                    row["stage"] = "saving_state"
                _save(orch, op)
                if row["stage"] != "saving_state":
                    return None
                row["restart_started"] = time.time()
                _save(orch, op)
                restart = getattr(orch.backend, "restart_for_update", None)
                row["stage"] = "restarting"
                _save(orch, op)
                if restart:
                    restart(handle, orch._agent_argv(name), op["target"].get("identity"))
                else:
                    orch.backend.stop(handle)
                    orch.backend.spawn(name, orch._agent_argv(name))
                row["stage"] = "checking_health"
                row["check_started"] = time.time()
                _save(orch, op)
        except Exception as exc:
            from harness.redaction import scrub as scrub_secrets

            row["stage"] = "needs_attention"
            row["error"] = scrub_secrets(str(exc))
            op["stage"] = "needs_attention"
            # Only rollback a quiescent, acknowledged agent, with an unchanged
            # machine/state contract and verified previous source still present.
            from .update_rollback import restore_agent

            with orch._lifecycle_lock(name):
                handle = orch._handle(name)
                if state.ready(orch.paths, name, handle, op["id"]):
                    try:
                        if restore_agent(orch, row, op["target"].get("identity")):
                            row["stage"] = op["stage"] = "recovering"
                            row["check_started"] = time.time()
                    except Exception as rollback_error:
                        row["error"] += "; recovery: " + scrub_secrets(str(rollback_error))
            op["error"] = "Update paused. Other bots have not been restarted."
            _save(orch, op)
        return None
