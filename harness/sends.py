"""Idempotent chat sends: the client-nonce acceptance ledger.

A client that loses its connection mid-send cannot tell whether the harness
accepted the message. Blind retries duplicate the chat; giving up drops it.
The fix is an idempotency key: the client attaches a `client_nonce` to the
send, and the server keeps a small persisted ledger of what each nonce was
accepted as:

    nonce -> {input_digest, status, turns: [{bot, request_id}], ts}

* Repeat send, same nonce + same digest  -> the original accepted result is
  returned (the recorded request ids), with no second dispatch.
* Same nonce + different digest         -> rejected with a distinct error
  code (`nonce_digest_mismatch`) — the client reused a key for new input.
* `GET /api/sends/<nonce>`              -> the recorded status, so a client
  that lost its socket can ask "did that land?" before retrying.

The ledger lives at `$HARNESS_HOME/sends/ledger.json`, is capped at
`LEDGER_CAP` records (oldest evicted first), and is written atomically so a
crash mid-write never leaves a torn file. Clients that send no nonce are
untouched — every send without one dispatches exactly as before.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time

from .fsutil import write_atomic
from .paths import HarnessPaths

#: Oldest accepted records are evicted past this many entries.
LEDGER_CAP = 256

#: Distinct error code for a nonce reused with different input.
NONCE_DIGEST_MISMATCH = "nonce_digest_mismatch"

# One server process writes the ledger from many handler threads.
_LOCK = threading.Lock()


class NonceMismatchError(Exception):
    """A client_nonce was already accepted with a different input digest."""

    code = NONCE_DIGEST_MISMATCH

    def __init__(self, nonce: str) -> None:
        super().__init__(
            f"client_nonce {nonce!r} was already accepted with different input "
            "(text/attachments/target changed); use a fresh nonce"
        )
        self.nonce = nonce


def input_digest(text: str, attachments: list | None, target: dict) -> str:
    """sha256 over the canonical JSON of what the send actually asked for.

    Key order is fixed (sort_keys) so semantically identical retries hash the
    same; attachment order is part of the input on purpose.
    """
    canonical = json.dumps(
        {"text": text or "", "attachments": attachments or [], "target": target},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load(paths: HarnessPaths) -> list[dict]:
    try:
        data = json.loads(paths.send_ledger().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list):
        return []
    return [r for r in records if isinstance(r, dict) and r.get("nonce")]


def _save(paths: HarnessPaths, records: list[dict]) -> None:
    write_atomic(
        paths.send_ledger(),
        json.dumps({"version": 1, "records": records[-LEDGER_CAP:]}, ensure_ascii=False),
    )


def lookup(paths: HarnessPaths, nonce: str) -> dict | None:
    """The recorded acceptance for `nonce`, or None if never seen."""
    if not nonce:
        return None
    with _LOCK:
        for record in _load(paths):
            if record.get("nonce") == nonce:
                return record
    return None


def admit(paths: HarnessPaths, nonce: str, digest: str) -> dict | None:
    """Gate one send: None means dispatch fresh, a record means safe retry.

    Raises NonceMismatchError when the nonce was already accepted for
    different input — the one case that must never silently dispatch.
    """
    record = lookup(paths, nonce)
    if record is None:
        return None
    if record.get("input_digest") != digest:
        raise NonceMismatchError(nonce)
    return record


def record_accept(
    paths: HarnessPaths,
    nonce: str,
    digest: str,
    turns: list[tuple[str, str]],
    *,
    room: str | None = None,
) -> dict:
    """Persist that this nonce dispatched as `turns` ([(bot, request_id)])."""
    record: dict = {
        "nonce": nonce,
        "input_digest": digest,
        "status": "accepted",
        "ts": time.time(),
        "turns": [{"bot": bot, "request_id": rid} for bot, rid in turns],
    }
    if room:
        record["room"] = room
    with _LOCK:
        records = [r for r in _load(paths) if r.get("nonce") != nonce]
        records.append(record)
        _save(paths, records)
    return record
