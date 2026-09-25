"""Connector OAuth for remote MCP servers + the generic MCP tool runtime."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from connectors import mcp
from connectors.base import ConnectorContext
from connectors.registry import tools_for_bot
from harness import mcp_oauth
from harness.connectors import Connectors, catalog
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.server import make_server

MCP_URL = "https://mcp.example.com/mcp"
AUTH = "https://auth.example.com"


@pytest.fixture(autouse=True)
def clean_state():
    mcp_oauth.reset_flows()
    mcp.reset_sessions()
    yield
    mcp_oauth.reset_flows()
    mcp.reset_sessions()


@pytest.fixture
def paths(tmp_path):
    return HarnessPaths(home=tmp_path)


class FakeAuth(mcp_oauth.Transport):
    """A spec-compliant service: PRM discovery, AS metadata, DCR, tokens."""

    def __init__(self):
        self.registered: list[dict] = []
        self.token_forms: list[dict] = []

    def get_json(self, url):
        if url == "https://mcp.example.com/.well-known/oauth-protected-resource/mcp":
            return {
                "resource": MCP_URL,
                "authorization_servers": [AUTH],
                "scopes_supported": ["read", "write"],
            }
        if url == f"{AUTH}/.well-known/oauth-authorization-server":
            return {
                "issuer": AUTH,
                "authorization_endpoint": f"{AUTH}/authorize",
                "token_endpoint": f"{AUTH}/token",
                "registration_endpoint": f"{AUTH}/register",
            }
        raise mcp_oauth.OAuthError(f"404: {url}")

    def post_json(self, url, payload):
        assert url == f"{AUTH}/register"
        self.registered.append(payload)
        return {"client_id": f"client-{len(self.registered)}"}

    def post_form(self, url, data):
        assert url == f"{AUTH}/token"
        self.token_forms.append(data)
        return {
            "access_token": f"at-{len(self.token_forms)}",
            "refresh_token": f"rt-{len(self.token_forms)}",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "read write",
        }


def _start(paths, record, redirect="http://127.0.0.1:9999/callback", transport=None):
    t = transport or FakeAuth()
    return mcp_oauth.start_authorize(paths, record, MCP_URL, redirect, t), t


# -- discovery + authorize URL ---------------------------------------------


def test_discover_resolves_endpoints_via_protected_resource():
    meta = mcp_oauth.discover(MCP_URL, FakeAuth())
    assert meta["authorization_endpoint"] == f"{AUTH}/authorize"
    assert meta["token_endpoint"] == f"{AUTH}/token"
    assert meta["registration_endpoint"] == f"{AUTH}/register"
    assert meta["scopes"] == "read write"


def test_discover_falls_back_to_mcp_origin_as_issuer():
    class OriginAS(mcp_oauth.Transport):
        def get_json(self, url):
            if url == "https://mcp.example.com/.well-known/openid-configuration":
                return {
                    "authorization_endpoint": "https://mcp.example.com/authorize",
                    "token_endpoint": "https://mcp.example.com/token",
                }
            raise mcp_oauth.OAuthError(f"404: {url}")

    meta = mcp_oauth.discover(MCP_URL, OriginAS())
    assert meta["authorization_endpoint"] == "https://mcp.example.com/authorize"
    assert meta["scopes"] == ""


def test_discover_rejects_plain_http_endpoints():
    class Insecure(mcp_oauth.Transport):
        def get_json(self, url):
            if "openid-configuration" in url:
                return {
                    "authorization_endpoint": "http://evil.example.com/authorize",
                    "token_endpoint": "http://evil.example.com/token",
                }
            raise mcp_oauth.OAuthError(f"404: {url}")

    with pytest.raises(mcp_oauth.OAuthError, match="not HTTPS"):
        mcp_oauth.discover(MCP_URL, Insecure())


def test_start_authorize_builds_consent_url(paths):
    record = {"id": "abc123", "type": "linear"}
    start, t = _start(paths, record)
    assert start["status"] == "pending"
    parsed = urllib.parse.urlparse(start["authorize_url"])
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == f"{AUTH}/authorize"
    q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
    assert q["response_type"] == "code"
    assert q["client_id"] == "client-1"
    assert q["redirect_uri"] == "http://127.0.0.1:9999/callback"
    assert q["code_challenge_method"] == "S256"
    assert q["code_challenge"]
    assert q["resource"] == MCP_URL
    assert q["scope"] == "read write"
    assert q["state"] == start["state"]
    # DCR asked for a public client with our redirect
    assert t.registered[0]["redirect_uris"] == ["http://127.0.0.1:9999/callback"]
    assert t.registered[0]["token_endpoint_auth_method"] == "none"


def test_dcr_missing_is_a_clear_error(paths):
    class NoDCR(FakeAuth):
        def get_json(self, url):
            meta = super().get_json(url)
            meta.pop("registration_endpoint", None)
            return meta

    with pytest.raises(mcp_oauth.OAuthError, match="dynamic client registration"):
        _start(paths, {"id": "abc123", "type": "linear"}, transport=NoDCR())


def test_static_client_skips_dcr(paths):
    class NoDCR(FakeAuth):
        def get_json(self, url):
            meta = super().get_json(url)
            meta.pop("registration_endpoint", None)
            return meta

    t = NoDCR()
    start = mcp_oauth.start_authorize(
        paths,
        {"id": "abc123", "type": "slack", "config": {}},
        MCP_URL,
        "http://127.0.0.1:18765/callback",
        t,
        client_id="slack-app-id",
        client_secret="slack-app-secret",
    )
    q = {
        k: v[0]
        for k, v in urllib.parse.parse_qs(
            urllib.parse.urlparse(start["authorize_url"]).query
        ).items()
    }
    assert q["client_id"] == "slack-app-id"
    assert t.registered == []
    stored = mcp_oauth.load_oauth_client(paths, "abc123")
    assert stored == {"client_id": "slack-app-id", "client_secret": "slack-app-secret"}
    result = mcp_oauth.exchange(paths, start["state"], "the-code", t)
    assert result["status"] == "connected"
    assert t.token_forms[0]["client_secret"] == "slack-app-secret"


def test_asana_v2_uses_registered_client_without_dynamic_registration(paths):
    entry = next(item for item in catalog() if item["type"] == "asana")
    assert entry["mcp_url"] == "https://mcp.asana.com/v2/mcp"
    assert "client_id" in entry["fields"]

    class AsanaAuth(FakeAuth):
        def get_json(self, url):
            if url == "https://mcp.asana.com/.well-known/oauth-protected-resource/v2/mcp":
                return {
                    "resource": entry["mcp_url"],
                    "authorization_servers": [AUTH],
                    "scopes_supported": ["default"],
                }
            metadata = super().get_json(url)
            metadata.pop("registration_endpoint", None)
            return metadata

    transport = AsanaAuth()
    record = Connectors(paths).add("asana", "Test", config={"client_id": "fixture-client"})
    start = mcp_oauth.start_authorize(
        paths, record, entry["mcp_url"], "http://127.0.0.1:18765/callback",
        transport, client_secret="fixture-secret",
    )
    query = urllib.parse.parse_qs(urllib.parse.urlparse(start["authorize_url"]).query)
    assert query["client_id"] == ["fixture-client"]
    assert query["resource"] == [entry["mcp_url"]]
    assert query["scope"] == ["default"]
    assert transport.registered == []
    assert mcp_oauth.exchange(paths, start["state"], "fixture-code", transport)["status"] == "connected"
    assert transport.token_forms[0]["client_secret"] == "fixture-secret"


# -- code exchange + tokens -------------------------------------------------


def test_exchange_stores_tokens_and_connects(paths):
    record = {"id": "abc123", "type": "linear"}
    start, t = _start(paths, record)
    result = mcp_oauth.exchange(paths, start["state"], "the-code", t)
    assert result["status"] == "connected"
    assert mcp_oauth.connected(paths, "abc123")
    form = t.token_forms[0]
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "the-code"
    assert form["client_id"] == "client-1"
    assert form["resource"] == MCP_URL
    assert form["code_verifier"]
    assert mcp_oauth.access_token(paths, "abc123") == "at-1"
    assert mcp_oauth.status(paths, "abc123")["status"] == "connected"


def test_exchange_state_is_single_use(paths):
    start, t = _start(paths, {"id": "abc123", "type": "linear"})
    mcp_oauth.exchange(paths, start["state"], "the-code", t)
    with pytest.raises(mcp_oauth.OAuthError, match="unknown or expired"):
        mcp_oauth.exchange(paths, start["state"], "the-code", t)


def test_exchange_unknown_state_rejected(paths):
    with pytest.raises(mcp_oauth.OAuthError, match="unknown or expired"):
        mcp_oauth.exchange(paths, "not-a-state", "code", FakeAuth())


def test_denied_authorization_surfaces_as_error_status(paths):
    start, _ = _start(paths, {"id": "abc123", "type": "linear"})
    assert mcp_oauth.status(paths, "abc123")["status"] == "pending"
    assert mcp_oauth.fail(start["state"], "access_denied") == "abc123"
    st = mcp_oauth.status(paths, "abc123")
    assert st["status"] == "error"
    assert "access_denied" in st["message"]


def test_access_token_refreshes_when_stale(paths):
    t = FakeAuth()
    mcp_oauth.save_tokens(
        paths,
        "abc123",
        {
            "access_token": "old",
            "refresh_token": "rt-old",
            "expires_at": 1.0,  # long past
            "token_endpoint": f"{AUTH}/token",
            "client_id": "client-1",
            "client_secret": "",
            "resource": MCP_URL,
        },
    )
    assert mcp_oauth.access_token(paths, "abc123", transport=t) == "at-1"
    form = t.token_forms[0]
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "rt-old"
    assert form["resource"] == MCP_URL
    # rotated refresh token was persisted
    assert mcp_oauth.load_tokens(paths, "abc123")["refresh_token"] == "rt-1"


def test_clear_tokens_signs_out(paths):
    start, t = _start(paths, {"id": "abc123", "type": "linear"})
    mcp_oauth.exchange(paths, start["state"], "code", t)
    assert mcp_oauth.clear_tokens(paths, "abc123") is True
    assert not mcp_oauth.connected(paths, "abc123")
    assert mcp_oauth.status(paths, "abc123")["status"] == "idle"


def test_reconnect_reuses_client_registration(paths):
    record = {"id": "abc123", "type": "linear"}
    start, t = _start(paths, record)
    mcp_oauth.exchange(paths, start["state"], "code", t)
    again, _ = _start(paths, record, transport=t)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(again["authorize_url"]).query)
    assert q["client_id"] == ["client-1"]
    assert len(t.registered) == 1


# -- MCP tool runtime -------------------------------------------------------


def _connect(paths, connector_id):
    """Store a valid (non-expiring) token bundle for a connector."""
    mcp_oauth.save_tokens(
        paths,
        connector_id,
        {
            "access_token": "at-live",
            "refresh_token": "",
            "expires_at": 0,  # no expiry recorded -> never stale
            "token_endpoint": f"{AUTH}/token",
            "client_id": "client-1",
            "client_secret": "",
            "resource": MCP_URL,
        },
    )


def fake_mcp_post(url, token, message, session_id):
    """A tiny MCP server: initialize, tools/list, tools/call."""
    if "id" not in message:
        return None, session_id
    rid = message["id"]
    method = message["method"]
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": mcp.PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "instructions": "Call create_issue to open a ticket.",
            },
        }, "sess-1"
    assert session_id == "sess-1", "requests after initialize must carry the session"
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "tools": [
                    {
                        "name": "create_issue",
                        "description": "Create an issue",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"title": {"type": "string"}},
                            "required": ["title"],
                        },
                    },
                    {"name": "linear_search", "description": "Already prefixed"},
                ]
            },
        }, session_id
    if method == "tools/call":
        name = message["params"]["name"]
        args = message["params"]["arguments"]
        if name == "boom":
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"content": [{"type": "text", "text": "no such issue"}], "isError": True},
            }, session_id
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"content": [{"type": "text", "text": f"called {name}: {json.dumps(args)}"}]},
        }, session_id
    raise AssertionError(f"unexpected method {method}")


def test_initialize_keeps_server_instructions(paths, monkeypatch):
    monkeypatch.setattr(mcp, "_post", fake_mcp_post)
    _connect(paths, "abc123")
    ctx = ConnectorContext(paths=paths, bot="atlas", record={"id": "abc123", "type": "linear"})
    mcp.bind_tools(ctx, MCP_URL)
    assert "create_issue" in mcp.instructions_for("abc123")


def test_bind_tools_namespaces_and_calls(paths, monkeypatch):
    monkeypatch.setattr(mcp, "_post", fake_mcp_post)
    _connect(paths, "abc123")
    ctx = ConnectorContext(paths=paths, bot="atlas", record={"id": "abc123", "type": "linear"})
    tools = mcp.bind_tools(ctx, MCP_URL)
    by_name = {t.spec.name: t for t in tools}
    assert set(by_name) == {"linear_create_issue", "linear_search"}
    spec = by_name["linear_create_issue"].spec
    assert spec.parameters["required"] == ["title"]
    out = by_name["linear_create_issue"].handler(ctx, {"title": "Hi"})
    assert out == 'called create_issue: {"title": "Hi"}'


def test_code_mode_tool_keeps_the_full_sandbox_description(paths, monkeypatch):
    """Cloudflare search/execute put the JS API in the description; clipping
    it at 1k chars made the model call search with `cloudflare`, which is
    not defined in that sandbox."""
    body = (
        "Search the OpenAPI spec.\n\n"
        + ("x" * 1200)
        + "\n\nExamples:\nasync () => Object.keys(spec.paths);"
    )

    def post(url, token, message, session_id):
        payload, sid = fake_mcp_post(url, token, message, session_id)
        if message.get("method") == "tools/list":
            payload["result"]["tools"] = [
                {
                    "name": "search",
                    "description": body,
                    "inputSchema": {
                        "type": "object",
                        "properties": {"code": {"type": "string"}},
                        "required": ["code"],
                    },
                },
                {
                    "name": "docs",
                    "description": "short docs " + ("y" * 1200),
                    "inputSchema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                },
            ]
        return payload, sid

    monkeypatch.setattr(mcp, "_post", post)
    _connect(paths, "cf1")
    ctx = ConnectorContext(paths=paths, bot="atlas", record={"id": "cf1", "type": "cloudflare"})
    tools = {t.spec.name: t.spec for t in mcp.bind_tools(ctx, MCP_URL)}
    assert tools["cloudflare_search"].description == body
    assert tools["cloudflare_search"].description.endswith("spec.paths);")
    assert len(tools["cloudflare_docs"].description) == mcp._MAX_DESCRIPTION + 1  # plus ellipsis
    assert tools["cloudflare_docs"].description.endswith("…")


def test_tool_error_results_surface_as_error_strings(paths, monkeypatch):
    monkeypatch.setattr(mcp, "_post", fake_mcp_post)
    _connect(paths, "abc123")
    ctx = ConnectorContext(paths=paths, bot="atlas", record={"id": "abc123", "type": "linear"})
    assert mcp._call(ctx, MCP_URL, "boom", {}) == "error: no such issue"


def test_sse_framed_responses_are_parsed():
    raw = 'event: message\ndata: {"jsonrpc": "2.0", "id": 7, "result": {"ok": true}}\n\n'
    assert mcp._from_sse(raw, 7) == {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}
    assert mcp._from_sse(raw, 8) is None


def test_unconnected_mcp_server_yields_no_tools(paths, monkeypatch):
    def refuse(url, token, message, session_id):
        raise mcp.MCPError("connection refused")

    monkeypatch.setattr(mcp, "_post", refuse)
    _connect(paths, "abc123")
    ctx = ConnectorContext(paths=paths, bot="atlas", record={"id": "abc123", "type": "linear"})
    assert mcp.bind_tools(ctx, MCP_URL) == []


def test_empty_mcp_bind_falls_back_to_static_runtime(paths, monkeypatch):
    def refuse(url, token, message, session_id):
        raise mcp.MCPError("connection refused")

    monkeypatch.setattr(mcp, "_post", refuse)
    record = Connectors(paths).add("linear", "Linear", secret="lin_x")
    _connect(paths, record["id"])
    names = set(tools_for_bot(paths, "atlas"))
    assert "linear_create_issue" in names
    assert "linear_list_issues" in names


def test_registry_prefers_mcp_when_connected(paths, monkeypatch):
    monkeypatch.setattr(mcp, "_post", fake_mcp_post)
    record = Connectors(paths).add("linear", "Linear")
    # not connected yet: no secret either, so static runtime tools still bind
    names = set(tools_for_bot(paths, "atlas"))
    assert "linear_create_issue" in names and "linear_list_teams" in names
    _connect(paths, record["id"])
    names = set(tools_for_bot(paths, "atlas"))
    assert names == {"linear_create_issue", "linear_search"}


def test_cloudflare_api_key_binds_mcp_without_oauth(paths, monkeypatch):
    """Cloudflare has no static runtime; a pasted token is the MCP bearer."""
    monkeypatch.setattr(mcp, "_post", fake_mcp_post)
    Connectors(paths).add("cloudflare", "Cloudflare", secret="cf_test_token")
    names = set(tools_for_bot(paths, "atlas"))
    assert "cloudflare_connect" not in names
    assert "cloudflare_create_issue" in names
    assert "cloudflare_linear_search" in names


def test_two_gmail_accounts_both_bind_namespaced_tools(paths):
    """Gmail is an app password per inbox (no MCP): two records, two prefixes."""
    Connectors(paths).add("gmail", "Gmail work", {"email": "w@example.com"}, secret="w")
    Connectors(paths).add("gmail", "Gmail home", {"email": "h@example.com"}, secret="h")
    names = set(tools_for_bot(paths, "atlas"))
    assert "gmail_work_search_threads" in names
    assert "gmail_home_search_threads" in names
    assert "gmail_search_threads" not in names
    assert "gmail_work_create_issue" not in names


def test_tools_for_bot_can_bind_one_record(paths):
    """A turn that @mentions one inbox must not list every account's tools."""
    a = Connectors(paths).add("gmail", "Gmail work", {"email": "w@example.com"}, secret="w")
    Connectors(paths).add("gmail", "Gmail home", {"email": "h@example.com"}, secret="h")
    names = set(tools_for_bot(paths, "atlas", record_ids={a["id"]}))
    assert "gmail_work_search_threads" in names
    assert "gmail_home_search_threads" not in names
    assert tools_for_bot(paths, "atlas", record_ids=set()) == {}


def test_one_gmail_account_keeps_the_plain_prefix(paths):
    Connectors(paths).add("gmail", "Gmail", {"email": "ada@example.com"}, secret="x")
    names = set(tools_for_bot(paths, "atlas"))
    assert "gmail_search_threads" in names
    assert "gmail_create_draft" in names
    assert "gmail_send" in names
    assert "gmail_send_draft" in names
    assert "gmail_work_search_threads" not in names


def test_github_api_key_keeps_the_static_runtime(paths, monkeypatch):
    """A PAT must not be sent to Copilot MCP; the hand-built tools stay."""
    monkeypatch.setattr(mcp, "_post", fake_mcp_post)
    Connectors(paths).add("github", "GitHub", secret="ghp_test")
    names = set(tools_for_bot(paths, "atlas"))
    assert "github_list_repos" in names
    assert "github_create_issue" in names
    assert "github_linear_search" not in names


def test_record_reports_oauth_and_cached_tools(paths):
    record = Connectors(paths).add("notion", "Notion")
    listed = Connectors(paths).list()[0]
    assert listed["oauth_configured"] is False
    assert listed["tools"] == []  # no static runtime for notion
    _connect(paths, record["id"])
    Connectors(paths).set_mcp_tools(record["id"], ["notion_search"])
    listed = Connectors(paths).list()[0]
    assert listed["oauth_configured"] is True
    assert listed["tools"] == ["notion_search"]


def test_gmail_listing_ignores_old_password_and_reports_static_tools(paths):
    """Old app passwords never make a Gmail connection look authorized."""
    Connectors(paths).add("gmail", "Gmail", {"email": "ada@example.com"}, secret="abcd")
    listed = Connectors(paths).list()[0]
    assert listed["secret_configured"] is False
    assert listed["oauth_configured"] is False
    assert listed["name"] == "Gmail"
    assert listed["config"] == {"email": "ada@example.com"}
    assert "gmail_send" in listed["tools"]
    assert "gmail_send_draft" in listed["tools"]
    assert "gmail_create_draft" in listed["tools"]


def test_remove_connector_drops_oauth_tokens(paths):
    record = Connectors(paths).add("notion", "Notion")
    _connect(paths, record["id"])
    assert Connectors(paths).remove(record["id"]) is True
    assert not mcp_oauth.connected(paths, record["id"])


def test_catalog_marks_mcp_types():
    by_type = {c["type"]: c for c in catalog()}
    assert by_type["linear"]["mcp"] is True and by_type["linear"]["mcp_url"]
    assert by_type["notion"]["mcp"] is True
    assert by_type["notion"]["implemented"] is True  # connectable although no static tools
    assert by_type["slack"]["mcp"] is True
    assert by_type["slack"]["implemented"] is True
    assert by_type["slack"]["mcp_url"] == "https://mcp.slack.com/mcp"
    assert by_type["cloudflare"]["mcp"] is True
    assert by_type["cloudflare"]["mcp_url"] == "https://mcp.cloudflare.com/mcp"
    assert by_type["granola"]["mcp"] is True
    assert by_type["granola"]["mcp_url"] == "https://mcp.granola.ai/mcp"
    assert by_type["composio"]["mcp"] is True
    assert by_type["composio"]["mcp_url"] == "https://connect.composio.dev/mcp"
    assert by_type["composio"]["implemented"] is True
    assert by_type["webflow"]["mcp"] is True
    assert by_type["webflow"]["mcp_url"] == "https://mcp.webflow.com/mcp"
    assert by_type["webflow"]["implemented"] is True
    assert by_type["ahrefs"]["mcp"] is True
    assert by_type["ahrefs"]["mcp_url"] == "https://api.ahrefs.com/mcp/mcp"
    assert by_type["ahrefs"]["implemented"] is True
    assert by_type["clickup"]["mcp_url"] == "https://mcp.clickup.com/mcp"
    assert by_type["klaviyo"]["mcp"] is True
    # Gmail: app password over IMAP/SMTP — a pasted secret plus the address,
    # no MCP server and no OAuth client, one record per inbox.
    assert by_type["gmail"]["mcp"] is False
    assert "mcp_url" not in by_type["gmail"]
    assert by_type["gmail"]["auth"] == "oauth"
    assert by_type["gmail"]["fields"] == []
    assert by_type["gmail"]["implemented"] is True
    assert by_type["github"]["prefer_static"] is True
    assert by_type["github"]["auth"] == "api_key"
    assert by_type["gmail"]["multi_account"] is True
    assert "gmail_search_threads" in by_type["gmail"]["tools"]
    assert by_type["gmail"]["oauth_supported"] is True
    assert by_type["gmail"]["docs"].endswith("/xoauth2-protocol")
    assert "OAuth" in by_type["gmail"]["notes"]
    assert by_type["n8n"]["mcp"] is True
    assert by_type["n8n"]["mcp_url_template"] == "https://{instance_host}/mcp-server/http"
    assert by_type["triggerdev"]["mcp"] is False
    assert by_type["triggerdev"]["implemented"] is False
    assert by_type["1password"]["mcp"] is False
    assert by_type["1password"]["implemented"] is False
    assert by_type["1password"]["name"] == "1Password"


# -- server endpoints -------------------------------------------------------

ROSTER = """
[[bots]]
name = "atlas"
role = "a terse research assistant"
provider = "echo"
"""


@pytest.fixture
def server(tmp_path, monkeypatch, request):
    import harness.server as server_mod

    fake = FakeAuth()
    monkeypatch.setattr(mcp_oauth, "Transport", lambda: fake)
    monkeypatch.setattr(mcp, "_post", fake_mcp_post)
    # point the catalog's linear entry at the fake service
    monkeypatch.setattr(
        server_mod, "connector_mcp_url", lambda t, config=None: MCP_URL if t == "linear" else None
    )
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    orch.use_json_store()
    httpd = make_server(orch, "127.0.0.1", 0, public_url=getattr(request, "param", None))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", orch, fake
    finally:
        httpd.shutdown()
        orch.down()


def _req(url, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read().decode()
        ctype = r.headers.get("Content-Type", "")
    return json.loads(body) if "json" in ctype else body


def test_list_never_sees_a_half_written_store(tmp_path, monkeypatch):
    """`_refresh_mcp_tools` saves the record from a thread right after the
    OAuth callback while the app is listing connectors. A save that
    truncates connectors.json in place lets that reader open an empty file
    and report no connectors (`listed[0]` IndexError on CI). Every file
    opened for writing during a save is observed by a concurrent reader,
    which must still see the whole record set."""
    import io

    home = HarnessPaths(home=tmp_path)
    store = Connectors(home)
    record = store.add("linear", "Linear")
    real_open = io.open
    seen: list[list[str]] = []

    def spy_open(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        if "w" in mode and str(file).startswith(str(tmp_path)):
            # The writer has its file open (and truncated, if it is the
            # live one): what does a reader see right now?
            seen.append([r["id"] for r in Connectors(home).list()])
        return handle

    monkeypatch.setattr(io, "open", spy_open)
    store.set_mcp_tools(record["id"], ["linear_get_issue"])
    monkeypatch.undo()

    assert seen, "the save never opened a file for writing — hook did not fire"
    assert all(ids == [record["id"]] for ids in seen), seen
    assert [r["id"] for r in store.list()] == [record["id"]]
    assert store.list()[0]["mcp_tools"] == ["linear_get_issue"]
    assert not list(tmp_path.glob("connectors.json.*.tmp")), "temp file left behind"


def test_connect_flow_over_http(server):
    base, orch, fake = server
    record = _req(f"{base}/api/connectors", "POST", {"type": "linear", "name": "Linear"})
    cid = record["id"]

    start = _req(f"{base}/api/connectors/{cid}/oauth/start", "POST", {})
    assert start["status"] == "pending"
    assert start["authorize_url"].startswith(f"{AUTH}/authorize?")
    # no redirect_uri supplied -> the harness's own callback endpoint
    assert start["redirect_uri"].endswith("/oauth/callback")

    page = _req(f"{base}/oauth/callback?code=the-code&state={start['state']}")
    assert "Connected" in page

    status = _req(f"{base}/api/connectors/{cid}/oauth/status")
    assert status["status"] == "connected"
    listed = _req(f"{base}/api/connectors")
    assert listed[0]["oauth_configured"] is True

    gone = _req(f"{base}/api/connectors/{cid}/oauth", "DELETE")
    assert gone["removed"] is True
    assert _req(f"{base}/api/connectors/{cid}/oauth/status")["status"] == "idle"


def test_exchange_endpoint_relays_loopback_redirect(server):
    base, orch, fake = server
    record = _req(f"{base}/api/connectors", "POST", {"type": "linear", "name": "Linear"})
    cid = record["id"]
    start = _req(
        f"{base}/api/connectors/{cid}/oauth/start",
        "POST",
        {"redirect_uri": "http://127.0.0.1:54321/callback"},
    )
    assert start["redirect_uri"] == "http://127.0.0.1:54321/callback"
    done = _req(
        f"{base}/api/connectors/oauth/exchange",
        "POST",
        {"state": start["state"], "code": "the-code"},
    )
    assert done["status"] == "connected"
    assert fake.token_forms[-1]["redirect_uri"] == "http://127.0.0.1:54321/callback"


@pytest.mark.parametrize("server", ["https://bots.example.com"], indirect=True)
def test_public_https_origin_is_used_for_connector_oauth_callback(server):
    base, _, fake = server
    record = _req(f"{base}/api/connectors", "POST", {"type": "linear", "name": "Linear"})
    start = _req(f"{base}/api/connectors/{record['id']}/oauth/start", "POST", {})
    assert start["redirect_uri"] == "https://bots.example.com/oauth/callback"
    assert fake.registered[-1]["redirect_uris"] == [start["redirect_uri"]]
    assert "Connected" in _req(f"{base}/oauth/callback?code=the-code&state={start['state']}")
    assert fake.token_forms[-1]["redirect_uri"] == start["redirect_uri"]


def test_callback_with_bad_state_is_rejected(server):
    base, _, _ = server
    with pytest.raises(urllib.error.HTTPError) as err:
        _req(f"{base}/oauth/callback?code=x&state=nope")
    assert err.value.code == 400


def test_oauth_start_without_mcp_url_is_501(server):
    base, _, _ = server
    record = _req(f"{base}/api/connectors", "POST", {"type": "slack", "name": "Slack"})
    with pytest.raises(urllib.error.HTTPError) as err:
        _req(f"{base}/api/connectors/{record['id']}/oauth/start", "POST", {})
    assert err.value.code == 501


@pytest.mark.parametrize("kind", ["gmail", "google_calendar", "google_drive", "google_sheets", "google_docs"])
def test_workspace_uses_delegation_not_native_google_oauth(server, monkeypatch, kind):
    from harness import delegated_oauth
    base, orch, fake = server
    record = _req(f"{base}/api/connectors", "POST", {"type": kind, "name": kind})
    cid = record["id"]
    with pytest.raises(urllib.error.HTTPError) as error:
        _req(f"{base}/api/connectors/{cid}/oauth/start", "POST", {})
    assert error.value.code == 409
    mcp_oauth.save_tokens(orch.paths, cid, {"access_token": "legacy", "refresh_token": "legacy-refresh"})
    assert _req(f"{base}/api/connectors/{cid}/oauth/status")["status"] == "idle"
    monkeypatch.setattr(delegated_oauth, "request", lambda *args: {
        "access_token": "short-lived", "email": "test@example.com", "service": kind})
    _req(f"{base}/api/connectors/{cid}/delegated-auth", "POST", {
        "broker_url": "https://broker.example/integrations", "grant_id": "grant", "capability": "opaque"})
    assert _req(f"{base}/api/connectors")[0]["oauth_configured"] is True
    assert _req(f"{base}/api/connectors/{cid}/oauth/status")["status"] == "connected"
    _req(f"{base}/api/connectors/{cid}/oauth", "DELETE")
    assert _req(f"{base}/api/connectors")[0]["oauth_configured"] is False
    assert _req(f"{base}/api/connectors/{cid}/oauth/status")["status"] == "idle"
