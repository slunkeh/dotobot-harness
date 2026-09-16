"""File-based message bus.

The cheapest transport that works locally: JSON files dropped into
`shared/messages/<to>/inbox/`. Every participant (each bot, plus "user") has an
inbox. Messages carry sender attribution and an optional `reply_to` id so
replies can be correlated.

This is deliberately throwaway — the orchestrator can swap in Redis/NATS/HTTP
later behind the same `send` / `read_inbox` shape.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from harness.paths import HarnessPaths
from harness.roster import valid_bot_name

from . import echoguard

# A group can contain six bots; permit a full chain plus a return to the
# initiator, but never an unbounded autonomous mention loop.
MAX_ROOM_HANDOFF_DEPTH = 6
MAX_PRIVATE_HANDOFF_DEPTH = 8

# inbox path -> (stamp, parsed items). Stamp is dir mtime + filenames so a
# send/mark_processed invalidates automatically.
_INBOX_CACHE: dict[str, tuple[tuple, list[tuple[Path, Msg]]]] = {}


def _inbox_stamp(inbox: Path) -> tuple:
    if not inbox.is_dir():
        return ()
    try:
        names = tuple(sorted(p.name for p in inbox.glob("*.json")))
        return (inbox.stat().st_mtime_ns, names)
    except OSError:
        return ()


@dataclass
class Msg:
    to: str
    frm: str
    text: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=time.time)
    reply_to: str | None = None
    attachments: list[dict] = field(default_factory=list)
    room: str | None = None
    skill: str | None = None
    mentions: list[str] = field(default_factory=list)
    #: user tapped "Send now": jump the queue and preempt the
    #: in-flight turn. Plain sends wait their turn instead.
    now: bool = False
    #: reply-with-quote context: {id, author, text} of the message
    #: this one answers, folded into the bot's turn so it knows which line.
    quote: dict | None = None
    #: where the message came from when the sender name is not enough
    #:: routines send as "user" but schedule background work, so
    #: they stamp origin="routine" and drain behind real chats. Dream turns
    #: (harness/dreaming.py) stamp origin="dream" ("idle" from the first
    #: release) — same lane, and the tool gate holds side-effect intents
    #: back on those unattended turns.
    origin: str | None = None
    #: Persisted across worker restarts; each in-group bot handoff increments it.
    room_handoff_depth: int = 0
    room_handoff_root: str | None = None
    room_handoff_source: str | None = None
    #: side-thread root (the parent's message_id). None = main 1:1.
    thread_id: str | None = None
    #: stable bubble id for this send (client-generated or server-assigned).
    message_id: str | None = None
    voice_call_id: str | None = None
    #: Runtime-owned return scope for a nonblocking private colleague request.
    resume: dict | None = None

    @property
    def is_continuation(self) -> bool:
        return self.reply_to is not None and isinstance(self.resume, dict)

    @property
    def reply_recipient(self) -> str:
        return str(self.reply_target["to"])

    @property
    def reply_target(self) -> dict:
        if self.is_continuation:
            return self.resume.get("reply_target") or {"to": "user", "reply_to": self.id}
        return {
            "to": self.frm,
            "reply_to": self.id,
            "resume": self.resume,
            "thread_id": self.thread_id,
            "room": self.room,
            "origin": self.origin,
        }

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_file(cls, path: Path) -> Msg:
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


_HANDOFF_PREFIXES = ("[Handoff from ", "[Private consult from ")


def handoff_brief(from_bot: str, text: str) -> str:
    """Wrap a bot-to-bot request so the asked bot knows how to reply."""
    return (
        f"[Handoff from {from_bot}. This request is posted in your chat with the "
        "user — do the work there. Reply to me with only a short summary of what "
        "you did, or a clarifying question if you cannot finish. Do not open "
        "secret/choice boxes; put any user question in your reply so I can ask "
        f"them.]\n\n{text}"
    )


def is_handoff(text: str) -> bool:
    """True when `text` is a message_agent wrapper, not the raw request."""
    return (text or "").lstrip().startswith(_HANDOFF_PREFIXES)


def handoff_visible_text(text: str) -> str:
    """The user-visible request, with the handoff wrapper stripped."""
    body = text or ""
    stripped = body.lstrip()
    if not stripped.startswith(_HANDOFF_PREFIXES):
        return body
    parts = stripped.split("\n\n", 1)
    if len(parts) == 2:
        return parts[1].strip()
    # Fallback: drop the leading [....] line.
    close = stripped.find("]")
    if close >= 0:
        return stripped[close + 1 :].strip()
    return stripped


#: scheduling lanes, drained in this priority order.
LANE_USER = "user"
LANE_AGENT = "agent"
LANE_BACKGROUND = "background"
LANE_ORDER = (LANE_USER, LANE_AGENT, LANE_BACKGROUND)

#: message origins that are background work rather than somebody talking
ORIGIN_ROUTINE = "routine"
ORIGIN_DREAM = "dream"
#: legacy wire value from the first release of dreaming ("idle think");
#: messages carrying it are still background work and still gated as dreams
ORIGIN_IDLE = "idle"
#: a new bot's first turn (`harness/welcome.py`). NOT background: it stays
#: in the user lane so the bot may ask its user questions — the origin only
#: keeps the prompt off transcripts and out of the user-bubble fan-out.
ORIGIN_WELCOME = "welcome"

_ROUTINE_HEAD = "[Routine:"


def split_routine_prompt(text: str) -> tuple[str, str]:
    """Title and body of a `[Routine: title]\n...` scheduler prompt."""
    raw = (text or "").strip()
    if not raw.startswith(_ROUTINE_HEAD):
        return "Routine", raw
    close = raw.find("]")
    if close < 0:
        return "Routine", raw
    title = raw[len(_ROUTINE_HEAD) : close].strip() or "Routine"
    body = raw[close + 1 :].strip()
    return title, body


def is_routine_prompt(text: str, origin: str | None = None) -> bool:
    """True when this line is a `[Routine: …]` scheduler dump.

    `origin` is ignored. Inbox fan-out (`relay_bot_turn`) stamps
    origin=routine on ordinary user turns too (late prompt answers,
    block actions); those are a person talking, not a scheduled run.
    """
    del origin
    return (text or "").lstrip().startswith(_ROUTINE_HEAD)


def lane_of(msg: Msg) -> str:
    """Scheduling lane for a pending message: human chat > bot-to-bot > routine."""
    if (msg.origin or "") in (ORIGIN_ROUTINE, ORIGIN_DREAM, ORIGIN_IDLE):
        return LANE_BACKGROUND
    if (msg.frm or "user") in ("", "user"):
        return LANE_USER
    return LANE_AGENT


@contextmanager
def queue_lock(paths: HarnessPaths, name: str):
    """Serialize queue edits with admission and steering across processes."""
    directory = paths.inbox(name).parent
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".queue.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def queue_order(paths: HarnessPaths, name: str) -> list[str]:
    try:
        value = json.loads((paths.inbox(name).parent / "queue-order.json").read_text())
        return value if isinstance(value, list) and all(isinstance(i, str) for i in value) else []
    except (OSError, ValueError):
        return []


def save_queue_order(paths: HarnessPaths, name: str, ids: list[str]) -> None:
    from harness.fsutil import write_atomic

    write_atomic(paths.inbox(name).parent / "queue-order.json", json.dumps(ids))


def ordered_pending(paths: HarnessPaths, name: str) -> list[Msg]:
    ranks = {rid: i for i, rid in enumerate(queue_order(paths, name))}
    return sorted(
        pending(paths, name),
        key=lambda m: (
            0 if m.now and m.id not in ranks and lane_of(m) == LANE_USER else 1,
            ranks.get(m.id, len(ranks)),
            LANE_ORDER.index(lane_of(m)),
            m.ts,
            m.id,
        ),
    )


def waiting_items(paths: HarnessPaths, name: str, current_id: str | None) -> list[Msg]:
    from .streaming import read_steered

    def waiting(msg):
        if msg.id == current_id:
            return False
        joined = read_steered(paths, msg.id) if current_id else None
        return joined is None or joined.target_request_id != current_id

    return [m for m in ordered_pending(paths, name) if waiting(m)]


def reorder_queue(paths: HarnessPaths, name: str, ids: list[str], *, current_id=None):
    """Caller holds queue_lock; reject stale snapshots instead of losing arrivals."""
    waiting = [m.id for m in waiting_items(paths, name, current_id)]
    if len(ids) != len(set(ids)) or set(ids) != set(waiting):
        raise ValueError("The queue changed. Refresh and try again.")
    save_queue_order(paths, name, ids)


def remove_queued(paths: HarnessPaths, name: str, rid: str, *, current_id=None) -> bool:
    """Caller holds queue_lock. Never cancel an admitted/running request."""
    if rid not in {m.id for m in waiting_items(paths, name, current_id)}:
        return False
    for path, msg in read_inbox(paths, name):
        if msg.id == rid and (msg.reply_to is None or msg.is_continuation):
            from harness.statestore import store_for

            from . import obligations, recovery
            from .streaming import StreamWriter

            archive = paths.processed(name)
            archive.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(path, archive / path.name)
            except FileNotFoundError:
                return False
            recovery.settle_turn(store_for(paths), rid)
            StreamWriter(paths, rid, room=msg.room).final("Removed from queue.", name)
            if msg.frm == "user":
                obligations.settle(paths, name)
            else:
                # A colleague waiting for this request must receive a terminal response.
                send(
                    paths, Msg(frm=name, text="Removed from queue by the user.", **msg.reply_target)
                )
            save_queue_order(paths, name, [i for i in queue_order(paths, name) if i != rid])
            return True
    return False


def send(paths: HarnessPaths, msg: Msg) -> Path:
    """Write a message atomically into the recipient's inbox."""
    if not str(msg.message_id or "").strip():
        msg.message_id = uuid.uuid4().hex
    # The recipient is a directory under messages/; whoever built the Msg,
    # a path-shaped name never becomes one (sink-side twin of the resolver
    # check in agent/tools.py).
    if not valid_bot_name(msg.to):
        raise ValueError(f"message recipient {msg.to!r} is not a bot name")
    # Reserve the outbound identity BEFORE the file becomes visible
    #: even an echo that beats this call returning is already
    # known to admission as our own message.
    echoguard.guard().reserve(msg.frm, echoguard.conversation_of(room=msg.room, to=msg.to), msg.id)
    inbox = paths.inbox(msg.to)
    inbox.mkdir(parents=True, exist_ok=True)
    # filename sorts by time then id so inbox reads are FIFO-ish
    fname = f"{msg.ts:.6f}-{msg.id}.json"
    final = inbox / fname
    tmp = inbox / f".{fname}.tmp"
    tmp.write_text(msg.to_json(), encoding="utf-8")
    os.replace(tmp, final)  # atomic on same filesystem
    return final


def read_inbox(paths: HarnessPaths, name: str) -> list[tuple[Path, Msg]]:
    """Return (path, Msg) pairs currently in `name`'s inbox, oldest first."""
    inbox = paths.inbox(name)
    key = str(inbox)
    stamp = _inbox_stamp(inbox)
    hit = _INBOX_CACHE.get(key)
    if hit is not None and hit[0] == stamp:
        return list(hit[1])
    if not inbox.is_dir():
        _INBOX_CACHE[key] = (stamp, [])
        return []
    items: list[tuple[Path, Msg]] = []
    for p in sorted(inbox.glob("*.json")):
        try:
            items.append((p, Msg.from_file(p)))
        except (json.JSONDecodeError, OSError):
            continue
    _INBOX_CACHE[key] = (stamp, items)
    return list(items)


def mark_processed(paths: HarnessPaths, name: str, path: Path) -> None:
    """Move a handled message into the processed archive."""
    dest_dir = paths.processed(name)
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(path, dest_dir / path.name)
    except OSError:
        pass


def pending(paths: HarnessPaths, name: str) -> list[Msg]:
    """Inbox messages waiting for a turn (not bot-to-user replies)."""
    return [m for _path, m in read_inbox(paths, name) if m.reply_to is None or m.is_continuation]


def queue_prompt_answers(paths: HarnessPaths, bot: str) -> None:
    """Called between turns: move unconsumed decisions to durable admission.

    The waiting tool owns answers during a turn. After it exits, the same
    mailbox drives a scoped continuation. Write the deterministic inbox file
    before consuming the answer, under the decision transaction: a crash can
    replay that write, but cannot lose the answer or mint a second request.
    """
    from harness.statestore import store_for

    queued = {msg.id for _, msg in read_inbox(paths, bot)}
    store = store_for(paths)
    query = (
        "SELECT payload FROM agent_prompts WHERE bot=? AND state='answered' "
        "AND task_id!='' AND COALESCE(json_extract(payload, '$.answer_consumed'), 0)=0"
    )
    conn = store._connect()
    try:
        if conn.execute(query + " LIMIT 1", (bot,)).fetchone() is None:
            return  # Idle polling does not acquire the database write lock.
    finally:
        conn.close()
    with store._tx() as conn:
        rows = conn.execute(query, (bot,)).fetchall()
        for raw in rows:
            row = json.loads(raw[0])
            resolution = row.get("resolution") or {}
            if (
                not row.get("task_conversation")
                or row.get("request_id") in queued
                or row.get("type") == "secret_request"
                or row.get("card_type") == "control_return"
                or "responded_value" not in resolution
            ):
                continue
            current = conn.execute(
                "SELECT task_id,revision,data FROM agent_tasks WHERE bot=? AND conversation=?",
                (bot, row["task_conversation"]),
            ).fetchone()
            if not (
                current
                and current[0] == row["task_id"]
                and current[1] == row.get("task_revision")
                and json.loads(current[2]).get("status") in {"active", "waiting", "idle"}
            ):
                continue
            rid = "answer-" + uuid.uuid5(uuid.NAMESPACE_URL, row["id"]).hex
            conversation = row["task_conversation"]
            source_origin = row.get("origin")
            if "origin" not in row and conversation.startswith("generated:"):
                source_origin = conversation.split(":", 2)[1]
            thread = row.get("thread_id") or (
                conversation.removeprefix("thread:") if conversation.startswith("thread:") else None
            )
            msg = Msg(
                to=bot,
                frm="user",
                id=rid,
                message_id=rid,
                ts=float(resolution.get("ts") or row["ts"]),
                text="Continue the original task using the recorded answer to this question. "
                "Apply only that decision; verify any previous action before repeating it.\n"
                + json.dumps(
                    {
                        "question": row.get("question") or row.get("payload"),
                        "resolution": resolution,
                    },
                    ensure_ascii=False,
                ),
                origin="prompt_answer",
                reply_to=row.get("request_id") or rid,
                room=row.get("room"),
                thread_id=thread,
                resume={
                    "prompt_id": row["id"],
                    "task_id": row["task_id"],
                    "revision": row["task_revision"],
                    "conversation": conversation,
                    "thread_id": thread,
                    "origin": source_origin,
                    "reply_target": {
                        "to": "user",
                        "reply_to": rid,
                        "room": row.get("room"),
                        "thread_id": thread,
                        "origin": source_origin,
                    },
                },
            )
            filename = f"{msg.ts:.6f}-{msg.id}.json"
            if not (paths.processed(bot) / filename).exists():
                send(paths, msg)
            row["answer_consumed"] = True
            row["answer_resume_id"] = rid
            conn.execute(
                "UPDATE agent_prompts SET payload=? WHERE prompt_id=?",
                (json.dumps(row, ensure_ascii=False), row["id"]),
            )


def continuation_scope(paths: HarnessPaths, bot: str, resume: dict | None) -> dict | None:
    """A late response can resume only the exact still-current task."""
    from harness import taskscope

    if not isinstance(resume, dict):
        return None
    try:
        if resume.get("prompt_id"):
            from harness.statestore import store_for

            row = store_for(paths).prompt(str(resume["prompt_id"]))
            if not (
                row
                and row.get("bot") == bot
                and row.get("answer_resume_id")
                and row.get("task_id") == resume.get("task_id")
                and row.get("task_revision") == resume.get("revision")
                and row.get("task_conversation") == resume.get("conversation")
            ):
                return None
        elif not 1 <= int(resume.get("depth", 0)) <= MAX_PRIVATE_HANDOFF_DEPTH:
            return None
        return taskscope.matching_scope(
            paths,
            bot,
            str(resume["conversation"]),
            str(resume["task_id"]),
            int(resume["revision"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def newer_user(
    paths: HarnessPaths,
    name: str,
    current_id: str | None,
    *,
    now_only: bool = True,
    after_ts: float | None = None,
) -> list[Msg]:
    """User chats that should preempt the in-flight turn.

    A message sent while the bot is busy queues; by default only ones the
    user promoted with "Send now" (`now=True`) count, so working turns are
    not preempted. Blocking human-input waits pass `now_only=False` — a bot
    parked on a choice/secret box yields to a chat that arrived *after this
    turn started* (or a Send-now) rather than holding it for the full wait
    window. Backlog that was already queued when the turn began does not
    count: otherwise a 2-minute Linear turn can never finish behind 6
    waiting messages.

    Only the user lane counts: routines and dream ticks are sent
    as frm="user" but are background work — letting one preempt a parked
    choice sweeps the box as skipped and the follow-up turn re-asks it,
    doubling the card with no visible message explaining why.
    """
    manual = set(queue_order(paths, name))
    items = [
        m
        for _path, m in read_inbox(paths, name)
        if m.reply_to is None
        and (m.frm or "") == "user"
        and lane_of(m) == LANE_USER
        and m.id != current_id
        and m.id not in manual
        and (m.now or not now_only)
        and ((m.text or "").strip() or m.attachments)
    ]
    if after_ts is not None:
        items = [m for m in items if m.now or float(m.ts) > after_ts]
    if not items:
        return []
    if current_id:
        current = next(
            (m for _p, m in read_inbox(paths, name) if m.id == current_id),
            None,
        )
        if current is not None:
            rank = (float(current.ts), current.id)
            items = [m for m in items if (float(m.ts), m.id) > rank]
    return items


def take_steer(paths, name, current_id, **kwargs):
    with queue_lock(paths, name):
        return _take_steer(paths, name, current_id, **kwargs)


def _take_steer(
    paths: HarnessPaths,
    name: str,
    current_id: str | None,
    *,
    room: str | None = None,
    thread_id: str | None = None,
    after_ts: float | None = None,
) -> list[Msg]:
    """Pop user follow-ups that can join the live turn (Hermes steer).

    Only messages that arrived *after* this turn started (`after_ts`) are
    taken — backlog already waiting stays a later turn. Leaves Send-now
    (`now=True`), attachments, background work, and other threads queued.
    Empty bodies are skipped. The caller logs and injects the returned
    messages.
    """
    want_room = room or None
    want_thread = thread_id or None
    manual = set(queue_order(paths, name))
    taken: list[tuple[Path, Msg]] = []
    for path, msg in read_inbox(paths, name):
        if msg.id in manual:
            continue
        if msg.reply_to is not None:
            continue
        if current_id and msg.id == current_id:
            continue
        if lane_of(msg) != LANE_USER:
            continue
        if msg.now:
            continue
        if msg.attachments:
            continue
        if (msg.room or None) != want_room:
            continue
        if (msg.thread_id or None) != want_thread:
            continue
        if after_ts is not None and float(msg.ts) <= after_ts:
            continue
        if not (msg.text or "").strip():
            continue
        taken.append((path, msg))
    out: list[Msg] = []
    for path, msg in taken:
        if current_id:
            from .streaming import StreamWriter

            # Publish before consuming the queue entry: a reconnect always
            # finds either its pending request or the turn it joined.
            StreamWriter(paths, msg.id, room=msg.room).steered(name, current_id)
        mark_processed(paths, name, path)
        out.append(msg)
    return out


def mark_now(paths: HarnessPaths, name: str, msg_id: str) -> bool:
    with queue_lock(paths, name):
        return _mark_now(paths, name, msg_id)


def _mark_now(paths: HarnessPaths, name: str, msg_id: str) -> bool:
    """Promote a queued message to "Send now".

    Rewrites the inbox file with `now=True` so the runtime preempts the
    in-flight turn and handles it next. False when the message is no longer
    queued (already picked up or dropped).
    """
    for path, msg in read_inbox(paths, name):
        if msg.id != msg_id or msg.reply_to is not None:
            continue
        msg.now = True
        save_queue_order(paths, name, [i for i in queue_order(paths, name) if i != msg.id])
        tmp = path.with_name(f".{path.name}.tmp")
        try:
            tmp.write_text(msg.to_json(), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            return False
        return True
    return False


def drop_pending(paths: HarnessPaths, name: str, *, count: int | None = None) -> int:
    """Archive waiting inbox items. `count` drops the newest N; None clears all."""
    items = [(p, m) for p, m in read_inbox(paths, name) if m.reply_to is None]
    if count is None:
        drop = items
    elif count <= 0:
        drop = []
    else:
        drop = items[-count:]
    for path, _msg in drop:
        mark_processed(paths, name, path)
    return len(drop)


def queue_state(
    paths: HarnessPaths, name: str, *, busy: bool = False, current_id: str | None = None
) -> dict:
    """Follow-ups waiting behind the in-flight turn (`current_id` is excluded)."""
    items = waiting_items(paths, name, current_id)
    manual = set(queue_order(paths, name))
    return {
        "bot": name,
        "busy": busy,
        "queued": len(items),
        "editable": True,
        "current_id": current_id,
        "items": [
            {
                "id": m.id,
                "text": handoff_visible_text(m.text),
                "ts": m.ts,
                "frm": m.frm,
                "room": m.room,
                "thread_id": m.thread_id,
                "origin": m.origin,
                "attachments": m.attachments,
                "now": m.now and m.id not in manual,
            }
            for m in items
        ],
    }


def wait_for_reply(
    paths: HarnessPaths,
    name: str,
    reply_to: str,
    *,
    timeout: float = 30.0,
    poll: float = 0.2,
) -> Msg | None:
    """Block until a message addressed to `name` with matching reply_to lands."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for path, msg in read_inbox(paths, name):
            if msg.reply_to == reply_to:
                mark_processed(paths, name, path)
                return msg
        time.sleep(poll)
    return None
