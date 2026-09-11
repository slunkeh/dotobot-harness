"""Ack obligations: never leave an accepted user message unanswered.

When the harness accepts a user chat for a bot it records an obligation file
(`$HARNESS_HOME/obligations/<bot>.json`): "this bot owes the user a visible
reply". The obligation is cleared the moment the bot sends any user-visible
reply; multiple pending messages coalesce into the one obligation per bot.

The failure mode this covers is the one the existing crash-retry bookkeeping
(`Agent._note_attempt`) cannot: that bookkeeping retries a message that is
still *in the inbox*, but says nothing when the message vanished entirely —
the process died after consuming it, the inbox file was lost, or the harness
restarted between accept and dispatch. On bot start and periodically in the
run loop, an obligation that is older than a short idle window with no user
work left in the inbox is *redriven*: a recovery prompt is enqueued (at most
`MAX_REDRIVES` times, counted on the obligation) worded so the bot tells the
user it may have missed their message rather than fabricating completion.

While user messages are still queued the run loop will reach them on its own,
so redrive stays quiet — the obligation is just re-stamped when a reply goes
out with work remaining, and cleared when the queue drains.
"""

from __future__ import annotations

import json
import os
import time

from harness.fsutil import write_atomic
from harness.paths import HarnessPaths

from . import messaging

#: Redrives per obligation before it is marked lost and dropped.
MAX_REDRIVES = 3

#: Seconds of quiet after the last accept/redrive before a redrive fires.
DEFAULT_IDLE_WINDOW = 20.0

#: Ids remembered per obligation (diagnostics only; the count is what matters).
_MAX_IDS = 20

RECOVERY_PROMPT = (
    "[System recovery] One or more of your user's messages may never have been "
    "answered — the turn handling them was interrupted, or the harness "
    "restarted before a reply went out, and their latest message may be "
    "missing from your context entirely. Respond to the user now: if you can "
    "see their latest message and already finished what it asked, send a brief "
    "confirmation with the result; if you can see it but the work is not done, "
    "acknowledge them and continue the work; if you cannot be certain what "
    "they last asked, tell them you may have missed their latest message and "
    "ask them to resend it. Never guess or claim completion of work you "
    "cannot see."
)


def idle_window() -> float:
    """Redrive idle window in seconds ($HARNESS_ACK_IDLE, default 20)."""
    try:
        return float(os.environ.get("HARNESS_ACK_IDLE", DEFAULT_IDLE_WINDOW))
    except ValueError:
        return DEFAULT_IDLE_WINDOW


def get(paths: HarnessPaths, bot: str) -> dict | None:
    """The bot's open obligation, or None."""
    try:
        data = json.loads(paths.obligation_file(bot).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write(paths: HarnessPaths, bot: str, record: dict) -> dict:
    write_atomic(paths.obligation_file(bot), json.dumps(record, ensure_ascii=False))
    return record


def clear(paths: HarnessPaths, bot: str) -> None:
    try:
        paths.obligation_file(bot).unlink()
    except OSError:
        pass


def record_send(paths: HarnessPaths, bot: str, msg_id: str, *, now: float | None = None) -> dict:
    """Record that `bot` owes the user a reply; coalesce into any open one.

    A second accepted message does not create a second obligation — it bumps
    `coalesced` and re-stamps `last_send_ts` so the idle window restarts. The
    redrive count carries over: redrives are per obligation, not per message.
    """
    now = time.time() if now is None else now
    record = get(paths, bot)
    if record is None:
        record = {
            "bot": bot,
            "created_ts": now,
            "last_send_ts": now,
            "message_ids": [msg_id],
            "coalesced": 1,
            "redrives": 0,
            "last_redrive_ts": None,
        }
    else:
        ids = [str(i) for i in record.get("message_ids") or []]
        if msg_id not in ids:
            ids.append(msg_id)
        record["message_ids"] = ids[-_MAX_IDS:]
        record["coalesced"] = int(record.get("coalesced", 1)) + 1
        record["last_send_ts"] = now
    return _write(paths, bot, record)


def _user_pending(paths: HarnessPaths, bot: str) -> bool:
    """A real user chat is still queued — background work does not count.

    Routines and dream ticks send as frm="user" (lanes tell them
    apart). Counting one here would keep an obligation alive with no user
    message behind it, and the eventual redrive posts a spurious "[System
    recovery]" turn.
    """
    return any(
        (m.frm or "") == "user" and messaging.lane_of(m) == messaging.LANE_USER
        for m in messaging.pending(paths, bot)
    )


def settle(paths: HarnessPaths, bot: str, *, now: float | None = None) -> None:
    """A user-visible reply went out: clear the obligation, or keep it alive.

    With user messages still queued the obligation stays (they are covered by
    the same coalesced obligation) but its clock restarts — the run loop is
    clearly making progress, so no redrive should fire underneath it.
    """
    record = get(paths, bot)
    if record is None:
        return
    if _user_pending(paths, bot):
        record["last_send_ts"] = time.time() if now is None else now
        _write(paths, bot, record)
        return
    clear(paths, bot)


def maybe_redrive(
    paths: HarnessPaths,
    bot: str,
    *,
    now: float | None = None,
    idle: float | None = None,
) -> str | None:
    """Redrive an unmet obligation after the idle window; None when quiet.

    Fires only when the obligation has sat past the idle window *and* no user
    message is waiting in the inbox — a still-queued message is handled by the
    normal run loop (and by the crash-retry attempt file when it keeps
    crashing), so redriving it would just double-send. Returns the recovery
    prompt it enqueued, or None. After `MAX_REDRIVES` counted attempts the
    obligation is dropped as lost instead of nagging forever.
    """
    record = get(paths, bot)
    if record is None:
        return None
    now = time.time() if now is None else now
    idle = idle_window() if idle is None else idle
    reference = max(
        float(record.get("last_send_ts") or 0.0),
        float(record.get("last_redrive_ts") or 0.0),
    )
    if now - reference < idle:
        return None
    if _user_pending(paths, bot):
        return None
    if int(record.get("redrives", 0)) >= MAX_REDRIVES:
        clear(paths, bot)  # lost: three recovery prompts went unanswered
        return None
    record["redrives"] = int(record.get("redrives", 0)) + 1
    record["last_redrive_ts"] = now
    _write(paths, bot, record)
    messaging.send(paths, messaging.Msg(to=bot, frm="user", text=RECOVERY_PROMPT))
    return RECOVERY_PROMPT
