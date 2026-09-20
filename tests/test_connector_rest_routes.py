"""Request contracts checked against the provider references in docs/connector-rest-routes.md."""

import io
import json
from unittest.mock import patch

import pytest

from connectors import generic
from connectors.base import ConnectorContext
from connectors.registry import tools_for_bot
from harness.connectors import Connectors
from harness.paths import HarnessPaths

# Literal expected requests intentionally independent of catalogue metadata.
CASES = [
    ("abyssale", {}, "/designs", "https://api.abyssale.com/designs", "X-api-key", "fixture-key"),
    (
        "activecampaign",
        {"api_domain": "sample.api-us1.com"},
        "/users/me",
        "https://sample.api-us1.com/api/3/users/me",
        "Api-token",
        "fixture-key",
    ),
    (
        "attentive",
        {},
        "/subscriptions",
        "https://api.attentivemobile.com/v1/subscriptions",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "callrail",
        {},
        "/a.json",
        "https://api.callrail.com/v3/a.json",
        "Authorization",
        "Token token=fixture-key",
    ),
    (
        "cloud_convert",
        {},
        "/users/me",
        "https://api.cloudconvert.com/v2/users/me",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "doppler",
        {},
        "/accounts/example@example.com/lists",
        "https://restapi.fromdoppler.com/accounts/example@example.com/lists",
        "Authorization",
        "token fixture-key",
    ),
    (
        "getresponse",
        {},
        "/accounts",
        "https://api.getresponse.com/v3/accounts",
        "X-auth-token",
        "api-key fixture-key",
    ),
]


@pytest.mark.parametrize("type_,config,path,url,header,value", CASES)
def test_bound_connector_read_and_write_contract(tmp_path, type_, config, path, url, header, value):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(type_, type_, config=config, secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    for method, args, name in [
        ("GET", {"path": path, "query": {"limit": 2}}, f"{type_}_get"),
        ("POST", {"path": path, "method": "POST", "body": {"fixture": True}}, f"{type_}_request"),
    ]:
        response = io.BytesIO(b'{"ok":true}')
        response.status = 200
        with patch("connectors.generic._open", return_value=response) as send:
            # Exercise the real registry wrapper and secret store, not only URL helpers.
            result = bound[name][1](args)
        assert "HTTP 200" in result
        request = send.call_args.args[0]
        assert request.full_url == url + ("?limit=2" if method == "GET" else "")
        assert request.method == method
        assert request.get_header(header) == value
        if header != "Authorization":
            assert request.get_header("Authorization") is None
        if method == "POST":
            assert json.loads(request.data) == {"fixture": True}
            assert request.get_header("Content-type") == "application/json"
        assert "fixture-key" not in request.full_url


@pytest.mark.parametrize(
    "domain",
    [
        "",
        "https://sample.api-us1.com",
        "evil.test/path",
        "sample.api-us1.com@evil.test",
        "sample.test?x=1",
    ],
)
def test_activecampaign_requires_host_only_configuration(tmp_path, domain):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "activecampaign", "test", config={"api_domain": domain}, secret="fixture-key"
    )
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    with patch("connectors.generic._open") as send:
        assert "no REST host" in generic._get(ctx, {"path": "/users/me"})
    send.assert_not_called()


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_http_redirect_cannot_replay_api_key(code):
    import urllib.error
    import urllib.request
    from email.message import Message

    # Exercise urllib's redirect handling without opening a socket. Even a
    # same-host redirect is refused: APIs should use their documented endpoint.
    request = urllib.request.Request(
        "https://api.abyssale.com/designs", headers={"X-API-Key": "fixture-key"}
    )
    handler = generic._NoRedirect()
    handler.parent = urllib.request.build_opener(handler)
    headers = Message()
    headers["Location"] = "https://other.example/collect"
    with patch.object(handler.parent, "open") as follow:
        with pytest.raises(urllib.error.HTTPError):
            handler.parent.error("http", request, io.BytesIO(), code, "Redirect", headers)
    follow.assert_not_called()
