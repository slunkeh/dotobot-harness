"""Per-provider usage ledger.

These are *activity records*, not a provider invoice: the harness counts what
it sent and received per (provider, model) so the app can show local activity.
The vendor's own dashboard stays authoritative for billing.

Storage is an append-only JSONL under `$HARNESS_HOME/usage/usage.jsonl` — one
record per logical agent turn, appended by whichever bot process finished the
turn. Append-only is deliberate: concurrent writers just append, so there is
no read-modify-write of a settings file to lose updates over. Readers
aggregate. Counts are sanitized to non-negative safe integers on write *and*
read, and malformed lines are skipped the same way `agent/memory.py`
tolerates them.
"""

from __future__ import annotations

import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .paths import HarnessPaths

#: vendor-neutral token counters carried by every ledger record
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")

#: JSON safe-integer ceiling (2**53 - 1); anything above is treated as garbage
_MAX_SAFE = 9_007_199_254_740_991

#: neutral field -> accepted names, ours first. Anthropic reports
#: input/output_tokens plus cache_read_input_tokens / cache_creation_input_tokens;
#: OpenAI-compatible APIs report prompt/completion_tokens (cached prompt tokens
#: hide in prompt_tokens_details.cached_tokens, handled below).
_ALIASES: dict[str, tuple[str, ...]] = {
    "input_tokens": ("input_tokens", "prompt_tokens"),
    "output_tokens": ("output_tokens", "completion_tokens"),
    "cache_read_tokens": ("cache_read_tokens", "cache_read_input_tokens"),
    "cache_write_tokens": ("cache_write_tokens", "cache_creation_input_tokens"),
}


def usage_file(paths: HarnessPaths) -> Path:
    return paths.usage / "usage.jsonl"


def safe_count(value: Any) -> int:
    """A non-negative safe integer, else 0 (bools, strings, negatives, junk)."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        n = value
    elif isinstance(value, float) and value.is_integer():
        n = int(value)
    else:
        return 0
    return n if 0 <= n <= _MAX_SAFE else 0


def normalize_usage(raw: Any) -> dict[str, int]:
    """Map one completion's vendor usage block onto the neutral counters."""
    out = dict.fromkeys(USAGE_FIELDS, 0)
    if not isinstance(raw, dict):
        return out
    for name, accepted in _ALIASES.items():
        for alias in accepted:
            if alias in raw:
                out[name] = safe_count(raw.get(alias))
                break
    if not out["cache_read_tokens"]:
        details = raw.get("prompt_tokens_details")
        if isinstance(details, dict):
            out["cache_read_tokens"] = safe_count(details.get("cached_tokens"))
    return out


def add_usage(totals: dict[str, int], raw: Any) -> dict[str, int]:
    """Fold one completion's usage into a running per-turn total (in place),
    so the tool loop can sum its steps into one logical-turn record."""
    for name, value in normalize_usage(raw).items():
        totals[name] = totals.get(name, 0) + value
    return totals


def record_usage(
    paths: HarnessPaths,
    provider: str,
    model: str,
    *,
    requests: int = 1,
    tokens: dict[str, Any] | None = None,
    now: float | None = None,
    bot: str = "",
    origin: str = "",
) -> None:
    """Append one activity record to the ledger (one line per logical turn).

    `bot` and `origin` (how the turn was scheduled — "dream", "routine", ""
    for chat) are optional attribution; the dream scheduler's daily budget
    reads them back via `tokens_today`. Rollups ignore them, so old readers and old
    records coexist.
    """
    record: dict[str, Any] = {
        "ts": float(now) if now is not None else time.time(),
        "provider": str(provider),
        "model": str(model),
        "requests": safe_count(requests),
        **normalize_usage(tokens),
    }
    if bot:
        record["bot"] = str(bot)
    if origin:
        record["origin"] = str(origin)
    path = usage_file(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def empty_usage() -> dict[str, Any]:
    """The zero rollup entry (also what the API shows for an unused provider)."""
    return {"requests": 0, **dict.fromkeys(USAGE_FIELDS, 0), "last_used_at": None}


def _safe_ts(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    ts = float(value)
    return ts if math.isfinite(ts) and ts > 0 else None


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")


def _records(paths: HarnessPaths):
    path = usage_file(paths)
    if not path.is_file():
        return
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if not isinstance(rec.get("provider"), str) or not rec["provider"]:
                    continue
                yield rec
    except OSError:
        return


def _fold(target: dict[str, Any], entry: dict[str, Any]) -> None:
    target["requests"] += entry["requests"]
    for name in USAGE_FIELDS:
        target[name] += entry[name]
    ts = entry.get("_ts")
    if ts is not None and (target.get("_ts") is None or ts > target["_ts"]):
        target["_ts"] = ts


def _finish(entry: dict[str, Any]) -> dict[str, Any]:
    entry["last_used_at"] = _iso(entry.pop("_ts", None))
    return entry


def rollup(paths: HarnessPaths) -> dict[str, Any]:
    """Aggregate the ledger: overall totals, per-provider totals, and per
    (provider, model) entries. Malformed lines are skipped; bad counts read
    as 0. This is local bookkeeping, not billing."""
    models: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in _records(paths):
        key = (rec["provider"], rec.get("model") if isinstance(rec.get("model"), str) else "")
        entry = models.setdefault(
            key, {"provider": key[0], "model": key[1], **empty_usage(), "_ts": None}
        )
        entry["requests"] += safe_count(rec.get("requests"))
        for name in USAGE_FIELDS:
            entry[name] += safe_count(rec.get(name))
        ts = _safe_ts(rec.get("ts"))
        if ts is not None and (entry["_ts"] is None or ts > entry["_ts"]):
            entry["_ts"] = ts

    providers: dict[str, dict[str, Any]] = {}
    totals: dict[str, Any] = {**empty_usage(), "_ts": None}
    for key in sorted(models):
        entry = models[key]
        per = providers.setdefault(key[0], {**empty_usage(), "_ts": None})
        _fold(per, entry)
        _fold(totals, entry)
    return {
        "kind": "activity",  # local counts, not a provider invoice
        "totals": _finish(totals),
        "providers": {name: _finish(per) for name, per in providers.items()},
        "models": [_finish(models[key]) for key in sorted(models)],
    }


def provider_totals(paths: HarnessPaths) -> dict[str, dict[str, Any]]:
    """Per-provider totals only (what `GET /api/providers` embeds)."""
    return rollup(paths)["providers"]


def tokens_today(
    paths: HarnessPaths, bot: str, *, origin: str | None = None, now: float | None = None
) -> int:
    """Input+output tokens `bot` spent since local midnight (activity, not
    billing). `origin` narrows to turns scheduled that way (e.g. "dream") —
    only records that carry the attribution count, so pre-attribution history
    never trips a budget it was not written for."""
    now = time.time() if now is None else float(now)
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    total = 0
    for rec in _records(paths):
        if str(rec.get("bot") or "") != bot:
            continue
        if origin is not None and str(rec.get("origin") or "") != origin:
            continue
        ts = _safe_ts(rec.get("ts"))
        if ts is None or ts < midnight:
            continue
        total += safe_count(rec.get("input_tokens")) + safe_count(rec.get("output_tokens"))
    return total
