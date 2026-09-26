"""Gmail connector using Google OAuth over IMAP and SMTP.

Each inbox has its own OAuth grant and connector record.

Stdlib only: imaplib against imap.gmail.com (Gmail's X-GM-* extensions
give Gmail-syntax search plus stable message and thread ids) and smtplib
against smtp.gmail.com. Ids are the X-GM-MSGID / X-GM-THRID values in
hex — the same ids Gmail's web UI and REST API show.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import html
import imaplib
import json
import re
import smtplib
import time
from collections.abc import Iterator
from contextlib import contextmanager
from email.message import EmailMessage
from typing import Any

from providers.base import ToolSpec

from . import pdftext
from .base import ConnectorContext, ConnectorTool

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
_TIMEOUT = 30
_MAX_RESULTS = 20
_MAX_BODY = 4000
_MAX_ATTACHMENT = 10 * 1024 * 1024
_MAX_ATTACHMENT_TEXT = 8000
_TEXT_MIMES = ("application/json", "application/xml", "application/csv")
_TEXT_EXTS = (".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".log", ".ics")
#: Gmail's SPECIAL-USE folders are found by attribute (`\All`, `\Drafts`)
#: because their names are localized; these are the en-US spellings for a
#: LIST that somehow carries no attributes.
_ALL_MAIL_FALLBACK = "[Gmail]/All Mail"
_DRAFTS_FALLBACK = "[Gmail]/Drafts"


class GmailError(RuntimeError):
    """An IMAP/SMTP exchange with Gmail failed (auth, network, or protocol)."""


# -- credentials -------------------------------------------------------------


def _account(ctx: ConnectorContext) -> tuple[str, str]:
    """Return the account address and current OAuth access token."""
    name = str(ctx.record.get("name") or ctx.record.get("type") or "Gmail")
    from harness import mcp_oauth

    address = str((mcp_oauth.load_tokens(ctx.paths, ctx.record["id"]) or {}).get("email") or "")
    if not address:
        raise GmailError(
            f"Reconnect {name!r} through Dotobot to choose a Google account."
        )
    from harness import delegated_oauth, mcp_oauth

    try:
        token = delegated_oauth.token(ctx.paths, ctx.record)
    except mcp_oauth.OAuthError as exc:
        raise GmailError(str(exc)) from None
    return address, token


# -- connections (module-level so tests can monkeypatch them) ------------------


def _connect_imap(address: str, token: str) -> imaplib.IMAP4:
    """An authenticated IMAP session for `address`."""
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=_TIMEOUT)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise GmailError(f"could not reach Gmail IMAP ({IMAP_HOST}): {exc}") from exc
    try:
        auth = f"user={address}\x01auth=Bearer {token}\x01\x01"
        conn.authenticate("XOAUTH2", lambda challenge: b"" if challenge else auth.encode())
    except imaplib.IMAP4.error as exc:
        _shutdown(conn)
        raise GmailError("Google rejected OAuth access; reconnect this Gmail account.") from exc
    except OSError as exc:
        _shutdown(conn)
        raise GmailError(f"could not sign in to Gmail IMAP: {exc}") from exc
    return conn


def _connect_smtp(address: str, token: str) -> smtplib.SMTP:
    """An authenticated SMTP session for `address`."""
    try:
        conn = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=_TIMEOUT)
    except (OSError, smtplib.SMTPException) as exc:
        raise GmailError(f"could not reach Gmail SMTP ({SMTP_HOST}): {exc}") from exc
    try:
        auth = f"user={address}\x01auth=Bearer {token}\x01\x01"
        conn.auth("XOAUTH2", lambda challenge=None: "" if challenge else auth)
    except smtplib.SMTPAuthenticationError as exc:
        _quit(conn)
        raise GmailError("Google rejected OAuth access; reconnect this Gmail account.") from exc
    except (smtplib.SMTPException, OSError) as exc:
        _quit(conn)
        raise GmailError(f"could not sign in to Gmail SMTP: {exc}") from exc
    return conn


def _shutdown(conn: Any) -> None:
    try:
        conn.logout()
    except Exception:
        pass


def _quit(conn: Any) -> None:
    try:
        conn.quit()
    except Exception:
        pass


def _smtp_send(address: str, token: str, msg: EmailMessage) -> None:
    conn = _connect_smtp(address, token)
    try:
        conn.send_message(msg, from_addr=address)
    except smtplib.SMTPRecipientsRefused as exc:
        who = ", ".join(str(k) for k in exc.recipients) or "the recipient"
        raise GmailError(f"Gmail refused {who}") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise GmailError(f"Gmail SMTP send failed: {exc}") from exc
    finally:
        _quit(conn)


# -- IMAP session ----------------------------------------------------------------


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return raw


def _text(data: Any) -> str:
    if isinstance(data, (list, tuple)):
        data = data[0] if data else b""
    if isinstance(data, tuple):
        data = data[0]
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return str(data or "")


_LIST_LINE = re.compile(rb'^\((?P<attrs>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.*)$')
_META_UID = re.compile(rb"\bUID (\d+)")
_META_MSGID = re.compile(rb"X-GM-MSGID (\d+)")
_META_THRID = re.compile(rb"X-GM-THRID (\d+)")
_META_LABELS = re.compile(rb'X-GM-LABELS \(((?:[^()"]|"(?:\\.|[^"\\])*")*)\)')
_LABEL_TOKEN = re.compile(rb'"(?:\\.|[^"\\])*"|[^\s()]+')
_APPENDUID = re.compile(rb"APPENDUID \d+ (\d+)")


def _labels(line: bytes) -> list[str] | None:
    match = _META_LABELS.search(line)
    if match is None:
        return None
    return [_unquote(_text(token)) for token in _LABEL_TOKEN.findall(match.group(1))]


def _meta(line: bytes) -> dict[str, Any]:
    uid = _META_UID.search(line)
    msgid = _META_MSGID.search(line)
    thrid = _META_THRID.search(line)
    return {
        "uid": int(uid.group(1)) if uid else 0,
        "msgid": int(msgid.group(1)) if msgid else None,
        "thrid": int(thrid.group(1)) if thrid else None,
        "labels": _labels(line),
        "data": b"",
    }


def _parse_fetch(data: list[Any]) -> list[dict[str, Any]]:
    """Rows from an imaplib FETCH response: `(meta, literal)` tuples carry a
    body/header literal, bare `b'N (...)'` lines carry only ids, and the
    labels may follow the literal in a separate response fragment."""
    rows: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[0], bytes):
            row = _meta(item[0])
            row["data"] = bytes(item[1]) if isinstance(item[1], (bytes, bytearray)) else b""
            rows.append(row)
        elif isinstance(item, bytes):
            line = item.strip()
            if line[:1].isdigit() and _META_UID.search(line):
                rows.append(_meta(line))
            elif rows and (labels := _labels(line)) is not None:
                rows[-1]["labels"] = labels
    return rows


class _Mailbox:
    """One logged-in IMAP session, with Gmail's folder roles resolved."""

    def __init__(self, conn: Any, address: str) -> None:
        self.conn = conn
        self.address = address
        self._folders: list[dict[str, Any]] | None = None

    def folders(self) -> list[dict[str, Any]]:
        if self._folders is not None:
            return self._folders
        typ, rows = self.conn.list()
        if typ != "OK":
            raise GmailError(f"Gmail folder listing failed: {_text(rows)}")
        out: list[dict[str, Any]] = []
        for row in rows or []:
            if isinstance(row, tuple) and len(row) >= 2:
                meta = row[0] if isinstance(row[0], bytes) else b""
                literal = row[1] if isinstance(row[1], bytes) else b""
                m = re.match(rb"^\((?P<attrs>[^)]*)\)", meta)
                attrs = m.group("attrs") if m else b""
                name = literal.decode("utf-8", "replace")
                out.append({"attrs": _attrs(attrs), "name": name, "arg": _quote(name)})
                continue
            if not isinstance(row, bytes):
                continue
            m = _LIST_LINE.match(row.strip())
            if not m:
                continue
            raw = m.group("name").decode("utf-8", "replace").strip()
            if not raw:
                continue
            name = _unquote(raw)
            arg = raw if raw.startswith('"') else _quote(name)
            out.append({"attrs": _attrs(m.group("attrs")), "name": name, "arg": arg})
        self._folders = out
        return out

    def special(self, attr: str, fallback: str) -> str:
        want = attr.lower()
        for folder in self.folders():
            if want in folder["attrs"]:
                return str(folder["arg"])
        return _quote(fallback)

    def all_mail(self) -> str:
        return self.special("\\all", _ALL_MAIL_FALLBACK)

    def drafts(self) -> str:
        return self.special("\\drafts", _DRAFTS_FALLBACK)

    def select(self, arg: str, *, readonly: bool = True) -> None:
        typ, data = self.conn.select(arg, readonly=readonly)
        if typ != "OK":
            raise GmailError(f"could not open Gmail folder {arg}: {_text(data)}")

    def search(self, *criteria: str, literal: str | None = None) -> list[int]:
        if literal is not None:
            self.conn.literal = literal.encode("utf-8")
        try:
            typ, data = self.conn.uid("SEARCH", *criteria)
        except imaplib.IMAP4.error as exc:
            raise GmailError(f"Gmail search failed: {exc}") from exc
        if typ != "OK":
            raise GmailError(f"Gmail search failed: {_text(data)}")
        out: list[int] = []
        for chunk in data or []:
            if isinstance(chunk, bytes):
                out.extend(int(tok) for tok in chunk.split() if tok.isdigit())
        return sorted(set(out))

    def search_gmail(self, query: str) -> list[int]:
        """Gmail search syntax (`from:ada newer_than:7d has:attachment`)
        through the X-GM-RAW extension, sent as a UTF-8 literal so quoting
        and non-ASCII never need escaping."""
        return self.search("CHARSET", "UTF-8", "X-GM-RAW", literal=query)

    def fetch(self, uids: list[int], items: str) -> list[dict[str, Any]]:
        if not uids:
            return []
        try:
            typ, data = self.conn.uid("FETCH", ",".join(str(u) for u in uids), items)
        except imaplib.IMAP4.error as exc:
            raise GmailError(f"Gmail fetch failed: {exc}") from exc
        if typ != "OK":
            raise GmailError(f"Gmail fetch failed: {_text(data)}")
        return _parse_fetch(list(data or []))

    def append(self, folder_arg: str, raw: bytes, flags: str = "\\Draft") -> int | None:
        try:
            typ, data = self.conn.append(
                folder_arg, flags, imaplib.Time2Internaldate(time.time()), raw
            )
        except imaplib.IMAP4.error as exc:
            raise GmailError(f"Gmail refused the draft: {exc}") from exc
        if typ != "OK":
            raise GmailError(f"Gmail refused the draft: {_text(data)}")
        for chunk in data or []:
            if isinstance(chunk, bytes):
                m = _APPENDUID.search(chunk)
                if m:
                    return int(m.group(1))
        return None

    def delete(self, uid: int) -> None:
        typ, data = self.conn.uid("STORE", str(uid), "+FLAGS", "(\\Deleted)")
        if typ != "OK":
            raise GmailError(f"could not delete the draft: {_text(data)}")
        try:
            self.conn.expunge()
        except imaplib.IMAP4.error as exc:
            raise GmailError(f"could not delete the draft: {exc}") from exc


def _attrs(raw: bytes) -> frozenset[str]:
    return frozenset(tok.decode("utf-8", "replace").lower() for tok in raw.split())


@contextmanager
def _mailbox(address: str, token: str) -> Iterator[_Mailbox]:
    conn = _connect_imap(address, token)
    try:
        yield _Mailbox(conn, address)
    finally:
        _shutdown(conn)


# -- message formatting ----------------------------------------------------------


def _hex(value: int | None) -> str:
    return format(int(value), "x") if value else ""


def _parse_id(value: Any) -> int | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def _parse_message(raw: bytes) -> EmailMessage:
    msg = email.message_from_bytes(raw or b"", policy=email.policy.default)
    return msg  # type: ignore[return-value]


def _hdr(msg: EmailMessage, name: str) -> str:
    try:
        value = msg.get(name)
    except Exception:  # a malformed header must not hide the message
        value = None
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _fmt_headers(
    msg: EmailMessage, msgid: int | None, thrid: int | None,
    labels: list[str] | None = None,
) -> str:
    bits = [
        f"- id={_hex(msgid) or '?'}",
        f"thread={_hex(thrid)}" if thrid else "",
        _hdr(msg, "from"),
        f"to={_hdr(msg, 'to') or '?'}",
        f"labels={json.dumps(labels)}" if labels is not None else "labels=unknown",
        _hdr(msg, "date"),
        _hdr(msg, "subject") or "(no subject)",
    ]
    return " · ".join(b for b in bits if b)


def _part_text(part: Any) -> str:
    try:
        content = part.get_content()
    except Exception:
        content = None
    if isinstance(content, str):
        return content
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(part.get_content_charset() or "utf-8", "replace")
    return ""


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _body_text(msg: EmailMessage, limit: int = _MAX_BODY) -> str:
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        part = None
    if part is None:
        return ""
    text = _part_text(part)
    if part.get_content_type() == "text/html":
        text = _strip_html(text)
    return text[:limit]


def _attachments(msg: EmailMessage) -> list[dict[str, Any]]:
    """Real attachments: leaf parts carrying a filename. `attachment_id` is
    the 1-based position, the handle gmail_read_attachment accepts."""
    out: list[dict[str, Any]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        try:
            filename = str(part.get_filename() or "").strip()
        except Exception:
            filename = ""
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        blob = payload if isinstance(payload, bytes) else b""
        out.append(
            {
                "filename": filename,
                "mime": part.get_content_type(),
                "size": len(blob),
                "attachment_id": str(len(out) + 1),
                "blob": blob,
            }
        )
    return out


def _fmt_attachments(atts: list[dict[str, Any]], message_id: str) -> str:
    # Named by suffix, not the literal gmail_read_attachment: with several
    # Gmail accounts the runtime namespaces tools to gmail_<account>_*.
    lines = [
        f"attachments ({len(atts)}) — read one with this connector's "
        f"read_attachment tool (message_id={message_id}, filename=...):"
    ]
    for att in atts:
        lines.append(
            f"- {att['filename']} · {att['mime'] or 'unknown type'} · {att['size']} bytes"
            f" · attachment_id={att['attachment_id']}"
        )
    return "\n".join(lines)


def _limit(args: dict[str, Any]) -> int:
    try:
        n = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        n = 10
    return max(1, min(n, _MAX_RESULTS))


def _compose(address: str, to: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = address
    msg["To"] = to
    if subject:
        msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=address.rsplit("@", 1)[-1] or None)
    msg.set_content(body or "")
    return msg


_FULL = "(X-GM-MSGID X-GM-THRID X-GM-LABELS BODY.PEEK[])"
_LISTING = "(X-GM-MSGID X-GM-THRID X-GM-LABELS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])"


# -- tools ---------------------------------------------------------------------


def _search_threads(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    query = str(args.get("query") or "in:inbox").strip() or "in:inbox"
    try:
        address, password = _account(ctx)
        with _mailbox(address, password) as mb:
            mb.select(mb.all_mail())
            uids = mb.search_gmail(query)[-_limit(args) :]
            rows = mb.fetch(uids, _LISTING)
    except GmailError as exc:
        return f"error: {exc}"
    rows.sort(key=lambda r: r["uid"], reverse=True)
    lines = [_fmt_headers(_parse_message(r["data"]), r["msgid"], r["thrid"], r["labels"]) for r in rows]
    return "\n".join(lines) if lines else "(no messages matched)"


def _get_thread(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    tid = _parse_id(args.get("thread_id") or args.get("id"))
    if tid is None:
        return "error: gmail_get_thread needs 'thread_id' (the hex id from gmail_search_threads)"
    try:
        address, password = _account(ctx)
        with _mailbox(address, password) as mb:
            mb.select(mb.all_mail())
            rows = mb.fetch(mb.search("X-GM-THRID", str(tid)), _FULL)
    except GmailError as exc:
        return f"error: {exc}"
    if not rows:
        return f"error: no thread {_hex(tid)}"
    rows.sort(key=lambda r: r["uid"])
    parts: list[str] = [f"thread {_hex(tid)} · {len(rows)} message(s)"]
    for row in rows:
        msg = _parse_message(row["data"])
        parts.append(_fmt_headers(msg, row["msgid"], row["thrid"], row["labels"]))
        body = _body_text(msg)
        if body:
            parts.append(body.strip())
        atts = _attachments(msg)
        if atts:
            parts.append(_fmt_attachments(atts, _hex(row["msgid"]) or "?"))
        if body or atts:
            parts.append("---")
    return "\n".join(parts).rstrip("-")


def _fetch_message(mb: _Mailbox, mid: int) -> dict[str, Any] | None:
    mb.select(mb.all_mail())
    rows = mb.fetch(mb.search("X-GM-MSGID", str(mid)), _FULL)
    return rows[0] if rows else None


def _get_message(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    mid = _parse_id(args.get("message_id") or args.get("id"))
    if mid is None:
        return "error: gmail_get_message needs 'message_id' (the hex id from gmail_search_threads)"
    try:
        address, password = _account(ctx)
        with _mailbox(address, password) as mb:
            row = _fetch_message(mb, mid)
    except GmailError as exc:
        return f"error: {exc}"
    if row is None:
        return f"error: no message {_hex(mid)}"
    msg = _parse_message(row["data"])
    text = _fmt_headers(msg, row["msgid"], row["thrid"], row["labels"]).lstrip("- ").strip()
    body = _body_text(msg)
    if body:
        text = f"{text}\n\n{body}".strip()
    atts = _attachments(msg)
    if atts:
        text = f"{text}\n\n{_fmt_attachments(atts, _hex(mid))}"
    return text


def _safe_filename(name: str) -> str:
    # Sanitize stem and extension separately: a fully non-ASCII stem
    # ("报告.pdf") must not collapse into its bare extension, and the
    # extension must survive so type detection and the saved path keep it.
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "attachment"
    ext = re.sub(r"[^A-Za-z0-9]+", "", ext)
    safe = f"{stem}.{ext}" if ext else stem
    return safe[:120]


def _is_texty(mime: str, filename: str) -> bool:
    low = mime.lower()
    return low.startswith("text/") or low in _TEXT_MIMES or filename.lower().endswith(_TEXT_EXTS)


def _read_attachment(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    mid = _parse_id(args.get("message_id") or args.get("id"))
    if mid is None:
        return "error: gmail_read_attachment needs 'message_id' (the hex id from the listing)"
    want_name = str(args.get("filename") or "").strip()
    attachment_id = str(args.get("attachment_id") or "").strip()
    try:
        address, password = _account(ctx)
        with _mailbox(address, password) as mb:
            row = _fetch_message(mb, mid)
    except GmailError as exc:
        return f"error: {exc}"
    if row is None:
        return f"error: no message {_hex(mid)}"
    hexid = _hex(mid)
    atts = _attachments(_parse_message(row["data"]))
    if attachment_id:
        meta = next((a for a in atts if a["attachment_id"] == attachment_id), None)
        if meta is None:
            ids = ", ".join(a["attachment_id"] for a in atts) or "(none)"
            return f"error: message {hexid} has no attachment_id {attachment_id!r}; it has: {ids}"
    elif want_name:
        meta = next((a for a in atts if a["filename"].lower() == want_name.lower()), None)
        if meta is None:
            names = ", ".join(a["filename"] for a in atts) or "(none)"
            return f"error: message {hexid} has no attachment named {want_name!r}; it has: {names}"
    elif len(atts) == 1:
        meta = atts[0]
    elif not atts:
        return f"error: message {hexid} has no attachments"
    else:
        names = ", ".join(a["filename"] for a in atts)
        return f"error: message {hexid} has several attachments ({names}); pass 'filename'"
    blob: bytes = meta["blob"]
    if len(blob) > _MAX_ATTACHMENT:
        return (
            f"error: attachment {meta['filename']!r} is {len(blob)} bytes, "
            f"over the {_MAX_ATTACHMENT}-byte limit"
        )
    if not blob:
        return f"error: Gmail returned no data for attachment {meta['filename']!r}"
    original = str(meta["filename"])
    dest_dir = ctx.paths.workspace / "email-attachments"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{_safe_filename(hexid)[:16]}-{_safe_filename(original)}"
    dest.write_bytes(blob)
    mime = str(meta["mime"] or "")
    head = f"{original} · {mime or 'unknown type'} · {len(blob)} bytes · saved to {dest}"
    if (
        mime.lower() == "application/pdf"
        or original.lower().endswith(".pdf")
        or blob.startswith(b"%PDF")
    ):
        text = pdftext.extract_text(blob, _MAX_ATTACHMENT_TEXT)
        if text:
            return f"{head}\n\n{text}"
        return (
            f"{head}\n(no extractable text — likely a scanned/image PDF; "
            "the saved file is available to other tools)"
        )
    if _is_texty(mime, original):
        text = blob.decode("utf-8", "replace")
        if len(text) > _MAX_ATTACHMENT_TEXT:
            text = text[:_MAX_ATTACHMENT_TEXT].rstrip() + "\n[truncated]"
        return f"{head}\n\n{text}"
    if mime.lower().startswith("image/"):
        return f"{head}\n(image saved — use post_image with that path to show it in chat)"
    return f"{head}\n(binary attachment saved; open the file with other tools)"


_ROLE_ATTRS = ("\\all", "\\drafts", "\\sent", "\\trash", "\\junk", "\\flagged", "\\important")


def _list_labels(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    try:
        address, password = _account(ctx)
        with _mailbox(address, password) as mb:
            folders = mb.folders()
    except GmailError as exc:
        return f"error: {exc}"
    lines = []
    for folder in folders:
        if "\\noselect" in folder["attrs"]:
            continue
        role = next((a.lstrip("\\") for a in _ROLE_ATTRS if a in folder["attrs"]), "")
        lines.append(f"- {folder['name']}" + (f" · {role}" if role else ""))
    return "\n".join(lines) if lines else "(no labels)"


def _list_drafts(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    try:
        address, password = _account(ctx)
        with _mailbox(address, password) as mb:
            mb.select(mb.drafts())
            uids = mb.search("ALL")[-_limit(args) :]
            rows = mb.fetch(uids, _LISTING)
    except GmailError as exc:
        return f"error: {exc}"
    if not rows:
        return "(no drafts)"
    rows.sort(key=lambda r: r["uid"], reverse=True)
    lines = []
    for row in rows:
        msg = _parse_message(row["data"])
        lines.append(
            f"- draft={_hex(row['msgid']) or '?'} · to={_hdr(msg, 'to') or '?'} · "
            f"{_hdr(msg, 'subject') or '(no subject)'}"
        )
    return "\n".join(lines)


def _create_draft(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    to = str(args.get("to") or "").strip()
    subject = str(args.get("subject") or "").strip()
    body = str(args.get("body") or args.get("text") or "")
    if not to:
        return "error: gmail_create_draft needs 'to'"
    try:
        address, password = _account(ctx)
        msg = _compose(address, to, subject, body)
        with _mailbox(address, password) as mb:
            drafts = mb.drafts()
            uid = mb.append(drafts, msg.as_bytes())
            draft_id = ""
            if uid:
                mb.select(drafts)
                rows = mb.fetch([uid], "(X-GM-MSGID)")
                if rows and rows[0]["msgid"]:
                    draft_id = _hex(rows[0]["msgid"])
    except GmailError as exc:
        return f"error: {exc}"
    return (
        f"ok: draft {draft_id or '(saved; id unavailable)'} to {to} · "
        f"{subject or '(no subject)'} · in {address}'s Drafts"
    )


def _send(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    to = str(args.get("to") or "").strip()
    subject = str(args.get("subject") or "").strip()
    body = str(args.get("body") or args.get("text") or "")
    if not to:
        return "error: gmail_send needs 'to'"
    try:
        address, password = _account(ctx)
        msg = _compose(address, to, subject, body)
        _smtp_send(address, password, msg)
    except GmailError as exc:
        return f"error: {exc}"
    return f"ok: sent to {to} from {address} · {subject or '(no subject)'} · {msg['Message-ID']}"


def _send_draft(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    did = _parse_id(args.get("draft_id") or args.get("id"))
    if did is None:
        return "error: gmail_send_draft needs 'draft_id' (the hex id from gmail_list_drafts)"
    try:
        address, password = _account(ctx)
        with _mailbox(address, password) as mb:
            drafts = mb.drafts()
            mb.select(drafts, readonly=False)
            rows = mb.fetch(mb.search("X-GM-MSGID", str(did)), _FULL)
            if not rows:
                return (
                    f"error: no draft {_hex(did)}; nothing was sent by this call. "
                    "Saving an edit can change a draft's message ID. Use list_drafts "
                    "to find the current ID, then get_message to review its recipient, "
                    "subject and body before requesting a send. Do not substitute or "
                    "resend a message whose previous send outcome is uncertain."
                )
            row = rows[0]
            msg = _parse_message(row["data"])
            to = _hdr(msg, "to")
            if not to:
                return f"error: draft {_hex(did)} has no recipient; add 'To' first"
            if not _hdr(msg, "from"):
                msg["From"] = address
            if not _hdr(msg, "message-id"):
                msg["Message-ID"] = email.utils.make_msgid(domain=address.rsplit("@", 1)[-1])
            if not _hdr(msg, "date"):
                msg["Date"] = email.utils.formatdate(localtime=True)
            _smtp_send(address, password, msg)
            # Sent: Gmail files the SMTP copy under Sent itself; the draft
            # would otherwise linger as a duplicate.
            try:
                mb.delete(row["uid"])
            except (GmailError, imaplib.IMAP4.error, OSError):
                return (
                    f"ok: sent draft {_hex(did)} to {to} from {address}; "
                    "Gmail accepted the send, but draft cleanup failed. "
                    "Do not resend. Check Sent and the remaining draft separately; "
                    "SMTP acceptance alone does not confirm recipient delivery."
                )
    except GmailError as exc:
        return f"error: {exc}"
    return f"ok: sent draft {_hex(did)} to {to} from {address} · {_hdr(msg, 'subject') or '(no subject)'}"


def tools() -> list[ConnectorTool]:
    return [
        ConnectorTool(
            ToolSpec(
                name="gmail_search_threads",
                description=(
                    "Search this Gmail mailbox with Gmail search syntax "
                    "(from:, to:, subject:, newer_than:7d, has:attachment, in:inbox, "
                    "is:unread). Returns message ids, thread ids, recipients and labels, "
                    "newest first. A subject match may be a draft; check labels before "
                    "claiming a message is in Inbox or Sent."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Gmail search query (default in:inbox)",
                        },
                        "limit": {"type": "integer", "description": "max results (1-20)"},
                    },
                },
            ),
            _search_threads,
        ),
        ConnectorTool(
            ToolSpec(
                name="gmail_get_thread",
                description="Read a whole Gmail thread by thread_id, oldest message first.",
                parameters={
                    "type": "object",
                    "properties": {
                        "thread_id": {"type": "string", "description": "Gmail thread id"},
                    },
                    "required": ["thread_id"],
                },
            ),
            _get_thread,
        ),
        ConnectorTool(
            ToolSpec(
                name="gmail_get_message",
                description="Read one Gmail message by message_id, including the body.",
                parameters={
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string", "description": "Gmail message id"},
                    },
                    "required": ["message_id"],
                },
            ),
            _get_message,
        ),
        ConnectorTool(
            ToolSpec(
                name="gmail_read_attachment",
                description=(
                    "Read a Gmail attachment (PDF, text, CSV, ...). Extracts and "
                    "returns the text of PDFs and text files; every attachment is "
                    "also saved under the workspace email-attachments folder for "
                    "other tools. Pass message_id plus the filename shown by "
                    "gmail_get_message / gmail_get_thread."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "message_id": {"type": "string", "description": "Gmail message id"},
                        "filename": {
                            "type": "string",
                            "description": "attachment filename from the message listing",
                        },
                        "attachment_id": {
                            "type": "string",
                            "description": (
                                "attachment number from the message listing "
                                "(alternative to filename)"
                            ),
                        },
                    },
                    "required": ["message_id"],
                },
            ),
            _read_attachment,
        ),
        ConnectorTool(
            ToolSpec(
                name="gmail_list_labels",
                description="List Gmail labels (Inbox, Sent, Drafts, user labels).",
                parameters={"type": "object", "properties": {}},
            ),
            _list_labels,
        ),
        ConnectorTool(
            ToolSpec(
                name="gmail_list_drafts",
                description="List Gmail drafts with their current message IDs, newest first. Saving edits can change an ID.",
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer"},
                    },
                },
            ),
            _list_drafts,
        ),
        ConnectorTool(
            ToolSpec(
                name="gmail_create_draft",
                description=(
                    "Create a Gmail draft. Does not send. Pass to, subject, and body. "
                    "Use gmail_send to deliver now, or gmail_send_draft with this id."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "recipient email"},
                        "subject": {"type": "string"},
                        "body": {"type": "string", "description": "plain-text body"},
                    },
                    "required": ["to"],
                },
            ),
            _create_draft,
        ),
        ConnectorTool(
            ToolSpec(
                name="gmail_send",
                description=(
                    "Send an email now from this Gmail account. Delivers to the "
                    "recipient. Pass to, subject, and body. Use gmail_create_draft "
                    "if the user asked to stage, not send."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "recipient email"},
                        "subject": {"type": "string"},
                        "body": {"type": "string", "description": "plain-text body"},
                    },
                    "required": ["to"],
                },
            ),
            _send,
        ),
        ConnectorTool(
            ToolSpec(
                name="gmail_send_draft",
                description=(
                    "Send an existing Gmail draft by draft_id. The draft is removed "
                    "from Drafts once Gmail accepts the send. Saving edits can change "
                    "the draft ID: use list_drafts and review the recipient, subject and "
                    "body with get_message before sending. Never automatically retry "
                    "an uncertain send or a successful send with failed draft cleanup."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "draft_id": {
                            "type": "string",
                            "description": "id from gmail_create_draft or gmail_list_drafts",
                        },
                    },
                    "required": ["draft_id"],
                },
            ),
            _send_draft,
        ),
    ]
