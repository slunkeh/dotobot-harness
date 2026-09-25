"""Docs tool discovery and requests, with no account or network dependency."""

import io
import json

import pytest

from agent.context import ConnectorCatalogue
from agent.tools import Tool
from connectors.registry import tools_for_bot
from harness.connectors import Connectors
from harness.paths import HarnessPaths


@pytest.fixture
def docs_tools(tmp_path, monkeypatch):
    paths = HarnessPaths(home=tmp_path)
    store = Connectors(paths)
    selected = store.add("google_docs", "Test")
    store.add("google_docs", "Other")
    monkeypatch.setattr("harness.delegated_oauth.token", lambda *args: "fixture-token")
    return tools_for_bot(paths, "atlas", record_ids={selected["id"]})


@pytest.mark.parametrize("operation", ["create", "edit"])
def test_deferred_docs_write_tool_is_discoverable_by_operation(docs_tools, operation):
    available = {
        name: Tool(spec, lambda ctx, args: "unused")
        for name, (spec, _) in docs_tools.items()
    }
    catalogue = ConnectorCatalogue(available)
    catalogue.schema_budget = 2000

    result = catalogue.load(None, {"query": f"google docs {operation}"})

    assert catalogue.selected == ["google_docs_test_request"], result
    assert "google_docs_other_request" not in result


def test_docs_read_schema_requires_a_document_not_an_account_probe(docs_tools):
    spec = docs_tools["google_docs_test_get"][0]

    assert "/documents/DOCUMENT_ID" in spec.description
    assert "no account" in spec.description.lower()
    assert "/accounts" not in json.dumps(spec.parameters)


@pytest.mark.parametrize(
    ("tool", "args", "method", "url"),
    [
        (
            "request",
            {"method": "POST", "path": "/documents", "body": {"title": "Test document"}},
            "POST",
            "https://docs.googleapis.com/v1/documents",
        ),
        (
            "get",
            {"path": "/documents/fixture-document"},
            "GET",
            "https://docs.googleapis.com/v1/documents/fixture-document",
        ),
        (
            "request",
            {
                "method": "POST",
                "path": "/documents/fixture-document:batchUpdate",
                "body": {"requests": [{"insertText": {"location": {"index": 1}, "text": "Test"}}]},
            },
            "POST",
            "https://docs.googleapis.com/v1/documents/fixture-document:batchUpdate",
        ),
    ],
)
def test_docs_selected_account_routes_create_read_and_edit(docs_tools, monkeypatch, tool, args, method, url):
    requests = []

    def respond(request, *, timeout):
        requests.append(request)
        response = io.BytesIO(b'{"documentId":"fixture-document"}')
        response.status = 200
        return response

    monkeypatch.setattr("connectors.generic._open", respond)
    result = docs_tools[f"google_docs_test_{tool}"][1](args)

    assert result.startswith("HTTP 200")
    assert len(requests) == 1
    assert requests[0].full_url == url
    assert requests[0].get_method() == method
    assert requests[0].get_header("Authorization") == "Bearer fixture-token"
    assert (json.loads(requests[0].data) if requests[0].data else None) == args.get("body")
