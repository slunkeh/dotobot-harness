"""Last-read timestamps per conversation (chat picker unread).

One JSON file under $HARNESS_HOME:

    {"reads": {"bot:atlas": 1710000000.0, "room:ops": 1710000001.0}}

`ts` is unix seconds of the last time the user opened (or caught up on)
that thread. A later inbound bubble is unread. Mac and iOS share this
file so a read on one device clears the other.
"""

from __future__ import annotations

import json
import re
from typing import Any

from harness.fsutil import write_atomic
from harness.paths import HarnessPaths

_KEY = re.compile(r"^(bot|room):[A-Za-z0-9._-]+$")


class ReceiptError(ValueError):
    """Bad conversation key or timestamp."""


def valid_key(key: str) -> bool:
    return bool(_KEY.fullmatch(key or ""))


def load(paths: HarnessPaths) -> dict[str, float]:
    path = paths.receipts
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    raw = data.get("reads") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, val in raw.items():
        if not valid_key(str(key)):
            continue
        try:
            out[str(key)] = float(val)
        except (TypeError, ValueError):
            continue
    return out


def save(paths: HarnessPaths, reads: dict[str, float]) -> dict[str, float]:
    clean = {k: float(v) for k, v in reads.items() if valid_key(k)}
    write_atomic(
        paths.receipts,
        json.dumps({"reads": clean}, ensure_ascii=False, indent=2) + "\n",
    )
    return clean


def merge(paths: HarnessPaths, updates: dict[str, Any]) -> dict[str, float]:
    """Overlay `updates` onto the stored map. Unknown keys are skipped."""
    reads = load(paths)
    for key, val in (updates or {}).items():
        k = str(key)
        if not valid_key(k):
            continue
        try:
            reads[k] = float(val)
        except (TypeError, ValueError):
            continue
    return save(paths, reads)


def set_read(paths: HarnessPaths, key: str, ts: float) -> dict[str, float]:
    if not valid_key(key):
        raise ReceiptError(f"invalid conversation key {key!r}")
    try:
        stamp = float(ts)
    except (TypeError, ValueError) as exc:
        raise ReceiptError("ts must be a unix timestamp") from exc
    return merge(paths, {key: stamp})
