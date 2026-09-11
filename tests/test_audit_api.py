"""GET /api/bots/<b>/audit: the gate's authorization ledger over HTTP.

The rows are written by harness/audit.py before every governed tool call
runs; this endpoint is how a client shows "what was this bot allowed to do,
and who said so" after the conversation that produced it is gone.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from harness import audit
from harness.linking import get_or_create_key
from harness.orchestrator import Orchestrator
from harness.server import make_server

ROSTER = '[[bots]]\nname = "atlas"\nprovider = "echo"\n'


@pytest.fixture
def keyed_server(tmp_path):
    """A keyed server over an initialized home; no bot processes needed."""
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    key = get_or_create_key(orch.paths)
    httpd = make_server(orch, "127.0.0.1", 0, key)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", key, orch.paths
    finally:
        httpd.shutdown()
        orch.down()


def _get(url: str, key: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def _seed(paths) -> None:
    audit.record(
        paths,
        "atlas",
        event="decision",
        tool="run_command",
        intent="run_command",
        target="ls",
        decision=audit.DECISION_ALLOW,
        rule="",
        source="policy",
        now=100.0,
    )
    audit.record(
        paths,
        "atlas",
        event="outcome",
        tool="run_command",
        intent="run_command",
        target="ls",
        decision=audit.OUTCOME_OK,
        now=101.0,
    )
    audit.record(
        paths,
        "atlas",
        event="decision",
        tool="run_command",
        intent="run_command",
        target="rm -rf /",
        decision=audit.DECISION_REFUSE,
        rule="shellguard",
        source="shell-guard",
        now=102.0,
    )


def test_audit_returns_the_ledger_newest_first(keyed_server):
    base, key, paths = keyed_server
    _seed(paths)
    status, body = _get(f"{base}/api/bots/atlas/audit", key)
    assert status == 200
    assert body["bot"] == "atlas"
    assert [r["ts"] for r in body["rows"]] == [102.0, 101.0, 100.0]
    refused = body["rows"][0]
    assert refused["decision"] == "refuse"
    assert refused["rule"] == "shellguard"
    assert refused["target"] == "rm -rf /"


def test_audit_filters_and_pages(keyed_server):
    base, key, paths = keyed_server
    _seed(paths)
    status, body = _get(f"{base}/api/bots/atlas/audit?decision=refuse", key)
    assert status == 200
    assert [r["ts"] for r in body["rows"]] == [102.0]
    status, body = _get(f"{base}/api/bots/atlas/audit?event=decision&limit=1", key)
    assert status == 200
    assert [r["ts"] for r in body["rows"]] == [102.0]
    status, body = _get(f"{base}/api/bots/atlas/audit?limit=0", key)
    assert status == 200 and len(body["rows"]) == 1, "limit clamps to at least one row"
    status, body = _get(f"{base}/api/bots/atlas/audit?limit=abc", key)
    assert status == 400


def test_audit_is_empty_for_a_bot_with_no_decisions(keyed_server):
    base, key, _paths = keyed_server
    status, body = _get(f"{base}/api/bots/atlas/audit", key)
    assert status == 200
    assert body == {"bot": "atlas", "rows": []}


def test_audit_404s_an_unknown_bot(keyed_server):
    base, key, _paths = keyed_server
    status, body = _get(f"{base}/api/bots/no-such-bot/audit", key)
    assert status == 404
    assert "error" in body


def test_audit_skips_a_corrupt_line(keyed_server):
    """One bad append must not make the whole trail unreadable — that is
    exactly when someone needs to read it."""
    base, key, paths = keyed_server
    _seed(paths)
    with audit.audit_file(paths, "atlas").open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    status, body = _get(f"{base}/api/bots/atlas/audit", key)
    assert status == 200
    assert len(body["rows"]) == 3
