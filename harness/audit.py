"""Append-only authorization trail: what was decided, before it happened.

This is deliberately *not* the activity trail. `agent/runtime.py` already emits
a trail card per tool call into `shared/streams/<request_id>.jsonl`, and that is
a live window: keyed by request, scoped to a conversation, swept with it, and
carrying no decision because there was none to carry. It answers "what did the
bot do just now".

This answers a different question — "what was this bot allowed to do, and who
said so" — and it has to survive the conversation it happened in. So it is its
own ledger, keyed by bot and time, holding the decision and the rule that
produced it.

Two properties are load-bearing:

* **The row is written before the action.** `agent/govern.py` awaits this write
  and only then calls the handler, so an action that was not recorded did not
  happen. A trail assembled afterwards is a report, and a report can be skipped
  by the code path that most needed to write it.
* **A permitted action that then fails gets a SECOND row.** `allowed` and
  `happened` are different claims. Recording only the first is how a trail ends
  up confidently wrong, which is worse than silent — a silent trail gets
  checked, a wrong one gets believed.

Storage mirrors `harness/usage.py`, which already proves the pattern for this
harness: append-only JSONL, one line per record, so concurrent bot processes
just append and there is no read-modify-write to lose. Malformed lines are
skipped on read the way `agent/memory.py` tolerates them. Records are sanitized
on write *and* on read, because a ledger that trusts its own file is a ledger
that a corrupt line can take down.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .paths import HarnessPaths

#: What a row can say about a decision. `dry-run` is a permit that a rule would
#: have refused: the operator is testing a policy against real traffic and the
#: trail is the only place that difference is visible.
DECISION_ALLOW = "allow"
DECISION_REFUSE = "refuse"
DECISION_DRY_RUN = "dry-run"

#: Outcome rows, written after a permitted action returns.
OUTCOME_OK = "ok"
OUTCOME_FAILED = "failed"

#: Longest target/detail string a row will carry. A command line can be
#: arbitrarily long and this file is read by people; the full text lives in the
#: stream trail, which is what that one is for.
_MAX_TEXT = 2000

#: Records kept per bot before the ledger is trimmed on write. Chosen so a busy
#: bot keeps roughly a week of decisions rather than growing without bound —
#: the stream trail's problem, which this file exists partly to avoid repeating.
DEFAULT_RETENTION = 20_000


def audit_file(paths: HarnessPaths, bot: str) -> Path:
    return paths.audit / f"{_safe_name(bot)}.jsonl"


def _safe_name(bot: str) -> str:
    """A bot name as a single path segment. Bot names reach here from the
    roster and from `create_bot`, so they are not assumed to be safe."""
    cleaned = "".join(ch for ch in str(bot) if ch.isalnum() or ch in "-_") or "unknown"
    return cleaned[:64]


def _text(value: Any, limit: int = _MAX_TEXT) -> str:
    out = "" if value is None else str(value)
    if len(out) > limit:
        return out[:limit] + f"…(+{len(out) - limit} chars)"
    return out


def record(
    paths: HarnessPaths,
    bot: str,
    *,
    event: str,
    tool: str = "",
    intent: str = "",
    target: str = "",
    decision: str = "",
    rule: str = "",
    source: str = "",
    detail: str = "",
    tool_call_id: str = "",
    now: float | None = None,
    retention: int = DEFAULT_RETENTION,
) -> None:
    """Append one row. Never raises: a ledger that can break a turn is a
    ledger someone will switch off, and the gate above it has already made the
    decision this is recording."""
    row: dict[str, Any] = {
        "ts": float(now) if now is not None else time.time(),
        "bot": str(bot),
        "event": str(event),
        "tool": str(tool),
        "intent": str(intent),
        "target": _text(target),
        "decision": str(decision),
        "rule": _text(rule, 200),
        "source": str(source),
        "detail": _text(detail),
        "tool_call_id": str(tool_call_id or ""),
    }
    try:
        path = audit_file(paths, bot)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        _trim(path, retention)
    except OSError:
        return


def _trim(path: Path, retention: int) -> None:
    """Keep the newest `retention` rows. Rewrites through a temp file and
    `os.replace`, so a reader sees the old file or the new one, never a
    half-trimmed one. Cheap because it only runs once the file is well past
    the cap, not on every append."""
    if retention <= 0:
        return
    try:
        if path.stat().st_size < retention * 120:
            return  # nowhere near the cap on any plausible row size
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return
    if len(lines) <= retention:
        return
    tmp = path.with_suffix(path.suffix + ".trim")
    try:
        tmp.write_text("\n".join(lines[-retention:]) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def read(
    paths: HarnessPaths,
    bot: str,
    *,
    limit: int = 200,
    event: str | None = None,
    decision: str | None = None,
) -> list[dict[str, Any]]:
    """The newest rows first, optionally filtered. Malformed lines are skipped
    rather than raising — one bad append must not make the whole trail
    unreadable, which is exactly when someone needs to read it."""
    path = audit_file(paths, bot)
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(row, dict):
            continue
        if event is not None and row.get("event") != event:
            continue
        if decision is not None and row.get("decision") != decision:
            continue
        out.append(row)
        if len(out) >= max(1, limit):
            break
    return out
