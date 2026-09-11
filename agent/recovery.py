"""Turn admission claims + restart recovery sweep.

A `harness restart` or crash used to drop an in-flight turn silently: the
inbox queue survives, but the admitted turn — and any steered follow-ups it
had already consumed from the inbox — did not. Port of OpenClaw v2's restart
recovery anchor, on the SQLite state store (`harness/statestore.py`).

Three overlapping detections of interrupted work:

1. **Admission** — when a bot accepts a message as a turn, one SQLite
   transaction records the input reference, the session marked running, and a
   recovery claim `{bot, session, request_id, ts}` before any provider call
   (`admit_turn`, called from `Agent.process_inbox_once`).
2. **Graceful shutdown** — SIGTERM stamps a recovery marker on every live
   claim (`stamp_shutdown`), the run loop stops admitting, and messages that
   arrived in the drain window are rejected with an explicit restart error
   instead of being queued into a dying process (`reject_drain_arrivals`).
3. **Startup** — the respawned agent sweeps for claims still marked running
   with no live owner process (SIGKILL/OOM left nothing to stamp them) and
   clears stale session locks (`startup_sweep`).

Recovery charges a durable budget of `DEFAULT_BUDGET` redispatch attempts per
interrupted turn: charged before dispatch, refunded only on a proven
pre-acceptance rejection, kept when the outcome is uncertain. An exhausted
budget tombstones the turn, drops its inputs from the queue (never loop), and
queues a one-line `push_turn_note` telling the bot a turn was lost.

The sweep runs inside the agent process after spawn, so the restart endpoint
(`POST /api/bots/<name>/restart`) keeps its return-on-record contract — spawn
never waits on the state store.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, fields

from harness.fsutil import pid_alive
from harness.paths import HarnessPaths
from harness.statestore import StateStore

from . import messaging, obligations
from .streaming import StreamWriter, clear_steered, push_turn_note

#: What a message arriving while the process is draining gets back, instead of
#: silently queueing into a process that is about to die.
RESTART_ERROR = (
    "(error: the harness is restarting; this message was not handled — "
    "please send it again in a moment)"
)


def store_for(paths: HarnessPaths) -> StateStore:
    return StateStore(paths)


# -- admission bookkeeping (best-effort: must never fail a turn) -------------
def admit_turn(
    store: StateStore,
    *,
    bot: str,
    session: str,
    msg: messaging.Msg,
    input_ref: str = "",
    pid: int | None = None,
) -> None:
    """Record the admission claim for `msg` before any provider call.

    Best-effort on purpose: a corrupt or locked state store must degrade to
    the legacy behavior (no recovery), never brick chat.
    """
    try:
        store.admit(
            bot,
            session,
            msg.id,
            input_ref=input_ref,
            input_json=json.dumps([asdict(msg)], ensure_ascii=False),
            pid=os.getpid() if pid is None else pid,
            ts=float(msg.ts),
        )
    except Exception:
        pass


def record_steered(store: StateStore, request_id: str, msgs: list[messaging.Msg]) -> None:
    """A steer consumed follow-ups from the inbox; fold them into the live
    claim so a crash after the steer still redispatches them."""
    try:
        store.record_steer(request_id, [asdict(m) for m in msgs])
    except Exception:
        pass


def settle_turn(store: StateStore, request_id: str) -> None:
    try:
        store.settle(request_id)
    except Exception:
        pass


def stamp_shutdown(store: StateStore, bot: str) -> int:
    """Graceful shutdown: recovery marker on every live claim for `bot`."""
    try:
        return store.mark_interrupted(bot, reason="shutdown")
    except Exception:
        return 0


# -- drain window ------------------------------------------------------------
def reject_drain_arrivals(paths: HarnessPaths, bot: str, *, since: float) -> list[messaging.Msg]:
    """Answer messages that arrived after the drain began with a restart
    error and archive them — a dying process must not eat new work. Messages
    queued before the drain stay queued: the queue survives the restart."""
    rejected: list[messaging.Msg] = []
    for path, msg in messaging.read_inbox(paths, bot):
        if msg.reply_to is not None or float(msg.ts) <= since:
            continue
        messaging.mark_processed(paths, bot, path)
        StreamWriter(paths, msg.id).final(RESTART_ERROR, bot)
        messaging.send(
            paths,
            messaging.Msg(
                to=msg.frm or "user",
                frm=bot,
                text=RESTART_ERROR,
                reply_to=msg.id,
                room=msg.room,
            ),
        )
        if (msg.frm or "") == "user":
            # The error IS the visible reply: the ack obligation is met.
            obligations.settle(paths, bot)
        rejected.append(msg)
    return rejected


# -- startup sweep -----------------------------------------------------------
def startup_sweep(
    store: StateStore,
    paths: HarnessPaths,
    bot: str,
    *,
    alive=pid_alive,
    pid: int | None = None,
) -> dict:
    """Find interrupted turns and redispatch them, on a charged budget.

    Runs in the agent process at boot (never on the spawn/restart path).
    Returns `{"redispatched": [...], "tombstoned": [...], "refunded": [...]}`
    of request ids.
    """
    pid = os.getpid() if pid is None else pid
    summary: dict[str, list[str]] = {"redispatched": [], "tombstoned": [], "refunded": []}
    store.sweep_stale_sessions(bot, alive=alive)
    for row in store.orphaned(bot, alive=alive):
        rid = str(row["request_id"])
        remaining = store.charge(rid)
        if remaining is None:
            _tombstone(store, paths, bot, row)
            summary["tombstoned"].append(rid)
            continue
        msgs = _claim_messages(row)
        if not msgs and not _queued_ids(paths, bot) & {rid}:
            # No stored input and the original message is gone from the inbox:
            # unrecoverable, and no later sweep will do better — tombstone now
            # instead of burning the rest of the budget across restarts.
            _tombstone(store, paths, bot, row)
            summary["tombstoned"].append(rid)
            continue
        try:
            _ensure_queued(paths, bot, msgs)
        except OSError:
            # Proven pre-acceptance rejection: the inbox never took the file,
            # so this attempt was not consumed — the charge is refunded.
            store.refund(rid)
            summary["refunded"].append(rid)
            continue
        store.mark_queued(rid, pid=pid)
        summary["redispatched"].append(rid)
    return summary


def _claim_messages(row: dict) -> list[messaging.Msg]:
    """The claim's stored input messages (empty when unparseable/absent)."""
    try:
        data = json.loads(str(row.get("input_json") or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        data = [data]
    known = {f.name for f in fields(messaging.Msg)}
    out: list[messaging.Msg] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            out.append(messaging.Msg(**{k: v for k, v in item.items() if k in known}))
        except TypeError:
            continue
    return out


def _queued_ids(paths: HarnessPaths, bot: str) -> set[str]:
    return {m.id for m in messaging.pending(paths, bot)}


def _ensure_queued(paths: HarnessPaths, bot: str, msgs: list[messaging.Msg]) -> int:
    """Re-send the claim's inputs that are no longer in the inbox (a message
    that survived the crash in the queue is its own redispatch vehicle)."""
    queued = _queued_ids(paths, bot)
    sent = 0
    for msg in msgs:
        # A crash can land after acknowledgment but before consuming the
        # inbox entry, so clear the alias even when this input is still queued.
        clear_steered(paths, msg.id)
        if msg.id in queued:
            continue
        msg.to = bot
        messaging.send(paths, msg)
        sent += 1
    return sent


def _tombstone(store: StateStore, paths: HarnessPaths, bot: str, row: dict) -> None:
    """Give up on an interrupted turn: tombstone it, pull its inputs out of
    the queue so it can never loop, and queue a one-line note into the bot's
    next-turn system prompt so it knows a turn was lost."""
    rid = str(row["request_id"])
    try:
        store.tombstone(rid)
    except Exception:
        pass
    msgs = _claim_messages(row)
    # Rooted peer handoffs joined this turn but kept their own queue files.
    # Exhausting a failed primary's budget must not discard those independent
    # inputs: let them get a turn without the failed source after the restart.
    joined_ids = {
        m.id
        for m in msgs
        if m.id != rid and m.origin == "room_handoff" and m.room and m.room_handoff_root
    }
    lost_ids = ({rid} | {m.id for m in msgs}) - joined_ids
    preview = ""
    for m in msgs:
        if (m.text or "").strip():
            preview = m.text.strip()[:80]
            break
    for path, msg in messaging.read_inbox(paths, bot):
        if msg.id in joined_ids:
            clear_steered(paths, msg.id)
        elif (msg.reply_to is None or msg.is_continuation) and msg.id in lost_ids:
            messaging.mark_processed(paths, bot, path)
    detail = f" (it began: {preview!r})" if preview else ""
    push_turn_note(
        paths,
        bot,
        "a previous turn of yours was interrupted by restarts and could not be "
        f"recovered{detail}; it was dropped — never assume its work happened, "
        "and tell the user if it matters",
    )
