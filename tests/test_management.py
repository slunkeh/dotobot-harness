"""UI-management API: provider keys, bot CRUD, connectors (all persisted)."""

from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from agent.memory import Memory
from harness.orchestrator import Orchestrator
from harness.server import make_server

ROSTER = """
[[bots]]
name = "atlas"
role = "a terse research assistant"
provider = "echo"
"""


@pytest.fixture
def server(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    orch.use_json_store()
    httpd = make_server(orch, "127.0.0.1", 0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", orch
    finally:
        httpd.shutdown()
        orch.down()


def _req(url, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _req_error(url, method="GET", payload=None):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _req(url, method, payload)
    return exc.value.code, json.loads(exc.value.read().decode())


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/bots/atlas/routines"),
        ("DELETE", "/api/bots/atlas/routines/draft"),
        ("GET", "/api/workflows?bot=atlas"),
    ],
)
def test_corrupt_routines_return_an_error_without_changing_data(server, method, path):
    base, orch = server
    saved = orch.paths.bot_routines("atlas")
    saved.parent.mkdir(parents=True, exist_ok=True)
    original = '{"routines": [{"id": "keep"}]} trailing data'
    saved.write_text(original)
    code, body = _req_error(base + path, method)
    assert code == 500
    assert "cannot read routines" in body["error"]
    assert saved.read_text() == original


def test_delete_draft_then_late_autosave_keeps_existing_routines(server):
    base, _ = server
    url = f"{base}/api/bots/atlas/routines"
    keep = _req(url, "POST", {"title": "Keep", "prompt": "Preserve instructions"})
    draft = _req(url, "POST", {})
    target = f"{url}/{draft['id']}"
    assert _req(target, "DELETE")["removed"] == draft["id"]
    assert _req_error(target, "PATCH", {"title": "", "prompt": ""})[0] == 404
    assert _req_error(target, "DELETE")[0] == 404
    assert _req(url) == [keep]


def test_provider_key_lifecycle(server):
    base, orch = server
    providers = {p["id"]: p for p in _req(f"{base}/api/providers")}
    assert providers["claude"]["configured"] is False
    assert providers["claude"]["implemented"] is True
    assert providers["codex"]["implemented"] is True
    # unsigned providers have no baked-in model list; the vendor fills it
    assert providers["claude"]["models"] == []

    out = _req(f"{base}/api/providers/claude/key", "POST", {"api_key": "sk-secret-123"})
    assert out["configured"] is True
    # key is stored server-side, never echoed back
    assert "sk-secret-123" not in json.dumps(out)
    assert (orch.paths.credentials / "ANTHROPIC_API_KEY").read_text() == "sk-secret-123"

    providers = {p["id"]: p for p in _req(f"{base}/api/providers")}
    assert providers["claude"]["configured"] is True

    out = _req(f"{base}/api/providers/claude/key", "DELETE")
    assert out["configured"] is False
    assert not (orch.paths.credentials / "ANTHROPIC_API_KEY").exists()


def test_key_delete_leaves_oauth_tokens(server):
    """Removing the grok API key must not sign the user out of xAI OAuth."""
    base, orch = server
    _req(f"{base}/api/providers/grok/key", "POST", {"api_key": "xai-secret"})
    token_file = orch.paths.credentials / "XAI_OAUTH"
    token_file.write_text('{"access_token": "tok"}', encoding="utf-8")

    out = _req(f"{base}/api/providers/grok/key", "DELETE")
    assert out["removed"] is True
    assert not (orch.paths.credentials / "XAI_API_KEY").exists()
    assert token_file.exists()
    # OAuth tokens still count as configured
    assert out["configured"] is True

    out = _req(f"{base}/api/providers/grok/oauth", "DELETE")
    assert out["removed"] is True
    assert not token_file.exists()
    assert out["configured"] is False


def test_oauth_start_runs_pkce_for_claude_and_codex(server, monkeypatch, tmp_path):
    """Claude/Codex start a harness-run OAuth flow, not a CLI-login hint."""
    from providers import anthropic_oauth, codex_oauth

    anthropic_oauth.reset_sessions()
    codex_oauth.reset_sessions()
    base, _ = server
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))

    out = _req(f"{base}/api/providers/claude/oauth/start", "POST", {})
    assert out["status"] == "pending"
    assert out["needs_code"] is True
    assert "claude.ai/oauth/authorize" in out["verification_uri"]

    out = _req(f"{base}/api/providers/codex/oauth/start", "POST", {})
    assert out["status"] == "pending"
    assert out["needs_loopback"] is True
    assert out["redirect_uri"] == "http://localhost:1455/auth/callback"
    assert "auth.openai.com" in out["verification_uri"]

    with pytest.raises(urllib.error.HTTPError) as exc:
        _req(f"{base}/api/providers/nope/oauth/start", "POST", {})
    assert exc.value.code == 501


def test_claude_oauth_exchange_saves_tokens(server, monkeypatch):
    from providers import anthropic_oauth

    anthropic_oauth.reset_sessions()
    base, orch = server

    class Fake:
        def post_json(self, url, data):
            assert data["grant_type"] == "authorization_code"
            assert data["code"] == "auth-code"
            return {
                "access_token": "at-claude",
                "refresh_token": "rt-claude",
                "expires_in": 3600,
            }

        def post_form(self, url, data):
            raise AssertionError("exchange uses JSON")

    started = _req(f"{base}/api/providers/claude/oauth/start", "POST", {})
    monkeypatch.setattr(anthropic_oauth, "Transport", Fake)
    out = _req(
        f"{base}/api/providers/claude/oauth/exchange",
        "POST",
        {"code": "auth-code#" + started["code_verifier"]},
    )
    assert out["status"] == "complete"
    assert (orch.paths.credentials / "ANTHROPIC_OAUTH").is_file()


def test_catalog_hides_echo(server):
    base, _ = server
    ids = {p["id"] for p in _req(f"{base}/api/providers")}
    assert "echo" not in ids
    for expected in ("claude", "codex", "grok", "deepseek", "qwen", "glm", "kimi", "minimax"):
        assert expected in ids


def test_codex_oauth_status_reuses_cli_login(server, monkeypatch, tmp_path):
    import base64
    import os

    from providers import codex_oauth

    codex_oauth.reset_sessions()
    base, _ = server
    home = tmp_path / "codex"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))

    st = _req(f"{base}/api/providers/codex/oauth/status")
    assert st["status"] == "idle"

    payload = base64.urlsafe_b64encode(b'{"aud": "client-1"}').decode().rstrip("=")
    auth = home / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "at",
                    "refresh_token": "rt",
                    "id_token": f"h.{payload}.s",
                    "account_id": "acct",
                },
            }
        ),
        encoding="utf-8",
    )
    os.chmod(auth, 0o600)

    st = _req(f"{base}/api/providers/codex/oauth/status")
    assert st["status"] == "complete"
    providers = {p["id"]: p for p in _req(f"{base}/api/providers")}
    assert providers["codex"]["configured"] is True


def test_grok_oauth_start_returns_device_code(server, monkeypatch):
    base, orch = server

    def fake_start(paths, provider="grok"):
        assert paths == orch.paths
        return {
            "provider": "grok",
            "status": "pending",
            "user_code": "WDJB-MJHT",
            "verification_uri": "https://accounts.x.ai/connect/device",
            "verification_uri_complete": "https://accounts.x.ai/connect/device?user_code=WDJB-MJHT",
            "expires_in": 900,
            "message": "Open the verification URL and approve access.",
        }

    monkeypatch.setattr("harness.server.start_grok_oauth", fake_start)
    out = _req(f"{base}/api/providers/grok/oauth/start", "POST", {})
    assert out["status"] == "pending"
    assert out["user_code"] == "WDJB-MJHT"
    assert out["verification_uri_complete"].startswith("https://accounts.x.ai/")

    monkeypatch.setattr(
        "harness.server.grok_oauth_status",
        lambda provider, paths: {"provider": "grok", "status": "pending", "user_code": "WDJB-MJHT"},
    )
    st = _req(f"{base}/api/providers/grok/oauth/status")
    assert st["status"] == "pending"


def test_grok_oauth_start_drains_body_on_keepalive(server, monkeypatch):
    """POST `{}` then GET on the same connection must not 501 as `{}GET`."""
    base, _ = server
    monkeypatch.setattr(
        "harness.server.start_grok_oauth",
        lambda paths, provider="grok": {
            "provider": "grok",
            "status": "pending",
            "user_code": "ABCD-1234",
            "verification_uri": "https://accounts.x.ai/oauth2/device",
            "verification_uri_complete": "https://accounts.x.ai/oauth2/device?user_code=ABCD-1234",
            "expires_in": 900,
            "message": "Open the verification URL and approve access.",
        },
    )
    monkeypatch.setattr(
        "harness.server.grok_oauth_status",
        lambda provider, paths: {"provider": "grok", "status": "pending", "user_code": "ABCD-1234"},
    )
    parsed = urllib.parse.urlparse(base)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        body = b"{}"
        conn.request(
            "POST",
            "/api/providers/grok/oauth/start",
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        r1 = conn.getresponse()
        raw1 = r1.read()
        assert r1.status == 200, raw1
        conn.request("GET", "/api/providers/grok/oauth/status")
        r2 = conn.getresponse()
        raw2 = r2.read()
        assert r2.status == 200, raw2
        assert b"Unsupported method" not in raw2
        assert json.loads(raw2)["status"] == "pending"
    finally:
        conn.close()


def test_bot_crud(server):
    base, orch = server
    created = _req(
        f"{base}/api/bots",
        "POST",
        {"name": "zephyr", "role": "tester", "provider": "echo", "avatar": "sparkles"},
    )
    assert created["name"] == "zephyr"
    spaced = _req(
        f"{base}/api/bots",
        "POST",
        {"name": "Chief of Staff", "provider": "echo"},
    )
    assert spaced["name"] == "chief-of-staff"
    assert spaced["title"] == "Chief of Staff"
    assert spaced["personality"] == "Chief of Staff"
    listed = {b["name"]: b for b in _req(f"{base}/api/bots")}
    assert listed["chief-of-staff"]["title"] == "Chief of Staff"
    patched = _req(f"{base}/api/bots/chief-of-staff", "PATCH", {"title": "Chief Of Staff"})
    assert patched["title"] == "Chief Of Staff"
    names = {b["name"] for b in _req(f"{base}/api/bots")}
    assert "zephyr" in names
    # persisted to the JSON store
    store = json.loads((orch.paths.home / "roster.json").read_text())
    assert any(b["name"] == "zephyr" for b in store["bots"])

    updated = _req(f"{base}/api/bots/zephyr", "PATCH", {"role": "updated role"})
    assert updated["role"] == "updated role"
    llm = _req(
        f"{base}/api/bots/zephyr",
        "PATCH",
        {"provider": "codex", "model": "gpt-5", "reasoning": "high"},
    )
    assert llm["provider"] == "codex"
    assert llm["model"] == "gpt-5"
    assert llm["reasoning"] == "high"
    listed = {b["name"]: b for b in _req(f"{base}/api/bots")}
    assert listed["zephyr"]["reasoning"] == "high"
    providers = {p["id"]: p for p in _req(f"{base}/api/providers")}
    assert "gpt-5" in providers["codex"]["models"]  # current bot model is included
    described = _req(
        f"{base}/api/bots/zephyr",
        "PATCH",
        {"personality": "Notes about zephyr"},
    )
    assert described["personality"] == "Notes about zephyr"
    listed = {b["name"]: b for b in _req(f"{base}/api/bots")}
    assert listed["zephyr"]["personality"] == "Notes about zephyr"
    store = json.loads((orch.paths.home / "roster.json").read_text())
    zephyr = next(b for b in store["bots"] if b["name"] == "zephyr")
    assert zephyr["personality"] == "Notes about zephyr"

    _req(f"{base}/api/bots/zephyr", "DELETE")
    names = {b["name"] for b in _req(f"{base}/api/bots")}
    assert "zephyr" not in names


def test_add_bot_uses_account_default_when_provider_omitted(server):
    base, orch = server
    first = _req(f"{base}/api/bots", "POST", {"name": "scout"})
    assert first["provider"] == "echo"
    _req(
        f"{base}/api/settings",
        "PATCH",
        {"default_provider": "grok", "default_model": "grok-4"},
    )
    second = _req(f"{base}/api/bots", "POST", {"name": "scout-2"})
    assert second["provider"] == "grok"
    assert second["model"] == "grok-4"
    named = _req(f"{base}/api/bots", "POST", {"name": "scout-3", "provider": "echo"})
    assert named["provider"] == "echo"
    assert not named.get("model")


def test_bot_and_group_count_is_capped_at_twenty_five(server):
    base, _orch = server
    for i in range(2, 25):
        _req(
            f"{base}/api/bots",
            "POST",
            {"name": f"bot-{i}", "provider": "echo", "start": False, "welcome": False},
        )
    assert len(_req(f"{base}/api/bots")) == 24

    room = _req(f"{base}/api/rooms", "POST", {"title": "Pair", "members": ["atlas", "bot-2"]})
    assert room["members"] == ["atlas", "bot-2"]

    code, body = _req_error(
        f"{base}/api/bots",
        "POST",
        {"name": "bot-25", "provider": "echo", "start": False, "welcome": False},
    )
    assert code == 400
    assert body["error"] == "Dotobot supports up to 25 bots and groups combined"

    code, body = _req_error(
        f"{base}/api/rooms", "POST", {"title": "Extra", "members": ["atlas", "bot-2"]}
    )
    assert code == 400
    assert body["error"] == "Dotobot supports up to 25 bots and groups combined"


def test_group_members_are_capped_at_six_via_api(server):
    base, _orch = server
    for i in range(2, 8):
        _req(
            f"{base}/api/bots",
            "POST",
            {"name": f"bot-{i}", "provider": "echo", "start": False, "welcome": False},
        )
    seven = ["atlas", "bot-2", "bot-3", "bot-4", "bot-5", "bot-6", "bot-7"]

    code, body = _req_error(f"{base}/api/rooms", "POST", {"title": "Too Big", "members": seven})
    assert code == 400
    assert body["error"] == "A group chat can include at most six bots"

    room = _req(f"{base}/api/rooms", "POST", {"title": "Six", "members": seven[:6]})
    code, body = _req_error(
        f"{base}/api/rooms/{room['id']}",
        "PATCH",
        {"members": seven},
    )
    assert code == 400
    assert body["error"] == "A group chat can include at most six bots"


def test_caveman_account_default_and_per_bot_override(server):
    """Account on can be switched off per bot; account off can be switched
    on per bot; a null override follows the account again."""
    base, orch = server
    assert _req(f"{base}/api/settings")["caveman"] is False
    atlas = next(b for b in _req(f"{base}/api/bots") if b["name"] == "atlas")
    assert atlas["caveman"] is None
    assert atlas["caveman_effective"] is False

    assert _req(f"{base}/api/settings", "PATCH", {"caveman": True})["caveman"] is True
    atlas = next(b for b in _req(f"{base}/api/bots") if b["name"] == "atlas")
    assert atlas["caveman"] is None
    assert atlas["caveman_effective"] is True

    out = _req(f"{base}/api/bots/atlas", "PATCH", {"caveman": False})
    assert out["caveman"] is False
    atlas = next(b for b in _req(f"{base}/api/bots") if b["name"] == "atlas")
    assert atlas["caveman_effective"] is False

    _req(f"{base}/api/settings", "PATCH", {"caveman": False})
    _req(f"{base}/api/bots/atlas", "PATCH", {"caveman": True})
    atlas = next(b for b in _req(f"{base}/api/bots") if b["name"] == "atlas")
    assert atlas["caveman"] is True
    assert atlas["caveman_effective"] is True

    _req(f"{base}/api/bots/atlas", "PATCH", {"caveman": None})
    atlas = next(b for b in _req(f"{base}/api/bots") if b["name"] == "atlas")
    assert atlas["caveman"] is None
    assert atlas["caveman_effective"] is False

    store = json.loads((orch.paths.home / "roster.json").read_text())
    assert next(b for b in store["bots"] if b["name"] == "atlas")["caveman"] is None
    # A PATCH of another setting never wipes the flag.
    _req(f"{base}/api/settings", "PATCH", {"caveman": True})
    _req(f"{base}/api/settings", "PATCH", {"default_provider": "echo"})
    assert _req(f"{base}/api/settings")["caveman"] is True


def test_content_filter_defaults_on_round_trips_and_reaches_the_prompt(server):
    """Guideline 1.2: a filter the owner can switch, on by default, and a
    system-prompt line every bot carries while it is on."""
    from agent.contentfilter import PROMPT
    from agent.memory import Memory
    from agent.runtime import Agent
    from harness.control import Control
    from harness.roster import Bot
    from providers.echo import EchoProvider

    base, orch = server
    assert _req(f"{base}/api/settings")["content_filter"] is True
    agent = Agent(
        paths=orch.paths,
        bot=Bot(name="atlas", role="assistant", provider="echo"),
        provider=EchoProvider(),
        memory=Memory(paths=orch.paths, bot="atlas"),
        control=Control(orch.paths),
    )
    assert PROMPT in agent.system_prompt("hello")
    assert (
        _req(f"{base}/api/settings", "PATCH", {"content_filter": False})["content_filter"] is False
    )
    assert PROMPT not in agent.system_prompt("hello"), "read every turn, no restart"
    # a PATCH of another setting never flips it back
    _req(f"{base}/api/settings", "PATCH", {"caveman": True})
    assert _req(f"{base}/api/settings")["content_filter"] is False
    settings = json.loads((orch.paths.home / "settings.json").read_text())
    assert settings["content_filter"] is False


def test_consents_round_trip_and_survive_reload(server):
    """Guideline 5.1.2(i): the consent to send content to a third-party AI is
    recorded per provider / connector, never refused server-side."""
    base, orch = server
    assert _req(f"{base}/api/settings")["consents"] == {}
    out = _req(f"{base}/api/settings", "PATCH", {"consents": {"provider:claude": 1_800_000_000}})
    assert out["consents"] == {"provider:claude": 1_800_000_000}
    out = _req(f"{base}/api/settings", "PATCH", {"consents": {"connector:linear": "1800000001"}})
    assert out["consents"] == {"provider:claude": 1_800_000_000, "connector:linear": 1_800_000_001}
    # withdrawing removes the entry; a bad key is a 400 that changes nothing
    out = _req(f"{base}/api/settings", "PATCH", {"consents": {"provider:claude": None}})
    assert out["consents"] == {"connector:linear": 1_800_000_001}
    code, body = _req_error(f"{base}/api/settings", "PATCH", {"consents": {"bogus": 1}})
    assert code == 400 and "consent" in body["error"]
    code, _ = _req_error(f"{base}/api/settings", "PATCH", {"consents": ["provider:x"]})
    assert code == 400
    assert _req(f"{base}/api/settings")["consents"] == {"connector:linear": 1_800_000_001}
    # a provider key still saves without a consent entry (soft gate)
    _req(f"{base}/api/providers/echo/key", "POST", {"api_key": "irrelevant"})
    settings = json.loads((orch.paths.home / "settings.json").read_text())
    assert settings["consents"] == {"connector:linear": 1800000001}


def test_blocked_bot_stays_on_the_roster_but_never_gets_a_turn(server):
    """Guideline 1.2: Block bot beside Report. The bot keeps its memory and
    settings; the harness just never dispatches it until it is unblocked."""
    base, orch = server
    atlas = next(b for b in _req(f"{base}/api/bots") if b["name"] == "atlas")
    assert atlas["blocked"] is False
    assert orch.recipients_for("hello", bot="atlas") == ["atlas"]
    out = _req(f"{base}/api/bots/atlas", "PATCH", {"blocked": True})
    assert out["blocked"] is True
    assert orch.recipients_for("hello", bot="atlas") == []
    assert orch.recipients_for("@atlas hello", bot=None) == []
    code, body = _req_error(f"{base}/api/chat", "POST", {"bot": "atlas", "text": "hi"})
    assert code in (400, 404, 409, 500) and "no bots" in body["error"]
    store = json.loads((orch.paths.home / "roster.json").read_text())
    assert next(b for b in store["bots"] if b["name"] == "atlas")["blocked"] is True
    # unblocking restores the turn; a PATCH of another field never flips it
    _req(f"{base}/api/bots/atlas", "PATCH", {"role": "a helper"})
    assert orch.recipients_for("hello", bot="atlas") == []
    _req(f"{base}/api/bots/atlas", "PATCH", {"blocked": False})
    assert orch.recipients_for("hello", bot="atlas") == ["atlas"]


def test_blocked_bot_gets_no_routine_or_dream_turn_either(server, monkeypatch):
    """Chat routing is not the only way a bot gets a turn: the routine tick,
    a routine test run and the dream tick all honour the block too."""
    from harness import dreaming
    from harness.routines import add_routine, scheduled_bots

    base, orch = server
    monkeypatch.setenv("HARNESS_DREAM_MIN_SECS", "600")
    routine = add_routine(
        orch.paths, "atlas", title="Ping", prompt="say hi", when="8am", enabled=True
    )
    _req(f"{base}/api/bots/atlas", "PATCH", {"blocked": True, "dreaming": True})
    assert orch.is_blocked("atlas")
    assert "atlas" not in scheduled_bots(orch.roster)
    atlas = orch.roster.get("atlas")
    assert dreaming.fire_due(orch.paths, [atlas], now=1_000_000.0) == []
    assert dreaming.fire_due(orch.paths, [atlas], now=1_000_700.0) == []
    code, body = _req_error(f"{base}/api/bots/atlas/routines/{routine['id']}/run", "POST", {})
    assert code == 409 and "blocked" in body["error"]
    assert list(orch.paths.inbox("atlas").glob("*.json")) == []
    _req(f"{base}/api/bots/atlas", "PATCH", {"blocked": False})
    assert not orch.is_blocked("atlas")
    assert "atlas" in scheduled_bots(orch.roster)


def test_restart_bot_respawns_and_keeps_the_inbox(server):
    from agent import messaging

    base, orch = server
    messaging.send(orch.paths, messaging.Msg(to="atlas", frm="user", text="hi"))
    before = next(h for h in orch.status() if h.bot == "atlas")
    out = _req(f"{base}/api/bots/atlas/restart", "POST")
    assert out["name"] == "atlas"
    assert out["status"] in {"starting", "restarting", "running"}
    import time

    deadline = time.monotonic() + 3
    while orch.is_starting("atlas") and time.monotonic() < deadline:
        time.sleep(0.01)
    after = next(h for h in orch.status() if h.bot == "atlas")
    assert after.pid
    assert after.pid != before.pid
    q = _req(f"{base}/api/bots/atlas/queue")
    assert q["queued"] >= 1
    assert any(i["text"] == "hi" for i in q["items"])


def test_restart_unknown_bot_is_404(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _req(f"{base}/api/bots/nope/restart", "POST")
    assert exc.value.code == 404


def test_restart_is_allowed_while_a_human_has_control(server):
    """Restart used to 409 while a human held the desktop. The shared computer
    dropped that guard: they drive it together, so holding control is no longer
    a reason to refuse a restart."""
    base, orch = server
    orch.control.take_over("atlas")
    try:
        assert _req(f"{base}/api/bots/atlas/restart", "POST")
    finally:
        orch.control.return_control("atlas")


def test_duplicate_bot_rejected(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _req(f"{base}/api/bots", "POST", {"name": "atlas", "provider": "echo"})
    assert exc.value.code == 400


def test_duplicate_bot_copies_profile_not_memory(server):
    base, orch = server
    Memory(paths=orch.paths, bot="atlas").remember("atlas only secret")
    from agent.skills import propose_skill

    propose_skill(
        orch.paths,
        "atlas",
        name="invoice-run",
        description="grab invoices",
        body="1. open the portal",
        when_to_use="invoices",
    )
    from harness.routines import add_routine

    add_routine(
        orch.paths, "atlas", title="Nightly", prompt="check mail", when="8am", enabled=False
    )
    copied = _req(f"{base}/api/bots/atlas/duplicate", "POST", {"name": "atlas-copy"})
    assert copied["name"] == "atlas-copy"
    assert copied["role"] == "a terse research assistant"
    assert copied["provider"] == "echo"
    names = {b["name"] for b in _req(f"{base}/api/bots")}
    assert {"atlas", "atlas-copy"} <= names
    from agent.memory import Memory as Mem
    from agent.skills import load_skills

    assert Mem(paths=orch.paths, bot="atlas-copy").recall("atlas only secret") == []
    assert any(s.name == "invoice-run" for s in load_skills(orch.paths, "atlas-copy"))
    from harness.routines import list_routines

    dest_routines = list_routines(orch.paths, "atlas-copy")
    assert dest_routines and dest_routines[0]["title"] == "Nightly"
    assert dest_routines[0]["id"] != list_routines(orch.paths, "atlas")[0]["id"]


def test_duplicate_corrupt_routines_does_not_create_partial_bot(server):
    from harness.roster import RosterError

    _, orch = server
    path = orch.paths.bot_routines("atlas")
    path.parent.mkdir(parents=True, exist_ok=True)
    original = '{"routines": [{"id": "existing"}]} trailing data'
    path.write_text(original)
    roster_before = (orch.paths.home / "roster.json").read_bytes()
    with pytest.raises(RosterError, match="cannot read routines"):
        orch.duplicate_bot("atlas", name="atlas-copy", start=False)
    assert orch.roster.names() == ["atlas"]
    assert (orch.paths.home / "roster.json").read_bytes() == roster_before
    assert not orch.paths.bot_memory("atlas-copy").exists()
    assert path.read_text() == original


def test_duplicate_corrupt_routines_returns_a_useful_api_error(server):
    base, orch = server
    path = orch.paths.bot_routines("atlas")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"routines": [')
    code, body = _req_error(f"{base}/api/bots/atlas/duplicate", "POST", {"name": "atlas-copy"})
    assert code == 400
    assert "cannot read routines" in body["error"]
    assert orch.roster.names() == ["atlas"]


def test_duplicate_uses_validated_routine_snapshot(server, monkeypatch):
    from harness.routines import add_routine, list_routines

    _, orch = server
    add_routine(orch.paths, "atlas", title="Nightly", prompt="Keep", when="8am")
    add_bot = orch.add_bot

    def corrupt_source_after_creation(**fields):
        bot = add_bot(**fields)
        orch.paths.bot_routines("atlas").write_text('{"routines": [')
        return bot

    monkeypatch.setattr(orch, "add_bot", corrupt_source_after_creation)
    copied = orch.duplicate_bot("atlas", name="atlas-copy", start=False)
    assert copied.name == "atlas-copy"
    assert list_routines(orch.paths, copied.name)[0]["prompt"] == "Keep"


def test_connectors_catalog_and_crud(server):
    base, orch = server
    catalog = {c["type"]: c for c in _req(f"{base}/api/connectors/catalog")}
    assert {"slack", "github", "google", "linear"} <= set(catalog)
    # gallery metadata for the plugin-store UI
    assert catalog["linear"]["category"] == "Project Management"
    assert catalog["linear"]["icon"]
    assert catalog["linear"]["implemented"] is True
    assert "linear_create_issue" in catalog["linear"]["tools"]
    assert catalog["slack"]["implemented"] is True
    assert catalog["slack"]["mcp_url"] == "https://mcp.slack.com/mcp"

    rec = _req(
        f"{base}/api/connectors",
        "POST",
        {"type": "github", "name": "gh", "config": {"org": "acme"}, "secret": "ghp_x"},
    )
    assert rec["type"] == "github"
    assert rec["secret_configured"] is True
    assert rec["enabled_for"] is None
    assert "ghp_x" not in json.dumps(rec)

    listing = _req(f"{base}/api/connectors")
    assert any(c["id"] == rec["id"] for c in listing)

    out = _req(f"{base}/api/connectors/{rec['id']}", "DELETE")
    assert out["ok"] is True
    assert all(c["id"] != rec["id"] for c in _req(f"{base}/api/connectors"))


def test_connector_bot_scoping_via_api(server):
    base, orch = server
    rec = _req(
        f"{base}/api/connectors",
        "POST",
        {"type": "linear", "name": "Linear", "secret": "lin_x", "enabled_for": ["atlas"]},
    )
    assert rec["enabled_for"] == ["atlas"]
    assert "linear_list_issues" in rec["tools"]

    patched = _req(f"{base}/api/connectors/{rec['id']}", "PATCH", {"enabled_for": None})
    assert patched["enabled_for"] is None
    patched = _req(f"{base}/api/connectors/{rec['id']}", "PATCH", {"name": "Linear (work)"})
    assert patched["name"] == "Linear (work)"
    assert patched["enabled_for"] is None  # untouched when absent from the PATCH

    with pytest.raises(urllib.error.HTTPError) as exc:
        _req(f"{base}/api/connectors/{rec['id']}", "PATCH", {"enabled_for": "atlas"})
    assert exc.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as exc:
        _req(f"{base}/api/connectors/nope", "PATCH", {"enabled_for": None})
    assert exc.value.code == 404


def test_soul_memory_skills_and_rooms_api(server):
    base, orch = server
    bots = {b["name"]: b for b in _req(f"{base}/api/bots")}
    assert bots["atlas"]["color"].startswith("#")

    skills = _req(f"{base}/api/skills?bot=atlas")
    names = {s["name"] for s in skills}
    assert {"memory", "remember", "soul", "skills"} <= names

    soul = _req(f"{base}/api/bots/atlas/soul")
    assert soul["bot"] == "atlas"
    updated = _req(f"{base}/api/bots/atlas/soul", "PUT", {"soul": "I am atlas."})
    assert "I am atlas." in updated["soul"]

    _req(f"{base}/api/bots/atlas/memory", "POST", {"text": "likes tea"})
    facts = _req(f"{base}/api/bots/atlas/memory")["facts"]
    assert any("tea" in f.get("text", "") for f in facts)

    mem = Memory(paths=orch.paths, bot="atlas")
    mem.log_turn("s1", "in:user", "hello from the app", peer="user")
    mem.log_turn("s1", "out", "hello back", peer="user")
    history = _req(f"{base}/api/bots/atlas/history")
    assert [(r["frm"], r["text"]) for r in history] == [
        ("user", "hello from the app"),
        ("atlas", "hello back"),
    ]
    q = _req(f"{base}/api/bots/atlas/queue")
    assert q["queued"] == 0
    assert q["busy"] is False

    _req(f"{base}/api/bots", "POST", {"name": "nova", "provider": "echo", "role": "writer"})
    room = _req(f"{base}/api/rooms", "POST", {"title": "Pair", "members": ["atlas", "nova"]})
    assert room["members"] == ["atlas", "nova"]
    assert room["owner"] == "user"
    listing = _req(f"{base}/api/rooms")
    assert any(r["id"] == room["id"] for r in listing)
    got = _req(f"{base}/api/rooms/{room['id']}")
    assert got["title"] == "Pair"
    patched = _req(f"{base}/api/rooms/{room['id']}", "PATCH", {"owner": "nova"})
    assert patched["owner"] == "user"
    _req(f"{base}/api/rooms/{room['id']}", "DELETE")
    assert all(r["id"] != room["id"] for r in _req(f"{base}/api/rooms"))


def test_unknown_connector_type_rejected(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _req(f"{base}/api/connectors", "POST", {"type": "myspace", "name": "x"})
    assert exc.value.code == 400


def test_bot_color_persists_and_overrides_hash(server):
    base, orch = server
    _req(f"{base}/api/bots", "POST", {"name": "prism", "provider": "echo"})
    listed = {b["name"]: b for b in _req(f"{base}/api/bots")}
    # No explicit color: the stable name-hash swatch is served.
    from harness.colors import bot_color

    assert listed["prism"]["color"] == bot_color("prism")

    patched = _req(f"{base}/api/bots/prism", "PATCH", {"color": "#E5484D"})
    assert patched["color"] == "#E5484D"
    listed = {b["name"]: b for b in _req(f"{base}/api/bots")}
    assert listed["prism"]["color"] == "#E5484D"

    # Persisted to the JSON store alongside avatar.
    store = json.loads((orch.paths.home / "roster.json").read_text())
    row = next(b for b in store["bots"] if b["name"] == "prism")
    assert row["color"] == "#E5484D"

    # Avatar (shape) edits are cosmetic and land without erroring.
    patched = _req(f"{base}/api/bots/prism", "PATCH", {"avatar": "hexagon"})
    assert patched["avatar"] == "hexagon"


def test_provider_picker_does_not_reinsert_saved_claude_three(server):
    base, _orch = server
    _req(
        f"{base}/api/bots/atlas",
        "PATCH",
        {
            "provider": "claude",
            "model": "claude-3-5-sonnet-latest",
        },
    )
    providers = {p["id"]: p for p in _req(f"{base}/api/providers")}
    assert "claude-3-5-sonnet-latest" not in providers["claude"]["models"]
    assert _req(f"{base}/api/bots")[0]["model"] == "claude-3-5-sonnet-latest"


def test_restart_acknowledges_slow_start_and_coalesces_requests(server, monkeypatch):
    """A retry from either app must not stop a bot twice during provisioning."""
    import time

    from isolation.base import BotHandle, Status

    base, orch = server
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def spawn(name, argv):
        calls.append(name)
        entered.set()
        assert release.wait(5)
        return BotHandle(bot=name, backend="process", status=Status.RUNNING)

    monkeypatch.setattr(orch.backend, "spawn", spawn)
    try:
        start = time.monotonic()
        row = _req(f"{base}/api/bots/atlas/restart", "POST")
        assert time.monotonic() - start < 1
        assert row["status"] == "restarting"
        assert entered.wait(1)
        assert orch.is_starting("atlas")
        assert _req(f"{base}/api/bots")[0]["status"] == "restarting"
        _req(f"{base}/api/bots/atlas/restart", "POST")
        assert calls == ["atlas"]
    finally:
        release.set()
        deadline = time.monotonic() + 2
        while orch.is_starting("atlas") and time.monotonic() < deadline:
            time.sleep(0.01)
    assert not orch.is_starting("atlas")


def test_failed_async_restart_exposes_error_and_allows_retry(server, monkeypatch):
    import time

    from isolation.base import IsolationUnavailable

    base, orch = server
    original = orch.backend.spawn

    def fail(*args):
        raise IsolationUnavailable("computer unavailable")

    monkeypatch.setattr(orch.backend, "spawn", fail)
    _req(f"{base}/api/bots/atlas/restart", "POST")
    deadline = time.monotonic() + 2
    while orch.is_starting("atlas") and time.monotonic() < deadline:
        time.sleep(0.01)
    row = _req(f"{base}/api/bots")[0]
    assert row["status"] == "stopped"
    assert row["startup_error"] == "computer unavailable"
    monkeypatch.setattr(orch.backend, "spawn", original)
    assert orch.restart("atlas").pid
    assert not orch._startup_error_path("atlas").exists()


@pytest.mark.parametrize("fail", [False, True], ids=["success", "failure"])
def test_automatic_update_pushes_completion_to_connected_client(server, monkeypatch, fail):
    """An idle app sees the roll settle without a fetch or manual restart."""
    from harness.agent_updater import roll_once
    from harness.version import __version__
    from isolation.base import BotHandle, IsolationUnavailable, Status
    from tests.test_ws import WSClient

    base, orch = server
    handle = BotHandle(
        bot="atlas", backend="process", status=Status.RUNNING, meta={"version": "0.0.1"}
    )
    entered, release = threading.Event(), threading.Event()
    errors = []

    def stop(old):
        nonlocal handle
        handle = BotHandle(bot="atlas", backend="process", status=Status.STOPPED)

    def spawn(name, argv):
        nonlocal handle
        entered.set()
        assert release.wait(5)
        if fail:
            raise IsolationUnavailable("computer unavailable")
        handle = BotHandle(
            bot=name, backend="process", status=Status.RUNNING, meta={"version": __version__}
        )
        return handle

    def roll():
        try:
            roll_once(orch, log=lambda _: None)
        except IsolationUnavailable as exc:
            errors.append(str(exc))

    monkeypatch.setattr(orch, "_handle", lambda _: handle)
    monkeypatch.setattr(orch.backend, "stop", stop)
    monkeypatch.setattr(orch.backend, "spawn", spawn)
    client = None
    worker = threading.Thread(target=roll)
    try:
        worker.start()
        assert entered.wait(1)
        # Reconnecting during a roll must not leave its preparation snapshot stuck.
        client = WSClient("127.0.0.1", int(base.rsplit(":", 1)[1]))
        client.sock.settimeout(2)
        preparing = next(frame for frame in client.seed if frame["type"] == "bots")
        assert preparing["bots"][0]["status"] == "updating"
        release.set()
        worker.join(3)
        assert not worker.is_alive()
        settled = client.recv_until("bots")[-1]
        assert settled["mutation"] == "snapshot"
        assert settled["bots"][0]["status"] == ("stopped" if fail else "running")
        assert settled["seq"] > preparing["seq"]
        if fail:
            assert errors == ["computer unavailable"]
            assert settled["bots"][0]["startup_error"] == errors[0]
        else:
            assert errors == []
            assert "startup_error" not in settled["bots"][0]
    finally:
        release.set()
        worker.join(3)
        if client is not None:
            client.close()
