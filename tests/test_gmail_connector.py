"""Gmail over IMAP/SMTP with OAuth: one grant per inbox.

The fakes below speak imaplib's response shapes (SPECIAL-USE LIST rows,
`(meta, literal)` FETCH tuples with X-GM-* ids, APPENDUID, the literal
that carries an X-GM-RAW query) so the runtime's parsing is what is under
test, not a mock of the runtime.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import re
import smtplib
from email.message import EmailMessage

import pytest

from connectors import gmail
from connectors.base import ConnectorContext
from connectors.registry import account_slug, tool_prefixes, tools_for_bot
from harness.connectors import Connectors
from harness.paths import HarnessPaths

ADDRESS = "ada@example.com"
APP_PASSWORD = "abcd efgh ijkl mnop"  # as Google displays it


@pytest.fixture
def paths(tmp_path):
    return HarnessPaths(home=tmp_path)


@pytest.fixture
def gmail_ctx(paths):
    record = Connectors(paths).add("gmail", "Gmail", {"email": ADDRESS}, secret=APP_PASSWORD)
    from harness.mcp_oauth import save_tokens

    save_tokens(paths, record["id"], {"access_token": "test-access-token"})
    return ConnectorContext(paths=paths, bot="atlas", record=record)


# -- imaplib / smtplib shaped fakes --------------------------------------------


def _raw(
    *,
    frm: str = "Ada <ada@example.com>",
    to: str = "bob@example.com",
    subject: str = "Hi",
    body: str = "hello",
    html: str | None = None,
    date: str = "Sun, 30 Aug 2026 10:00:00 +0000",
    attachments: list[tuple[str, str, bytes]] | None = None,
    message_id: str | None = "<m1@example.com>",
) -> bytes:
    msg = EmailMessage()
    msg["From"] = frm
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = date
    if message_id:
        msg["Message-ID"] = message_id
    if html is not None:
        msg.set_content(body)
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(body)
    for filename, mime, blob in attachments or []:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(blob, maintype=maintype, subtype=subtype, filename=filename)
    return msg.as_bytes()


def _header_block(raw: bytes, fields: list[str]) -> bytes:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    lines = []
    for field in fields:
        value = msg.get(field)
        if value is not None:
            lines.append(f"{field.title()}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")


class FakeIMAP:
    """Gmail as imaplib sees it: SPECIAL-USE folders, X-GM-* ids on FETCH,
    UID SEARCH by X-GM-RAW literal / X-GM-MSGID / X-GM-THRID / ALL, APPEND
    with APPENDUID, STORE \\Deleted + EXPUNGE."""

    ALL = "[Gmail]/All Mail"
    DRAFTS = "[Gmail]/Drafts"

    def __init__(self) -> None:
        self.attrs = {
            "INBOX": "\\HasNoChildren",
            "[Gmail]": "\\HasChildren \\Noselect",
            self.ALL: "\\All \\HasNoChildren",
            self.DRAFTS: "\\Drafts \\HasNoChildren",
            "[Gmail]/Sent Mail": "\\HasNoChildren \\Sent",
            "Receipts": "\\HasNoChildren",
        }
        self.folders: dict[str, list[dict]] = {n: [] for n in self.attrs if n != "[Gmail]"}
        self.selected: str | None = None
        self.readonly = True
        self.literal: bytes | None = None
        self.searches: list[tuple[tuple[str, ...], bytes | None]] = []
        self.appends: list[tuple[str, str, bytes]] = []
        self.logged_out = False
        self._next_uid = 1
        self._next_msgid = 0x1000
        self.raw_matcher = None  # callable(query, message dict) -> bool

    def add(self, folder: str, raw: bytes, msgid: int, thrid: int | None = None) -> int:
        uid = self._next_uid
        self._next_uid += 1
        self.folders[folder].append(
            {"uid": uid, "msgid": msgid, "thrid": thrid or msgid, "raw": raw, "flags": set()}
        )
        return uid

    # imaplib surface -----------------------------------------------------

    def list(self):
        rows = []
        for name, attrs in self.attrs.items():
            quoted = f'"{name}"' if (" " in name or "[" in name) else name
            rows.append(f'({attrs}) "/" {quoted}'.encode())
        return "OK", rows

    def select(self, mailbox, readonly=False):
        name = mailbox.strip('"')
        if name not in self.folders:
            return "NO", [b"[NONEXISTENT] Unknown Mailbox: " + name.encode()]
        self.selected = name
        self.readonly = readonly
        return "OK", [str(len(self.folders[name])).encode()]

    def _msgs(self) -> list[dict]:
        assert self.selected is not None, "SELECT first"
        return self.folders[self.selected]

    def uid(self, command, *args):
        command = command.upper()
        if command == "SEARCH":
            literal, self.literal = self.literal, None
            crit = [a for a in args if a is not None]
            self.searches.append((tuple(crit), literal))
            msgs = self._msgs()
            if "X-GM-RAW" in crit:
                assert crit[:2] == ["CHARSET", "UTF-8"]
                query = (literal or b"").decode("utf-8")
                hits = [m for m in msgs if self.raw_matcher is None or self.raw_matcher(query, m)]
            elif "X-GM-MSGID" in crit:
                want = int(crit[crit.index("X-GM-MSGID") + 1])
                hits = [m for m in msgs if m["msgid"] == want]
            elif "X-GM-THRID" in crit:
                want = int(crit[crit.index("X-GM-THRID") + 1])
                hits = [m for m in msgs if m["thrid"] == want]
            elif crit == ["ALL"]:
                hits = msgs
            else:
                raise AssertionError(crit)
            return "OK", [" ".join(str(m["uid"]) for m in hits).encode()]
        if command == "FETCH":
            wanted = {int(u) for u in str(args[0]).split(",")}
            items = str(args[1])
            out: list = []
            for m in self._msgs():
                if m["uid"] not in wanted:
                    continue
                meta = f"{m['uid']} (X-GM-MSGID {m['msgid']} X-GM-THRID {m['thrid']} UID {m['uid']}"
                if "BODY.PEEK[]" in items:
                    payload = m["raw"]
                    out.append(((meta + f" BODY[] {{{len(payload)}}}").encode(), payload))
                    out.append(b")")
                elif "HEADER.FIELDS" in items:
                    fields = re.search(r"HEADER\.FIELDS \(([^)]*)\)", items).group(1).split()
                    payload = _header_block(m["raw"], fields)
                    head = meta + f" BODY[HEADER.FIELDS ({' '.join(fields)})] {{{len(payload)}}}"
                    out.append((head.encode(), payload))
                    out.append(b")")
                else:
                    out.append((meta + ")").encode())
            return "OK", out
        if command == "STORE":
            assert not self.readonly, "STORE on a read-only SELECT"
            uid = int(args[0])
            flag = str(args[2]).strip("()")
            for m in self._msgs():
                if m["uid"] == uid:
                    m["flags"].add(flag)
            return "OK", [b""]
        raise AssertionError(command)

    def append(self, mailbox, flags, date_time, message):
        name = mailbox.strip('"')
        self.appends.append((name, flags, message))
        msgid = self._next_msgid
        self._next_msgid += 1
        uid = self.add(name, message, msgid)
        return "OK", [f"[APPENDUID 605713573 {uid}] (Success)".encode()]

    def expunge(self):
        msgs = self._msgs()
        gone = [m["uid"] for m in msgs if "\\Deleted" in m["flags"]]
        msgs[:] = [m for m in msgs if "\\Deleted" not in m["flags"]]
        return "OK", [str(u).encode() for u in gone]

    def logout(self):
        self.logged_out = True
        return "BYE", [b"LOGOUT Requested"]


class FakeSMTP:
    def __init__(self) -> None:
        self.sent: list[tuple[str | None, EmailMessage]] = []
        self.quit_called = False

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.sent.append((from_addr, msg))
        return {}

    def quit(self):
        self.quit_called = True


@pytest.fixture
def imap(monkeypatch):
    fake = FakeIMAP()
    fake.logins = []

    def connect(address, password):
        fake.logins.append((address, password))
        return fake

    monkeypatch.setattr(gmail, "_connect_imap", connect)
    return fake


@pytest.fixture
def smtp(monkeypatch):
    fake = FakeSMTP()
    fake.logins = []

    def connect(address, password):
        fake.logins.append((address, password))
        return fake

    monkeypatch.setattr(gmail, "_connect_smtp", connect)
    return fake


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("must not open a connection")

    monkeypatch.setattr(gmail, "_connect_imap", refuse)
    monkeypatch.setattr(gmail, "_connect_smtp", refuse)


# -- credentials ---------------------------------------------------------------


def test_login_uses_the_address_and_oauth_token(gmail_ctx, imap):
    gmail._list_labels(gmail_ctx, {})
    assert imap.logins == [(ADDRESS, "test-access-token")]
    assert imap.logged_out is True


def test_missing_oauth_asks_to_connect(paths, no_network):
    record = Connectors(paths).add("gmail", "Gmail", {"email": ADDRESS})
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    out = gmail._list_labels(ctx, {})
    assert out.startswith("error:")
    assert "Connect this Google account" in out
    assert "request_secret" not in out


def test_missing_address_names_the_field(paths, no_network):
    record = Connectors(paths).add("gmail", "Gmail", secret=APP_PASSWORD)
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    out = gmail._search_threads(ctx, {})
    assert out.startswith("error:")
    assert "address" in out.lower()


def test_old_app_password_is_not_used(paths, imap):
    from harness.secrets import set_secret

    set_secret("GMAIL_APP_PASSWORD", "old-password", paths)
    record = Connectors(paths).add("gmail", "Gmail", {"email": ADDRESS}, secret="old-password")
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    assert "Connect this Google account" in gmail._list_labels(ctx, {})
    assert imap.logins == []


def test_imap_auth_failure_asks_to_reconnect(monkeypatch):
    class Rejecting:
        def __init__(self, host, port, timeout=None):
            assert (host, port) == (gmail.IMAP_HOST, gmail.IMAP_PORT)

        def authenticate(self, mechanism, callback):
            assert mechanism == "XOAUTH2"
            assert callback(b"") == b"user=ada@example.com\x01auth=Bearer wrong\x01\x01"
            assert callback(b"challenge") == b""
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")

        def logout(self):
            return "BYE", [b""]

    monkeypatch.setattr(imaplib, "IMAP4_SSL", Rejecting)
    with pytest.raises(gmail.GmailError) as exc:
        gmail._connect_imap(ADDRESS, "wrong")
    text = str(exc.value)
    assert "reconnect this Gmail account" in text
    assert "wrong" not in text


def test_smtp_auth_failure_asks_to_reconnect(monkeypatch):
    class Rejecting:
        def __init__(self, host, port, timeout=None):
            assert (host, port) == (gmail.SMTP_HOST, gmail.SMTP_PORT)

        def auth(self, mechanism, callback):
            assert mechanism == "XOAUTH2"
            assert callback() == "user=ada@example.com\x01auth=Bearer wrong\x01\x01"
            assert callback(b"challenge") == ""
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

        def quit(self):
            return None

    monkeypatch.setattr(smtplib, "SMTP_SSL", Rejecting)
    with pytest.raises(gmail.GmailError) as exc:
        gmail._connect_smtp(ADDRESS, "wrong")
    assert "reconnect this Gmail account" in str(exc.value)


def test_unreachable_imap_is_a_tool_error_not_a_crash(gmail_ctx, monkeypatch):
    def down(host, port, timeout=None):
        raise OSError("network unreachable")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", down)
    out = gmail._list_labels(gmail_ctx, {})
    assert out.startswith("error:")
    assert "could not reach Gmail IMAP" in out


# -- search / read -------------------------------------------------------------


def test_search_sends_gmail_syntax_as_a_utf8_literal(gmail_ctx, imap):
    imap.add(FakeIMAP.ALL, _raw(subject="Older"), msgid=0xA1)
    imap.add(FakeIMAP.ALL, _raw(subject="Newer", frm="Bob <bob@example.com>"), msgid=0xA2)
    out = gmail._search_threads(gmail_ctx, {"query": "from:bob newer_than:7d", "limit": 5})
    assert imap.searches == [(("CHARSET", "UTF-8", "X-GM-RAW"), b"from:bob newer_than:7d")]
    assert imap.selected == FakeIMAP.ALL and imap.readonly is True
    lines = out.splitlines()
    assert lines[0].startswith("- id=a2")  # newest first
    assert "thread=a2" in lines[0]
    assert "Bob" in lines[0] and "Newer" in lines[0]
    assert lines[1].startswith("- id=a1")


def test_search_defaults_to_the_inbox(gmail_ctx, imap):
    gmail._search_threads(gmail_ctx, {})
    assert imap.searches[0][1] == b"in:inbox"


def test_search_limit_keeps_the_newest(gmail_ctx, imap):
    for i in range(5):
        imap.add(FakeIMAP.ALL, _raw(subject=f"m{i}"), msgid=0xB0 + i)
    out = gmail._search_threads(gmail_ctx, {"limit": 2})
    assert [ln.split()[1] for ln in out.splitlines()] == ["id=b4", "id=b3"]


def test_search_with_no_hits(gmail_ctx, imap):
    assert gmail._search_threads(gmail_ctx, {"query": "from:nobody"}) == "(no messages matched)"


def test_get_message_decodes_plain_body(gmail_ctx, imap):
    imap.add(FakeIMAP.ALL, _raw(subject="Note", body="body text"), msgid=0xC1)
    out = gmail._get_message(gmail_ctx, {"message_id": "c1"})
    assert out.startswith("id=c1")
    assert "Note" in out
    assert "body text" in out
    assert imap.searches == [(("X-GM-MSGID", str(0xC1)), None)]


def test_get_message_prefers_plain_and_strips_html_otherwise(gmail_ctx, imap):
    imap.add(FakeIMAP.ALL, _raw(body="plain wins", html="<p>html <b>loses</b></p>"), msgid=0xC2)
    assert "plain wins" in gmail._get_message(gmail_ctx, {"message_id": "c2"})
    html_only = EmailMessage()
    html_only["From"] = "x@example.com"
    html_only["Subject"] = "H"
    html_only.set_content("<style>p{}</style><p>Hello &amp; <b>welcome</b></p>", subtype="html")
    imap.add(FakeIMAP.ALL, html_only.as_bytes(), msgid=0xC3)
    out = gmail._get_message(gmail_ctx, {"message_id": "c3"})
    assert "Hello & welcome" in out
    assert "<b>" not in out and "p{}" not in out


def test_get_message_unknown_or_malformed_id(gmail_ctx, imap):
    assert gmail._get_message(gmail_ctx, {"message_id": "c9"}) == "error: no message c9"
    out = gmail._get_message(gmail_ctx, {"message_id": "not-hex"})
    assert out.startswith("error:") and "message_id" in out
    assert gmail._get_message(gmail_ctx, {}).startswith("error:")


def test_get_thread_orders_oldest_first_and_lists_attachments(gmail_ctx, imap):
    imap.add(FakeIMAP.ALL, _raw(subject="Re: plan", body="second"), msgid=0xD2, thrid=0xD1)
    imap.add(FakeIMAP.ALL, _raw(subject="plan", body="first"), msgid=0xD1, thrid=0xD1)
    imap.add(
        FakeIMAP.ALL,
        _raw(subject="Re: plan", body="third", attachments=[("a.pdf", "application/pdf", b"%PDF")]),
        msgid=0xD3,
        thrid=0xD1,
    )
    imap.add(FakeIMAP.ALL, _raw(subject="other"), msgid=0xE1, thrid=0xE1)
    # fake uids are insertion order; the runtime sorts by uid (chronological)
    out = gmail._get_thread(gmail_ctx, {"thread_id": "d1"})
    assert out.startswith("thread d1 · 3 message(s)")
    assert out.index("second") < out.index("first") < out.index("third")
    assert "a.pdf" in out and "read_attachment" in out
    assert "other" not in out
    assert imap.searches == [(("X-GM-THRID", str(0xD1)), None)]


def test_get_thread_unknown(gmail_ctx, imap):
    assert gmail._get_thread(gmail_ctx, {"thread_id": "d9"}) == "error: no thread d9"
    assert gmail._get_thread(gmail_ctx, {}).startswith("error:")


def test_get_message_lists_attachments_with_ids(gmail_ctx, imap):
    imap.add(
        FakeIMAP.ALL,
        _raw(
            subject="Report",
            attachments=[
                ("report.pdf", "application/pdf", b"%PDF-1"),
                ("data.csv", "text/csv", b"a,b\n"),
            ],
        ),
        msgid=0xF1,
    )
    out = gmail._get_message(gmail_ctx, {"message_id": "f1"})
    assert "attachments (2)" in out
    assert "report.pdf" in out and "application/pdf" in out and "attachment_id=1" in out
    assert "data.csv" in out and "attachment_id=2" in out
    assert "read_attachment" in out


# -- attachments ---------------------------------------------------------------

_PDF_CONTENT = b"BT /F1 12 Tf 72 720 Td (Quarterly numbers look good) Tj ET"
_PDF = (
    b"%PDF-1.4\n1 0 obj\n<< /Length "
    + str(len(_PDF_CONTENT)).encode("ascii")
    + b" >>\nstream\n"
    + _PDF_CONTENT
    + b"\nendstream\nendobj\ntrailer\n%%EOF\n"
)


def _with(imap, atts, msgid=0x11):
    imap.add(FakeIMAP.ALL, _raw(subject="Report", body="see attached", attachments=atts), msgid)
    return format(msgid, "x")


def test_read_attachment_extracts_pdf_text_and_saves_file(gmail_ctx, imap):
    mid = _with(imap, [("report.pdf", "application/pdf", _PDF)])
    out = gmail._read_attachment(gmail_ctx, {"message_id": mid, "filename": "report.pdf"})
    assert "Quarterly numbers look good" in out
    assert "report.pdf" in out
    saved = list((gmail_ctx.paths.workspace / "email-attachments").iterdir())
    assert len(saved) == 1
    assert saved[0].read_bytes() == _PDF


def test_read_attachment_single_attachment_needs_no_filename(gmail_ctx, imap):
    mid = _with(imap, [("report.pdf", "application/pdf", _PDF)])
    assert "Quarterly numbers look good" in gmail._read_attachment(gmail_ctx, {"message_id": mid})


def test_read_attachment_by_attachment_id(gmail_ctx, imap):
    mid = _with(imap, [("a.pdf", "application/pdf", _PDF), ("b.csv", "text/csv", b"x,y\n1,2\n")])
    out = gmail._read_attachment(gmail_ctx, {"message_id": mid, "attachment_id": "2"})
    assert "1,2" in out
    out = gmail._read_attachment(gmail_ctx, {"message_id": mid, "attachment_id": "7"})
    assert out.startswith("error:") and "1, 2" in out


def test_read_attachment_scanned_pdf_reports_no_text(gmail_ctx, imap):
    scan = b"%PDF-1.4\nstream\n\x00\x01imagebytes\nendstream\n%%EOF"
    mid = _with(imap, [("scan.pdf", "application/pdf", scan)])
    out = gmail._read_attachment(gmail_ctx, {"message_id": mid, "filename": "scan.pdf"})
    assert not out.startswith("error:")
    assert "no extractable text" in out


def test_read_attachment_returns_text_files_verbatim(gmail_ctx, imap):
    mid = _with(imap, [("totals.csv", "text/csv", b"name,total\nada,42\n")])
    out = gmail._read_attachment(gmail_ctx, {"message_id": mid, "filename": "totals.csv"})
    assert "name,total" in out and "ada,42" in out


def test_read_attachment_unknown_filename_lists_what_exists(gmail_ctx, imap):
    mid = _with(imap, [("report.pdf", "application/pdf", _PDF)])
    out = gmail._read_attachment(gmail_ctx, {"message_id": mid, "filename": "nope.pdf"})
    assert out.startswith("error:") and "report.pdf" in out


def test_read_attachment_several_attachments_require_filename(gmail_ctx, imap):
    mid = _with(imap, [("a.pdf", "application/pdf", _PDF), ("b.pdf", "application/pdf", _PDF)])
    out = gmail._read_attachment(gmail_ctx, {"message_id": mid})
    assert out.startswith("error:") and "a.pdf" in out and "b.pdf" in out


def test_read_attachment_no_attachments_errors(gmail_ctx, imap):
    mid = _with(imap, [])
    out = gmail._read_attachment(gmail_ctx, {"message_id": mid})
    assert out.startswith("error:") and "no attachments" in out


def test_read_attachment_nonascii_filename_keeps_extension(gmail_ctx, imap):
    mid = _with(imap, [("报告.pdf", "application/octet-stream", _PDF)])
    out = gmail._read_attachment(gmail_ctx, {"message_id": mid, "filename": "报告.pdf"})
    assert "Quarterly numbers look good" in out
    saved = list((gmail_ctx.paths.workspace / "email-attachments").iterdir())
    assert len(saved) == 1 and saved[0].name.endswith(".pdf")


def test_read_attachment_nonascii_text_file_is_still_texty(gmail_ctx, imap):
    mid = _with(imap, [("数据.csv", "application/octet-stream", b"name,total\nada,42\n")])
    assert "ada,42" in gmail._read_attachment(
        gmail_ctx, {"message_id": mid, "filename": "数据.csv"}
    )


def test_read_attachment_needs_message_id(gmail_ctx, no_network):
    out = gmail._read_attachment(gmail_ctx, {"filename": "report.pdf"})
    assert out.startswith("error:") and "message_id" in out


def test_read_attachment_over_size_limit_refuses(gmail_ctx, imap, monkeypatch):
    monkeypatch.setattr(gmail, "_MAX_ATTACHMENT", 16)
    mid = _with(imap, [("huge.pdf", "application/pdf", _PDF)])
    out = gmail._read_attachment(gmail_ctx, {"message_id": mid, "filename": "huge.pdf"})
    assert out.startswith("error:") and "limit" in out
    assert not (gmail_ctx.paths.workspace / "email-attachments").exists()


def test_read_attachment_sanitizes_hostile_filename(gmail_ctx, imap):
    mid = _with(imap, [("../../etc/passwd.txt", "text/plain", b"plain text")])
    out = gmail._read_attachment(gmail_ctx, {"message_id": mid, "filename": "../../etc/passwd.txt"})
    assert "plain text" in out
    saved = list((gmail_ctx.paths.workspace / "email-attachments").iterdir())
    assert len(saved) == 1
    assert ".." not in saved[0].name and "/" not in saved[0].name


# -- labels / drafts / send ----------------------------------------------------


def test_list_labels_shows_folders_with_roles_and_skips_containers(gmail_ctx, imap):
    out = gmail._list_labels(gmail_ctx, {})
    assert "- INBOX" in out
    assert "- [Gmail]/All Mail · all" in out
    assert "- [Gmail]/Drafts · drafts" in out
    assert "- [Gmail]/Sent Mail · sent" in out
    assert "- Receipts" in out
    assert "- [Gmail]\n" not in out + "\n"


def test_localized_special_folders_are_found_by_attribute(gmail_ctx, imap):
    imap.attrs = {
        "INBOX": "\\HasNoChildren",
        "[Gmail]/Alle Nachrichten": "\\All \\HasNoChildren",
        "[Gmail]/Entw\xfcrfe": "\\Drafts \\HasNoChildren",
    }
    imap.folders = {n: [] for n in imap.attrs}
    imap.add("[Gmail]/Alle Nachrichten", _raw(subject="Hallo"), msgid=0x21)
    assert "Hallo" in gmail._search_threads(gmail_ctx, {})
    assert imap.selected == "[Gmail]/Alle Nachrichten"
    gmail._list_drafts(gmail_ctx, {})
    assert imap.selected == "[Gmail]/Entw\xfcrfe"


def test_list_drafts_newest_first(gmail_ctx, imap):
    imap.add(FakeIMAP.DRAFTS, _raw(to="one@example.com", subject="First"), msgid=0x31)
    imap.add(FakeIMAP.DRAFTS, _raw(to="two@example.com", subject="Second"), msgid=0x32)
    out = gmail._list_drafts(gmail_ctx, {})
    lines = out.splitlines()
    assert lines[0].startswith("- draft=32") and "two@example.com" in lines[0]
    assert lines[1].startswith("- draft=31") and "First" in lines[1]
    assert imap.selected == FakeIMAP.DRAFTS
    assert gmail._list_drafts(ConnectorContext(**vars(gmail_ctx)), {"limit": 1}).count("\n") == 0


def test_list_drafts_empty(gmail_ctx, imap):
    assert gmail._list_drafts(gmail_ctx, {}) == "(no drafts)"


def test_create_draft_appends_to_drafts_from_the_account(gmail_ctx, imap, no_network=None):
    out = gmail._create_draft(
        gmail_ctx, {"to": "bob@example.com", "subject": "Hi", "body": "Hello"}
    )
    assert out.startswith("ok: draft 1000 to bob@example.com")
    assert ADDRESS in out
    ((folder, flags, raw),) = imap.appends
    assert folder == FakeIMAP.DRAFTS and flags == "\\Draft"
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert msg["From"] == ADDRESS
    assert msg["To"] == "bob@example.com"
    assert msg["Subject"] == "Hi"
    assert msg["Message-ID"].endswith("@example.com>")
    assert msg.get_content().strip() == "Hello"


def test_create_draft_needs_to(gmail_ctx, no_network):
    out = gmail._create_draft(gmail_ctx, {"subject": "Hi"})
    assert out.startswith("error:") and "to" in out


def test_send_delivers_over_smtp_from_the_account(gmail_ctx, smtp, monkeypatch):
    monkeypatch.setattr(gmail, "_connect_imap", lambda *a: pytest.fail("send needs no IMAP"))
    out = gmail._send(gmail_ctx, {"to": "bob@example.com", "subject": "Hi", "body": "Hello"})
    assert out.startswith("ok: sent to bob@example.com from ada@example.com")
    assert smtp.logins == [(ADDRESS, "test-access-token")]
    assert smtp.quit_called is True
    ((from_addr, msg),) = smtp.sent
    assert from_addr == ADDRESS
    assert msg["From"] == ADDRESS and msg["To"] == "bob@example.com"
    assert msg["Subject"] == "Hi"
    assert msg.get_content().strip() == "Hello"
    assert msg["Message-ID"] in out


def test_send_needs_to(gmail_ctx, no_network):
    out = gmail._send(gmail_ctx, {"subject": "Hi", "body": "Hello"})
    assert out.startswith("error:") and "to" in out


def test_send_refused_recipient_is_a_tool_error(gmail_ctx, monkeypatch):
    class Refusing(FakeSMTP):
        def send_message(self, msg, from_addr=None, to_addrs=None):
            raise smtplib.SMTPRecipientsRefused({"bob@example.com": (550, b"no such user")})

    fake = Refusing()
    monkeypatch.setattr(gmail, "_connect_smtp", lambda a, p: fake)
    out = gmail._send(gmail_ctx, {"to": "bob@example.com", "body": "x"})
    assert out.startswith("error:") and "bob@example.com" in out
    assert fake.quit_called is True


def test_send_draft_sends_then_removes_the_draft(gmail_ctx, imap, smtp):
    imap.add(FakeIMAP.DRAFTS, _raw(to="bob@example.com", subject="Draft", body="Later"), 0x41)
    out = gmail._send_draft(gmail_ctx, {"draft_id": "41"})
    assert out.startswith("ok: sent draft 41 to bob@example.com from ada@example.com")
    ((from_addr, msg),) = smtp.sent
    assert from_addr == ADDRESS
    assert msg["To"] == "bob@example.com" and msg["Subject"] == "Draft"
    assert imap.folders[FakeIMAP.DRAFTS] == []  # STORE \Deleted + EXPUNGE
    assert imap.readonly is False


def test_send_draft_fills_in_missing_from_and_message_id(gmail_ctx, imap, smtp):
    bare = EmailMessage()
    bare["To"] = "bob@example.com"
    bare.set_content("x")
    imap.add(FakeIMAP.DRAFTS, bare.as_bytes(), 0x42)
    assert gmail._send_draft(gmail_ctx, {"draft_id": "42"}).startswith("ok:")
    ((_, msg),) = smtp.sent
    assert msg["From"] == ADDRESS
    assert msg["Message-ID"] and msg["Date"]


def test_send_draft_without_recipient_keeps_the_draft(gmail_ctx, imap, smtp):
    bare = EmailMessage()
    bare["Subject"] = "no one"
    bare.set_content("x")
    imap.add(FakeIMAP.DRAFTS, bare.as_bytes(), 0x43)
    out = gmail._send_draft(gmail_ctx, {"draft_id": "43"})
    assert out.startswith("error:") and "recipient" in out
    assert smtp.sent == []
    assert len(imap.folders[FakeIMAP.DRAFTS]) == 1


def test_send_draft_unknown_or_missing_id(gmail_ctx, imap, smtp):
    assert gmail._send_draft(gmail_ctx, {"draft_id": "99"}) == "error: no draft 99"
    out = gmail._send_draft(gmail_ctx, {})
    assert out.startswith("error:") and "draft_id" in out
    assert smtp.sent == []


# -- imaplib response parsing ---------------------------------------------------


def test_parse_fetch_handles_literals_bare_lines_and_trailing_flags():
    data = [
        (
            b"1 (X-GM-MSGID 1278455344230334865 X-GM-THRID 1278455344230334865 UID 7 BODY[] {5}",
            b"hello",
        ),
        b" FLAGS (\\Seen))",
        b"2 (X-GM-MSGID 42 UID 8)",
        b")",
    ]
    rows = gmail._parse_fetch(data)
    assert [(r["uid"], r["msgid"], r["thrid"], r["data"]) for r in rows] == [
        (7, 1278455344230334865, 1278455344230334865, b"hello"),
        (8, 42, None, b""),
    ]


def test_folder_listing_parses_quoted_bare_and_literal_names():
    class Conn:
        def list(self):
            return "OK", [
                b'(\\HasNoChildren) "/" INBOX',
                b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
                b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
                (b'(\\Drafts \\HasNoChildren) "/" {14}', b"[Gmail]/Brouillons"),
                b'(\\HasNoChildren) "/" "Say \\"hi\\""',
            ]

    mb = gmail._Mailbox(Conn(), ADDRESS)
    names = [f["name"] for f in mb.folders()]
    assert names == ["INBOX", "[Gmail]/All Mail", "[Gmail]", "[Gmail]/Brouillons", 'Say "hi"']
    assert mb.all_mail() == '"[Gmail]/All Mail"'
    assert mb.drafts() == '"[Gmail]/Brouillons"'
    assert mb.folders()[0]["arg"] == '"INBOX"'
    assert mb.folders()[-1]["arg"] == '"Say \\"hi\\""'


def test_folder_roles_fall_back_to_english_names():
    class Conn:
        def list(self):
            return "OK", [b'(\\HasNoChildren) "/" INBOX']

    mb = gmail._Mailbox(Conn(), ADDRESS)
    assert mb.all_mail() == '"[Gmail]/All Mail"'
    assert mb.drafts() == '"[Gmail]/Drafts"'


def test_ids_are_gmail_hex():
    assert gmail._hex(1278455344230334865) == "11bdfc5cae0c8191"
    assert gmail._parse_id("11BDFC5CAE0C8191") == 1278455344230334865
    assert gmail._parse_id("") is None and gmail._parse_id("zz") is None


# -- several inboxes -----------------------------------------------------------


def test_record_is_named_by_its_address_unless_the_user_named_it(paths):
    store = Connectors(paths)
    by_address = store.add("gmail", "Gmail", {"email": "ada@example.com"}, secret="x")
    assert by_address["name"] == "ada@example.com"
    named = store.add("gmail", "Work", {"email": "ada@work.example"}, secret="x")
    assert named["name"] == "Work"
    bare = store.add("gmail", "", None, secret="x")
    assert bare["name"] == "Gmail"
    other = store.add("github", "GitHub", {"email": "ignored@example.com"}, secret="x")
    assert other["name"] == "GitHub"


def test_two_inboxes_bind_under_their_own_prefixes(paths):
    store = Connectors(paths)
    store.add("gmail", "Gmail", {"email": "ada@example.com"}, secret="x")
    store.add("gmail", "Work", {"email": "ada@work.example"}, secret="y")
    bound = tools_for_bot(paths, "atlas")
    assert "gmail_ada_example_com_send" in bound
    assert "gmail_work_send" in bound
    assert "gmail_send" not in bound
    assert bound["gmail_work_send"][0].description.startswith("Work: ")
    assert bound["gmail_ada_example_com_search_threads"][0].description.startswith(
        "ada@example.com: "
    )


def test_one_inbox_keeps_the_plain_prefix_and_description(paths):
    Connectors(paths).add("gmail", "Gmail", {"email": "ada@example.com"}, secret="x")
    bound = tools_for_bot(paths, "atlas")
    assert "gmail_send" in bound
    assert not bound["gmail_send"][0].description.startswith("ada@example.com")


def test_each_inbox_tool_uses_its_own_credentials(paths, monkeypatch):
    store = Connectors(paths)
    from harness.mcp_oauth import save_tokens

    first = store.add("gmail", "Gmail", {"email": "ada@example.com"})
    second = store.add("gmail", "Work", {"email": "ada@work.example"})
    save_tokens(paths, first["id"], {"access_token": "aaaaaaaaaaaaaaaa"})
    save_tokens(paths, second["id"], {"access_token": "bbbbbbbbbbbbbbbb"})
    logins = []

    def connect(address, password):
        logins.append((address, password))
        return FakeIMAP()

    monkeypatch.setattr(gmail, "_connect_imap", connect)
    bound = tools_for_bot(paths, "atlas")
    bound["gmail_work_list_labels"][1]({})
    bound["gmail_ada_example_com_list_labels"][1]({})
    assert logins == [
        ("ada@work.example", "bbbbbbbbbbbbbbbb"),
        ("ada@example.com", "aaaaaaaaaaaaaaaa"),
    ]


def test_account_slug_never_ends_in_an_underscore():
    long_name = {"type": "gmail", "name": "averyveryverylongaddress@example.com", "id": "abc"}
    slug = account_slug(long_name)
    assert len(slug) <= 24 and not slug.endswith("_")
    assert account_slug({"type": "gmail", "name": "Gmail", "id": "deadbeef"}) == "deadbeef"


def test_colliding_account_slugs_get_a_unique_prefix(paths):
    from collections import Counter

    a = {"id": "aaaa1111", "type": "gmail", "name": "Gmail Work"}
    b = {"id": "bbbb2222", "type": "gmail", "name": "gmail-work!"}
    prefixes = tool_prefixes([a, b], Counter({"gmail": 2}))
    assert prefixes["aaaa1111"] == "gmail_work"
    assert prefixes["bbbb2222"] == "gmail_work_bbbb"
    store = Connectors(paths)
    store.add("gmail", "Gmail Work", {"email": "a@example.com"}, secret="x")
    store.add("gmail", "gmail-work!", {"email": "b@example.com"}, secret="y")
    bound = tools_for_bot(paths, "atlas")
    sends = sorted(n for n in bound if n.endswith("_send"))
    assert len(sends) == 2 and "gmail_work_send" in sends


def test_removing_an_inbox_drops_only_its_secret(paths):
    from harness.secrets import get_secret

    store = Connectors(paths)
    a = store.add("gmail", "Gmail", {"email": "ada@example.com"}, secret="aaaa")
    b = store.add("gmail", "Work", {"email": "ada@work.example"}, secret="bbbb")
    assert store.remove(a["id"]) is True
    assert get_secret(f"connector_{a['id']}", paths) is None
    assert get_secret(f"connector_{b['id']}", paths) == "bbbb"
    assert "gmail_send" in tools_for_bot(paths, "atlas")  # back to the plain prefix
