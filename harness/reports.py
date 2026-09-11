"""Problem reports: what a client saw, plus the server's own context.

A report lands in `$HARNESS_HOME/reports/<id>.json` either because someone
pressed **Report** on a message (the ellipsis menu in the apps) or because
the app hit an error in a chat (`kind: app_error`, filed automatically).
The client sends what it knows — the message, the conversation, its
version, the error text, its last few client-side events — and the server
attaches what only it can see at that moment: its own log tail
(`run/server/serve.log`), the bot's log tail, status, queue and control
state, the bot's last audit rows, and the request's stream events when a
request id is known. One file per report, readable with `harness reports
<id>` or `GET /api/reports/<id>`, so a problem is debugged from one place
instead of a chat screenshot and a shell rummage.

Everything the client sends is data, bounded and scrubbed through the
redaction registry before it is stored; nothing in a report is ever a
credential (the gate's audit rows and the log tails are already
secret-free). The store keeps the newest `HARNESS_REPORTS_KEEP` (200)
reports and prunes the rest on write.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .fsutil import write_atomic
from .paths import HarnessPaths
from .redaction import scrub as scrub_secrets
from .roster import valid_bot_name
from .version import __version__

KINDS = ("user_report", "app_error")
DEFAULT_KEEP = 200

MAX_SUMMARY = 2000
MAX_DESCRIPTION = 20_000
MAX_MESSAGE_TEXT = 4000
MAX_SHORT = 200
MAX_CLIENT_LOG_LINES = 200
MAX_CLIENT_LOG_LINE = 1000
LOG_TAIL_LINES = 100
STREAM_TAIL_LINES = 60
AUDIT_ROWS = 30

_ID_RE = re.compile(r"^rpt-\d{8}-\d{6}-[0-9a-f]{6}$")
_SAFE_ID = re.compile(r"[^A-Za-z0-9._:-]+")


class ReportError(ValueError):
    """A submission the store will not accept (nothing to report)."""


def keep_count() -> int:
    raw = os.environ.get("HARNESS_REPORTS_KEEP", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_KEEP
    except ValueError:
        return DEFAULT_KEEP
    return max(1, value)


def new_id(now: float | None = None) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now if now is not None else time.time()))
    return f"rpt-{stamp}-{uuid.uuid4().hex[:6]}"


def valid_id(report_id: str) -> bool:
    return bool(_ID_RE.fullmatch(report_id or ""))


def report_file(paths: HarnessPaths, report_id: str) -> Path | None:
    if not valid_id(report_id):
        return None
    return paths.reports / f"{report_id}.json"


# -- the client's half ----------------------------------------------------


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            value = str(value)
    return scrub_secrets(value[:limit])


def _ident(value: Any, limit: int = MAX_SHORT) -> str:
    """An id-shaped field: one safe token, never a path or a sentence."""
    if not isinstance(value, str):
        return ""
    return _SAFE_ID.sub("", value.strip())[:limit]


def normalize(payload: Any) -> dict[str, Any]:
    """The client-submitted part of a report, bounded and scrubbed.

    Raises ReportError when there is nothing to file (no summary, no error).
    """
    if not isinstance(payload, dict):
        raise ReportError("report body must be a JSON object")
    kind = payload.get("kind")
    if kind not in KINDS:
        kind = "app_error" if payload.get("error") else "user_report"
    error_in = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    error = {
        "message": _text(error_in.get("message"), MAX_SUMMARY),
        "where": _text(error_in.get("where"), MAX_SHORT),
    }
    summary = _text(payload.get("summary"), MAX_SUMMARY).strip()
    if not summary:
        summary = error["message"].strip()[:MAX_SUMMARY]
    if not summary:
        raise ReportError("report needs a summary or an error message")
    app_in = payload.get("app") if isinstance(payload.get("app"), dict) else {}
    client_log = payload.get("client_log")
    if not isinstance(client_log, list):
        client_log = []
    bot = payload.get("bot") if isinstance(payload.get("bot"), str) else ""
    if bot and not valid_bot_name(bot):
        bot = ""
    request_id = _ident(payload.get("request_id"))
    message_id = payload.get("message_id")
    # StreamWriter's assistant bubble id survives history reloads, while
    # the app's request-to-queue map does not. Recover only that exact shape
    # from the original input; sanitizing an arbitrary id could change its meaning.
    if not request_id and isinstance(message_id, str) and len(message_id) <= MAX_SHORT:
        match = re.fullmatch(r"msg-([A-Za-z0-9_-]+)", message_id)
        if match:
            request_id = match.group(1)
    return {
        "kind": kind,
        "summary": summary,
        "description": _text(payload.get("description"), MAX_DESCRIPTION),
        "bot": bot[:128],
        "room": _ident(payload.get("room")),
        "conversation": _text(payload.get("conversation"), MAX_SHORT),
        "request_id": request_id,
        "message_id": _ident(payload.get("message_id")),
        "thread_id": _ident(payload.get("thread_id")),
        "message_text": _text(payload.get("message_text"), MAX_MESSAGE_TEXT),
        "message_author": _text(payload.get("message_author"), MAX_SHORT),
        "message_ts": _number(payload.get("message_ts")),
        "app": {
            "version": _text(app_in.get("version"), MAX_SHORT),
            "platform": _text(app_in.get("platform"), MAX_SHORT),
            "os": _text(app_in.get("os"), MAX_SHORT),
        },
        "error": error if (error["message"] or error["where"]) else None,
        "client_log": [
            _text(line, MAX_CLIENT_LOG_LINE) for line in client_log[-MAX_CLIENT_LOG_LINES:]
        ],
    }


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


# -- the server's half -----------------------------------------------------


def _best_effort(fn, default=None):
    """Enrichment never fails a report: each part is optional."""
    try:
        return fn()
    except Exception:  # noqa: BLE001 - context is best-effort by design
        return default


def context(
    orch, submitted: dict[str, Any], *, boot_id: str | None = None, started_at: float | None = None
) -> dict[str, Any]:
    """What the server knows right now that bears on the report."""
    from . import audit as audit_log
    from . import logstream

    paths: HarnessPaths = orch.paths
    out: dict[str, Any] = {
        "harness": {
            "version": __version__,
            "boot_id": boot_id,
            "started_at": started_at,
            "uptime_s": (time.time() - started_at) if started_at else None,
            "backend": getattr(orch, "backend_name", None),
            "python": sys.version.split()[0],
        },
        "server_log": _best_effort(
            lambda: (
                logstream.tail(
                    logstream.server_log_file(paths),
                    lines=LOG_TAIL_LINES,
                    previous=logstream.previous_generation(logstream.server_log_file(paths)),
                ).lines
            ),
            [],
        ),
    }
    bot = submitted.get("bot") or ""
    if bot:
        out["bot"] = _bot_context(orch, bot, audit_log, logstream)
    rid = submitted.get("request_id") or ""
    if rid:
        out["request"] = {
            "id": rid,
            "stream": _best_effort(
                lambda: logstream.tail(paths.stream_file(rid), lines=STREAM_TAIL_LINES).lines, []
            ),
        }
    room = submitted.get("room") or ""
    if room:
        out["room"] = _best_effort(lambda: _room_context(orch, room))
    return out


def _bot_context(orch, name: str, audit_log, logstream) -> dict[str, Any]:
    paths = orch.paths
    info: dict[str, Any] = {"name": name}

    def _roster():
        b = orch.roster.get(name)
        info["provider"] = b.provider
        info["model"] = b.model
        return True

    info["in_roster"] = bool(_best_effort(_roster, False))
    info["status"] = _best_effort(
        lambda: next((h.status.value for h in orch.status() if h.bot == name), "unknown"),
        "unknown",
    )

    def _control():
        state = asdict(orch.control.state(name))
        busy, current = orch.control.busy_state(name)
        return {"control": state, "busy": busy, "busy_request": current}

    info.update(_best_effort(_control, {}) or {})

    def _queue():
        from agent import messaging

        busy, current = orch.control.busy_state(name)
        q = messaging.queue_state(paths, name, busy=busy, current_id=current)
        return {"queued": q["queued"], "queue": q["items"]}

    info.update(_best_effort(_queue, {}) or {})
    info["log"] = _best_effort(
        lambda: logstream.tail(paths.log_file(name), lines=LOG_TAIL_LINES).lines, []
    )
    info["audit"] = _best_effort(lambda: audit_log.read(paths, name, limit=AUDIT_ROWS), [])
    return info


def _room_context(orch, room_id: str) -> dict[str, Any] | None:
    for r in orch.rooms():
        if r.id == room_id:
            d = r.to_dict()
            return {k: d.get(k) for k in ("id", "title", "members") if k in d}
    return None


# -- the store ------------------------------------------------------------


def file_report(
    orch,
    payload: Any,
    *,
    boot_id: str | None = None,
    started_at: float | None = None,
    now: float | None = None,
    announce: bool = True,
) -> dict[str, Any]:
    """Validate, enrich, persist. Returns the stored record."""
    submitted = normalize(payload)
    ts = now if now is not None else time.time()
    record = {
        "id": new_id(ts),
        "ts": ts,
        "kind": submitted["kind"],
        "summary": submitted["summary"],
        "report": submitted,
        "context": context(orch, submitted, boot_id=boot_id, started_at=started_at),
    }
    paths: HarnessPaths = orch.paths
    paths.reports.mkdir(parents=True, exist_ok=True)
    path = paths.reports / f"{record['id']}.json"
    write_atomic(path, json.dumps(record, ensure_ascii=False, indent=1))
    prune(paths, keep_count())
    if announce:
        # Into the server log (and so the log stream) as well: a report and
        # the lines around it then read in one place.
        who = f" bot={submitted['bot']}" if submitted["bot"] else ""
        print(f"report {record['id']}: {submitted['kind']}{who} — {submitted['summary'][:120]}")
    return record


def _summary_row(record: dict[str, Any]) -> dict[str, Any]:
    report = record.get("report") if isinstance(record.get("report"), dict) else {}
    app = report.get("app") if isinstance(report.get("app"), dict) else {}
    error = report.get("error") if isinstance(report.get("error"), dict) else None
    return {
        "id": record.get("id"),
        "ts": record.get("ts"),
        "kind": record.get("kind"),
        "summary": record.get("summary"),
        "bot": report.get("bot") or "",
        "room": report.get("room") or "",
        "app_version": app.get("version") or "",
        "platform": app.get("platform") or "",
        "where": (error or {}).get("where") or "",
    }


def _files(paths: HarnessPaths) -> list[Path]:
    if not paths.reports.is_dir():
        return []
    files = [p for p in paths.reports.glob("rpt-*.json") if valid_id(p.stem) and p.is_file()]
    return sorted(files, key=lambda p: p.name, reverse=True)  # the id embeds the time


def load_report(paths: HarnessPaths, report_id: str) -> dict[str, Any] | None:
    path = report_file(paths, report_id)
    if path is None or not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def list_reports(paths: HarnessPaths, *, limit: int = 50) -> list[dict[str, Any]]:
    """Newest first, one summary row each."""
    out = []
    for path in _files(paths)[: max(0, limit)]:
        record = load_report(paths, path.stem)
        if record is not None:
            out.append(_summary_row(record))
    return out


def delete_report(paths: HarnessPaths, report_id: str) -> bool:
    path = report_file(paths, report_id)
    if path is None or not path.is_file():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def prune(paths: HarnessPaths, keep: int) -> int:
    """Drop the oldest reports past `keep`; returns how many went."""
    removed = 0
    for path in _files(paths)[max(1, keep) :]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed
