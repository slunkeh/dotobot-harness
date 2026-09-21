"""Streaming responses over the file bus (typing indicator + token deltas).

A request (a user/bot message) gets a stream keyed by its message id. The bot
appends JSONL events to `shared/streams/<request_id>.jsonl`; a channel (desktop
app, CLI) tails that file and renders them live:

    {"type": "status", "value": "typing"}        # show a typing indicator
    {"type": "delta",  "text": "partial ..."}     # append to the bubble
    {"type": "message", "id": "msg-...", "text": "full text so far",
     "streaming": true, "mutation": "appended"}  # full-content upsert:
                                                 # rate-limited chunk boundaries
                                                 # re-send the whole accumulated
                                                 # text so a dropped frame
                                                 # self-heals; the closing frame
                                                 # is streaming=false and always
                                                 # carries the final full text
    {"type": "takeover", "reason": "...", "bot": "atlas"}   # bot is stuck
    {"type": "secret_request", "bot": "atlas", "name": "SMTP_PASSWORD",
     "title": "SMTP password", "reason": "..."}  # render a secure input box
    {"type": "choice", "bot": "atlas", "id": "...", "question": "...",
     "options": ["a", "b"]}                      # render a choice box
    {"type": "card", "bot": "atlas", "id": "...", "card_type": "github_pull",
     "payload": {...}}                           # render a rich card
    {"type": "card", "bot": "atlas", "id": "...", "card_type": "control_return",
     "payload": {...}}                           # accept card: hand control back
    {"type": "block", "bot": "atlas", "id": "blk_...", "surface": "chat",
     "title": "...", "view": {...view tree...}, "blocking": true}
                                                 # render a block (agent/blocks.py)
    {"type": "handoff", "bot": "pm", "frm": "chief-of-staff",
     "text": "create three tickets"}  # inbound bot-to-bot request:
                                     # posted in the asked bot's chat
    {"type": "final",  "text": "full reply", "frm": "atlas"}  # stream complete

The user answers a `choice` via `write_answer()` (the server's POST
/api/answers and WS `choice_response` land there); a `secret_request` is
answered by storing the secret (POST /api/secrets) — the value itself never
travels through the stream or the transcript.

This keeps the transport dumb and swappable (files now; SSE/websocket later)
while giving channels real-time updates instead of a single atomic reply.

Mutation model: anything a client renders by id — the streamed
message, cards, blocks — carries a `mutation` field with one shared meaning:
`snapshot` (server pushed full state, replace what you have), `appended`
(first sight of this id, insert), `updated` (replace the content for a known
id; an update for an UNKNOWN id must be ignored, never inserted), `cleared`.
Raw `delta` frames stay for old clients; new clients converge on the
full-content `message` upserts instead, so any dropped frame self-heals at
the next upsert boundary. Upserts carry the whole accumulated text, so they
are rate-limited (time/size) rather than emitted per chunk — per-chunk
upserts made a turn's stream O(n²) in reply length across the file, the
relay and every client. The first upsert (the `appended` insert) and the
closing `streaming=false` frame are never throttled.

Resolution state: when a blocking prompt settles, its OUTCOME is
persisted on the `prompts/` record as a `resolution` field
(`{state: answered|skipped, responded_value, skipped, approved,
secret_provided, ts}` — never a secret's value) and the same id is re-emitted
as a card with `mutation: "updated"` carrying that resolution, so live
clients settle the box in place and the reseed snapshot renders settled
prompts as settled. Resolved records expire after `RESOLVED_PROMPT_TTL`;
`list_prompts` hides them unless asked (`include_resolved=True` — the WS
reseed asks). Choice/confirm/control_return prompts whose turn ends
unanswered are swept: marked skipped, re-emitted, expired from `prompts/`,
and a one-line note is queued for the bot's next turn so it knows its
question was ignored. Secret requests are never swept — they keep the 24h
wait, then settle as skipped on the session log when that wait ends.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.fsutil import write_atomic
from harness.paths import HarnessPaths
from harness.redaction import scrub as scrub_secrets

#: Rate limit for the full-content `message` upserts: re-sending the whole
#: accumulated text every provider chunk is O(n²) per turn; a ~10 Hz resync
#: is indistinguishable in the UI (deltas still carry every chunk) while the
#: self-heal contract only needs periodic convergence points.
UPSERT_MIN_INTERVAL = 0.1
UPSERT_MIN_BYTES = 512

#: Deltas are flush-only plus a periodic fsync; the old every-8th-delta rule
#: synced hundreds of times per long reply. Settling events still fsync
#: unconditionally in `_should_fsync`.
DELTA_FSYNC_INTERVAL = 0.25


class StreamWriter:
    """Bot-side: append streaming events for one request."""

    def __init__(self, paths: HarnessPaths, request_id: str, *, room: str | None = None) -> None:
        self.request_id = request_id
        self.origin: str | None = None
        self.thread_id: str | None = None
        self.voice_call_id = None
        self.voice_input_id = None
        self._voice_text = ""
        self.paths = paths
        #: group id when this turn is a room. Stamped on every event at the
        #: source, so a relay that never saw the inbox message (the consult
        #: poller picking up a bot-to-bot room handoff) still routes the
        #: frames to the room instead of the bot's 1:1.
        self.room = str(room or "").strip() or None
        self.path = paths.stream_file(request_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._final = False
        self._fh: Any = None
        # Start the delta-fsync clock now: deltas are flush-only until the
        # stream has run for an interval (settling events sync regardless).
        self._last_delta_sync = time.monotonic()
        # Full-content upsert state: one streamed message per request.
        self._message_id = f"msg-{request_id}"
        self._accum = ""
        self._last_upsert = 0.0
        self._bytes_since_upsert = 0
        # ids (message, cards, blocks) already sent at least once — first
        # emission is `appended`, every later one `updated`.
        self._upserted: set[str] = set()

    def _should_fsync(self, event: dict[str, Any]) -> bool:
        """Durable sync for settling events; flush-only for token deltas."""
        kind = event.get("type")
        if kind in {
            "final",
            "steered",
            "choice",
            "secret_request",
            "takeover",
            "handoff",
            "skill_saved",
        }:
            return True
        if kind == "block" and event.get("blocking"):
            return True
        if kind == "card" and event.get("card_type") in {"confirm", "control_return"}:
            return True
        if kind == "delta":
            now = time.monotonic()
            if now - self._last_delta_sync >= DELTA_FSYNC_INTERVAL:
                self._last_delta_sync = now
                return True
        return False

    def set_thread(self, thread_id: str | None) -> None:
        self.thread_id = thread_id

    def set_origin(self, origin: str | None) -> None:
        """Set metadata through guarded proxies, which forward method calls."""
        self.origin = origin

    def set_voice_context(self, call_id: str | None, input_id: str | None) -> None:
        """A steer begins a new speech generation, without resetting the chat upsert."""
        self.voice_call_id = call_id
        self.voice_input_id = input_id
        self._voice_text = ""

    def _emit(self, event: dict[str, Any]) -> None:
        if self.thread_id:
            event.setdefault("thread_id", self.thread_id)
        if self.origin:
            event.setdefault("origin", self.origin)
        if self.voice_call_id:
            event.update(
                origin="voice", voice_call_id=self.voice_call_id, voice_input_id=self.voice_input_id
            )
            if event.get("type") == "message":
                from harness.redaction import registry

                event["voice_text"] = registry().speech(
                    self._voice_text, final=event.get("streaming") is False
                )
        event.setdefault("ts", time.time())
        if self.room:
            event.setdefault("room", self.room)
        # One handle for the life of the request (a per-event open/close was
        # measurable at token rate). Reopened if a settling emit arrives after
        # `final` closed it (card resolutions, skipped-prompt sweeps).
        if self._fh is None or self._fh.closed:
            self._fh = self.path.open("a", encoding="utf-8")
        fh = self._fh
        # Scrub the serialized line: the registry knows the
        # JSON-escaped form of each secret, so scrubbing after dumps catches
        # values the encoder escaped. Sentinels are JSON-safe, so the line
        # stays valid.
        fh.write(scrub_secrets(json.dumps(event, ensure_ascii=False)) + "\n")
        fh.flush()
        if self._should_fsync(event):
            os.fsync(fh.fileno())

    def close(self) -> None:
        fh = self._fh
        # Always drop the reference: on an OSError the old handle would
        # otherwise read as open and a later _emit would write into it.
        self._fh = None
        if fh is not None and not fh.closed:
            try:
                fh.close()
            except OSError:
                pass

    def status(self, value: str) -> None:
        self._emit({"type": "status", "value": value})

    def handoff(self, bot: str, frm: str, text: str) -> None:
        """Inbound bot-to-bot request posted in the asked bot's chat."""
        hid = f"handoff-{self._message_id}"
        self._emit(
            {
                "type": "handoff",
                "bot": bot,
                "frm": frm,
                "text": text,
                "id": hid,
                "mutation": self._mutation(hid),
                "streaming": False,
            }
        )

    def tool(
        self,
        name: str,
        state: str,
        label: str,
        *,
        title: str | None = None,
        detail: str | None = None,
    ) -> None:
        """One step in the live activity trail (Codex-style expandable Working)."""
        ev: dict[str, Any] = {"type": "tool", "name": name, "value": state, "text": label}
        if title:
            ev["title"] = title
        if detail:
            ev["detail"] = detail
        self._emit(ev)

    def _mutation(self, upsert_id: str) -> str:
        """Shared upsert bookkeeping: `appended` on first sight, then `updated`."""
        if upsert_id in self._upserted:
            return "updated"
        self._upserted.add(upsert_id)
        return "appended"

    def _emit_message(self, *, streaming: bool, text: str | None = None) -> None:
        """Full-content upsert of the streamed message.

        Carries the WHOLE accumulated text every time, so a client that lost
        any earlier frame converges at the next chunk boundary instead of
        rendering a hole forever.
        """
        if text is not None:
            self._accum = text
            if not self._voice_text:
                self._voice_text = text
        self._emit(
            {
                "type": "message",
                "id": self._message_id,
                "text": self._accum,
                "streaming": streaming,
                "mutation": self._mutation(self._message_id),
            }
        )

    def delta(self, text: str) -> None:
        if text:
            # The raw chunk stays for old clients (CLI, SSE tails); the
            # accompanying `message` frame is the self-healing form. The
            # upsert re-sends the WHOLE accumulated text, so it is
            # rate-limited: the first one must go out immediately (it is the
            # `appended` insert clients render the bubble from), later ones
            # only at time/size boundaries. `final()` always closes with the
            # authoritative full text, so convergence is unconditional.
            self._voice_text += text
            self._emit({"type": "delta", "text": text})
            self._accum += text
            self._bytes_since_upsert += len(text)
            now = time.monotonic()
            if (
                self._message_id not in self._upserted
                or now - self._last_upsert >= UPSERT_MIN_INTERVAL
                or self._bytes_since_upsert >= UPSERT_MIN_BYTES
            ):
                self._last_upsert = now
                self._bytes_since_upsert = 0
                self._emit_message(streaming=True)

    def takeover(self, bot: str, reason: str) -> None:
        self._emit({"type": "takeover", "bot": bot, "reason": reason, "id": uuid.uuid4().hex})

    def secret_request(
        self,
        bot: str,
        name: str,
        reason: str,
        title: str | None = None,
        *,
        prompt_id: str | None = None,
    ) -> None:
        """Ask the user for a secret by NAME; the value never enters the stream."""
        ev: dict[str, Any] = {
            "type": "secret_request",
            "id": prompt_id or uuid.uuid4().hex,
            "bot": bot,
            "name": name,
            "reason": reason,
        }
        if title:
            ev["title"] = title
        self._emit(ev)

    def choice(self, bot: str, choice_id: str, question: str, options: list[str]) -> None:
        """Offer the user a set of options; the pick comes back via write_answer."""
        self._emit(
            {
                "type": "choice",
                "bot": bot,
                "id": choice_id,
                "question": question,
                "options": list(options),
            }
        )

    def skill_saved(self, bot: str, name: str, path: str) -> None:
        """A private skill was written; clients refresh the `/` catalog."""
        self._emit(
            {
                "type": "skill_saved",
                "bot": bot,
                "name": name,
                "text": path,
            }
        )

    def card(self, bot: str, card_id: str, card_type: str, payload: dict[str, Any]) -> None:
        """Render a rich card (PR, ticket, table, progress...) in the chat.

        Re-emitting with the same `card_id` replaces the card in place —
        that is how a progress card updates. Settling cards fsync; keep
        payloads small.
        """
        self._emit(
            {
                "type": "card",
                "bot": bot,
                "id": card_id,
                "card_type": card_type,
                "payload": dict(payload),
                "mutation": self._mutation(card_id),
            }
        )

    def block(self, bot: str, block: dict, *, update: bool = False) -> None:
        """Render (or update) a block instance; submits come back via write_answer."""
        block_id = str(block.get("id") or "")
        mutation = "updated" if update else self._mutation(block_id)
        self._upserted.add(block_id)
        self._emit(
            {
                "type": "block",
                "bot": bot,
                "id": block.get("id"),
                "mutation": mutation,
                "block_type": block.get("block_type"),
                "surface": block.get("surface"),
                "title": block.get("title"),
                "view": block.get("view"),
                "state": block.get("state"),
                "status": block.get("status"),
                "blocking": bool(block.get("blocking")),
                "update": update,
            }
        )

    def card_resolution(
        self,
        bot: str,
        card_id: str,
        card_type: str,
        payload: dict[str, Any],
        resolution: dict[str, Any],
    ) -> None:
        """Re-emit a settled prompt/card with its outcome.

        Always `mutation: "updated"`: a client that rendered the id settles
        it in place; one that never saw it drops the frame (updates are
        never inserts), so a resolution can never conjure a phantom box.
        """
        self._upserted.add(card_id)
        self._emit(
            {
                "type": "card",
                "bot": bot,
                "id": card_id,
                "card_type": card_type,
                "payload": dict(payload),
                "resolution": dict(resolution),
                "mutation": "updated",
            }
        )

    def steered(self, bot: str, target_request_id: str) -> None:
        """Settle this accepted request by linking it to the turn it joined.

        This is an acknowledgment, never a final reply: relays stop tailing
        the consumed request without closing the original turn's prompts.
        """
        try:
            target_offset = self.paths.stream_file(target_request_id).stat().st_size
        except FileNotFoundError:
            target_offset = 0
        self._emit(
            {
                "type": "steered",
                "bot": bot,
                "target_request_id": target_request_id,
                "target_offset": target_offset,
            }
        )
        self.close()

    def final(self, text: str, frm: str, *, notification_priority: int = 0) -> None:
        # Close the streamed message with the authoritative full text BEFORE
        # `final`, so upsert clients settle on exactly what the transcript keeps.
        self._emit_message(streaming=False, text=text)
        self._emit(
            {
                "type": "final",
                "text": text,
                "frm": frm,
                "message_id": self._message_id,
                "notification_priority": notification_priority,
            }
        )
        self._final = True
        self.close()


@dataclass
class StreamEvent:
    type: str
    ts: float = 0.0
    value: str | None = None
    text: str | None = None
    frm: str | None = None
    bot: str | None = None
    reason: str | None = None
    room: str | None = None
    thread_id: str | None = None
    id: str | None = None
    message_id: str | None = None
    name: str | None = None
    question: str | None = None
    options: list[str] | None = None
    # card frames (see StreamWriter.card); `payload` is shared with blocks
    card_type: str | None = None
    payload: dict[str, Any] | None = None
    #: outcome of a settled prompt/card, on `updated` re-emits
    resolution: dict[str, Any] | None = None
    # block frames (see StreamWriter.block); the tree is `view`, not `payload`,
    # so clients can type the two shapes separately
    block_type: str | None = None
    surface: str | None = None
    title: str | None = None
    view: dict | None = None
    state: dict | None = None
    status: str | None = None
    blocking: bool | None = None
    update: bool | None = None
    #: run_command: the shell line (`title`) and captured output (`detail`)
    detail: str | None = None
    #: message upserts: True while tokens are still arriving; the
    #: closing frame is False and carries the final full text
    streaming: bool | None = None
    #: shared upsert semantics: snapshot | appended | updated | cleared
    mutation: str | None = None
    #: byte offset in the stream file just past this event — a client that
    #: reconnects resumes from here instead of replaying the whole turn.
    offset: int | None = None
    origin: str | None = None
    voice_call_id: str | None = None
    voice_input_id: str | None = None
    voice_text: str | None = None
    notification_priority: int = 0
    #: a consumed follow-up belongs to this still-independent stream
    target_request_id: str | None = None
    #: canonical cursor at injection, so CLI does not replay old questions
    target_offset: int | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StreamEvent:
        return cls(
            type=d.get("type", ""),
            origin=d.get("origin"),
            thread_id=d.get("thread_id"),
            voice_call_id=d.get("voice_call_id"),
            voice_input_id=d.get("voice_input_id"),
            voice_text=d.get("voice_text"),
            notification_priority=d.get("notification_priority", 0),
            ts=d.get("ts", 0.0),
            value=d.get("value"),
            text=d.get("text"),
            frm=d.get("frm"),
            bot=d.get("bot"),
            reason=d.get("reason"),
            room=d.get("room"),
            id=d.get("id"),
            message_id=d.get("message_id"),
            name=d.get("name"),
            question=d.get("question"),
            options=d.get("options"),
            card_type=d.get("card_type"),
            payload=d.get("payload"),
            resolution=d.get("resolution"),
            block_type=d.get("block_type"),
            surface=d.get("surface"),
            title=d.get("title"),
            view=d.get("view"),
            state=d.get("state"),
            status=d.get("status"),
            blocking=d.get("blocking"),
            update=d.get("update"),
            detail=d.get("detail"),
            streaming=d.get("streaming"),
            mutation=d.get("mutation"),
            target_request_id=d.get("target_request_id"),
            target_offset=d.get("target_offset"),
        )


def read_steered(paths: HarnessPaths, request_id: str) -> StreamEvent | None:
    """Read a consumed request's durable acknowledgment, regardless of cursor."""
    try:
        with paths.stream_file(request_id).open("rb") as fh:
            raw = fh.readline()
        if not raw.endswith(b"\n"):
            return None
        ev = StreamEvent.from_dict(json.loads(raw))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    target = ev.target_request_id
    if ev.type != "steered" or not isinstance(target, str) or not target:
        return None
    if target == request_id or re.fullmatch(r"[A-Za-z0-9_-]+", target) is None:
        return None
    ev.offset = len(raw)
    return ev


def clear_steered(paths: HarnessPaths, request_id: str) -> None:
    """A recovered input admitted independently no longer belongs to its old turn."""
    if read_steered(paths, request_id) is not None:
        paths.stream_file(request_id).unlink(missing_ok=True)


class StreamReader:
    """Channel-side: tail a request's stream until `final` or timeout."""

    def __init__(self, paths: HarnessPaths, request_id: str, *, offset: int = 0) -> None:
        self.paths = paths
        self.path: Path = paths.stream_file(request_id)
        self._offset = max(0, offset)
        self._fh = None

    def _read_new(self) -> list[StreamEvent]:
        # One binary handle held across polls (a token-rate tail used to pay
        # an open/close plus a re-encode of every line per 50 ms tick).
        # Binary keeps `_offset` in exact bytes — it is the resume contract.
        fh = self._fh
        if fh is None:
            try:
                fh = self.path.open("rb")
            except FileNotFoundError:
                return []
            self._fh = fh
        events: list[StreamEvent] = []
        fh.seek(self._offset)
        while True:
            raw = fh.readline()
            if not raw or not raw.endswith(b"\n"):
                break  # partial write; wait for the rest
            self._offset += len(raw)
            try:
                line = raw.strip().decode("utf-8")
            except UnicodeDecodeError:
                continue
            if line:
                try:
                    ev = StreamEvent.from_dict(json.loads(line))
                except json.JSONDecodeError:
                    continue
                ev.offset = self._offset
                events.append(ev)
        return events

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def events(self, *, timeout: float = 45.0, poll: float = 0.05):
        """Yield through the actual reply, following a steered request's target."""
        deadline = time.time() + timeout
        followed = {self.path.stem}
        try:
            while time.time() < deadline:
                for ev in self._read_new():
                    yield ev
                    if ev.type == "final":
                        return
                    if ev.type == "steered":
                        target = ev.target_request_id or ""
                        if target in followed or re.fullmatch(r"[A-Za-z0-9_-]+", target) is None:
                            return
                        followed.add(target)
                        self.close()
                        self.path = self.paths.stream_file(target)
                        self._offset = max(0, int(ev.target_offset or 0))
                        break
                time.sleep(poll)
        finally:
            self.close()

    def collect(self, *, timeout: float = 45.0) -> str:
        """Consume the stream and return the final text (CLI convenience)."""
        final = ""
        for ev in self.events(timeout=timeout):
            if ev.type == "final":
                final = ev.text or ""
        return final


_ANSWER_ID = re.compile(r"[^A-Za-z0-9_-]+")


def _answer_file(paths: HarnessPaths, answer_id: str) -> Path | None:
    safe = _ANSWER_ID.sub("", answer_id or "")
    if not safe:
        return None
    return paths.answers / f"{safe}.json"


def write_answer(paths: HarnessPaths, answer_id: str, value: str) -> bool:
    """User-side: record the answer to an in-chat prompt (choice box)."""
    if get_prompt(paths, answer_id) is not None:
        status, _row = answer_prompt(paths, answer_id, value)
        return status in {"applied", "replayed"}
    path = _answer_file(paths, answer_id)
    if path is None:
        return False
    write_atomic(path, scrub_secrets(json.dumps({"value": value, "ts": time.time()})))
    return True


def read_answer(paths: HarnessPaths, answer_id: str, *, consume: bool = True) -> str | None:
    """Consume the answer mailbox once, retaining the durable decision for replay."""
    row = get_prompt(paths, answer_id)
    resolution = (row or {}).get("resolution") or {}
    if resolution.get("state") == "answered":
        from harness.statestore import store_for

        return store_for(paths).read_prompt_answer(answer_id, consume=consume)
    path = _answer_file(paths, answer_id)
    if path is None or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("value")
    except (json.JSONDecodeError, OSError):
        return None
    if consume:
        try:
            path.unlink()
        except OSError:
            pass
    return None if value is None else str(value)


def _prompt_file(paths: HarnessPaths, prompt_id: str) -> Path | None:
    safe = _ANSWER_ID.sub("", prompt_id or "")
    if not safe:
        return None
    return paths.prompts / f"{safe}.json"


def write_prompt(paths: HarnessPaths, payload: dict[str, Any]) -> str | None:
    """Persist an open secret/choice box so any client can render it later."""
    pid = str(payload.get("id") or "")
    if not pid:
        pid = f"{int(time.time())}{os.urandom(3).hex()}"
        payload = {**payload, "id": pid}
    path = _prompt_file(paths, pid)
    if path is None:
        return None
    payload.setdefault("ts", time.time())
    from harness.statestore import store_for

    row, _created = store_for(paths).open_prompt(payload)
    _mirror_prompt(paths, row)
    return pid


def _mirror_prompt(paths: HarnessPaths, row: dict) -> None:
    """Compatibility projection; SQLite owns the decision even if this fails."""
    path = _prompt_file(paths, str(row.get("id") or ""))
    if path is not None:
        try:
            write_atomic(path, scrub_secrets(json.dumps(row, ensure_ascii=False)))
        except OSError:
            pass


def get_prompt(paths: HarnessPaths, prompt_id: str) -> dict | None:
    """Read authoritative state, importing an old file once when necessary."""
    from harness.statestore import store_for

    store = store_for(paths)
    row = store.prompt(prompt_id)
    if row is not None:
        return row
    path = _prompt_file(paths, prompt_id)
    if path is not None and path.is_file():
        try:
            legacy = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(legacy, dict):
            return store.open_prompt(legacy)[0]
    return None


def answer_prompt(
    paths: HarnessPaths, prompt_id: str, value: str, *, bot: str = ""
) -> tuple[str, dict | None]:
    """Atomically answer once; return applied/replayed/conflict/stale/missing."""
    from harness.statestore import store_for

    live = get_prompt(paths, prompt_id)
    if live is None:
        return "missing", None
    resolution = {"state": "answered", "responded_value": value, "skipped": False}
    if live.get("card_type") == "confirm":
        resolution["approved"] = value in {"confirm", "allow_all", "confirm_all"}
    status, row = store_for(paths).settle_prompt(prompt_id, resolution, bot=bot)
    if row is not None:
        _mirror_prompt(paths, row)
    return status, row


def decision_context(
    paths: HarnessPaths, bot: str, task_id: str, task_revision: int, *, max_chars: int = 5000
) -> str:
    """Bounded decision projection; exact records remain outside lossy context."""
    if not task_id:
        return ""
    from harness.statestore import store_for

    records = [
        row
        for row in store_for(paths).prompts(bot, task_id=task_id, task_revision=task_revision)
        if row.get("type") != "secret_request"
    ]
    if not records:
        return ""
    records.sort(key=lambda row: (bool(row.get("resolution")), -float(row.get("ts") or 0)))
    limit = max(256, max_chars)
    header = "[Current task decisions recorded by the harness]\n"
    result = {"decisions": [], "omitted": len(records)}
    for row in records:
        full = {
            k: row[k]
            for k in (
                "id",
                "type",
                "card_type",
                "question",
                "options",
                "payload",
                "subject",
                "resolution",
            )
            if k in row
        }
        resolution = row.get("resolution") or {}
        compact = {
            "id": row["id"],
            "state": resolution.get("state", "open"),
            "question": str(
                row.get("question") or (row.get("payload") or {}).get("question") or ""
            )[:160],
            "answer": str(resolution.get("responded_value") or "")[:160],
            "details_omitted": True,
        }
        for item in (full, compact):
            candidate = {
                "decisions": result["decisions"] + [item],
                "omitted": result["omitted"] - 1,
            }
            if len(header) + len(json.dumps(candidate, ensure_ascii=False)) <= limit:
                result = candidate
                break
        else:
            break
    return header + json.dumps(result, ensure_ascii=False)


#: How long a settled prompt record stays in `prompts/` so the reseed can
#: still render it as settled. Conservative: long enough for a
#: reconnect after a settle, short enough that resolved boxes don't pile up.
RESOLVED_PROMPT_TTL = 3600.0


def resolve_prompt(
    paths: HarnessPaths, prompt_id: str, resolution: dict[str, Any]
) -> dict[str, Any] | None:
    """Persist a settled prompt's outcome on its record.

    The record stays on disk (pruned after RESOLVED_PROMPT_TTL) so the WS
    reseed renders the prompt as settled instead of resurrecting an open
    box. Never write a secret's value here — only `secret_provided: bool`.
    Returns the updated row, or None when the prompt is already gone.
    """
    if get_prompt(paths, prompt_id) is None:
        return None
    from harness.statestore import store_for

    _status, row = store_for(paths).settle_prompt(prompt_id, resolution)
    if row is not None:
        _mirror_prompt(paths, row)
    return row


def list_prompts(
    paths: HarnessPaths, bot: str | None = None, *, include_resolved: bool = False
) -> list[dict[str, Any]]:
    """Open prompt boxes; resolved records only with `include_resolved`.

    Default callers (the tools, GET /api/prompts, old clients) keep seeing
    only OPEN boxes. The reseed passes `include_resolved=True` so settled
    prompts render as settled. Resolved records past RESOLVED_PROMPT_TTL are
    pruned here as a side effect.
    """
    from harness.statestore import store_for

    store = store_for(paths)
    if paths.prompts.is_dir():
        for path in paths.prompts.glob("*.json"):
            get_prompt(paths, path.stem)  # import legacy rows before the snapshot
    sync_stale_prompts(paths, bot)
    out: list[dict[str, Any]] = []
    now = time.time()
    for row in store.prompts(bot):
        if row.get("cleared"):
            continue
        if not row.get("resolution") and not store.prompt_current(row):
            continue
        resolution = row.get("resolution")
        if isinstance(resolution, dict):
            settled_ts = float(resolution.get("ts") or row.get("ts") or 0.0)
            if now - settled_ts > RESOLVED_PROMPT_TTL:
                path = _prompt_file(paths, str(row.get("id") or ""))
                if path is not None:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                continue
            if not include_resolved:
                continue
        out.append(row)
    return out


def sync_stale_prompts(
    paths: HarnessPaths, bot: str | None = None, *, writer: StreamWriter | None = None
) -> list[dict]:
    """Project superseded decisions into card history and the available stream."""
    from harness.statestore import store_for

    from .memory import Memory

    changed = store_for(paths).supersede_prompts(bot)
    for row in changed:
        _mirror_prompt(paths, row)
        kind = "choice" if row.get("type") == "choice" else str(row.get("card_type") or "")
        payload = row.get("payload") or {
            "question": row.get("question", ""),
            "options": row.get("options", []),
        }
        if row.get("room"):
            from harness.rooms import RoomError, append_card

            try:
                append_card(
                    paths,
                    row["room"],
                    frm=row["bot"],
                    card_id=row["id"],
                    card_type=kind,
                    payload=payload,
                    resolution=row["resolution"],
                )
            except RoomError:
                # A deleted room has no history to repair. A storage error
                # must keep projection_pending so the next read retries.
                pass
        else:
            memory = Memory(paths, row["bot"])
            memory.log_card(
                memory.latest_session_id(),
                card_id=row["id"],
                card_type=kind,
                payload=payload,
                frm=row["bot"],
                resolution=row["resolution"],
            )
        if writer is not None:
            writer.card_resolution(row["bot"], row["id"], kind, payload, row["resolution"])
        store_for(paths).prompt_projected(row["id"])
    return changed


def clear_prompt(paths: HarnessPaths, prompt_id: str) -> None:
    from harness.statestore import store_for

    if get_prompt(paths, prompt_id) is not None:
        store_for(paths).hide_prompt(prompt_id)
    path = _prompt_file(paths, prompt_id)
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


def clear_secret_prompts(paths: HarnessPaths, name: str) -> None:
    for row in list_prompts(paths):
        if row.get("type") == "secret_request" and str(row.get("name") or "") == name:
            clear_prompt(paths, str(row.get("id") or ""))


def resolve_secret_prompts(paths: HarnessPaths, name: str) -> None:
    """The secret arrived: settle its open boxes — value never stored."""
    for row in list_prompts(paths):
        if row.get("type") == "secret_request" and str(row.get("name") or "") == name:
            resolve_prompt(
                paths,
                str(row.get("id") or ""),
                {"state": "answered", "secret_provided": True, "skipped": False},
            )


# -- turn notes ---------------------------------------------------
# One-line messages queued for a bot's NEXT turn (e.g. "your question was not
# answered"), so a bot whose prompt was skipped knows it was ignored.


def _notes_file(paths: HarnessPaths, bot: str) -> Path | None:
    safe = _ANSWER_ID.sub("", bot or "")
    if not safe:
        return None
    return paths.home / "notes" / f"{safe}.jsonl"


def push_turn_note(paths: HarnessPaths, bot: str, text: str) -> None:
    """Queue a one-liner for the bot's next turn (appended to its system prompt)."""
    path = _notes_file(paths, bot)
    text = (text or "").strip()
    if path is None or not text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"text": text, "ts": time.time()}, ensure_ascii=False) + "\n")


def pop_turn_notes(paths: HarnessPaths, bot: str) -> list[str]:
    """Drain the queued notes (read once, then gone)."""
    path = _notes_file(paths, bot)
    if path is None or not path.is_file():
        return []
    out: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        path.unlink()
    except OSError:
        return []
    for line in lines:
        try:
            text = str(json.loads(line).get("text") or "").strip()
        except json.JSONDecodeError:
            continue
        if text:
            out.append(text)
    return out


# -- skipped-prompt sweep -----------------------------------------

_SWEEPABLE_CARDS = frozenset({"confirm", "control_return"})


def _sweepable(row: dict[str, Any]) -> bool:
    """Only unanswered choice/confirm/return boxes are swept — never secret
    requests (those keep the 24h wait) and never already-resolved records."""
    if row.get("resolution"):
        return False
    if row.get("type") == "choice":
        return True
    return row.get("type") == "card" and str(row.get("card_type") or "") in _SWEEPABLE_CARDS


def sweep_skipped_prompts(
    paths: HarnessPaths,
    bot: str,
    *,
    writer: StreamWriter | None = None,
    prompt_id: str | None = None,
) -> list[dict[str, Any]]:
    """Mark unanswered choice/confirm/return prompts as skipped.

    For each open, unresolved choice/confirm/control_return box owned by
    `bot` (or just `prompt_id` when given): emit the same-id card as
    `mutation: "updated"` with `resolution.skipped`, expire the record from
    `prompts/` so the box cannot reappear on reseed, and queue a one-line
    note for the bot's next turn so it knows the question went unanswered.
    Returns the swept rows. Secret requests are never swept.
    """
    swept: list[dict[str, Any]] = []
    sync_stale_prompts(paths, bot, writer=writer)
    for row in list_prompts(paths, bot):
        pid = str(row.get("id") or "")
        if not pid or (prompt_id is not None and pid != prompt_id):
            continue
        if not _sweepable(row):
            continue
        if row.get("task_id") and prompt_id is None:
            continue  # recoverable task questions outlive a worker attempt
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else None
        question = str(row.get("question") or (payload or {}).get("question") or "").strip()
        if writer is not None:
            card_type = "choice" if row.get("type") == "choice" else str(row.get("card_type") or "")
            writer.card_resolution(
                bot,
                pid,
                card_type,
                payload or {"question": question, "options": row.get("options") or []},
                {"state": "skipped", "skipped": True, "ts": time.time()},
            )
        resolve_prompt(paths, pid, {"state": "skipped", "skipped": True})
        clear_prompt(paths, pid)
        push_turn_note(
            paths,
            bot,
            f"your question {question!r} was not answered; do not assume an answer"
            if question
            else "your last question/confirmation was not answered; do not assume an answer",
        )
        swept.append(row)
    return swept


# Card types that park the bot on a human answer the way `choice` does.
_BLOCKING_CARDS = frozenset({"confirm", "control_return"})
_PROMPT_SETTLE = frozenset({"choice", "secret_request"})


def is_blocking(ev: StreamEvent) -> bool:
    """True for a card or block event that parks its bot on a human answer.

    A card carrying a `resolution` is already settled — it never
    parks, even when its card_type is normally blocking.
    """
    if ev.type == "card":
        return (ev.card_type or "") in _BLOCKING_CARDS and not ev.resolution
    return ev.type == "block" and bool(ev.blocking)


def settles(ev: StreamEvent, *, park_prompts: bool = True) -> bool:
    """True when this event ends the wait for its bot's stream.

    HTTP/SSE parks on a choice so the request can close. The WebSocket stays
    on the stream (`park_prompts=False`) until `final` so the app sees the
    rest of the turn.
    """
    if ev.type in ("final", "steered"):
        return True
    if not park_prompts:
        return False
    return ev.type in _PROMPT_SETTLE or is_blocking(ev)


def multiplex(
    readers: list[tuple[str, StreamReader]],
    *,
    timeout: float = 120.0,
    poll: float = 0.05,
    keep_waiting=None,
    park_prompts: bool = True,
):
    """Yield `(bot, StreamEvent)` from several streams until every one finals.

    `choice` / `secret_request` / blocking cards settle HTTP/SSE (`park_prompts`).
    The WebSocket passes `park_prompts=False` and follows until `final`.
    `keep_waiting(names)` extends the idle deadline (queued behind a busy turn).
    """
    pending = {name: reader for name, reader in readers}
    # Idle timeout: wait this long *without events*, not a wall clock on the
    # whole turn. Computer-use loops can run many minutes if they keep emitting.
    deadline = time.time() + timeout
    try:
        while pending:
            progressed = False
            for name, reader in list(pending.items()):
                for ev in reader._read_new():
                    progressed = True
                    yield name, ev
                    if settles(ev, park_prompts=park_prompts):
                        pending.pop(name, None)
                        reader.close()
                        break
            if progressed:
                deadline = time.time() + timeout
                continue
            if time.time() >= deadline:
                if keep_waiting is not None and keep_waiting(tuple(pending)):
                    deadline = time.time() + timeout
                    continue
                break
            time.sleep(poll)
    finally:
        for reader in pending.values():
            reader.close()
