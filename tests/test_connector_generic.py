"""Generic API-key HTTP runtime for catalog stubs with no MCP."""

from __future__ import annotations

import pytest

from connectors import generic
from connectors.base import ConnectorContext
from connectors.registry import tool_names, tools_for_bot
from harness.connectors import Connectors, catalog
from harness.paths import HarnessPaths


@pytest.fixture
def paths(tmp_path):
    return HarnessPaths(home=tmp_path)


def _ctx(paths, type_="mailchimp", secret="key-us6", config=None):
    record = Connectors(paths).add(type_, type_, config=config, secret=secret)
    return ConnectorContext(paths=paths, bot="atlas", record=record)


def test_stubs_are_api_key_and_implemented():
    from harness.connector_stubs import STUBS

    by_type = {c["type"]: c for c in catalog()}
    for stub in STUBS:
        rec = by_type[stub["type"]]
        assert rec["mcp"] is False, stub["type"]
        assert rec["auth"] == "api_key", stub["type"]
        assert rec["implemented"] is True, stub["type"]
        assert f"{stub['type']}_get" in rec["tools"]
        assert f"{stub['type']}_request" in rec["tools"]


def test_mailchimp_base_comes_from_the_key_suffix():
    assert generic._mailchimp_base("abc-us21") == "https://us21.api.mailchimp.com/3.0"
    assert generic._mailchimp_base("nope") == ""


def test_mailchimp_get_hits_the_dc_host(paths, monkeypatch):
    ctx = _ctx(paths, secret="abc-us6")
    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        class Resp:
            status = 200
            def read(self):
                return b'{"ok": true}'
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        return Resp()

    monkeypatch.setattr(generic, "_open", fake_urlopen)
    out = generic._get(ctx, {"path": "/lists"})
    assert seen["url"] == "https://us6.api.mailchimp.com/3.0/lists"
    assert seen["auth"] == "Bearer abc-us6"
    assert "HTTP 200" in out


def test_request_refuses_to_leave_the_api_host(paths):
    ctx = _ctx(paths, secret="abc-us6")
    out = generic._get(ctx, {"path": "https://evil.example/steal"})
    assert "error" in out
    out = generic._get(ctx, {"path": "/../secret"})
    assert "error" in out


def test_missing_secret_names_request_secret(paths):
    ctx = _ctx(paths, secret="")
    # add() with empty secret still creates the record
    record = Connectors(paths).add("twilio", "Twilio")
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    out = generic._get(ctx, {"path": "/2010-04-01/Accounts.json"})
    assert "request_secret" in out
    assert "connector_" in out


def test_unknown_stub_without_a_host_does_not_guess(paths):
    ctx = _ctx(paths, type_="360nrs", secret="k")
    out = generic._get(ctx, {"path": "/x"})
    assert "no REST host" in out


def test_zendesk_template_needs_subdomain(paths, monkeypatch):
    ctx = _ctx(paths, type_="zendesk", secret="tok", config={"subdomain": "acme"})
    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        class Resp:
            status = 200
            def read(self):
                return b"[]"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        return Resp()

    monkeypatch.setattr(generic, "_open", fake_urlopen)
    out = generic._get(ctx, {"path": "/tickets.json"})
    assert seen["url"] == "https://acme.zendesk.com/api/v2/tickets.json"
    assert "HTTP 200" in out

    ctx = _ctx(paths, type_="zendesk", secret="tok", config={})
    assert "no REST host" in generic._get(ctx, {"path": "/tickets.json"})


def test_twilio_uses_basic_auth(paths, monkeypatch):
    ctx = _ctx(paths, type_="twilio", secret="sid:token")
    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["auth"] = req.get_header("Authorization")
        seen["url"] = req.full_url
        class Resp:
            status = 200
            def read(self):
                return b"{}"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        return Resp()

    monkeypatch.setattr(generic, "_open", fake_urlopen)
    generic._get(ctx, {"path": "/2010-04-01/Accounts.json"})
    assert seen["url"].startswith("https://api.twilio.com/")
    assert seen["auth"].startswith("Basic ")


def test_configured_stub_gives_bot_tools(paths):
    Connectors(paths).add("mailchimp", "Mailchimp", secret="k-us1")
    tools = tools_for_bot(paths, "atlas")
    assert "mailchimp_get" in tools
    assert "mailchimp_request" in tools
    assert "mailchimp_connect" not in tools


def test_tool_names_for_api_key_stub():
    assert tool_names("mailchimp") == ["mailchimp_get", "mailchimp_request"]
    assert "linear_create_issue" in tool_names("linear")
    assert tool_names("notion") == []  # MCP, names come from the server
