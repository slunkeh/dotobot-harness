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
    ("beamer", {}, "/posts", "https://api.getbeamer.com/v0/posts", "Beamer-api-key", "fixture-key"),
    (
        "convertkit",
        {},
        "/account",
        "https://api.kit.com/v4/account",
        "X-kit-api-key",
        "fixture-key",
    ),
    (
        "esputnik",
        {},
        "/v1/account/info",
        "https://esputnik.com/api/v1/account/info",
        "Authorization",
        "Basic dGVzdC11c2VyOnRlc3QtYXBpLXBhc3N3b3Jk",
    ),
    ("joggai", {}, "/endpoints", "https://api.jogg.ai/v2/endpoints", "X-api-key", "fixture-key"),
    (
        "copicake",
        {},
        "/image/get",
        "https://api.copicake.com/v1/image/get",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "emailoctopus",
        {},
        "/lists",
        "https://api.emailoctopus.com/lists",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "acelle_mail",
        {"instance_domain": "mail.example.com"},
        "/me",
        "https://mail.example.com/api/v1/me",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "emailable",
        {},
        "/account",
        "https://api.emailable.com/v1/account",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "dropcontact",
        {},
        "/webhook",
        "https://api.dropcontact.com/v1/enrich/webhook",
        "X-access-token",
        "fixture-key",
    ),
    (
        "dynapictures",
        {},
        "/workspaces",
        "https://api.dynapictures.com/workspaces",
        "Authorization",
        "Bearer fixture-key",
    ),
    ("egoi", {}, "/my-account", "https://api.egoiapp.com/my-account", "Apikey", "fixture-key"),
    (
        "email_on_acid",
        {},
        "/auth",
        "https://api.emailonacid.com/v5/auth",
        "Authorization",
        "Basic dGVzdC11c2VyOnRlc3QtYXBpLXBhc3N3b3Jk",
    ),
    (
        "fomo",
        {},
        "/applications/me/events",
        "https://api.fomo.com/api/v1/applications/me/events",
        "Authorization",
        "Token fixture-key",
    ),
    (
        "growsurf",
        {},
        "/campaign/example",
        "https://api.growsurf.com/v2/campaign/example",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "instasent",
        {},
        "/project/example",
        "https://api.instasent.com/v1/project/example",
        "Authorization",
        "Bearer fixture-key",
    ),
    ("bigmailer", {}, "/me", "https://api.bigmailer.io/v1/me", "X-api-key", "fixture-key"),
    ("cardly", {}, "/art", "https://api.card.ly/v2/art", "Api-key", "fixture-key"),
    (
        "360nrs",
        {},
        "/account",
        "https://dashboard.360nrs.com/api/rest/account",
        "Authorization",
        "Basic dGVzdC11c2VyOnRlc3QtYXBpLXBhc3N3b3Jk",
    ),
    (
        "active_trail",
        {},
        "/groups",
        "https://webapi.mymarketing.co.il/api/groups",
        "Authorization",
        "fixture-key",
    ),
    (
        "campaign_monitor",
        {},
        "/clients.json",
        "https://api.createsend.com/api/v3.3/clients.json",
        "Authorization",
        "Basic Zml4dHVyZS1rZXk6",
    ),
    (
        "drip",
        {},
        "/v2/accounts",
        "https://api.getdrip.com/v2/accounts",
        "Authorization",
        "Basic Zml4dHVyZS1rZXk6",
    ),
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
    secret = (
        "test-user:test-api-password"
        if type_ in {"360nrs", "email_on_acid", "esputnik"}
        else "fixture-key"
    )
    record = Connectors(paths).add(type_, type_, config=config, secret=secret)
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
            assert request.get_header("Content-type") == (
                "text/json" if type_ == "cardly" else "application/json"
            )
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
@pytest.mark.parametrize(
    "type_,field", [("activecampaign", "api_domain"), ("acelle_mail", "instance_domain")]
)
def test_template_requires_host_only_configuration(tmp_path, domain, type_, field):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(type_, "test", config={field: domain}, secret="fixture-key")
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


def _response(payload):
    response = io.BytesIO(json.dumps(payload).encode())
    response.status = 200
    return response


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_4dem_exchanges_key_before_each_resource_request(tmp_path, method):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("4dem", "4Dem", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    args = {"path": "/addressbook/"}
    name = "4dem_get"
    if method == "POST":
        args.update(method="POST", body={"name": "fixture"})
        name = "4dem_request"
    responses = [
        _response({"token": "first-token"}),
        _response([]),
        _response({"token": "second-token"}),
        _response([]),
    ]
    with patch("connectors.generic._open", side_effect=responses) as send:
        assert "HTTP 200" in bound[name][1](args)
        assert "HTTP 200" in bound[name][1](args)
    requests = [call.args[0] for call in send.call_args_list]
    for index, token in [(0, "first-token"), (2, "second-token")]:
        auth, request = requests[index : index + 2]
        assert auth.full_url == "https://api.4dem.it/authenticate"
        assert auth.method == "POST"
        assert json.loads(auth.data) == {"APIKey": "fixture-key"}
        assert auth.get_header("Authorization") is None
        assert request.full_url == "https://api.4dem.it/addressbook/"
        assert request.get_header("Authorization") == "Bearer " + token
        assert request.method == method
        if method == "POST":
            assert json.loads(request.data) == {"name": "fixture"}
        else:
            assert request.data is None
    assert ConnectorContext(paths=paths, bot="atlas", record=record).secret() == "fixture-key"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"token": None},
        {"token": 123},
        {"token": ""},
        {"token": "bad\r\nheader"},
        {"token": "bad\x00header"},
        {"token": "\u00e9"},
    ],
)
def test_4dem_refuses_resource_call_without_valid_token(tmp_path, payload):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("4dem", "4Dem", secret="fixture-key")
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    with patch("connectors.generic._open", return_value=_response(payload)) as send:
        result = generic._get(ctx, {"path": "/addressbook/"})
    assert "no usable token" in result
    assert send.call_count == 1


def test_4dem_auth_error_never_echoes_credentials_or_sends_write(tmp_path):
    import urllib.error

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("4dem", "4Dem", secret="fixture-key")
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    error = urllib.error.HTTPError(
        "https://api.4dem.it/authenticate", 401, "denied", {}, io.BytesIO(b"fixture-key")
    )
    with patch("connectors.generic._open", side_effect=error) as send:
        result = generic._request(ctx, {"path": "/addressbook/", "method": "POST", "body": {}})
    assert result == "error: 4Dem authentication failed (HTTP 401)"
    assert send.call_count == 1


def test_4dem_refuses_foreign_path_before_authentication(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("4dem", "4Dem", secret="fixture-key")
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    with patch("connectors.generic._open") as send:
        assert "path must stay" in generic._get(ctx, {"path": "https://other.example/collect"})
    send.assert_not_called()


def test_4dem_malformed_auth_response_is_not_forwarded(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("4dem", "4Dem", secret="fixture-key")
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    with patch(
        "connectors.generic._open", return_value=io.BytesIO(b"not-json fixture-key")
    ) as send:
        result = generic._get(ctx, {"path": "/addressbook/"})
    assert result == "error: 4Dem authentication returned invalid JSON"
    assert send.call_count == 1


@pytest.mark.parametrize(
    "type_,path,url,body",
    [
        (
            "joggai",
            "/endpoint",
            "https://api.jogg.ai/v2/endpoint",
            {
                "url": "https://example.com/webhook",
                "status": "enabled",
                "events": ["generated_avatar_video_success"],
            },
        ),
        (
            "copicake",
            "/image/create",
            "https://api.copicake.com/v1/image/create",
            {
                "template_id": "fixture-template",
                "changes": [{"name": "title", "text": "Hello"}],
                "options": {"format": "png"},
            },
        ),
        (
            "emailoctopus",
            "/lists",
            "https://api.emailoctopus.com/lists",
            {"name": "Fixture list"},
        ),
    ],
)
def test_documented_json_write_paths(tmp_path, type_, path, url, body):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(type_, type_, secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({"ok": True})) as send:
        result = bound[type_ + "_request"][1]({"method": "POST", "path": path, "body": body})
    assert "HTTP 200" in result
    request = send.call_args.args[0]
    assert request.full_url == url
    assert json.loads(request.data) == body
