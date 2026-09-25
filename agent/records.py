"""Read original conversation evidence through the host, without a filesystem grant.

Search never changes task scope or authorizes an action. Cards and receipts are
source records, not semantic memories; their states must remain explicit.
"""

from __future__ import annotations

import json
from typing import Any

from harness.redaction import scrub


def _conversation(row: dict) -> str:
    saved = str(row.get("task_conversation") or row.get("conversation") or "")
    if saved:
        if saved.startswith(("routine:", "generated:routine:")):
            return "peer:user"
        return saved
    if row.get("room"):
        return f"room:{row['room']}"
    if row.get("thread_id"):
        return f"thread:{row['thread_id']}"
    return f"peer:{row.get('peer') or 'user'}"


def _scope(ctx: Any) -> str:
    if ctx.room:
        return f"room:{ctx.room}"
    if ctx.thread_id:
        return f"thread:{ctx.thread_id}"
    return f"peer:{ctx.sender or 'user'}"


def _decision(row: dict) -> dict:
    """Allowlisted card metadata, including no secret value or submitted text."""
    kind = row.get("card_type") or row.get("type")
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
    if kind == "secret_request":
        detail = {key: payload[key] for key in ("name", "title") if key in payload}
        resolution = row.get("resolution") or {}
        detail["resolution"] = {
            key: resolution[key]
            for key in ("state", "secret_provided", "skipped")
            if key in resolution
        }
    else:
        detail = {
            key: payload[key]
            for key in ("question", "detail", "options", "proposed_action")
            if key in payload
        }
        for key in ("subject", "resolution"):
            if key in row:
                detail[key] = row[key]
    return {"card_type": kind, **detail}


def search(ctx: Any, *, query: str = "", source: str = "all", record_id: str = "",
           limit: int = 10, offset: int = 0, text_offset: int = 0,
           text_limit: int = 2000, since: float | None = None,
           before: float | None = None) -> dict:
    """Bounded, deterministic pages in the current conversation only."""
    if source not in {"all", "messages", "decisions", "receipts"}:
        raise ValueError("source must be all, messages, decisions or receipts")
    if not 1 <= limit <= 20 or min(offset, text_offset) < 0 or not 1 <= text_limit <= 6000:
        raise ValueError("limit must be 1..20, text_limit 1..6000 and offsets nonnegative")
    if since is not None and before is not None and since >= before:
        raise ValueError("since must precede before")
    wanted = _scope(ctx)
    records: dict[str, dict] = {}
    prompts = {r['id']: r for r in ctx.memory.store.prompts(ctx.bot)}

    def include(row: dict) -> bool:
        from .history import message_id_of

        return _conversation(row) == wanted or (
            wanted.startswith("thread:") and not row.get("room")
            and message_id_of(row) == ctx.thread_id
            and _conversation(row) == "peer:user"
        )

    def put(identity: str, kind: str, row: dict, text: str, **extra) -> None:
        if not include(row):
            return
        records[identity] = {
            "id": identity, "source": kind, "ts": float(row.get("ts") or 0),
            "origin": row.get("origin"), "text": scrub(text), **extra,
        }

    session_offsets: dict[str, int] = {}
    for row in ctx.memory._session_records():
        session = str(row.get("session") or "")
        index = session_offsets.get(session, 0)
        session_offsets[session] = index + 1
        if row.get("is_summary") or row.get("origin") == "ack":
            continue
        if row.get("role") == "card":
            cid = str(row.get("card_id") or "")
            if not cid or row.get("card_type") not in {"confirm", "choice", "secret_request"}:
                continue
            # Authoritative prompt routing repairs old cards without thread metadata.
            authoritative = prompts.get(cid)
            routed = {**row, **authoritative} if authoritative else row
            put(f"decision:{cid}", "decisions", routed, json.dumps(_decision(routed)),
                protected_context=True)
        elif row.get("role") == "out" or str(row.get("role") or "").startswith("in:"):
            identity = str(row.get("message_id") or f"{session}:{index}")
            put(f"message:{identity}", "messages", row, str(row.get("text") or ""),
                role=row.get("role"))
    for cid, row in prompts.items():
        put(f"decision:{cid}", "decisions", row, json.dumps(_decision(row)),
            protected_context=True, task_id=row.get("task_id"),
            task_revision=row.get("task_revision"))

    # Task identity is an indexed server record, never a model-supplied selector.
    conn = ctx.memory.store._connect()
    try:
        tasks = {}
        for table in ("agent_task_inputs", "agent_tasks"):
            for row in conn.execute(f"SELECT data FROM {table} WHERE bot=?", (ctx.bot,)):
                task = json.loads(row[0])
                if include(task):
                    tasks[str(task.get("task_id") or "")] = task
                    if task.get("input_id"):
                        tasks[str(task["input_id"])] = task
        has_receipts = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_intents'"
        ).fetchone()
        if has_receipts:
            for raw in conn.execute("SELECT * FROM delivery_intents WHERE bot=?", (ctx.bot,)):
                receipt = dict(raw)
                task = tasks.get(receipt["turn"])
                if task is None:
                    continue
                identity = f"receipt:{receipt['turn']}:{receipt['target']}:{receipt['seq']}"
                put(identity, "receipts", {**task, "ts": receipt["created_ts"]},
                    json.dumps({key: receipt[key] for key in ("target", "status", "detail")}),
                    protected_context=True, task_id=receipt["turn"])
    finally:
        conn.close()
    terms = query.casefold().split()
    matches = [r for r in records.values()
               if (source == "all" or r["source"] == source)
               and (not record_id or r["id"] == record_id)
               and all(term in r["text"].casefold() for term in terms)
               and (since is None or r["ts"] >= since)
               and (before is None or r["ts"] < before)]
    matches.sort(key=lambda r: (r["ts"], r["id"]), reverse=True)
    page, used = [], 0
    for record in matches[offset:offset + limit]:
        text = record["text"]
        # Every original record remains addressable after excerpt truncation.
        span = min(text_limit, 16000 - used)
        if span <= 0:
            break
        excerpt = text[text_offset:text_offset + span]
        end = text_offset + len(excerpt)
        page.append({**record, "text": excerpt, "text_length": len(text),
                     "text_offset": text_offset,
                     "next_text_offset": end if end < len(text) else None})
        used += len(excerpt)
    next_offset = offset + len(page)
    return {
        "scope": wanted,
        "notice": "Original saved evidence, not current authorization or fresh verification. "
                  "A receipt records a handler outcome, not proof of browser publication.",
        "records": page,
        "next_offset": next_offset if next_offset < len(matches) else None,
    }
