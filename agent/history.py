"""Multi-turn conversation history.

Rebuilds a bot's 1:1 thread with one peer from its private session logs so the
model sees prior turns instead of only the memory-recall block. Rooms are
excluded — the shared room transcript block already carries that history.

Token budgeting is a chars/4 estimate (stdlib only, no tokenizer). Over-budget
windows are truncated max-min fairly: every turn gets an equal char
share, small turns return their surplus to the pool, an over-share turn is cut
with a `[... truncated]` marker, and one whose share is below a useful minimum
becomes an `[omitted message]` marker — so a giant tool dump gets cut while the
oldest user instruction survives in truncated form. Whenever anything was
trimmed, a transcript-pointer note tells the bot where the session JSONL logs
live so it can grep dropped content back via `run_command`.

Compaction (`agent/compaction.py`) partitions at the history seam:
records covered by the live `is_summary` chain (see `summary_chain`) are
replaced by one user-role turn at the head of the thread carrying every live
summary — recent compactions verbatim, older ranges folded into coarser
epoch records — and everything from the split point onward stays verbatim.
Summary records never render as chat bubbles (`user_thread`) and never count
as real user messages.
"""

from __future__ import annotations

import hashlib
import json
import os

from providers.base import Message, Provider, ToolSpec

from . import messaging
from .memory import Memory

_LEGACY_IN_PREFIX = "in:"
_DEFAULT_HISTORY_TOKENS = 8_000
_RESPONSE_MARGIN = 1_024
_DEFAULT_MAX_TOKENS = 1_024
TOOL_RESULT_CHAR_LIMIT = 12_000
_IMAGE_BYTES_PER_TOKEN = 750
_OMITTED = "(earlier tool result omitted to stay in context)"
_HISTORY_OMITTED = (
    "(earlier conversation omitted to make room for current tool results; "
    "use recall for older details)"
)

#: Screenshot frames kept in the in-flight tool loop. Every provider call
#: re-sends the whole loop, so a 20-screenshot turn was re-uploading all 20
#: frames on every step; only the newest `DEFAULT_LOOP_IMAGES` stay. The
#: dropped frame's carrier message keeps its note, so the turn still reads
#: as a sequence, and the bot is told to look again if it needs the screen.
#: $HARNESS_LOOP_IMAGES overrides (minimum 1).
DEFAULT_LOOP_IMAGES = 3
_LOOP_IMAGES_ENV = "HARNESS_LOOP_IMAGES"
DROPPED_FRAME_NOTE = (
    "(earlier screenshot frame dropped to stay in context — call "
    "computer_screenshot if you need to see the screen again)"
)
_MIN_USEFUL_CHARS = 200  # a smaller share carries no meaning; omit instead


def message_id_of(record: dict) -> str:
    """Stable bubble id: stored `message_id`, or a hash for pre-thread rows."""
    mid = str(record.get("message_id") or "").strip()
    if mid:
        return mid
    ts = record.get("ts", 0.0)
    text = str(record.get("text") or "")
    frm = str(record.get("frm") or record.get("role") or "")
    raw = f"{ts}|{frm}|{text[:80]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _thread_id_of(record: dict) -> str | None:
    tid = str(record.get("thread_id") or "").strip()
    return tid or None


def thread_root_id(memory: Memory, thread_id: str) -> str:
    """No nested threads: a reply's message_id snaps to its parent root."""
    want = str(thread_id or "").strip()
    seen: set[str] = set()
    while want and want not in seen:
        seen.add(want)
        parent = None
        for record in memory._session_records():
            if message_id_of(record) == want:
                parent = _thread_id_of(record)
                break
        if not parent:
            return want
        want = parent
    return want


def reply_counts(memory: Memory) -> dict[str, int]:
    """How many side-thread records hang off each root message_id."""
    counts: dict[str, int] = {}
    for record in memory._session_records():
        tid = _thread_id_of(record)
        if tid:
            counts[tid] = counts.get(tid, 0) + 1
    return counts


_THREAD_STARTED = "[Thread started on this message]:"
_SECONDARY_HEAD = (
    "[Secondary context — the rest of the 1:1 chat, outside this thread. "
    "Background only: use it when the thread needs something from the wider "
    "conversation. Reply in the thread; do not continue the main chat.]"
)
#: Cap so the main 1:1 cannot drown the primary thread in the system prompt.
_SECONDARY_HISTORY_TOKENS = 2_000


def _thread_rows(
    memory: Memory,
    thread_id: str,
    *,
    peer: str,
    exclude_message_id: str | None,
    limit: int,
) -> list[dict]:
    skip = str(exclude_message_id or "").strip()
    rows = []
    for row in user_thread(memory, peer=peer, thread_id=thread_id, limit=limit):
        if skip and str(row.get("message_id") or "") == skip:
            continue
        if not str(row.get("text") or "").strip():
            continue
        rows.append(row)
    return rows


def thread_history_messages(
    memory: Memory,
    thread_id: str,
    *,
    peer: str = "user",
    exclude_message_id: str | None = None,
    limit: int = 80,
) -> list[Message]:
    """Primary conversation for a side-thread turn (parent + prior replies).

    The live user line is excluded so it is sent once as the current turn.
    A bot-authored parent is rewritten as a user-role opener so the
    provider thread does not start on assistant.
    """
    out: list[Message] = []
    for row in _thread_rows(
        memory,
        thread_id,
        peer=peer,
        exclude_message_id=exclude_message_id,
        limit=limit,
    ):
        body = str(row.get("text") or "").strip()
        frm = str(row.get("frm") or "")
        role = "user" if frm == "user" else "assistant"
        if out and out[-1].role == role:
            prev = out[-1]
            out[-1] = Message(
                role=role,
                content=f"{prev.content}\n\n{body}",
                name=prev.name,
            )
        else:
            out.append(Message(role=role, content=body, name=frm or None))
    if out and out[0].role == "assistant":
        first = out[0]
        out[0] = Message(
            role="user",
            content=f"{_THREAD_STARTED}\n{first.content}",
            name=first.name,
        )
        if len(out) > 1 and out[1].role == "user":
            nxt = out[1]
            out[0] = Message(
                role="user",
                content=f"{out[0].content}\n\n{nxt.content}",
                name=out[0].name,
            )
            out.pop(1)
    return out


def thread_context_block(
    memory: Memory,
    thread_id: str,
    *,
    peer: str = "user",
    exclude_message_id: str | None = None,
) -> str:
    """Parent + replies as a labeled text block (rooms-style).

    `exclude_message_id` drops the in-flight user line so a recovered turn
    does not replay it both here and as the live user message.
    """
    rows = _thread_rows(
        memory,
        thread_id,
        peer=peer,
        exclude_message_id=exclude_message_id,
        limit=200,
    )
    if not rows:
        return ""
    lines = [
        "[This is a side thread on an earlier message. Stay in this thread. "
        "The parent is the first line; replies follow.]"
    ]
    for row in rows:
        who = str(row.get("frm") or "user")
        body = str(row.get("text") or "").strip()
        if body:
            lines.append(f"{who}: {body}")
    return "\n".join(lines)


def secondary_main_block(
    memory: Memory,
    *,
    peer: str = "user",
    omit_message_id: str | None = None,
    token_cap: int = _SECONDARY_HISTORY_TOKENS,
) -> str:
    """The main 1:1 as secondary background — not the live message list.

    Side-thread replies are already omitted by `user_thread`. The thread
    parent (`omit_message_id`) is skipped because it is already primary.
    Newest main-line turns are kept when the cap is hit.
    """
    skip = str(omit_message_id or "").strip()
    bodies: list[str] = []
    for row in user_thread(memory, peer=peer, limit=200):
        if skip and str(row.get("message_id") or "") == skip:
            continue
        body = str(row.get("text") or "").strip()
        if not body:
            continue
        who = str(row.get("frm") or "user")
        bodies.append(f"{who}: {body}")
    if not bodies:
        return ""
    kept: list[str] = []
    used = estimate_tokens(_SECONDARY_HEAD)
    cap = max(200, int(token_cap))
    for line in reversed(bodies):
        cost = estimate_tokens(line)
        if used + cost > cap and kept:
            break
        kept.append(line)
        used += cost
    kept.reverse()
    return "\n".join([_SECONDARY_HEAD, *kept])


def _attachment_history_note(attachments) -> str:
    """One line so a past chat file stays on its own history turn.

    Name plus path: Linear attach needs the path, and the model must not
    treat the file as attached to a later question.
    """
    if not isinstance(attachments, list) or not attachments:
        return ""
    parts: list[str] = []
    for a in attachments:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "file").strip() or "file"
        path = str(a.get("path") or "").strip()
        parts.append(f"{name} path={path}" if path else name)
    if not parts:
        return ""
    return "[attached: " + "; ".join(parts) + "]"


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token for English text and code)."""
    return max(1, len(text) // 4)


def prompt_tokens_from_usage(usage: dict | None) -> int | None:
    """Read input/prompt tokens from a provider `Completion.usage` payload."""
    if not usage:
        return None
    for key in ("input_tokens", "prompt_tokens"):
        val = usage.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
    return None


def history_budget(
    provider: Provider,
    *,
    system: str = "",
    tools: list[ToolSpec] | None = None,
    current: str = "",
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    usage: dict | None = None,
    last_history_tokens: int = 0,
) -> int:
    """Tokens available for history after the rest of the request is counted.

    Capped by $HARNESS_HISTORY_TOKENS (default 8000) so long-lived bots don't
    grow every request to the model's full window. When the last completion
    reported `input_tokens` / `prompt_tokens`, that measurement (minus the
    history that was in it) replaces the chars/4 estimate of the fixed prefix.
    """
    try:
        cap = int(os.environ.get("HARNESS_HISTORY_TOKENS", _DEFAULT_HISTORY_TOKENS))
    except ValueError:
        cap = _DEFAULT_HISTORY_TOKENS
    window = getattr(provider, "context_window", 32_000)
    actual = prompt_tokens_from_usage(usage)
    specs = json.dumps(
        [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in (tools or [])
        ]
    )
    estimated = (
        estimate_tokens(system or "") + estimate_tokens(specs) + estimate_tokens(current or "")
    )
    # Previous usage calibrates estimates; it cannot hide newly selected schemas.
    fixed = max(estimated, max(0, actual - max(0, last_history_tokens)) if actual else 0)
    available = window - fixed - max_tokens - _RESPONSE_MARGIN
    return max(0, min(cap, available))


def estimate_message_tokens(message: Message) -> int:
    """Chars/4 for text, plus a cheap vision estimate for attached frames."""
    n = estimate_tokens(message.content or "")
    for _mime, data in message.images or []:
        n += max(1, len(data) // _IMAGE_BYTES_PER_TOKEN)
    if message.tool_calls:
        n += estimate_tokens(
            json.dumps(
                [{"name": c.name, "arguments": c.arguments} for c in message.tool_calls],
                ensure_ascii=False,
            )
        )
    return n


def cap_tool_result(text: str, limit: int = TOOL_RESULT_CHAR_LIMIT) -> str:
    """Hard-cap one tool result the way `run_command` already does."""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(truncated)"


def loop_message_budget(
    provider: Provider,
    *,
    system: str = "",
    tools: list[ToolSpec] | None = None,
) -> int:
    """Tokens the in-flight tool-loop `messages` list may occupy."""
    window = getattr(provider, "context_window", 32_000)
    specs = json.dumps(
        [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in (tools or [])
        ]
    )
    reserved = (
        estimate_tokens(system or "")
        + estimate_tokens(specs)
        + _DEFAULT_MAX_TOKENS
        + _RESPONSE_MARGIN
    )
    return max(0, window - reserved)


def trim_loop_messages(
    messages: list[Message], budget: int, *, replayed_history: list[Message] | None = None
) -> list[Message]:
    """Shrink replayed history before discarding the current turn's evidence.

    A large non-tool history can already exceed the budget. Erasing every
    fresh observation cannot fix that and leaves the agent retrying blind.
    Individual results are still capped, including the protected batch.
    """
    for msg in messages:
        if msg.role == "tool" and len(msg.content) > TOOL_RESULT_CHAR_LIMIT:
            msg.content = cap_tool_result(msg.content)
    total = sum(estimate_message_tokens(m) for m in messages)
    if total <= budget:
        return messages
    # History and tool-loop budgets use different estimates. A large tool
    # catalogue can leave a loop allowance smaller than the replayed chat.
    # Dropping only tool results then erases a report as soon as post_image
    # returns. Trim explicitly identified replay text first; current requests
    # and steered instructions must never be mistaken for old history.
    history_ids = {id(m) for m in replayed_history or []}
    replacement_tokens = estimate_tokens(_HISTORY_OMITTED)
    for msg in messages:
        if total <= budget:
            break
        if id(msg) not in history_ids:
            continue
        # Rebuilt history is text-only today. Leave any protocol-bearing
        # message intact rather than break tool pairing or signed replay.
        if msg.tool_calls or msg.tool_call_id or msg.reasoning or msg.responses_output:
            continue
        old = estimate_message_tokens(msg)
        if old <= replacement_tokens:
            continue
        msg.content = _HISTORY_OMITTED
        msg.images = None
        total += replacement_tokens - old
    latest_batch = next(
        (i for i in range(len(messages) - 1, -1, -1) if messages[i].tool_calls),
        next(
            (i for i in range(len(messages) - 1, -1, -1) if messages[i].role == "tool"),
            len(messages),
        ),
    )
    for i, msg in enumerate(messages):
        if total <= budget:
            break
        if i >= latest_batch:
            break
        if msg.role != "tool" or msg.content == _OMITTED:
            continue
        # Human decisions are current-turn instructions, not disposable
        # observations. Their paired calls retain the question/action and
        # exact payload; keep acceptance, rejection and choices with them.
        if msg.name in {"confirm", "ask_user_choice"}:
            continue
        old = estimate_message_tokens(msg)
        msg.content = _OMITTED
        total += estimate_message_tokens(msg) - old
    # Preserve a bounded part of fresh observations when the last batch alone
    # is too large. Never shorten human input, decisions, or call arguments.
    for msg in messages[latest_batch:]:
        if total <= budget:
            break
        if (
            msg.role != "tool"
            or msg.name in {"confirm", "ask_user_choice"}
            or len(msg.content) <= 1024
        ):
            continue
        old = estimate_message_tokens(msg)
        keep = max(512, len(msg.content) - (total - budget + 30) * 4)
        msg.content = msg.content[:keep] + "\n(observation truncated to fit current context)"
        total += estimate_message_tokens(msg) - old
    return messages


def loop_image_limit() -> int:
    """How many screenshot frames the tool loop keeps (see DEFAULT_LOOP_IMAGES)."""
    try:
        value = int(os.environ.get(_LOOP_IMAGES_ENV, DEFAULT_LOOP_IMAGES))
    except ValueError:
        return DEFAULT_LOOP_IMAGES
    return max(1, value)


def prune_loop_images(messages: list[Message], keep: int, *, frame_note: str) -> int:
    """Drop screenshot frames older than the newest `keep` from the loop.

    Only frame carriers are touched — user-role messages whose text is
    exactly `frame_note` (the runtime's screenshot preamble). The user's
    own image attachments ride on their message text and stay. Returns the
    number of messages that lost their frames.
    """
    carriers = [m for m in messages if m.role == "user" and m.images and m.content == frame_note]
    stale = carriers[: max(0, len(carriers) - max(0, keep))]
    for m in stale:
        m.images = None
        m.content = f"{frame_note}\n{DROPPED_FRAME_NOTE}"
    return len(stale)


def fair_char_allocations(sizes: list[int], budget: int) -> list[int]:
    """Max-min fair split of a char `budget` across messages of `sizes` chars.

    Walks messages smallest-first: each takes at most an equal share of what
    is left (`remaining // remaining_count`), so a small message keeps only
    what it needs and returns its surplus to the pool, and the giant ones
    split whatever remains.
    """
    shares = [0] * len(sizes)
    remaining = max(0, budget)
    count = len(sizes)
    for i in sorted(range(len(sizes)), key=sizes.__getitem__):
        shares[i] = min(sizes[i], remaining // count)
        remaining -= shares[i]
        count -= 1
    return shares


def _fair_truncate_turns(
    turns: list[tuple[str, str, float]], budget: int
) -> tuple[list[tuple[str, str, float]], bool]:
    """Fit `turns` to `budget` tokens with max-min fair per-turn char shares.

    Turns keep their position and role; an over-share turn is cut with a
    trailing `[... truncated, N chars]` marker and one whose share is below
    `_MIN_USEFUL_CHARS` is replaced by `[omitted message, N chars]`. The
    marker overhead rides in `_RESPONSE_MARGIN`. Returns the rewritten turns
    plus False when nothing survived (all markers), so the caller can fall
    back to drop-oldest instead of sending a wall of markers.
    """
    budget_chars = budget * 4  # inverse of the chars/4 estimate
    # Bound the window so omission markers alone can never outgrow the
    # budget; anything older is dropped whole (the pointer note covers it).
    window = turns[-max(1, budget_chars // 100) :]
    shares = fair_char_allocations([len(text) for _role, text, _ts in window], budget_chars)
    out: list[tuple[str, str, float]] = []
    survived = False
    for (role, text, ts), share in zip(window, shares, strict=True):
        if share >= len(text):
            out.append((role, text, ts))
            survived = True
        elif share < _MIN_USEFUL_CHARS:
            out.append((role, f"[omitted message, {len(text)} chars]", ts))
        else:
            marker = f"\n[... truncated, {len(text)} chars]"
            out.append((role, text[: max(0, share - len(marker))] + marker, ts))
            survived = True
    return out, survived


def _drop_oldest(turns: list[tuple[str, str, float]], budget: int) -> list[tuple[str, str, float]]:
    """Keep whole newest turns within `budget` tokens (legacy fallback)."""
    kept: list[tuple[str, str, float]] = []
    used = 0
    for role, text, ts in reversed(turns):
        cost = estimate_tokens(text)
        if used + cost > budget:
            break  # summarization seam: a summary turn would replace the rest
        kept.append((role, text, ts))
        used += cost
    kept.reverse()
    return kept


def transcript_pointer(memory: Memory) -> str:
    """Context note pointing at the session logs that back this history.

    Injected whenever the window was trimmed so the bot can recover dropped
    content itself via `run_command`.
    """
    return (
        "[Note: earlier history above was trimmed to fit the context window. "
        f"The full transcript is in {memory.sessions_dir} — archived JSONL "
        "under its archive/ subfolder (one JSON record per line) — and in the "
        "transcripts table of the harness home's state.sqlite (query it with "
        "sqlite3). To recover dropped content, grep or query for keywords "
        "(names, IDs, error text) first, then read a small window around the "
        "matches — never read them linearly, single records can be huge.]"
    )


def _record_turn(record: dict) -> tuple[str, str, str] | None:
    """Map a session record to (role, peer, text); None for non-chat records."""
    text = str(record.get("text", ""))
    role = str(record.get("role", ""))
    peer = record.get("peer")
    if role == "out":
        return "assistant", str(peer or ""), text
    if role.startswith(_LEGACY_IN_PREFIX):
        # Records predating the peer field carry the sender in the role.
        return "user", str(peer or role[len(_LEGACY_IN_PREFIX) :]), text
    return None


def _normalized_summary(record: dict) -> dict:
    """A summary record with the chain fields defaulted for legacy rows."""
    out = dict(record)
    out["covers_until"] = float(record.get("covers_until", record.get("ts", 0.0)))
    # Pre-chain records carry no covers_from; 0.0 makes such a record supersede
    # every earlier summary, which is exactly the old fold-everything behavior.
    out["covers_from"] = float(record.get("covers_from", 0.0))
    return out


def summary_chain(memory: Memory, peer: str) -> list[dict]:
    """Live compaction summaries for the 1:1 thread with `peer`, oldest first.

    Summaries form a generational chain: each compaction
    covers `[covers_from, covers_until)` of raw records, and an epoch fold
    writes one coarser record over the union of older summaries' ranges. A
    record is dead once a later-written record's range contains its own — the
    fold replaces exactly what it folded, and a legacy record (covers_from
    treated as 0.0) replaces everything older than itself.
    """
    records = [
        _normalized_summary(r)
        for r in memory._session_records()
        if r.get("is_summary")
        and r.get("room") is None
        and not _thread_id_of(r)
        and str(r.get("peer") or "") == peer
    ]
    live: list[dict] = []
    for i, rec in enumerate(records):
        superseded = any(
            later["covers_from"] <= rec["covers_from"]
            and later["covers_until"] >= rec["covers_until"]
            for later in records[i + 1 :]
        )
        if not superseded:
            live.append(rec)
    live.sort(key=lambda r: r["covers_until"])
    return live


def summaries_block(chain: list[dict]) -> str:
    """The rendered summary head: every live summary oldest first, then the
    durable blocks once (from the newest record that has them — every chain
    record repeating its own copy would drown the summaries in boilerplate)."""
    parts = [str(r.get("text") or "") for r in chain if str(r.get("text") or "")]
    durable = next((str(r["durable"]) for r in reversed(chain) if r.get("durable")), "")
    if durable:
        parts.append(durable)
    return "\n\n".join(parts)


def _conversation(
    memory: Memory, peer: str, *, with_summaries: bool = True
) -> list[tuple[str, str, float]]:
    """(role, text, ts) turns of the 1:1 thread with `peer`, oldest first.

    Reads every session file (sorted by boot timestamp), so the thread
    survives bot restarts. Consecutive same-role turns are coalesced because
    some providers require strict user/assistant alternation.

    Compaction: records covered by the summary chain (ts < the
    newest `covers_until` seam) are replaced by one user-role turn at the
    head of the thread carrying every live summary — recent ones verbatim,
    older ranges already folded coarser — and everything from the seam onward
    is verbatim. `with_summaries=False` skips that head turn (the raw
    post-seam view compaction itself partitions), never the seam filter.
    """
    chain = summary_chain(memory, peer)
    covered = chain[-1]["covers_until"] if chain else None
    turns: list[tuple[str, str, float]] = []
    last_in_peer = ""
    for record in memory._session_records():
        if record.get("room") is not None:
            continue
        if _thread_id_of(record):
            continue  # side-thread replies are not the main 1:1
        if record.get("is_summary"):
            continue  # replayed once below (latest only), never as a raw turn
        if str(record.get("origin") or "") == "ack":
            continue  # fast-ack receipts are not conversation
        mapped = _record_turn(record)
        if mapped is None:
            continue
        role, rec_peer, text = mapped
        if role == "user":
            last_in_peer = rec_peer
        elif not rec_peer:  # legacy out records carry no peer field
            rec_peer = last_in_peer
        if rec_peer != peer:
            continue
        if covered is not None and float(record.get("ts", 0.0)) < covered:
            continue  # the summary turn below already stands in for this record
        note = _attachment_history_note(record.get("attachments"))
        if not text:
            if not note:
                continue
            text = note
        elif note:
            # Keep the file on THIS turn so a later Linear attach can still
            # name the path. Do not promote it onto the live user message.
            text = f"{text}\n{note}"
        ts = float(record.get("ts", 0.0))
        if turns and turns[-1][0] == role:
            prev_role, prev_text, prev_ts = turns[-1]
            turns[-1] = (prev_role, f"{prev_text}\n\n{text}", prev_ts)
        else:
            turns.append((role, text, ts))
    if with_summaries and chain:
        text = summaries_block(chain)
        if text:
            # The chain leads the thread as one user-role turn. Its ts is the
            # covers_until seam so the reported cutoff hands recall everything
            # the summaries swallowed. Merge into a leading user turn to keep
            # strict alternation.
            if turns and turns[0][0] == "user":
                role0, text0, _ts0 = turns[0]
                turns[0] = (role0, f"{text}\n\n{text0}", covered or 0.0)
            else:
                turns.insert(0, ("user", text, covered or 0.0))
    return turns


def user_thread(
    memory: Memory,
    *,
    peer: str = "user",
    limit: int = 200,
    before: float | None = None,
    thread_id: str | None = None,
) -> list[dict]:
    """1:1 turns with `peer` for the client transcript, oldest first.

    Same session logs as `build_history`, but not token-budgeted or coalesced —
    each logged turn is one bubble. Room turns and other peers are omitted.
    Side-thread replies are omitted from the main feed; pass `thread_id` to
    get the root plus its replies instead. `limit` is one page (newest page
    when `before` is omitted; the page immediately older than `before`
    otherwise). The client loads further pages when the user scrolls up.
    """
    want = str(thread_id or "").strip() or None
    counts = reply_counts(memory) if want is None else {}
    rows: list[dict] = []
    last_in_peer = ""
    card_index: dict[str, int] = {}
    for record in memory._session_records():
        if record.get("room") is not None:
            continue
        if record.get("origin") == "colleague_reply" and str(record.get("role", "")).startswith(
            "in:"
        ):
            continue  # The peer exchange already displays this reply; keep it in model history.
        if record.get("is_summary"):
            continue  # a compaction artifact, not a chat bubble
        if str(record.get("origin") or "") == "ack":
            continue  # fast-ack receipts are not conversation
        rec_tid = _thread_id_of(record)
        rec_mid = message_id_of(record)
        if want:
            if rec_tid != want and rec_mid != want:
                continue
        elif rec_tid:
            continue
        if str(record.get("role") or "") == "card":
            rec_peer = str(record.get("peer") or last_in_peer or peer)
            if rec_peer != peer:
                continue
            cid = str(record.get("card_id") or "").strip()
            ctype = str(record.get("card_type") or "").strip()
            if not cid or not ctype:
                continue
            payload = record.get("payload")
            row = {
                "ts": float(record.get("ts", 0.0)),
                "frm": str(record.get("frm") or memory.bot),
                "text": "",
                "type": "card",
                "card_id": cid,
                "card_type": ctype,
                "payload": payload if isinstance(payload, dict) else {},
                "message_id": rec_mid,
            }
            origin = str(record.get("origin") or "").strip()
            if origin:
                row["origin"] = origin
            resolution = record.get("resolution")
            if isinstance(resolution, dict) and resolution:
                row["resolution"] = resolution
            if cid in card_index:
                keep = rows[card_index[cid]]
                row["ts"] = keep["ts"]
                rows[card_index[cid]] = row
            else:
                card_index[cid] = len(rows)
                rows.append(row)
            continue
        mapped = _record_turn(record)
        if mapped is None:
            continue
        role, rec_peer, text = mapped
        if role == "user":
            # Dream / leftover idle-think ticks are background work, not a
            # person talking. Keep the bot's reply; drop the scheduler prompt.
            origin = str(record.get("origin") or "").strip()
            if origin in ("dream", "idle", messaging.ORIGIN_WELCOME):
                continue
            if text.lstrip().startswith("[Dreaming") or text.lstrip().startswith(
                "[Idle reflection"
            ):
                continue
            # A new bot's welcome prompt (harness/welcome.py) is its
            # instruction to open the chat; only the greeting is the thread.
            if text.lstrip().startswith("[Welcome"):
                continue
            last_in_peer = rec_peer
        elif not rec_peer:
            rec_peer = last_in_peer
        if rec_peer != peer:
            continue
        if role == "user" and messaging.is_routine_prompt(text):
            # Scheduler prompts stay on the transcript as a collapsed card,
            # not a full user bubble of the instruction dump.
            title, body = messaging.split_routine_prompt(text)
            cid = rec_mid or message_id_of(record)
            row = {
                "ts": float(record.get("ts", 0.0)),
                "frm": "user",
                "text": "",
                "type": "card",
                "card_id": f"routine:{cid}",
                "card_type": "routine",
                "payload": {"title": title, "detail": body},
                "message_id": rec_mid,
                "origin": messaging.ORIGIN_ROUTINE,
            }
            if rec_tid:
                row["thread_id"] = rec_tid
            n = counts.get(rec_mid, 0)
            if n:
                row["reply_count"] = n
            rows.append(row)
            continue
        atts = record.get("attachments") if isinstance(record.get("attachments"), list) else None
        if not text and not atts:
            continue
        explicit = str(record.get("frm") or "").strip()
        frm = explicit or (peer if role == "user" else memory.bot)
        row = {
            "ts": float(record.get("ts", 0.0)),
            "frm": frm,
            "text": text,
            "message_id": rec_mid,
        }
        if atts:
            row["attachments"] = atts
        origin = str(record.get("origin") or "").strip()
        if origin:
            row["origin"] = origin
        if record.get("voice_call_id"):
            row["voice_call_id"] = record["voice_call_id"]
        if rec_tid:
            row["thread_id"] = rec_tid
        n = counts.get(rec_mid, 0)
        if n:
            row["reply_count"] = n
        rows.append(row)
    if before is not None:
        rows = [r for r in rows if r["ts"] < before]
    if want:
        # Parent and replies can land in different sessions (test "s1" vs a
        # bot's timestamped session). Thread order is timestamp, parent first.
        rows.sort(key=lambda r: (r["ts"], 0 if r.get("message_id") == want else 1))
    return rows[-limit:]


def build_history(
    memory: Memory,
    *,
    peer: str,
    provider: Provider,
    system: str = "",
    tools: list[ToolSpec] | None = None,
    current: str = "",
    usage: dict | None = None,
    last_history_tokens: int = 0,
) -> tuple[list[Message], float | None]:
    """History messages for the 1:1 thread with `peer`.

    Returns the messages plus the oldest included timestamp (so memory recall
    can skip session records the history already covers), or ([], None).
    History is text-only: past tool calls/results are ephemeral host state and
    are not replayed.
    """
    budget = history_budget(
        provider,
        system=system,
        tools=tools,
        current=current,
        usage=usage,
        last_history_tokens=last_history_tokens,
    )
    if budget <= 0:
        return [], None
    turns = _conversation(memory, peer)
    if not turns:
        return [], None
    trimmed = sum(estimate_tokens(text) for _role, text, _ts in turns) > budget
    if trimmed:
        kept, survived = _fair_truncate_turns(turns, budget)
        if not survived:  # even fair shares are useless; keep newest whole turns
            kept = _drop_oldest(turns, budget)
    else:
        kept = list(turns)
    while kept and kept[0][0] == "assistant":
        kept.pop(0)  # the thread sent to a provider must open with a user turn
    if not kept:
        return [], None
    if trimmed:
        # Prepend the pointer to the oldest user turn (a standalone note turn
        # would break the strict user/assistant alternation some providers
        # require); its ~90 tokens ride in _RESPONSE_MARGIN.
        role, text, ts = kept[0]
        kept[0] = (role, f"{transcript_pointer(memory)}\n\n{text}", ts)
    messages = [
        Message(role=role, content=text, name=peer if role == "user" else None)
        for role, text, _ts in kept
    ]
    return messages, kept[0][2]
