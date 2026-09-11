"""Trust-scope invariant: ids route, the key authorizes.

`request_id`s, bot names, room ids, and session/prompt ids are routing
selectors, never authorization tokens — knowing one grants nothing. The
bearer check (linking key / $HARNESS_TOKEN) at the top of every route is the
authorization; these tests sweep id-carrying routes on every verb without the
key and require 401 before any handler logic sees the id. The one deliberate
exception, GET /oauth/callback, is pinned too: its check is the single-use
random OAuth `state` (a bearer-equivalent secret), so it must answer the
browser rather than 401.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from harness.linking import get_or_create_key
from harness.orchestrator import Orchestrator
from harness.server import make_server

ROSTER = '[[bots]]\nname = "atlas"\nprovider = "echo"\n'

# Every verb, with ids a caller could plausibly know or guess (bot names,
# request ids, prompt ids, upload basenames, send nonces). None of these may
# reach a handler without the key.
ID_ROUTES = [
    ("GET", "/api/voice/elevenlabs/voices"),
    ("POST", "/api/voice/elevenlabs/key"),
    ("DELETE", "/api/voice/elevenlabs/key"),
    ("POST", "/api/voice/elevenlabs/token"),
    ("POST", "/api/voice/session"),
    ("PATCH", "/api/voice"),
    ("GET", "/ws"),
    ("GET", "/api/bots"),
    ("GET", "/api/bots/atlas/history"),
    ("GET", "/api/bots/atlas/threads/abc123"),
    ("GET", "/api/bots/atlas/queue"),
    ("GET", "/api/bots/atlas/audit"),
    ("GET", "/api/logs"),
    ("GET", "/api/logs/server"),
    ("GET", "/api/logs/atlas/stream"),
    ("GET", "/api/reports"),
    ("GET", "/api/reports/rpt-20260907-120000-abcdef"),
    ("POST", "/api/reports"),
    ("DELETE", "/api/reports/rpt-20260907-120000-abcdef"),
    ("GET", "/api/streams/rid123?bot=atlas"),
    ("GET", "/api/sends/nonce123"),
    ("GET", "/api/control/atlas"),
    ("GET", "/api/screen/atlas"),
    ("GET", "/api/prompts?bot=atlas"),
    ("GET", "/api/uploads/somefile.png"),
    ("POST", "/api/chat"),
    ("POST", "/api/answers"),
    ("POST", "/api/secrets"),
    ("POST", "/api/bots/atlas/queue/rid123/now"),
    ("POST", "/api/bots/atlas/restart"),
    ("POST", "/api/control/atlas/takeover"),
    ("POST", "/api/control/atlas/return"),
    ("PATCH", "/api/bots/atlas"),
    ("PUT", "/api/bots/atlas/soul"),
    ("DELETE", "/api/bots/atlas"),
]


@pytest.fixture(scope="module")
def keyed_server(tmp_path_factory):
    """A keyed server over an initialized home; no bot processes needed."""
    tmp_path = tmp_path_factory.mktemp("trust-scope")
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    key = get_or_create_key(orch.paths)
    httpd = make_server(orch, "127.0.0.1", 0, key)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", key
    finally:
        httpd.shutdown()
        orch.down()


def _request(url: str, method: str, token: str | None = None) -> tuple[int, bytes]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if method in ("POST", "PATCH", "PUT"):
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url,
        data=b"{}" if method in ("POST", "PATCH", "PUT") else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.mark.parametrize(("method", "path"), ID_ROUTES)
def test_knowing_an_id_grants_nothing_without_the_key(keyed_server, method, path):
    base, _key = keyed_server
    status, body = _request(f"{base}{path}", method)
    assert status == 401, f"{method} {path} answered {status}, not 401"
    assert json.loads(body.decode()) == {"error": "unauthorized"}


def test_with_the_key_an_id_only_routes(keyed_server):
    """The same unknown ids are reachable with the key and decide only the
    routing outcome (404), proving the id was never the authorization."""
    base, key = keyed_server
    status, body = _request(f"{base}/api/sends/nonce123", "GET", token=key)
    assert status == 404
    assert json.loads(body.decode())["status"] == "unknown"
    status, _body = _request(f"{base}/api/bots/no-such-bot/history", "GET", token=key)
    assert status == 404


def test_oauth_callback_is_checked_by_state_not_bearer(keyed_server):
    """The one unauthenticated route: the browser cannot carry the key, so
    the single-use OAuth `state` is the check. A bogus state must be refused
    by the exchange (400 page), never by the bearer gate (401)."""
    base, _key = keyed_server
    status, body = _request(f"{base}/oauth/callback?state=bogus&code=x", "GET")
    assert status == 400
    assert b"unauthorized" not in body
