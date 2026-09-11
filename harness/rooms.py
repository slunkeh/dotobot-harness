"""Named group chats (multiple bots + the user).

Rooms live under `$HARNESS_HOME/rooms/`:
    <id>.json     metadata (title, description, members, owner)
    <id>.jsonl    transcript (user + every bot, attributed)
    archive/      a deleted room's metadata + transcript (never unlinked)

Each member bot still uses its own memory and soul when it replies. The room
only shares the conversation transcript.

Transcript rows are one of two shapes:

    text row   {id, ts, frm, text, mentions, message_id?, request_id?,
                attachments?}
    card row   {id, ts, frm, type: "card", card_id, card_type, payload,
                resolution?, text: ""}

A card row re-appended with the same `card_id` (an answered confirm, a
progress update) replaces the earlier one when the transcript is read back,
so `GET /api/rooms/<id>/messages` hydrates a settled box, not a second one.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from harness.paths import HarnessPaths
from harness.redaction import scrub as scrub_secrets

_SLUG = re.compile(r"[^a-z0-9]+")

#: chars/4 token budget for the transcript block a member sees on a turn.
DEFAULT_TRANSCRIPT_TOKENS = 3000
#: hard cap on rows scanned for the transcript block regardless of budget.
TRANSCRIPT_ROW_CAP = 200
MIN_ROOM_MEMBERS = 2
MAX_ROOM_MEMBERS = 6


class RoomError(ValueError):
    """Unknown or invalid room."""


@dataclass
class Room:
    id: str
    title: str
    members: list[str]
    created: float = field(default_factory=time.time)
    #: The human account owner. Bot membership never grants room ownership.
    owner: str = field(default="user", init=False)
    #: Optional blurb shown in the group's settings (persisted here so every
    #: device sees the same text).
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Room:
        members = list(data.get("members") or [])
        # Legacy rooms stored a bot as owner. Interpret them as user-owned
        # without rewriting files on read or changing membership/history.
        return cls(
            id=str(data["id"]),
            title=str(data.get("title") or data["id"]),
            members=members,
            created=float(data.get("created") or time.time()),
            description=str(data.get("description") or ""),
        )


def _slug(title: str) -> str:
    base = _SLUG.sub("-", (title or "group").lower()).strip("-")[:32] or "group"
    return f"{base}-{uuid.uuid4().hex[:6]}"


def room_file(paths: HarnessPaths, room_id: str) -> Path:
    return paths.rooms / f"{room_id}.json"


def transcript_file(paths: HarnessPaths, room_id: str) -> Path:
    return paths.rooms / f"{room_id}.jsonl"


def archive_dir(paths: HarnessPaths) -> Path:
    return paths.rooms / "archive"


def list_rooms(paths: HarnessPaths) -> list[Room]:
    if not paths.rooms.is_dir():
        return []
    rooms: list[Room] = []
    for path in sorted(paths.rooms.glob("*.json")):
        try:
            rooms.append(Room.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError):
            continue
    return rooms


def mentioned_rooms(text: str, rooms: list[Room], *, member: str | None = None) -> list[Room]:
    """Rooms whose title (or id) the text @mentions, longest title first.

    Grok Bot lets a 1:1 line carry a group chip ("@New Bot, New Agent, …
    say hi"); the bot then posts into that group. Titles hold spaces and
    commas, so this is an exact, case-insensitive match right after the
    `@`, not the slug parser in `agent/mentions.py`. `member` narrows the
    result to groups that bot belongs to.
    """
    hay = (text or "").lower()
    if "@" not in hay:
        return []
    out: list[Room] = []
    taken: list[tuple[int, int]] = []  # spans already claimed by a longer title
    for room in sorted(rooms, key=lambda r: -len(r.title)):
        if member and member not in room.members:
            continue
        for needle in (room.title, room.id):
            needle = (needle or "").strip().lower()
            if not needle:
                continue
            start = 0
            hit: tuple[int, int] | None = None
            while True:
                i = hay.find("@" + needle, start)
                if i < 0:
                    break
                end = i + 1 + len(needle)
                bounded = end == len(hay) or not hay[end].isalnum()
                inside = any(a <= i < b for a, b in taken)
                if bounded and not inside:
                    hit = (i, end)
                    break
                start = i + 1
            if hit:
                taken.append(hit)
                out.append(room)
                break
    return out


def get_room(paths: HarnessPaths, room_id: str) -> Room:
    path = room_file(paths, room_id)
    if not path.is_file():
        raise RoomError(f"No room {room_id!r}")
    try:
        return Room.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RoomError(f"Corrupt room {room_id!r}") from exc


def save_room(paths: HarnessPaths, room: Room) -> Room:
    room.owner = "user"
    paths.rooms.mkdir(parents=True, exist_ok=True)
    path = room_file(paths, room.id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(room.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return room


def create_room(
    paths: HarnessPaths,
    title: str,
    members: list[str],
    owner: str | None = None,
    description: str = "",
) -> Room:
    # Keep the legacy owner argument for older callers; bots cannot own rooms.
    names = []
    seen: set[str] = set()
    for name in members:
        n = str(name).strip()
        if n and n not in seen:
            seen.add(n)
            names.append(n)
    if len(names) < MIN_ROOM_MEMBERS:
        raise RoomError("A group chat needs at least two bots")
    if len(names) > MAX_ROOM_MEMBERS:
        raise RoomError("A group chat can include at most six bots")
    room = Room(
        id=_slug(title),
        title=(title or "").strip() or "Group",
        members=names,
        description=(description or "").strip(),
    )
    return save_room(paths, room)


def delete_room(paths: HarnessPaths, room_id: str) -> None:
    """Remove a room from the list. Its files move to `rooms/archive/`.

    Same discipline as session JSONLs: a transcript is never unlinked, so a
    mis-click (or a bot deleting the wrong group) is recoverable by hand.
    """
    get_room(paths, room_id)  # validate
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    target = archive_dir(paths)
    target.mkdir(parents=True, exist_ok=True)
    for path in (room_file(paths, room_id), transcript_file(paths, room_id)):
        if not path.is_file():
            continue
        dest = target / f"{room_id}.{stamp}{path.suffix}"
        try:
            os.replace(path, dest)
        except OSError:
            try:
                path.unlink()
            except OSError:
                pass


def _append_row(paths: HarnessPaths, room_id: str, record: dict) -> dict:
    path = transcript_file(paths, room_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def append_message(
    paths: HarnessPaths,
    room_id: str,
    *,
    frm: str,
    text: str,
    mentions: list[str] | None = None,
    attachments: list[dict] | None = None,
    message_id: str | None = None,
    request_id: str | None = None,
    quote: dict | None = None,
) -> dict:
    """Append one attributed text row.

    `message_id` is the client's id for a user line (the same id the bot's
    turn carries, so `transcript_block` can leave the line being answered out
    of the replayed history). `request_id` is the turn a bot line closed, so
    a client can pair the hydrated row with the stream it already rendered.
    """
    get_room(paths, room_id)
    record: dict = {
        "id": uuid.uuid4().hex,
        "ts": time.time(),
        "frm": frm,
        # Same rule as session JSONLs: a registered secret never
        # lands in the room transcript — its sentinel stands in, and
        # transcript_block replays only the scrubbed text into prompts.
        "text": scrub_secrets(text),
        "mentions": list(mentions or []),
    }
    if message_id:
        record["message_id"] = str(message_id)
    if request_id:
        record["request_id"] = str(request_id)
    atts = [a for a in (attachments or []) if isinstance(a, dict)]
    if atts:
        record["attachments"] = [
            {k: v for k, v in a.items() if k in ("name", "path", "size", "url", "type")}
            for a in atts
        ]
    if isinstance(quote, dict) and quote:
        record["quote"] = {
            k: scrub_secrets(str(v)) if isinstance(v, str) else v
            for k, v in quote.items()
            if k in ("id", "author", "text")
        }
    # A transcript append is an outbound send: record its identity
    # so a copy fanned back at the author is dropped at admission. Deferred
    # import — agent.runtime imports this module the same way.
    from agent.echoguard import guard

    guard().record(frm, room_id, record["id"])
    return _append_row(paths, room_id, record)


def append_card(
    paths: HarnessPaths,
    room_id: str,
    *,
    frm: str,
    card_id: str,
    card_type: str,
    payload: dict,
    resolution: dict | None = None,
) -> dict:
    """Append a durable card row (or the same `card_id` with its resolution).

    Mirrors `Memory.log_card` for the 1:1 log: live WS still fans the card
    out, this is what a client that reloads the room reads back.
    """
    get_room(paths, room_id)
    record: dict = {
        "id": uuid.uuid4().hex,
        "ts": time.time(),
        "frm": frm,
        "type": "card",
        "text": "",
        "card_id": str(card_id),
        "card_type": str(card_type),
        "payload": json.loads(scrub_secrets(json.dumps(dict(payload), ensure_ascii=False))),
    }
    if resolution:
        record["resolution"] = dict(resolution)
    return _append_row(paths, room_id, record)


def _read_rows(paths: HarnessPaths, room_id: str) -> list[dict]:
    path = transcript_file(paths, room_id)
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return _coalesce_cards(rows)


def _coalesce_cards(rows: list[dict]) -> list[dict]:
    """A re-appended card (progress update, answered prompt) replaces the
    first row with that `card_id` in place — same as `history.user_thread`."""
    out: list[dict] = []
    index: dict[str, int] = {}
    for row in rows:
        cid = str(row.get("card_id") or "") if row.get("type") == "card" else ""
        if cid and cid in index:
            keep = dict(row)
            keep["ts"] = out[index[cid]].get("ts", keep.get("ts"))
            out[index[cid]] = keep
            continue
        if cid:
            index[cid] = len(out)
        out.append(row)
    return out


def recent_messages(
    paths: HarnessPaths, room_id: str, *, limit: int = 40, before: float | None = None
) -> list[dict]:
    rows = _read_rows(paths, room_id)
    if before is not None:
        rows = [r for r in rows if float(r.get("ts") or 0) < before]
    return rows[-limit:]


def _estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def _row_line(row: dict) -> str:
    frm = row.get("frm", "?")
    if row.get("type") == "card":
        kind = str(row.get("card_type") or "card")
        res = row.get("resolution") if isinstance(row.get("resolution"), dict) else None
        if res and res.get("responded_value") is not None:
            return f"{frm}: [{kind} card — answered: {res.get('responded_value')}]"
        if res and res.get("state") == "skipped":
            return f"{frm}: [{kind} card — not answered]"
        return f"{frm}: [{kind} card]"
    text = str(row.get("text") or "")
    atts = row.get("attachments") if isinstance(row.get("attachments"), list) else []
    names = [str(a.get("name") or "") for a in atts if isinstance(a, dict) and a.get("name")]
    if names:
        text = f"{text} [attached: {', '.join(names)}]".strip()
    return f"{frm}: {text}"


def handoff_source_block(paths: HarnessPaths, room_id: str, source_id: str) -> str:
    """Resolve the exact triggering row, even when it has left the replay budget."""
    for row in _read_rows(paths, room_id):
        if row.get("id") == source_id:
            return f"[Handoff source {source_id}]\n{_row_line(row)}"
    return f"[Handoff source {source_id} is unavailable; do not substitute a newer message.]"


def transcript_block(
    paths: HarnessPaths,
    room_id: str,
    *,
    limit: int = TRANSCRIPT_ROW_CAP,
    budget_tokens: int | None = None,
    exclude_message_id: str | None = None,
    exclude_row_ids: tuple[str, ...] = (),
) -> str:
    """The shared history a member sees on a turn, newest rows kept first.

    Budgeted like 1:1 history (chars/4 against `budget_tokens`, default
    `$HARNESS_ROOM_TRANSCRIPT_TOKENS` or 3000) instead of a fixed line
    count, so a busy room keeps as much context as fits. The row carrying
    `exclude_message_id` — the very line this turn is answering — is left
    out, because the turn already carries it verbatim.
    """
    if budget_tokens is None:
        raw = os.environ.get("HARNESS_ROOM_TRANSCRIPT_TOKENS", "")
        try:
            budget_tokens = int(raw) if raw else DEFAULT_TRANSCRIPT_TOKENS
        except ValueError:
            budget_tokens = DEFAULT_TRANSCRIPT_TOKENS
    rows = recent_messages(paths, room_id, limit=limit)
    if exclude_message_id:
        rows = [r for r in rows if str(r.get("message_id") or "") != str(exclude_message_id)]
    rows = [r for r in rows if r.get("id") not in exclude_row_ids]
    if not rows:
        return ""
    kept: list[str] = []
    used = 0
    omitted = 0
    for i, row in enumerate(reversed(rows)):
        line = _row_line(row)
        cost = _estimate_tokens(line)
        if kept and used + cost > budget_tokens:
            # Everything older is dropped as a block — no gaps in the middle.
            omitted = len(rows) - i
            break
        kept.append(line)
        used += cost
    kept.reverse()
    head = "[Group chat transcript]"
    if omitted:
        head += f"\n[{omitted} earlier message(s) omitted]"
    return head + "\n" + "\n".join(kept)
