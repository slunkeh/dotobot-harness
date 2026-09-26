"""Exact browser-message proposals; presentation metadata is never sent."""

from __future__ import annotations

import json
import sqlite3
from urllib.parse import urlsplit

from harness import cdp
from harness.redaction import scrub
from harness.statestore import store_for

from .gate import GateRefusal


def proposal(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - {"target_url", "text", "context"}:
        raise ValueError("outgoing_message needs target_url, text and optional context")
    url, text = value.get("target_url"), value.get("text")
    context = value.get("context", "")
    if not isinstance(url, str) or not isinstance(text, str) or not isinstance(context, str):
        raise ValueError("outgoing_message fields must be strings")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("outgoing_message needs a complete HTTP(S) target URL")
    if not text.strip() or len(text) > 2000 or len(context) > 4000 or len(url) > 2048:
        raise ValueError(
            "outgoing_message needs exact text (1–2000 characters) and bounded context"
        )
    result = {"target_url": url, "text": text, "context": context}
    if scrub(json.dumps(result)) != json.dumps(result):
        raise ValueError("outgoing_message must not contain secrets")
    return result


def submit(ctx, args):
    """Dispatch once using only the proposal admitted by govern, then require verification."""
    admitted = getattr(ctx, "outgoing_approval", None)
    if not admitted or admitted.get("id") != args.get("approval_id"):
        return "error: this submit has no current governed approval"
    revalidate = getattr(ctx, "outgoing_revalidate", None)
    if not callable(revalidate):
        return "error: outgoing dispatch revalidation is unavailable; nothing was posted"
    session = getattr(ctx.computer, "browser_session", None)
    if not callable(session):
        return "error: verified browser submission is unavailable; nothing was posted"
    message = admitted["payload"]["outgoing_message"]
    attempted = False
    try:
        with session() as browser:
            if browser.prepare_outgoing(message["target_url"], message["text"]) != "ready":
                return "error: target or composer is different, unsupported, or ambiguous; nothing was posted"
            if refusal := revalidate():
                return refusal
            if not store_for(ctx.paths).claim_prompt_execution(
                admitted["id"], bot=ctx.bot, task_id=ctx.task_id, revision=ctx.task_revision
            ):
                return "error: this approval changed or was already used; do not retry the post"
            attempted = True
            result = browser.submit_outgoing(message["target_url"], message["text"])
            if result != "ok":
                return "error: the composer changed before submission; this approval is spent, inspect before requesting a new proposal"
    except (cdp.CdpError, OSError, TimeoutError, ValueError, KeyError, sqlite3.Error, GateRefusal):
        if attempted:
            return "error: submission outcome is unknown; inspect the page and never retry this approval"
        return "error: browser verification unavailable; nothing was posted"
    return "Submission dispatched once using the approved exact text. Inspect the page to verify publication before reporting success."
