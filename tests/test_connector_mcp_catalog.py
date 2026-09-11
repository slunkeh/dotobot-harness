"""Live MCP catalog types: each is implemented and binds a connect stub.

Official remote MCP URLs are verified against the vendor host (401 + OAuth
protected-resource metadata, or a successful initialize). Types still in
`connector_stubs.py` stay config-only until someone confirms a URL.
"""

from __future__ import annotations

import pytest

from connectors.registry import tools_for_bot
from harness.connector_stubs import STUBS
from harness.connectors import Connectors, catalog, mcp_url
from harness.paths import HarnessPaths

#: Official hosted Streamable HTTP endpoints promoted off the stub list.
#: Keep in lockstep with CATALOG — a missing or wrong URL is a Connect that
#: 501s or hits the wrong host.
PROMOTED_MCP = {
    "calendly": "https://mcp.calendly.com",
    "clickup": "https://mcp.clickup.com/mcp",
    "dropbox": "https://mcp.dropbox.com/mcp",
    "trello": "https://mcp.trello.com/v1",
    "typeform": "https://api.typeform.com/mcp",
    "jira": "https://mcp.atlassian.com/v1/mcp",
    "klaviyo": "https://mcp.klaviyo.com/mcp",
    "beehiiv": "https://mcp.beehiiv.com/mcp",
    "bitly": "https://api-ssl.bitly.com/v4/mcp",
    "braze": "https://mcp.braze.com/mcp",
    "customer_io": "https://mcp.customer.io/mcp",
    "contentful": "https://mcp.contentful.com/mcp",
    "instantly": "https://mcp.instantly.ai/mcp",
    "ez_texting": "https://mcp.eztexting.com/mcp",
    "frontify": "https://mcp.frontify-integrations.com/mcp",
    "handwrytten": "https://mcp.handwrytten.com/mcp",
    "front": "https://mcp.frontapp.com/mcp",
    "aweber": "https://mcp.aweber.com/mcp",
    "hygraph": "https://mcp.hygraph.com/mcp",
    "fullenrich": "https://mcp.fullenrich.com/mcp",
    "brandfetch": "https://mcp.brandfetch.io/mcp",
    "amazon_advertising": "https://advertising-ai.amazon.com/mcp",
    "dub": "https://mcp.dub.sh/mcp/dub-partners",
    "elastic_email": "https://mcp.elasticemail.com/mcp",
    "cufinder": "https://mcp.cufinder.io/mcp",
    "heyreach": "https://mcp.heyreach.io/mcp",
    "xero": "https://mcp.xero.com/mcp",
    "pipedrive": "https://mcp.pipedrive.com/mcp",
}

STATIC_RUNTIME = {"linear", "github", "gmail"}


@pytest.fixture
def paths(tmp_path):
    return HarnessPaths(home=tmp_path)


def _mcp_entries() -> list[dict]:
    return [c for c in catalog() if c.get("mcp")]


def test_promoted_types_left_the_stub_list():
    stub_types = {s["type"] for s in STUBS}
    still = sorted(PROMOTED_MCP.keys() & stub_types)
    assert still == [], still


def test_promoted_official_mcp_urls():
    by_type = {c["type"]: c for c in catalog()}
    for type_, url in PROMOTED_MCP.items():
        rec = by_type[type_]
        assert rec["mcp"] is True, type_
        assert rec["implemented"] is True, type_
        assert rec["mcp_url"] == url, type_
        assert rec["fields"] == [], type_
        assert mcp_url(type_) == url, type_


@pytest.mark.parametrize("type_", sorted(PROMOTED_MCP), ids=sorted(PROMOTED_MCP))
def test_each_promoted_connector_offers_a_connect_stub(paths, type_):
    Connectors(paths).add(type_, type_)
    bound = tools_for_bot(paths, "atlas")
    assert f"{type_}_connect" in bound, sorted(bound)


@pytest.mark.parametrize(
    "type_",
    sorted(c["type"] for c in _mcp_entries()),
    ids=sorted(c["type"] for c in _mcp_entries()),
)
def test_every_mcp_catalog_type_is_connectable_when_added(paths, type_):
    """Unconnected MCP types bind a sign-in stub; Linear/GitHub keep the
    static runtime so a PAT still works without OAuth."""
    Connectors(paths).add(type_, type_)
    bound = tools_for_bot(paths, "atlas")
    assert bound, type_
    if type_ in STATIC_RUNTIME:
        assert any(name.startswith(f"{type_}_") for name in bound)
        return
    if type_ in ("shopify", "n8n"):
        # Tenant URL is empty until the operator fills the host field.
        assert f"{type_}_connect" in bound
        return
    assert f"{type_}_connect" in bound, sorted(bound)


def test_every_mcp_catalog_entry_is_marked_implemented():
    missing = [
        c["type"]
        for c in catalog()
        if (c.get("mcp_url") or c.get("mcp_url_template")) and not c["implemented"]
    ]
    assert missing == []
