"""Regression tests for the infrastructure findings of the security audit.

Each section pins one boundary the audit found open. The tests are written
against the contract (what an attacker-shaped input must NOT achieve), so a
refactor that keeps the boundary keeps passing; each one fails on the
pre-fix code.
"""

from __future__ import annotations

import io
import json
import os
import stat
import tarfile
from pathlib import Path

import pytest

from harness import hostinput
from harness.fsutil import write_private
from harness.linking import get_or_create_key
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.redaction import _REGISTRY as registry
from harness.redaction import scrub
from harness.roster import RosterError, valid_bot_name
from harness.secrets import (
    SecretNameError,
    delete_secret,
    get_secret,
    secret_source,
    set_secret,
    valid_secret_name,
)
from isolation import state_sync
from providers import oauth_store

# -- secret names are bare filenames, never paths -------------------------


def _paths(tmp_path) -> HarnessPaths:
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    return paths


TRAVERSAL_NAMES = [
    "../link-key",
    "../serve.json",
    "/etc/hosts",
    "..",
    ".",
    "../../root/.ssh/authorized_keys",
    "a/b",
    "a\\b",
    ".hidden",
    "name\x00x",
    "",
]


@pytest.mark.parametrize("name", TRAVERSAL_NAMES)
def test_path_shaped_secret_names_are_never_resolved(tmp_path, name):
    """`get_secret("../link-key")` used to read the linking key; the store
    must answer "not found" for anything that is not one bare filename."""
    paths = _paths(tmp_path)
    (paths.home / "link-key").write_text("the-linking-key-value", encoding="utf-8")
    (paths.home / "serve.json").write_text('{"key": "k"}', encoding="utf-8")
    assert not valid_secret_name(name)
    assert get_secret(name, paths) is None
    assert secret_source(name, paths) is None
    assert delete_secret(name, paths) is False
    assert (paths.home / "link-key").read_text(encoding="utf-8") == "the-linking-key-value"


@pytest.mark.parametrize("name", TRAVERSAL_NAMES)
def test_path_shaped_secret_names_are_never_written(tmp_path, name):
    """`POST /api/providers/../../x/key` used to write outside the home."""
    paths = _paths(tmp_path)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    with pytest.raises(SecretNameError):
        set_secret(name, "ssh-ed25519 AAAA attacker", paths)
    after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    assert after == before  # nothing created anywhere


def test_ordinary_secret_names_still_round_trip(tmp_path):
    paths = _paths(tmp_path)
    for name in ("ANTHROPIC_API_KEY", "connector_ab12cd34", "smtp.password-1", "claude"):
        assert valid_secret_name(name)
        set_secret(name, f"value-for-{name}", paths)
        assert get_secret(name, paths) == f"value-for-{name}"
        assert secret_source(name, paths) == "file"
        assert delete_secret(name, paths) is True
        assert get_secret(name, paths) is None


def test_a_symlink_in_the_store_is_not_followed_out_of_it(tmp_path):
    """Containment is checked on the resolved path, not the name."""
    paths = _paths(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("not-a-credential", encoding="utf-8")
    paths.credentials.mkdir(parents=True, exist_ok=True)
    (paths.credentials / "LINKED").symlink_to(outside)
    assert get_secret("LINKED", paths) is None


# -- credential files are born private ------------------------------------


def _born_mode(monkeypatch, tmp_path, write) -> tuple[int, list[str]]:
    """Mode the file has before any post-hoc chmod could run, and leftovers."""
    old = os.umask(0o022)
    try:
        # A writer that relies on chmod after the fact is exposed for the
        # window between creation and chmod; make that window permanent.
        monkeypatch.setattr(Path, "chmod", lambda self, mode: None)
        monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
        path = write()
    finally:
        os.umask(old)
    leftovers = [
        p.name for p in path.parent.iterdir() if p.name.startswith(".") and "tmp" in p.name
    ]
    return stat.S_IMODE(path.stat().st_mode), leftovers


def test_write_private_creates_0600_without_relying_on_chmod(tmp_path, monkeypatch):
    target = tmp_path / "secret.txt"
    mode, leftovers = _born_mode(
        monkeypatch, tmp_path, lambda: (write_private(target, "s3cret"), target)[1]
    )
    assert mode == 0o600
    assert leftovers == []
    assert target.read_text(encoding="utf-8") == "s3cret"


def test_link_key_is_born_private(tmp_path, monkeypatch):
    """link-key sits in the home dir, which is not 0700, so the file itself
    must never exist with a wide mode — not even between write and chmod."""
    paths = _paths(tmp_path)

    def write():
        get_or_create_key(paths)
        return paths.home / "link-key"

    mode, leftovers = _born_mode(monkeypatch, tmp_path, write)
    assert mode == 0o600
    assert leftovers == []


def test_api_key_files_are_born_private(tmp_path, monkeypatch):
    paths = _paths(tmp_path)

    def write():
        set_secret("SOME_API_KEY", "sk-live-0123456789", paths)
        return paths.credentials / "SOME_API_KEY"

    mode, _ = _born_mode(monkeypatch, tmp_path, write)
    assert mode == 0o600


def test_xai_tokens_are_born_private(tmp_path, monkeypatch):
    from providers import xai_oauth

    paths = _paths(tmp_path)

    def write():
        xai_oauth.save_tokens(
            paths,
            {"access_token": "acc-token-0123", "refresh_token": "ref-token-0123"},
            token_endpoint="https://x/token",
        )
        return paths.credentials / "XAI_OAUTH"

    mode, _ = _born_mode(monkeypatch, tmp_path, write)
    assert mode == 0o600


# -- bearer tokens are registered for redaction ---------------------------


@pytest.fixture
def _clean_registry():
    registry.clear()
    yield
    registry.clear()


def test_link_key_is_scrubbed_once_loaded(tmp_path, _clean_registry):
    paths = _paths(tmp_path)
    key = get_or_create_key(paths)
    assert key not in scrub(f"error: bad request for token={key}")
    registry.clear()
    again = get_or_create_key(paths)  # re-read from disk registers too
    assert again == key
    assert key not in scrub(f"Bearer {key}")


def test_oauth_tokens_are_scrubbed_once_loaded(tmp_path, _clean_registry):
    paths = _paths(tmp_path)
    oauth_store.save_tokens(
        paths, "PROVIDER_X", {"access_token": "access-0123456789", "refresh_token": "refresh-0123"}
    )
    assert "access-0123456789" not in scrub("401 for access-0123456789")
    assert "refresh-0123" not in scrub("refresh-0123")
    registry.clear()
    assert oauth_store.load_tokens(paths, "PROVIDER_X")["access_token"] == "access-0123456789"
    assert "access-0123456789" not in scrub("access-0123456789")


# -- xdotool input: a key name can only ever press keys --------------------


def _capture_lines(monkeypatch):
    lines: list[str] = []
    hostinput._session._last = None
    monkeypatch.setattr(hostinput.shutil, "which", lambda _n: "/usr/bin/xdotool")
    monkeypatch.setattr(hostinput, "_pointer_window", lambda machine=None: None)
    monkeypatch.setattr(hostinput._session, "_write", lambda line: lines.append(line) or True)
    return lines


@pytest.mark.parametrize(
    "name",
    [
        "Return\nexec sh -c id",
        "Return\rexec sh -c id",
        "a\x00exec id",
        "--delay",
        "-x",
        "$HOME",
        "Return; exec id",
        'Return "x"',
        "ctrl+",
        "+a",
        "ctrl++a",
        "",
        " Return",
    ],
)
def test_injection_shaped_key_names_write_nothing(monkeypatch, name):
    """`xdotool -` is one command per line and `exec` runs a program: a key
    name carrying a line break used to be host (or machine) RCE from a web
    page, through a tool the gate leaves open on browsing turns."""
    lines = _capture_lines(monkeypatch)
    assert hostinput.valid_key_name(name) is False
    assert hostinput.key(name) is False
    assert hostinput.dispatch({"action": "key", "key": name}) is False
    assert lines == []


@pytest.mark.parametrize(
    "name",
    ["Return", "ctrl+l", "ctrl+shift+t", "alt+F4", "XF86AudioPlay", "Page_Down", "ctrl+a ctrl+c"],
)
def test_real_key_names_still_press(monkeypatch, name):
    lines = _capture_lines(monkeypatch)
    assert hostinput.valid_key_name(name)
    assert hostinput.key(name) is True
    assert lines == [f"key --clearmodifiers {name}"]


def test_session_refuses_any_line_break_from_any_caller(monkeypatch):
    """The guard sits where lines are written, so a future caller cannot
    reintroduce the hole; a text payload with a newline is refused whole."""
    lines = _capture_lines(monkeypatch)
    assert hostinput._session.send("type", "--", "hello\nexec id") is False
    assert hostinput._session.send("type", "--", "hello\rexec id") is False
    assert lines == []
    assert hostinput._session.send("type", "--", "hello world") is True
    assert lines == ["type -- hello world"]


# -- bot names and skill ids are single path components --------------------


@pytest.mark.parametrize("name", ["..", ".", "/", "../..", "!!!", "\\", "/../"])
def test_unsluggable_bot_names_are_refused_not_used_verbatim(tmp_path, name):
    """`bot_slug("..")` is "", and add_bot used to fall back to the raw
    string — which then became messages/<..>/inbox and memory/<..>."""
    rp = tmp_path / "roster.toml"
    rp.write_text('[[bots]]\nname = "atlas"\nprovider = "echo"\n', encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    orch.use_json_store()
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    with pytest.raises(RosterError):
        orch.add_bot(name=name, start=False)
    assert orch.roster.names() == ["atlas"]
    after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    assert after == before


@pytest.mark.parametrize("name", ["..", ".", "a/b", "a\\b", "x\x00y", "", "n" * 129])
def test_a_hand_edited_roster_cannot_carry_a_path_shaped_name(name):
    assert valid_bot_name(name) is False


def test_display_names_still_slug_to_ids(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text('[[bots]]\nname = "atlas"\nprovider = "echo"\n', encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    orch.use_json_store()
    bot = orch.add_bot(name="Sales Bot!", start=False)
    assert bot.name == "sales-bot"
    assert valid_bot_name(bot.name)


@pytest.mark.parametrize(
    "skill_id", ["/tmp/evil", "../../../skills/evil", "..", "helper/../../../x", ".hidden"]
)
def test_bundle_skill_ids_cannot_leave_the_bots_skill_dir(tmp_path, skill_id):
    """A shared bundle used to plant SKILL.md wherever its `id` pointed."""
    from harness import bundle

    rp = tmp_path / "roster.toml"
    rp.write_text('[[bots]]\nname = "atlas"\nprovider = "echo"\n', encoding="utf-8")
    orch = Orchestrator.create(home=str(tmp_path / "home"), roster_path=rp)
    data = bundle.loads(
        json.dumps(
            {
                "version": 1,
                "bots": [],
                "skills": [{"bot": "atlas", "id": skill_id, "name": "evil", "body": "do it"}],
            }
        )
    )
    before = sorted(str(p) for p in tmp_path.rglob("SKILL.md"))
    report = bundle.apply(orch.paths, data, orch)
    assert report.added_skills == []
    assert f"atlas/{skill_id}" in report.failed
    assert sorted(str(p) for p in tmp_path.rglob("SKILL.md")) == before
    assert not Path("/tmp/evil/SKILL.md").exists()


def test_bundle_skill_ids_are_slugged_into_place(tmp_path):
    from harness import bundle

    rp = tmp_path / "roster.toml"
    rp.write_text('[[bots]]\nname = "atlas"\nprovider = "echo"\n', encoding="utf-8")
    orch = Orchestrator.create(home=str(tmp_path / "home"), roster_path=rp)
    data = bundle.loads(
        json.dumps(
            {
                "version": 1,
                "bots": [],
                "skills": [{"bot": "atlas", "id": "Cite Sources", "name": "cite", "body": "b"}],
            }
        )
    )
    report = bundle.apply(orch.paths, data, orch)
    assert report.added_skills == ["atlas/cite-sources"]
    assert (orch.paths.bot_memory("atlas") / "skills" / "cite-sources" / "SKILL.md").is_file()


# -- machine state sync cannot fill the host disk --------------------------


def _tar_with(members: dict[str, bytes]) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, payload in members.items():
            info = tarfile.TarInfo(f"agent/{name}")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    buf.seek(0)
    return buf


def test_index_tar_skips_members_over_the_per_file_cap(monkeypatch):
    """A `truncate -s 2T keep.bin` inside the machine reports its logical
    size; it must never be planned, extracted, or replicated."""
    monkeypatch.setenv("HARNESS_MACHINE_SYNC_MAX_FILE", "1024")
    small = b"x" * 10
    index = state_sync.index_tar(
        _tar_with({"Desktop/note.txt": small, "big.bin": b"y" * 2048}), strip_components=1
    )
    assert "Desktop/note.txt" in index
    assert "big.bin" not in index
    assert index.oversized == ["big.bin"]


def test_per_file_cap_has_a_default_and_can_be_disabled(monkeypatch):
    monkeypatch.delenv("HARNESS_MACHINE_SYNC_MAX_FILE", raising=False)
    assert state_sync.max_file_bytes() == state_sync.DEFAULT_MAX_FILE_BYTES > 0
    monkeypatch.setenv("HARNESS_MACHINE_SYNC_MAX_FILE", "0")
    index = state_sync.index_tar(_tar_with({"big.bin": b"y" * 2048}), strip_components=1)
    assert "big.bin" in index and index.oversized == []
    monkeypatch.setenv("HARNESS_MACHINE_SYNC_MAX_FILE", "not-a-number")
    assert state_sync.max_file_bytes() == state_sync.DEFAULT_MAX_FILE_BYTES


def test_machine_tar_is_sparse_and_output_capped(monkeypatch):
    from isolation import machines

    monkeypatch.delenv("HARNESS_MACHINE_SYNC_MAX_TAR", raising=False)
    assert machines.tar_output_cap() > 0
    assert machines._tar_output_limit() is not None
    monkeypatch.setenv("HARNESS_MACHINE_SYNC_MAX_TAR", "0")
    assert machines._tar_output_limit() is None


# -- HTTP/WS server: auth compare, query tokens, idle peers, bodies ----------


ROSTER = '[[bots]]\nname = "atlas"\nprovider = "echo"\n'


@pytest.fixture
def keyed_server(tmp_path, monkeypatch):
    """A keyed server with a short request timeout (the idle-peer tests)."""
    import threading

    from harness.server import make_server

    monkeypatch.setenv("HARNESS_HTTP_TIMEOUT", "1")
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    key = get_or_create_key(orch.paths)
    httpd = make_server(orch, "127.0.0.1", 0, key)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield port, key, orch
    finally:
        httpd.shutdown()
        orch.down()


def _raw(port: int, request: bytes, *, wait: float = 5.0) -> tuple[bytes, bool]:
    """Send raw bytes; return (response, server_closed_within_wait)."""
    import socket

    with socket.create_connection(("127.0.0.1", port), timeout=wait) as sock:
        sock.sendall(request)
        sock.settimeout(wait)
        chunks: list[bytes] = []
        closed = False
        try:
            while True:
                data = sock.recv(65536)
                if not data:
                    closed = True
                    break
                chunks.append(data)
        except TimeoutError:
            closed = False
        return b"".join(chunks), closed


def _wrong(key: str) -> str:
    return ("A" if key[0] != "A" else "B") + key[1:]


def test_bearer_compare_is_constant_time(monkeypatch):
    """The single authorization check of the product must not be `==`."""
    import hmac

    from harness import server

    calls: list[tuple[bytes, bytes]] = []

    def spy(a, b):
        calls.append((a, b))
        return a == b

    monkeypatch.setattr(hmac, "compare_digest", spy)
    assert server._same_secret("abc", b"abc") is True
    assert server._same_secret("abd", b"abc") is False
    assert server._same_secret("\udcff", b"abc") is False  # hostile header never raises
    assert len(calls) == 3


def test_wrong_key_of_the_same_length_is_refused_everywhere(keyed_server):
    port, key, _ = keyed_server
    bad = _wrong(key)
    body, _ = _raw(
        port, f"GET /api/health HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {bad}\r\n\r\n".encode()
    )
    assert body.startswith(b"HTTP/1.1 401")
    body, _ = _raw(
        port,
        f"GET /ws?token={bad} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: AAAAAAAAAAAAAAAAAAAAAA==\r\nSec-WebSocket-Version: 13\r\n\r\n".encode(),
    )
    assert body.startswith(b"HTTP/1.1 401")


def test_query_token_opens_the_websocket_and_nothing_else(keyed_server):
    """`?token=` exists for URLSessionWebSocketTask, which cannot reliably
    carry a header on the upgrade; every other route must refuse it so the
    bearer never rides in a URL."""
    port, key, _ = keyed_server
    body, _ = _raw(
        port,
        f"GET /ws?token={key} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: AAAAAAAAAAAAAAAAAAAAAA==\r\nSec-WebSocket-Version: 13\r\n\r\n".encode(),
        wait=2,
    )
    assert body.startswith(b"HTTP/1.1 101")
    for line in (
        f"GET /api/health?token={key} HTTP/1.1",
        f"GET /api/bots?token={key} HTTP/1.1",
        f"DELETE /api/bots/atlas?token={key} HTTP/1.1",
        f"POST /api/chat?token={key} HTTP/1.1",
    ):
        body, _ = _raw(port, f"{line}\r\nHost: x\r\nContent-Length: 0\r\n\r\n".encode())
        assert body.startswith(b"HTTP/1.1 401"), line


def test_a_refused_request_closes_the_connection(keyed_server):
    """HTTP/1.1 keep-alive after a 401 let an unauthenticated peer park on
    a server thread for free; the refusal must end the connection."""
    port, _, _ = keyed_server
    body, closed = _raw(port, b"GET /api/health HTTP/1.1\r\nHost: x\r\n\r\n", wait=3)
    assert body.startswith(b"HTTP/1.1 401")
    assert b"connection: close" in body.lower()
    assert closed


def test_an_idle_peer_is_dropped_by_the_request_timeout(keyed_server):
    """Slowloris: connect and send nothing (or a partial request line)."""
    port, _, _ = keyed_server
    _, closed = _raw(port, b"", wait=4)
    assert closed
    _, closed = _raw(port, b"GET /api/health HTTP/1.1\r\nX-Slow: ", wait=4)
    assert closed


def test_oversized_bodies_are_refused_before_they_are_read(keyed_server):
    from harness import server

    port, key, _ = keyed_server
    head = (
        f"POST /api/answers HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {key}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {server.MAX_JSON_BYTES + 1}\r\n\r\n"
    )
    body, closed = _raw(port, head.encode() + b"{", wait=3)
    assert body.startswith(b"HTTP/1.1 413")
    assert closed
    head = (
        f"POST /api/upload HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {key}\r\n"
        f"X-Filename: big.bin\r\nContent-Length: {server.MAX_UPLOAD_BYTES + 1}\r\n\r\n"
    )
    body, closed = _raw(port, head.encode() + b"x", wait=3)
    assert body.startswith(b"HTTP/1.1 413")
    assert closed


def test_authenticated_websocket_outlives_the_request_timeout(keyed_server):
    """The 1s request timeout must not cut an idle, authenticated socket."""
    import time

    from tests.test_ws import WSClient

    port, key, _ = keyed_server
    ws = WSClient("127.0.0.1", port, path=f"/ws?token={key}")
    time.sleep(2.5)
    ws.send({"type": "ping"})
    frame = ws.recv()
    assert frame is not None
    ws.close()


@pytest.mark.parametrize("pid", ["../../x", "..", "../link-key", ".hidden", "a/b"])
def test_provider_routes_refuse_path_shaped_ids(keyed_server, pid):
    """`POST /api/providers/../../root/.ssh/authorized_keys/key` used to
    write there (as root on a tenant VM)."""
    import http.client

    port, key, orch = keyed_server
    home = orch.paths.home
    before = sorted(p.relative_to(home.parent) for p in home.parent.rglob("*"))
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    for method, path, payload in (
        ("POST", f"/api/providers/{pid}/key", '{"api_key": "ssh-ed25519 AAAA"}'),
        ("DELETE", f"/api/providers/{pid}/key", None),
        ("DELETE", f"/api/providers/{pid}/oauth", None),
        ("POST", "/api/secrets", json.dumps({"name": pid, "value": "v"})),
    ):
        conn.request(
            method,
            path,
            body=payload,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        resp.read()
        if path == "/api/secrets":
            # This route strips separators to a bare name ("a/b" -> "ab"),
            # which is contained; a name that strips to nothing is a 400.
            assert resp.status in (200, 400), (method, path, resp.status)
        else:
            assert resp.status == 400, (method, path, resp.status)
    conn.close()
    after = sorted(p.relative_to(home.parent) for p in home.parent.rglob("*"))
    created = [p for p in after if p not in before]
    creds = (home / "credentials").relative_to(home.parent)
    assert all(p.parent == creds and valid_secret_name(p.name) for p in created), created


def test_pairing_banner_is_not_printed_to_a_journal_on_every_boot():
    from harness.linking import pairing_notice

    url, key = "http://10.0.0.5:8765", "the-linking-key-0123456789"
    assert key in pairing_notice(url, key, first_run=True, interactive=False)
    assert key in pairing_notice(url, key, first_run=False, interactive=True)
    quiet = pairing_notice(url, key, first_run=False, interactive=False)
    assert key not in quiet
    assert "harness_" not in quiet  # the link code decodes to the key
    assert "harness link" in quiet


def test_app_release_relays_only_https_pins_with_a_real_digest(tmp_path):
    from harness.server import app_release

    paths = _paths(tmp_path)
    (paths.home / "app_release.json").write_text(
        json.dumps(
            {
                "min_app_version": "0.2.0",
                "latest_app_version": "0.3.0",
                "app_download_url": "http://x/Dotobot.zip",
                "app_sha256": "zz",
            }
        ),
        encoding="utf-8",
    )
    out = app_release(paths)
    assert out["latest_app_version"] == "0.3.0"
    assert out["app_download_url"] is None
    assert out["app_sha256"] is None
    (paths.home / "app_release.json").write_text(
        json.dumps({"app_download_url": "https://x/Dotobot.zip", "app_sha256": "AB" * 32}),
        encoding="utf-8",
    )
    out = app_release(paths)
    assert out["app_download_url"] == "https://x/Dotobot.zip"
    assert out["app_sha256"] == "ab" * 32


# -- MCP connector credentials ------------------------------------------------


def test_mcp_connector_credentials_are_born_private(tmp_path, monkeypatch, _clean_registry):
    from harness import mcp_oauth

    paths = _paths(tmp_path)

    def write_client():
        mcp_oauth.save_oauth_client(
            paths, "ab12cd34", {"client_id": "cid", "client_secret": "csecret-0123456789"}
        )
        return paths.credentials / "connector_ab12cd34_oauth_client"

    mode, leftovers = _born_mode(monkeypatch, tmp_path, write_client)
    assert mode == 0o600 and leftovers == []
    assert "csecret-0123456789" not in scrub("csecret-0123456789")

    def write_tokens():
        mcp_oauth.save_tokens(
            paths,
            "ab12cd34",
            {"access_token": "mcp-access-0123", "refresh_token": "mcp-refresh-0123"},
        )
        return paths.credentials / "connector_ab12cd34_oauth"

    mode, leftovers = _born_mode(monkeypatch, tmp_path, write_tokens)
    assert mode == 0o600 and leftovers == []
    assert "mcp-access-0123" not in scrub("mcp-access-0123")
    registry.clear()
    assert mcp_oauth.load_tokens(paths, "ab12cd34")["access_token"] == "mcp-access-0123"
    assert "mcp-access-0123" not in scrub("mcp-access-0123")


# -- deployment surface: systemd units and the CI runner ----------------------


def test_uploads_never_render_as_html_on_the_harness_origin(keyed_server):
    """An uploaded .html served inline would run script with the API's
    origin; documents download, images stay inline for chat tiles."""
    import urllib.request

    port, key, orch = keyed_server
    uploads = orch.paths.uploads
    uploads.mkdir(parents=True, exist_ok=True)
    (uploads / "evil.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    (uploads / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    for name, disposition in (("evil.html", "attachment"), ("photo.png", "inline")):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/uploads/{name}",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.headers["X-Content-Type-Options"] == "nosniff"
            assert r.headers["Content-Disposition"].startswith(disposition), name


# -- teach-a-task: the recording copied out of a machine ----------------------


def _cp_tar(members: list[tuple[str, str, bytes | str]]) -> bytes:
    """A `docker cp <m>:/path -` style tar: (kind, name, payload|target)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for kind, name, payload in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = payload
                tar.addfile(info)
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
    return buf.getvalue()


def test_teach_copy_out_keeps_only_a_regular_member_under_the_cap(tmp_path):
    from harness import teachrec

    dest = tmp_path / "demo.mp4"
    # a symlink planted at /tmp/harness-teach/demo.mp4 inside the machine
    assert (
        teachrec._extract_single_regular(
            io.BytesIO(_cp_tar([("symlink", "demo.mp4", "/etc/shadow")])), dest, 1 << 20
        )
        is False
    )
    assert not dest.exists() and not dest.is_symlink()
    # an oversized (sparse) file is not materialised on the host
    assert (
        teachrec._extract_single_regular(
            io.BytesIO(_cp_tar([("file", "demo.mp4", b"x" * 4096)])), dest, 1024
        )
        is False
    )
    assert not dest.exists()
    # the real recording lands
    assert (
        teachrec._extract_single_regular(
            io.BytesIO(_cp_tar([("dir", "x", ""), ("file", "demo.mp4", b"MP4DATA")])), dest, 1 << 20
        )
        is True
    )
    assert dest.read_bytes() == b"MP4DATA"
    assert [p.name for p in tmp_path.iterdir()] == ["demo.mp4"]  # no temp leftovers


def test_teach_probe_and_stills_never_follow_a_symlink(tmp_path, monkeypatch):
    from harness import teachrec

    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x" * 4096)
    link = tmp_path / "demo.mp4"
    link.symlink_to(outside)
    called: list[list[str]] = []
    monkeypatch.setattr(teachrec.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        teachrec.subprocess, "run", lambda argv, **kw: called.append(list(argv)) or None
    )
    assert teachrec._probe_duration(link, 7.5) == 7.5
    assert teachrec._extract_still(link, 1.0, tmp_path / "still.jpg") is False
    assert called == []  # ffprobe/ffmpeg were never handed the symlink


def test_teach_copy_out_streams_through_the_capped_tar_path(tmp_path, monkeypatch):
    from harness import teachrec
    from isolation import engine

    seen: dict = {}

    def fake_run_raw(args, *, stdin=None, stdout=None, timeout=600, preexec_fn=None):
        seen["args"] = list(args)
        seen["preexec"] = preexec_fn
        stdout.write(_cp_tar([("file", "demo.mp4", b"REC")]))

        class P:
            returncode = 0

        return P()

    monkeypatch.setattr(engine, "run_raw", fake_run_raw)
    live = teachrec._Live(
        bot="atlas",
        session_id="s1",
        session_dir=tmp_path,
        video=tmp_path / "demo.mp4",
        machine="harness-machine-0",
        started_at=0.0,
    )
    live.inner_video = "/tmp/harness-teach/demo.mp4"
    teachrec._copy_machine_video(live)
    assert seen["args"] == ["cp", "harness-machine-0:/tmp/harness-teach/demo.mp4", "-"]
    assert seen["preexec"] is not None  # RLIMIT_FSIZE on the docker client
    assert (tmp_path / "demo.mp4").read_bytes() == b"REC"


# -- the curl | sh Mac installer ---------------------------------------------


# -- tools that call the loopback API -----------------------------------------


def test_bot_tools_cannot_smuggle_another_route_into_the_loopback_api(tmp_path, monkeypatch):
    """`duplicate_bot(source="atlas/restart?")` used to become
    `POST /api/bots/atlas/restart` with the operator bearer, outside any
    gate classification; ids are single path segments or refused."""
    import urllib.request

    from agent import tools
    from agent.memory import Memory

    paths = _paths(tmp_path)
    (paths.home / "serve.json").write_text(
        json.dumps({"url": "http://127.0.0.1:1", "key": "k"}), encoding="utf-8"
    )

    def boom(*a, **k):
        raise AssertionError("the loopback API was called")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    ctx = tools.ToolContext(paths=paths, bot="atlas", memory=Memory(paths, "atlas"))
    for bad in ("atlas/restart?", "nova/routines/x/run#", "../nova", "atlas?x=1", ""):
        out = (
            tools._duplicate_bot(ctx, {"source": bad or "atlas", "as": "helper"})
            if bad
            else "error:"
        )
        assert out.startswith("error:"), bad
        out = tools._run_routine(ctx, {"id": bad or "x?"})
        assert out.startswith("error:"), bad
        out = tools._update_routine(ctx, {"id": bad or "x?", "enabled": True})
        assert out.startswith("error:"), bad
        out = tools._delete_routine(ctx, {"id": bad or "x?"})
        assert out.startswith("error:"), bad
    assert tools._api_json(paths, "POST", "/api/bots/atlas/restart?x", {}).startswith("error:")
    assert tools._api_json(paths, "POST", "/api/bots/../x", {}).startswith("error:")


def test_message_sink_refuses_a_path_shaped_recipient(tmp_path):
    """Whoever builds the Msg, `messages/<to>/inbox` is never built from a
    path-shaped name (sink twin of the resolver check in the tool)."""
    from agent import messaging

    paths = _paths(tmp_path)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    for bad in ("../x", "..", "a/b", "/abs", "x\x00y", ""):
        with pytest.raises(ValueError):
            messaging.send(paths, messaging.Msg(to=bad, frm="user", text="hi"))
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before
    messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="hi"))
    assert list(paths.inbox("atlas").iterdir())


def test_screenshot_recode_runs_convert_under_resource_limits(monkeypatch):
    from harness import screen

    seen: dict = {}

    def fake_run(argv, **kw):
        seen["argv"] = list(argv)

        class P:
            returncode = 0
            stdout = b"\xff\xd8ok"

        return P()

    monkeypatch.setattr(screen.shutil, "which", lambda name: "/usr/bin/convert")
    monkeypatch.setattr(screen.subprocess, "run", fake_run)
    out, mime = screen.compress_for_model(b"\x89PNG\r\n\x1a\n")
    assert mime == "image/jpeg"
    argv = seen["argv"]
    assert "-limit" in argv and "memory" in argv and "area" in argv
    assert argv.index("-limit") < argv.index("png:-")


def test_lan_fetch_opt_in_never_reopens_loopback_or_metadata(monkeypatch):
    from harness import netguard

    monkeypatch.delenv(netguard.PRIVATE_FETCH_ENV, raising=False)
    assert netguard.is_public_address("192.168.1.10") is False
    monkeypatch.setenv(netguard.PRIVATE_FETCH_ENV, "1")
    assert netguard.is_public_address("192.168.1.10") is True
    assert netguard.is_public_address("10.0.0.5") is True
    for still_refused in ("127.0.0.1", "169.254.169.254", "::1", "0.0.0.0", "::ffff:127.0.0.1"):
        assert netguard.is_public_address(still_refused) is False, still_refused
    assert netguard.is_public_address("93.184.216.34") is True
