"""Connector tool runtime: gallery catalog, per-bot scoping, Linear tools."""

from __future__ import annotations

import pytest

from agent.tools import connector_tools
from connectors import github, linear
from connectors.base import ConnectorContext
from connectors.registry import tool_names, tools_for_bot
from harness.connectors import Connectors, catalog
from harness.paths import HarnessPaths
from providers.base import ToolSpec


@pytest.fixture
def paths(tmp_path):
    return HarnessPaths(home=tmp_path)


@pytest.fixture
def linear_ctx(paths):
    """A configured Linear connector with a stored key, as a bound context."""
    record = Connectors(paths).add("linear", "Linear", secret="lin_api_test")
    return ConnectorContext(paths=paths, bot="atlas", record=record)


# -- catalog ---------------------------------------------------------------


def test_catalog_carries_gallery_metadata():
    for entry in catalog():
        assert entry["category"], entry["type"]
        assert entry["description"], entry["type"]
        assert entry["icon"], entry["type"]
        assert isinstance(entry["tools"], list)
        # implemented = has a static runtime, or connectable to an MCP server
        assert entry["implemented"] == (bool(entry["tools"]) or entry["mcp"])
    types = [c["type"] for c in catalog()]
    assert len(types) == len(set(types))


def test_connector_docs_cover_the_catalog():
    from harness.connector_docs import DOCS
    from harness.connectors import catalog

    by_type = {c["type"]: c for c in catalog()}
    missing = sorted(set(by_type) - set(DOCS))
    extra = sorted(set(DOCS) - set(by_type))
    assert missing == [], missing
    assert extra == [], extra
    ac = by_type["activecampaign"]
    assert ac["publisher"] == "official"
    assert "developers.activecampaign.com" in ac["docs"]
    assert "Official remote MCP" in ac["notes"]
    assert by_type["360nrs"]["publisher"] == "none"
    assert by_type["n8n"]["docs"].startswith("https://docs.n8n.io")
    assert by_type["linear"]["docs"] == "https://linear.app/docs/mcp"


def test_featured_gallery_matches_the_popular_set():
    from harness.connectors import FEATURED, catalog

    by_type = {c["type"]: c for c in catalog()}
    for i, type_ in enumerate(FEATURED):
        assert type_ in by_type, type_
        assert by_type[type_]["featured"] is True
        assert by_type[type_]["featured_rank"] == i
    assert by_type["mailchimp"]["featured"] is True
    assert by_type["beehiiv"].get("featured") is False
    assert "featured_rank" not in by_type["beehiiv"]
    assert by_type["openai"]["name"] == "OpenAI (ChatGPT)"
    assert by_type["anthropic"]["name"] == "Anthropic (Claude)"


def test_heymarcus_stubs_are_api_key_http():
    from harness.connector_stubs import STUBS

    by_type = {c["type"]: c for c in catalog()}
    assert by_type["mailchimp"]["mcp"] is False
    assert by_type["mailchimp"]["implemented"] is True
    assert "mailchimp_get" in by_type["mailchimp"]["tools"]
    assert by_type["whatsapp"]["name"] == "WhatsApp Business"
    assert by_type["linear"]["mcp"] is True  # live entries stay live
    assert "gdpr" not in by_type
    for stub in STUBS:
        assert "mcp_url" not in stub
        rec = by_type[stub["type"]]
        assert rec["mcp"] is False
        assert rec["implemented"] is True
        assert rec["auth"] == "api_key"


def test_catalog_linear_has_runtime_tools():
    by_type = {c["type"]: c for c in catalog()}
    assert by_type["linear"]["implemented"] is True
    assert "linear_create_issue" in by_type["linear"]["tools"]
    assert by_type["github"]["implemented"] is True
    assert "github_list_repos" in by_type["github"]["tools"]
    assert by_type["github"]["prefer_static"] is True
    assert "personal access token" in (by_type["github"].get("notes") or "").lower()
    assert by_type["slack"]["implemented"] is True
    assert by_type["slack"]["mcp_url"]
    assert by_type["slack"]["tools"] == []
    # MCP-capable types are connectable even before any static runtime
    assert by_type["notion"]["implemented"] is True
    assert by_type["notion"]["mcp_url"]
    assert by_type["cloudflare"]["implemented"] is True
    assert by_type["cloudflare"]["mcp_url"] == "https://mcp.cloudflare.com/mcp"
    assert by_type["cloudflare"]["tools"] == []
    assert by_type["cloudflare"]["fields"] == []
    assert by_type["gmail"]["implemented"] is True
    assert "gmail_search_threads" in by_type["gmail"]["tools"]
    assert "gmail_create_draft" in by_type["gmail"]["tools"]
    assert "gmail_send" in by_type["gmail"]["tools"]
    assert "gmail_send_draft" in by_type["gmail"]["tools"]


# -- per-tenant MCP URLs ---------------------------------------------------


def test_mcp_url_returns_the_static_url_and_ignores_config():
    from harness.connectors import mcp_url

    assert mcp_url("notion") == "https://mcp.notion.com/mcp"
    assert mcp_url("notion", {"store_domain": "shop.example.com"}) == "https://mcp.notion.com/mcp"
    assert mcp_url("granola") == "https://mcp.granola.ai/mcp"
    assert mcp_url("composio") == "https://connect.composio.dev/mcp"
    assert mcp_url("webflow") == "https://mcp.webflow.com/mcp"
    assert mcp_url("ahrefs") == "https://api.ahrefs.com/mcp/mcp"
    assert mcp_url("gmail") is None  # app password over IMAP/SMTP, not MCP


def test_mcp_url_fills_a_template_from_the_record_config():
    """Shopify runs the server on the merchant's own store."""
    from harness.connectors import mcp_url

    got = mcp_url("shopify", {"store_domain": "acme-supplies.myshopify.com"})
    assert got == "https://acme-supplies.myshopify.com/api/mcp"
    assert mcp_url("n8n", {"instance_host": "n8n.example.com"}) == (
        "https://n8n.example.com/mcp-server/http"
    )


def test_mcp_url_is_none_until_the_tenant_field_is_filled_in():
    """A half-built record must reach the sign-in stub, not a guessed host."""
    from harness.connectors import mcp_url

    assert mcp_url("shopify") is None
    assert mcp_url("shopify", {}) is None
    assert mcp_url("shopify", {"store_domain": "   "}) is None
    assert mcp_url("n8n") is None
    assert mcp_url("n8n", {"instance_host": "   "}) is None


def test_a_templated_type_is_mcp_capable_in_the_catalog():
    by_type = {c["type"]: c for c in catalog()}
    assert by_type["shopify"]["mcp"] is True
    assert by_type["shopify"]["implemented"] is True
    assert by_type["n8n"]["mcp"] is True
    assert by_type["n8n"]["implemented"] is True


def test_mcp_url_refuses_a_tenant_value_that_is_not_a_bare_host():
    """The value lands in the host the OAuth token is later sent to, so a
    field carrying a path, a scheme, credentials, or a query is refused
    rather than silently redirecting the flow."""
    from harness.connectors import mcp_url

    for hostile in (
        "evil.example.com/api/mcp#",
        "https://evil.example.com",
        "shop.example.com@evil.example.com",
        "shop.example.com:8443",
        "shop.example.com?next=evil",
        "shop.example.com/../x",
    ):
        assert mcp_url("shopify", {"store_domain": hostile}) is None, hostile
        assert mcp_url("n8n", {"instance_host": hostile}) is None, hostile


def test_registry_tool_names_unknown_type_empty():
    assert tool_names("myspace") == []
    assert set(tool_names("linear")) == {
        "linear_list_teams",
        "linear_list_projects",
        "linear_list_issues",
        "linear_search_issues",
        "linear_get_issue",
        "linear_create_issue",
        "linear_update_issue",
        "linear_comment",
        "linear_attach_files",
    }
    assert set(tool_names("github")) == {
        "github_get_file",
        "github_list_repos",
        "github_list_issues",
        "github_search_issues",
        "github_create_issue",
        "github_comment",
        "github_get_pull",
        "github_get_issue",
        "github_list_pulls",
        "github_merge_pull",
    }


# -- per-bot binding and scoping ------------------------------------------


def test_no_connectors_no_tools(paths):
    assert tools_for_bot(paths, "atlas") == {}
    assert connector_tools(paths, "atlas") == {}


def test_configured_connector_gives_bot_tools(paths):
    Connectors(paths).add("linear", "Linear", secret="lin_api_test")
    tools = connector_tools(paths, "atlas")
    assert "linear_create_issue" in tools
    assert isinstance(tools["linear_create_issue"].spec, ToolSpec)


def test_tool_binding_skips_public_connector_enrichment(paths, monkeypatch):
    Connectors(paths).add("linear", "Linear", secret="lin_api_test")

    def boom(_self, _item):
        raise AssertionError("tool binding should read raw connector records")

    monkeypatch.setattr(Connectors, "_public", boom)
    assert "linear_create_issue" in connector_tools(paths, "atlas")


def test_grant_bot_adds_new_peer_to_fleet_wide_lists(paths):
    store = Connectors(paths)
    fleet = store.add("linear", "Linear", secret="k", enabled_for=["atlas", "nova"])
    subset = store.add("github", "GitHub", secret="g", enabled_for=["atlas"])
    store.grant_bot("scout", ["atlas", "nova", "scout"])
    granted = next(c for c in store.list() if c["id"] == fleet["id"])
    assert granted["enabled_for"] == ["atlas", "nova", "scout"]
    kept = next(c for c in store.list() if c["id"] == subset["id"])
    assert kept["enabled_for"] == ["atlas"]
    open_ = store.add("linear", "Work", secret="w", enabled_for=None)
    store.grant_bot("scout", ["atlas", "nova", "scout"])
    still = next(c for c in store.list() if c["id"] == open_["id"])
    assert still["enabled_for"] is None


def test_enabled_for_scopes_tools_to_named_bots(paths):
    store = Connectors(paths)
    rec = store.add("linear", "Linear", secret="k", enabled_for=["atlas"])
    assert connector_tools(paths, "atlas")
    assert connector_tools(paths, "nova") == {}
    # null enabled_for re-opens the connector to every bot
    store.update(rec["id"], enabled_for=None)
    assert connector_tools(paths, "nova")


def test_oauth_type_without_mcp_url_offers_a_connect_stub(paths):
    """Google has no MCP URL and no static runtime; bots get the sign-in stub
    because auth is oauth, not a silent empty tool list."""
    Connectors(paths).add("google", "Google")
    bound = connector_tools(paths, "atlas")
    assert list(bound) == ["google_connect"]


def test_unconnected_cloudflare_offers_a_connect_stub(paths):
    Connectors(paths).add("cloudflare", "Cloudflare")
    bound = connector_tools(paths, "atlas")
    assert "cloudflare_connect" in bound
    assert "cloudflare_search" not in bound


def test_oauth_connector_offers_a_connect_stub(paths):
    Connectors(paths).add("slack", "Slack")
    bound = connector_tools(paths, "atlas")
    assert "slack_connect" in bound


def test_update_validates_enabled_for(paths):
    rec = Connectors(paths).add("linear", "Linear")
    with pytest.raises(ValueError):
        Connectors(paths).update(rec["id"], enabled_for="atlas")
    assert Connectors(paths).update("nope", enabled_for=["atlas"]) is None


def test_records_report_secret_and_tools(paths):
    store = Connectors(paths)
    rec = store.add("linear", "Linear", secret="lin_api_test")
    assert rec["secret_configured"] is True
    assert rec["enabled_for"] is None
    assert "linear_comment" in rec["tools"]
    listed = store.list()[0]
    assert listed["secret_configured"] is True
    assert listed["tools"] == rec["tools"]


def test_malformed_records_are_skipped(paths):
    """Hand-edited connectors.json must never kill a bot turn."""
    Connectors(paths).add("linear", "Linear", secret="k")
    file = paths.home / "connectors.json"
    import json

    data = json.loads(file.read_text())
    data["connectors"].insert(0, {"type": "linear"})  # no id
    data["connectors"].insert(0, "not-even-a-dict")
    file.write_text(json.dumps(data))
    tools = connector_tools(paths, "atlas")
    assert "linear_create_issue" in tools


def test_handler_exceptions_become_tool_errors(paths, monkeypatch):
    Connectors(paths).add("linear", "Linear", secret="k")

    def boom(*_args, **_kwargs):
        raise RuntimeError("wires crossed")

    monkeypatch.setattr(linear, "_graphql", boom)
    tool = connector_tools(paths, "atlas")["linear_list_teams"]
    # the adapter ignores the agent ToolContext; bound context is baked in.
    # Results come back inside the untrusted-content envelope, with
    # a failure's error: prefix kept outside it so the audit trail still fires.
    out = tool.handler(None, {})
    assert out.startswith("error:")
    assert "wires crossed" in out
    assert '<<<EXTERNAL_UNTRUSTED_CONTENT id="' in out


# -- secrets ---------------------------------------------------------------


def test_missing_secret_message_names_request_secret(paths):
    record = Connectors(paths).add("linear", "Linear")  # no secret stored
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    out = linear._list_teams(ctx, {})
    assert out.startswith("error:")
    assert "request_secret" in out
    assert f"connector_{record['id']}" in out
    assert "LINEAR_API_KEY" in out


def test_fallback_secret_env_name(paths, monkeypatch):
    record = Connectors(paths).add("linear", "Linear")
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_env")
    assert ctx.secret(fallback="LINEAR_API_KEY") == "lin_api_env"


# -- Linear tools (transport stubbed) --------------------------------------


TEAMS = {"teams": {"nodes": [{"id": "team-1", "key": "ALT", "name": "Example"}]}}
PROJECTS = {
    "projects": {
        "nodes": [
            {
                "id": "proj-1",
                "name": "Dotobot",
                "slug": "dotobot",
                "url": "https://linear.app/x/project/dotobot",
            }
        ]
    }
}


def _issue_node(identifier="TEST-1", title="Fix login"):
    return {
        "identifier": identifier,
        "title": title,
        "url": f"https://linear.app/x/issue/{identifier}",
        "priority": 2,
        "state": {"name": "In Progress"},
        "assignee": {"displayName": "Alex"},
        "updatedAt": "2026-08-23T00:00:00Z",
    }


def _stub(monkeypatch, responder):
    calls: list[tuple[str, dict | None]] = []

    def fake(api_key, query, variables=None):
        assert api_key == "lin_api_test"
        calls.append((query, variables))
        return responder(query, variables)

    monkeypatch.setattr(linear, "_graphql", fake)
    return calls


def test_linear_list_teams(linear_ctx, monkeypatch):
    _stub(monkeypatch, lambda q, v: TEAMS)
    out = linear._list_teams(linear_ctx, {})
    assert out == "- ALT · Example"


def test_linear_list_issues_builds_filter(linear_ctx, monkeypatch):
    def responder(query, variables):
        if "teams(" in query:
            return TEAMS
        assert variables["filter"]["team"] == {"key": {"eq": "ALT"}}
        assert variables["filter"]["assignee"] == {"displayName": {"containsIgnoreCase": "Alex"}}
        assert variables["first"] == 5
        return {"issues": {"nodes": [_issue_node()]}}

    _stub(monkeypatch, responder)
    out = linear._list_issues(linear_ctx, {"team": "alt", "assignee": "Alex", "limit": 5})
    assert "TEST-1" in out and "In Progress" in out and "Alex" in out


def test_linear_list_issues_unknown_team(linear_ctx, monkeypatch):
    _stub(monkeypatch, lambda q, v: TEAMS)
    out = linear._list_issues(linear_ctx, {"team": "nope"})
    assert out.startswith("error:") and "ALT" in out


def test_linear_search_requires_query(linear_ctx):
    assert linear._search_issues(linear_ctx, {}).startswith("error:")


def test_linear_search_issues(linear_ctx, monkeypatch):
    def responder(query, variables):
        ors = variables["filter"]["or"]
        assert {"title": {"containsIgnoreCase": "login"}} in ors
        return {"issues": {"nodes": []}}

    _stub(monkeypatch, responder)
    assert "no issues match" in linear._search_issues(linear_ctx, {"query": "login"})


def test_linear_create_issue(linear_ctx, monkeypatch):
    def responder(query, variables):
        if "teams(" in query:
            return TEAMS
        assert "issueCreate" in query
        assert variables["input"]["teamId"] == "team-1"
        assert variables["input"]["title"] == "Ship connectors"
        assert variables["input"]["priority"] == 2
        return {
            "issueCreate": {
                "success": True,
                "issue": {
                    "identifier": "TEST-33",
                    "title": "Ship connectors",
                    "url": "https://linear.app/x/issue/TEST-33",
                },
            }
        }

    _stub(monkeypatch, responder)
    out = linear._create_issue(
        linear_ctx, {"title": "Ship connectors", "team": "Example", "priority": "high"}
    )
    assert out.startswith("ok:") and "TEST-33" in out


def test_linear_create_issue_needs_title_and_team(linear_ctx):
    assert linear._create_issue(linear_ctx, {"title": "x"}).startswith("error:")
    assert linear._create_issue(linear_ctx, {"team": "ALT"}).startswith("error:")


def test_linear_list_projects_filters(linear_ctx, monkeypatch):
    _stub(monkeypatch, lambda q, v: PROJECTS)
    out = linear._list_projects(linear_ctx, {"query": "doto"})
    assert "Dotobot" in out
    assert "(no projects match)" in linear._list_projects(linear_ctx, {"query": "nope"})


def test_linear_create_issue_sets_project(linear_ctx, monkeypatch):
    def responder(query, variables):
        if "teams(" in query:
            return TEAMS
        if "projects(" in query:
            return PROJECTS
        assert "issueCreate" in query
        assert variables["input"]["projectId"] == "proj-1"
        return {
            "issueCreate": {
                "success": True,
                "issue": {
                    "identifier": "TEST-42",
                    "title": "Chat images on tickets",
                    "url": "https://linear.app/x/issue/TEST-42",
                },
            }
        }

    _stub(monkeypatch, responder)
    out = linear._create_issue(
        linear_ctx,
        {
            "title": "Chat images on tickets",
            "team": "ALT",
            "project": "Dotobot",
        },
    )
    assert out.startswith("ok:") and "TEST-42" in out


def test_linear_create_issue_attaches_workspace_files(linear_ctx, monkeypatch):
    uploads = linear_ctx.paths.uploads
    uploads.mkdir(parents=True, exist_ok=True)
    mockup = uploads / "mockup-1.png"
    mockup.write_bytes(b"\x89PNG mock")
    puts: list[tuple[str, bytes, dict]] = []

    def responder(query, variables):
        if "teams(" in query:
            return TEAMS
        if "fileUpload" in query:
            assert variables["filename"] == "mockup-1.png"
            assert variables["size"] == mockup.stat().st_size
            return {
                "fileUpload": {
                    "success": True,
                    "uploadFile": {
                        "uploadUrl": "https://storage.example/put",
                        "assetUrl": "https://uploads.linear.app/mockup-1.png",
                        "headers": [{"key": "x-goog-meta-test", "value": "1"}],
                    },
                }
            }
        assert "issueCreate" in query
        desc = variables["input"]["description"]
        assert "![mockup-1.png](https://uploads.linear.app/mockup-1.png)" in desc
        return {
            "issueCreate": {
                "success": True,
                "issue": {
                    "identifier": "TEST-43",
                    "title": "Mockups",
                    "url": "https://linear.app/x/issue/TEST-43",
                },
            }
        }

    _stub(monkeypatch, responder)
    monkeypatch.setattr(
        linear, "_put_bytes", lambda url, data, headers: puts.append((url, data, headers))
    )
    out = linear._create_issue(
        linear_ctx,
        {
            "title": "Mockups",
            "team": "ALT",
            "description": "Need these on the ticket.",
            "attachments": [str(mockup)],
        },
    )
    assert "TEST-43" in out and "1 file" in out
    assert puts and puts[0][0] == "https://storage.example/put"
    assert puts[0][1].startswith(b"\x89PNG")
    assert puts[0][2]["x-goog-meta-test"] == "1"


def test_linear_attach_files_appends_markdown(linear_ctx, monkeypatch):
    uploads = linear_ctx.paths.uploads
    uploads.mkdir(parents=True, exist_ok=True)
    mockup = uploads / "composer.png"
    mockup.write_bytes(b"png-bytes")
    updated: dict = {}

    def responder(query, variables):
        if "fileUpload" in query:
            return {
                "fileUpload": {
                    "success": True,
                    "uploadFile": {
                        "uploadUrl": "https://storage.example/put",
                        "assetUrl": "https://uploads.linear.app/composer.png",
                        "headers": [],
                    },
                }
            }
        if "query($id: String!) { issue(id: $id) { description } }" in query:
            return {"issue": {"description": "Existing body"}}
        if "query($id: String!) { issue" in query:
            return {
                "issue": {
                    "id": "uuid-9",
                    "identifier": "TEST-45",
                    "url": "https://linear.app/x/issue/TEST-45",
                    "team": {"id": "t"},
                }
            }
        if "issueUpdate" in query:
            updated["input"] = variables["input"]
            return {
                "issueUpdate": {
                    "success": True,
                    "issue": {"identifier": "TEST-45", "url": "https://linear.app/x/issue/TEST-45"},
                }
            }
        raise AssertionError(query)

    _stub(monkeypatch, responder)
    monkeypatch.setattr(linear, "_put_bytes", lambda *a, **k: None)
    out = linear._attach_files(linear_ctx, {"issue": "TEST-45", "paths": [str(mockup)]})
    assert out.startswith("ok:") and "composer.png" in out
    assert "Existing body" in updated["input"]["description"]
    assert (
        "![composer.png](https://uploads.linear.app/composer.png)"
        in updated["input"]["description"]
    )


def test_linear_attach_rejects_path_outside_workspace(linear_ctx):
    out = linear._attach_files(linear_ctx, {"issue": "TEST-45", "paths": ["/etc/hosts"]})
    assert out.startswith("error:") and "workspace" in out


def test_attachment_block_lists_disk_path(paths):
    from agent.runtime import _attachment_block

    uploads = paths.uploads
    uploads.mkdir(parents=True)
    shot = uploads / "shot.png"
    shot.write_bytes(b"png")
    block = _attachment_block([{"name": "shot.png", "path": str(shot)}])
    assert "path=" in block and str(shot) in block
    assert "linear_attach_files" in block


def test_attachment_block_is_current_turn_only(paths):
    """Earlier chat uploads must not be fused onto a later question."""
    from agent.runtime import _attachment_block

    uploads = paths.uploads
    uploads.mkdir(parents=True)
    earlier = uploads / "earlier.png"
    earlier.write_bytes(b"png")
    now = uploads / "now.png"
    now.write_bytes(b"png")
    block = _attachment_block([{"name": "now.png", "path": str(now)}])
    assert str(now) in block
    assert str(earlier) not in block
    assert "now.png" in block
    assert "earlier.png" not in block


def test_linear_update_issue_resolves_state(linear_ctx, monkeypatch):
    def responder(query, variables):
        if "query($id: String!) { issue" in query:
            return {"issue": {"id": "uuid-1", "identifier": "TEST-44", "team": {"id": "team-1"}}}
        if "states(" in query:
            return {"team": {"states": {"nodes": [{"id": "st-done", "name": "Done"}]}}}
        assert "issueUpdate" in query
        assert variables["id"] == "uuid-1"
        assert variables["input"] == {"stateId": "st-done"}
        return {
            "issueUpdate": {
                "success": True,
                "issue": {
                    "identifier": "TEST-44",
                    "url": "https://linear.app/x/issue/TEST-44",
                    "state": {"name": "Done"},
                },
            }
        }

    _stub(monkeypatch, responder)
    out = linear._update_issue(linear_ctx, {"id": "TEST-44", "state": "done"})
    assert out.startswith("ok:") and "Done" in out


def test_linear_update_issue_nothing_to_do(linear_ctx, monkeypatch):
    _stub(
        monkeypatch,
        lambda q, v: {"issue": {"id": "u", "identifier": "TEST-44", "team": {"id": "t"}}},
    )
    assert "nothing to update" in linear._update_issue(linear_ctx, {"id": "TEST-44"})


def test_linear_comment(linear_ctx, monkeypatch):
    def responder(query, variables):
        if "query($id: String!) { issue" in query:
            return {"issue": {"id": "uuid-9", "identifier": "TEST-45", "team": {"id": "t"}}}
        assert variables["input"] == {"issueId": "uuid-9", "body": "on it"}
        return {"commentCreate": {"success": True, "comment": {"url": "https://linear.app/c/1"}}}

    _stub(monkeypatch, responder)
    out = linear._comment(linear_ctx, {"issue": "TEST-45", "body": "on it"})
    assert out.startswith("ok:") and "TEST-45" in out


def test_linear_api_error_surfaced(linear_ctx, monkeypatch):
    def responder(query, variables):
        raise linear.LinearError("Linear API error: token revoked")

    _stub(monkeypatch, responder)
    out = linear._list_teams(linear_ctx, {})
    assert out == "error: Linear API error: token revoked"


def test_priority_words_and_bounds():
    assert linear._priority("urgent") == 1
    assert linear._priority(4) == 4
    assert linear._priority(None) is None
    with pytest.raises(linear.LinearError):
        linear._priority("someday")


# -- GitHub tools (transport stubbed) --------------------------------------


@pytest.fixture
def github_ctx(paths):
    record = Connectors(paths).add(
        "github", "GitHub", config={"org": "example-owner"}, secret="ghp_test"
    )
    return ConnectorContext(paths=paths, bot="atlas", record=record)


def _gh_stub(monkeypatch, responder):
    calls: list[tuple[str, str, dict | None, dict | None]] = []

    def fake(token, method, path, *, query=None, body=None):
        assert token == "ghp_test"
        calls.append((method, path, query, body))
        return responder(method, path, query, body)

    monkeypatch.setattr(github, "_request", fake)
    return calls


def test_github_missing_secret_names_request_secret(paths, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    record = Connectors(paths).add("github", "GitHub")
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    out = github._list_repos(ctx, {})
    assert out.startswith("error:")
    assert "request_secret" in out
    assert "GITHUB_TOKEN" in out


def test_github_list_repos_uses_org(github_ctx, monkeypatch):
    calls = _gh_stub(
        monkeypatch,
        lambda m, p, q, b: [
            {
                "full_name": "example-owner/dotobot",
                "private": True,
                "html_url": "https://github.com/example-owner/dotobot",
            }
        ],
    )
    out = github._list_repos(github_ctx, {})
    assert "example-owner/dotobot" in out
    assert calls[0][1] == "/orgs/example-owner/repos"


def test_github_list_issues_skips_pulls(github_ctx, monkeypatch):
    def responder(method, path, query, body):
        assert path == "/repos/example-owner/dotobot/issues"
        return [
            {
                "number": 1,
                "title": "Bug",
                "state": "open",
                "html_url": "https://github.com/s/a/issues/1",
            },
            {
                "number": 2,
                "title": "PR",
                "state": "open",
                "html_url": "https://github.com/s/a/pull/2",
                "pull_request": {},
            },
        ]

    _gh_stub(monkeypatch, responder)
    out = github._list_issues(github_ctx, {"repo": "dotobot"})
    assert "#1" in out and "Bug" in out
    assert "issue #2" not in out
    assert "pull/2" not in out


def test_github_create_issue(github_ctx, monkeypatch):
    def responder(method, path, query, body):
        assert method == "POST"
        assert path == "/repos/example-owner/dotobot/issues"
        assert body["title"] == "Ship GitHub connector"
        return {"number": 9, "title": body["title"], "html_url": "https://github.com/s/a/issues/9"}

    _gh_stub(monkeypatch, responder)
    out = github._create_issue(github_ctx, {"repo": "dotobot", "title": "Ship GitHub connector"})
    assert out.startswith("ok:") and "#9" in out


def test_github_repo_requires_owner_without_org(paths, monkeypatch):
    record = Connectors(paths).add("github", "GitHub", secret="ghp_test")
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    _gh_stub(monkeypatch, lambda *a, **k: [])
    out = github._list_issues(ctx, {"repo": "dotobot"})
    assert out.startswith("error:") and "owner/name" in out


def test_github_configured_connector_gives_bot_tools(paths):
    Connectors(paths).add("github", "GitHub", secret="ghp_test")
    tools = connector_tools(paths, "atlas")
    assert "github_list_repos" in tools
    assert "github_create_issue" in tools


def test_github_add_also_stores_GITHUB_TOKEN(paths, monkeypatch):
    from harness.secrets import get_secret

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    rec = Connectors(paths).add("github", "GitHub", secret="ghp_test_token")
    assert get_secret(f"connector_{rec['id']}", paths) == "ghp_test_token"
    assert get_secret("GITHUB_TOKEN", paths) == "ghp_test_token"


# -- entity cards ----------------------------------------------------------


def _card_ctx(paths, service="github"):
    """A bound context that records every emitted card (and its id, when the
    handler chose one — deterministic PR ids are how merge updates in place)."""
    emitted: list[tuple[str, dict]] = []
    ids: list[str | None] = []
    secret = "ghp_test" if service == "github" else "lin_api_test"
    record = Connectors(paths).add(
        service,
        service.title(),
        config={"org": "example-owner"} if service == "github" else None,
        secret=secret,
    )

    def emit(card_type, payload, card_id=None):
        emitted.append((card_type, payload))
        ids.append(card_id)
        return card_id or "cid"

    ctx = ConnectorContext(paths=paths, bot="atlas", record=record, emit_card=emit)
    return ctx, emitted, ids


def _pull_row(**over):
    row = {
        "number": 123,
        "title": "Add card protocol",
        "state": "open",
        "html_url": "https://github.com/example-owner/dotobot/pull/123",
        "user": {"login": "octocat"},
        "base": {"ref": "main"},
        "head": {"ref": "feat/cards", "sha": "abc123"},
        "merged_at": None,
        "draft": False,
    }
    row.update(over)
    return row


def test_github_get_pull_emits_card_with_checks(paths, monkeypatch):
    ctx, emitted, _ids = _card_ctx(paths)

    def responder(method, path, query, body):
        if path.endswith("/pulls/123"):
            return _pull_row()
        assert "check-runs" in path and "abc123" in path
        return {"check_runs": [{"conclusion": "success"}, {"conclusion": "success"}]}

    _gh_stub(monkeypatch, responder)
    out = github._get_pull(ctx, {"repo": "dotobot", "number": "123"})
    assert "PR #123" in out and "card" in out
    assert emitted == [
        (
            "github_pull",
            {
                "title": "Add card protocol",
                "number": 123,
                "state": "open",
                "repo": "example-owner/dotobot",
                "author": "octocat",
                "url": "https://github.com/example-owner/dotobot/pull/123",
                "base": "main",
                "head": "feat/cards",
                "checks": "passing",
            },
        )
    ]


@pytest.mark.parametrize(
    ("over", "state"),
    [
        ({"merged_at": "2026-08-23T00:00:00Z", "state": "closed"}, "merged"),
        ({"draft": True}, "draft"),
        ({"state": "closed"}, "closed"),
        ({}, "open"),
    ],
)
def test_github_pull_state_mapping(paths, monkeypatch, over, state):
    ctx, emitted, _ids = _card_ctx(paths)

    def responder(method, path, query, body):
        if "/pulls/" in path:
            return _pull_row(**over)
        return {"check_runs": []}

    _gh_stub(monkeypatch, responder)
    github._get_pull(ctx, {"repo": "dotobot", "number": "123"})
    assert emitted[0][1]["state"] == state


def test_github_get_pull_checks_failure_omits_checks(paths, monkeypatch):
    ctx, emitted, _ids = _card_ctx(paths)

    def responder(method, path, query, body):
        if "/pulls/" in path:
            return _pull_row()
        raise github.GitHubError("no checks API")

    _gh_stub(monkeypatch, responder)
    out = github._get_pull(ctx, {"repo": "dotobot", "number": "123"})
    assert not out.startswith("error:")
    assert "checks" not in emitted[0][1]


def test_github_get_pull_card_id_is_deterministic(paths, monkeypatch):
    """The PR card id is stable across get/list/search/merge so a re-emit
    updates the card in place instead of adding a second one."""
    ctx, emitted, ids = _card_ctx(paths)

    def responder(method, path, query, body):
        if "/pulls/" in path:
            return _pull_row()
        return {"check_runs": []}

    _gh_stub(monkeypatch, responder)
    github._get_pull(ctx, {"repo": "dotobot", "number": "123"})
    assert ids == ["github-pull-example-owner-dotobot-123"]


def test_github_list_pulls_emits_a_card_per_row_without_checks(paths, monkeypatch):
    ctx, emitted, ids = _card_ctx(paths)
    calls = _gh_stub(
        monkeypatch,
        lambda m, p, q, b: [
            _pull_row(),
            _pull_row(number=124, title="Fix roster", draft=True),
        ],
    )
    out = github._list_pulls(ctx, {"repo": "dotobot"})
    assert "PR #123" in out and "cards are already shown" in out
    # exactly one request: no per-PR check-runs fan-out on list
    assert [c[1] for c in calls] == ["/repos/example-owner/dotobot/pulls"]
    assert [e[0] for e in emitted] == ["github_pull", "github_pull"]
    assert emitted[0][1]["number"] == 123
    assert "checks" not in emitted[0][1]
    assert emitted[1][1]["state"] == "draft"
    assert ids == [
        "github-pull-example-owner-dotobot-123",
        "github-pull-example-owner-dotobot-124",
    ]


def test_github_list_pulls_carries_mergeable_only_when_present(paths, monkeypatch):
    ctx, emitted, _ids = _card_ctx(paths)
    _gh_stub(
        monkeypatch,
        lambda m, p, q, b: [_pull_row(mergeable=True), _pull_row(number=124)],
    )
    github._list_pulls(ctx, {"repo": "dotobot"})
    assert emitted[0][1]["mergeable"] is True
    assert "mergeable" not in emitted[1][1]


def test_github_search_pr_items_emit_pull_cards(paths, monkeypatch):
    ctx, emitted, ids = _card_ctx(paths)
    items = [
        {
            "number": 7,
            "title": "Add cards",
            "state": "closed",
            "html_url": "https://github.com/example-owner/dotobot/pull/7",
            "repository_url": "https://api.github.com/repos/example-owner/dotobot",
            "user": {"login": "octocat"},
            "pull_request": {"merged_at": "2026-08-23T00:00:00Z"},
        },
        {
            "number": 8,
            "title": "Just an issue",
            "state": "open",
            "html_url": "https://github.com/example-owner/dotobot/issues/8",
            "repository_url": "https://api.github.com/repos/example-owner/dotobot",
        },
    ]
    _gh_stub(monkeypatch, lambda m, p, q, b: {"items": items})
    out = github._search_issues(ctx, {"query": "cards"})
    assert "cards are already shown" in out
    assert "Just an issue" in out  # plain issues stay text lines
    assert [e[0] for e in emitted] == ["github_pull"]
    assert emitted[0][1]["state"] == "merged"
    assert emitted[0][1]["repo"] == "example-owner/dotobot"
    assert ids == ["github-pull-example-owner-dotobot-7"]


def test_github_merge_pull_merges_and_flips_the_card(paths, monkeypatch):
    ctx, emitted, ids = _card_ctx(paths)

    def responder(method, path, query, body):
        if path.endswith("/pulls/123/merge"):
            assert method == "PUT"
            assert body == {"merge_method": "squash"}
            return {"merged": True, "sha": "abc123"}
        assert method == "GET" and path.endswith("/pulls/123")
        return _pull_row(merged_at="2026-08-31T00:00:00Z", state="closed")

    _gh_stub(monkeypatch, responder)
    out = github._merge_pull(ctx, {"repo": "dotobot", "number": "123", "method": "squash"})
    assert out.startswith("ok: merged example-owner/dotobot#123")
    assert emitted[0][0] == "github_pull"
    assert emitted[0][1]["state"] == "merged"
    # same id the list/get card used, so the card updates in place
    assert ids == ["github-pull-example-owner-dotobot-123"]


def test_github_merge_pull_card_survives_a_failed_refetch(paths, monkeypatch):
    ctx, emitted, ids = _card_ctx(paths)

    def responder(method, path, query, body):
        if method == "PUT":
            return {"merged": True}
        raise github.GitHubError("GitHub API HTTP 500: hiccup")

    _gh_stub(monkeypatch, responder)
    out = github._merge_pull(ctx, {"repo": "dotobot", "number": "123"})
    assert out.startswith("ok: merged")
    assert emitted[0][1]["state"] == "merged"
    assert emitted[0][1]["number"] == 123
    assert ids == ["github-pull-example-owner-dotobot-123"]


def test_github_merge_pull_conflict_is_an_error_and_no_card(paths, monkeypatch):
    ctx, emitted, _ids = _card_ctx(paths)

    def responder(method, path, query, body):
        raise github.GitHubError("GitHub API HTTP 409: Head branch was modified")

    _gh_stub(monkeypatch, responder)
    out = github._merge_pull(ctx, {"repo": "dotobot", "number": "123"})
    assert out.startswith("error:") and "409" in out
    assert emitted == []


def test_github_merge_pull_rejects_a_bad_method(paths, monkeypatch):
    ctx, emitted, _ids = _card_ctx(paths)
    calls = _gh_stub(monkeypatch, lambda m, p, q, b: {})
    out = github._merge_pull(ctx, {"repo": "dotobot", "number": "1", "method": "yolo"})
    assert out.startswith("error:") and "squash" in out
    assert calls == [] and emitted == []


def test_github_merge_pull_without_a_stored_pat(paths, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    record = Connectors(paths).add("github", "GitHub")
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    out = github._merge_pull(ctx, {"repo": "example-owner/dotobot", "number": "1"})
    assert out.startswith("error:")
    assert "request_secret" in out and "GITHUB_TOKEN" in out


def test_github_get_issue_emits_card_and_delegates_prs(paths, monkeypatch):
    ctx, emitted, _ids = _card_ctx(paths)
    issue = {
        "number": 88,
        "title": "Crash on empty roster",
        "state": "open",
        "html_url": "https://github.com/example-owner/dotobot/issues/88",
        "user": {"login": "octocat"},
        "labels": [{"name": "bug"}, {"name": "p1"}],
        "assignee": {"login": "hubot"},
    }

    def responder(method, path, query, body):
        if path.endswith("/issues/88"):
            return issue
        if path.endswith("/issues/123"):
            return {"number": 123, "pull_request": {"url": "x"}, "state": "open"}
        if path.endswith("/pulls/123"):
            return _pull_row()
        return {"check_runs": []}

    _gh_stub(monkeypatch, responder)
    out = github._get_issue(ctx, {"repo": "dotobot", "number": "88"})
    assert "issue" in out and "#88" in out
    assert emitted[-1] == (
        "github_issue",
        {
            "title": "Crash on empty roster",
            "number": 88,
            "state": "open",
            "repo": "example-owner/dotobot",
            "author": "octocat",
            "url": "https://github.com/example-owner/dotobot/issues/88",
            "labels": ["bug", "p1"],
            "assignee": "hubot",
        },
    )
    # an issue number that is really a PR renders the PR card instead
    github._get_issue(ctx, {"repo": "dotobot", "number": "123"})
    assert emitted[-1][0] == "github_pull"


def test_github_create_issue_emits_card(paths, monkeypatch):
    ctx, emitted, _ids = _card_ctx(paths)
    _gh_stub(
        monkeypatch,
        lambda m, p, q, b: {
            "number": 9,
            "title": b["title"],
            "state": "open",
            "html_url": "https://github.com/example-owner/dotobot/issues/9",
            "user": {"login": "atlas"},
        },
    )
    out = github._create_issue(ctx, {"repo": "dotobot", "title": "Ship cards"})
    assert out.startswith("ok:")
    assert emitted[0][0] == "github_issue"
    assert emitted[0][1]["number"] == 9


def test_linear_get_issue_emits_card(paths, monkeypatch):
    ctx, emitted, _ids = _card_ctx(paths, service="linear")
    node = {
        "identifier": "TEST-44",
        "title": "Rate limit the webhook",
        "url": "https://linear.app/x/issue/TEST-44",
        "priorityLabel": "High",
        "state": {"name": "In Progress"},
        "assignee": {"displayName": "Alex"},
        "team": {"key": "ALT"},
    }

    def fake(api_key, query, variables=None):
        assert variables == {"id": "TEST-44"}
        return {"issue": node}

    monkeypatch.setattr(linear, "_graphql", fake)
    out = linear._get_issue(ctx, {"id": "TEST-44"})
    assert "TEST-44" in out and "card" in out
    assert emitted == [
        (
            "linear_issue",
            {
                "identifier": "TEST-44",
                "title": "Rate limit the webhook",
                "url": "https://linear.app/x/issue/TEST-44",
                "status": "In Progress",
                "priority": "High",
                "assignee": "Alex",
                "team": "ALT",
            },
        )
    ]


def test_linear_get_issue_missing(paths, monkeypatch):
    ctx, emitted, _ids = _card_ctx(paths, service="linear")
    monkeypatch.setattr(linear, "_graphql", lambda k, q, v=None: {"issue": None})
    out = linear._get_issue(ctx, {"id": "TEST-27"})
    assert out.startswith("error:")
    assert emitted == []


def test_connector_card_is_noop_without_writer(paths):
    record = Connectors(paths).add("github", "GitHub", secret="ghp_test")
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    assert ctx.card("github_pull", {"title": "x"}) is None


def test_google_connector_offers_a_sign_in_card(paths):
    Connectors(paths).add("google", "Google Drive")
    cards: list[tuple[str, dict]] = []
    bound = tools_for_bot(paths, "atlas", emit_card=lambda t, p: cards.append((t, p)) or "cid")
    assert "google_connect" in bound
    out = bound["google_connect"][1]({})
    assert "sign-in card" in out
    assert cards and cards[0][0] == "connector"
    payload = cards[0][1]
    assert payload["title"] == "Google Drive"
    assert "share files" in payload["description"]
    assert payload["type"] == "google"
    assert payload["connector_id"]
