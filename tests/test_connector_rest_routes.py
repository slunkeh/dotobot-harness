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
    (
        "apexverify",
        {},
        "/account/credits",
        "https://api.apexverify.com/v1/account/credits",
        "X-api-key",
        "fixture-key",
    ),
    (
        "emaillistverify",
        {},
        "/credits",
        "https://api.emaillistverify.com/api/credits",
        "X-api-key",
        "fixture-key",
    ),
    (
        "clickfunnels",
        {"subdomain": "accounts"},
        "/teams",
        "https://accounts.myclickfunnels.com/api/v2/teams",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "clickfunnels",
        {"subdomain": "fixture"},
        "/workspaces/42/contacts",
        "https://fixture.myclickfunnels.com/api/v2/workspaces/42/contacts",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "adtraction",
        {},
        "/v2/partner/markets/",
        "https://api.adtraction.net/v2/partner/markets/",
        "X-token",
        "fixture-key",
    ),
    (
        "benchmark_email",
        {},
        "/Contact/",
        "https://clientapi.benchmarkemail.com/Contact/",
        "Authtoken",
        "fixture-key",
    ),
    (
        "botconversa",
        {},
        "/tags/",
        "https://backend.botconversa.com.br/api/v1/webhook/tags/",
        "Api-key",
        "fixture-key",
    ),
    (
        "easypromos",
        {},
        "/promotions",
        "https://api.easypromosapp.com/v2/promotions",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "klenty",
        {},
        "/user/fixture%40example.com/lists",
        "https://api.klenty.com/apis/v1/user/fixture%40example.com/lists",
        "X-api-key",
        "fixture-key",
    ),
    ("humanitix", {}, "/events", "https://api.humanitix.com/v1/events", "X-api-key", "fixture-key"),
    (
        "campaign_cleaner",
        {},
        "/get_credits",
        "https://api.campaigncleaner.com/v1/get_credits",
        "X-cc-api-key",
        "fixture-key",
    ),
    (
        "campayn",
        {},
        "/lists.json",
        "https://campayn.com/api/v1/lists.json",
        "Authorization",
        "TRUEREST apikey=fixture-key",
    ),
    (
        "gist",
        {},
        "/contacts",
        "https://api.getgist.com/contacts",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "cometly",
        {},
        "/events",
        "https://app.cometly.com/public-api/v1/events",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "ecologi",
        {},
        "/users/fixture/trees",
        "https://public.ecologi.com/users/fixture/trees",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "greenspark",
        {},
        "/projects",
        "https://api.getgreenspark.com/v1/projects",
        "X-api-key",
        "fixture-key",
    ),
    (
        "cleverreach",
        {},
        "/groups",
        "https://rest.cleverreach.com/v3/groups",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "constant_contact",
        {},
        "/contacts",
        "https://api.cc.email/v3/contacts",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "lawmatics",
        {},
        "/users/me",
        "https://api.lawmatics.com/v1/users/me",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "infusionsoft",
        {},
        "/contacts",
        "https://api.infusionsoft.com/crm/rest/v1/contacts",
        "X-keap-api-key",
        "fixture-key",
    ),
    (
        "google_calendar",
        {},
        "/users/me/calendarList",
        "https://www.googleapis.com/calendar/v3/users/me/calendarList",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "google_drive",
        {},
        "/files",
        "https://www.googleapis.com/drive/v3/files",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "google_sheets",
        {},
        "/spreadsheets/fixture",
        "https://sheets.googleapis.com/v4/spreadsheets/fixture",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "microsoft_excel",
        {},
        "/me/drive/items/fixture/workbook/worksheets",
        "https://graph.microsoft.com/v1.0/me/drive/items/fixture/workbook/worksheets",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "microsoft_outlook",
        {},
        "/me/messages",
        "https://graph.microsoft.com/v1.0/me/messages",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "microsoft_teams",
        {},
        "/me/joinedTeams",
        "https://graph.microsoft.com/v1.0/me/joinedTeams",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "cyberimpact",
        {},
        "/groups",
        "https://api.cyberimpact.com/groups",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "eventbrite",
        {},
        "/users/me/",
        "https://www.eventbriteapi.com/v3/users/me/",
        "Authorization",
        "Bearer fixture-key",
    ),
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
        assert request.get_header("User-agent").startswith("dotobot/")
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
    "type_,field",
    [
        ("activecampaign", "api_domain"),
        ("acelle_mail", "instance_domain"),
        ("clickfunnels", "subdomain"),
    ],
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
            "apexverify",
            "/unit",
            "https://api.apexverify.com/v1/unit",
            {
                "type": "email",
                "target_country": "GB",
                "unit": "fixture@example.com",
                "use_global_cache": False,
            },
        ),
        (
            "emaillistverify",
            "/emailJobs",
            "https://api.emaillistverify.com/api/emailJobs",
            {"email": "fixture@example.com", "quality": "standard"},
        ),
        (
            "adtraction",
            "/v3/partner/programs/",
            "https://api.adtraction.net/v3/partner/programs/",
            {"market": "SE"},
        ),
        (
            "benchmark_email",
            "/Contact",
            "https://clientapi.benchmarkemail.com/Contact",
            {"Data": {"Name": "Fixture", "Description": "Test list"}},
        ),
        (
            "botconversa",
            "/subscriber/",
            "https://backend.botconversa.com.br/api/v1/webhook/subscriber/",
            {
                "phone": "15555550100",
                "first_name": "Fixture",
                "last_name": "Test",
                "has_opt_in_whatsapp": True,
            },
        ),
        (
            "easypromos",
            "/participations/123/check_requirement/456",
            "https://api.easypromosapp.com/v2/participations/123/check_requirement/456",
            {"lt": "fixture-login-token"},
        ),
        (
            "klenty",
            "/user/fixture%40example.com/prospects",
            "https://api.klenty.com/apis/v1/user/fixture%40example.com/prospects",
            {"Email": "prospect@example.com", "FirstName": "Fixture"},
        ),
        (
            "campaign_cleaner",
            "/send_campaign",
            "https://api.campaigncleaner.com/v1/send_campaign",
            {
                "send_campaign": {
                    "campaign_html": "<html><body>Fixture</body></html>",
                    "campaign_name": "Fixture",
                }
            },
        ),
        (
            "campayn",
            "/lists/123/contacts.json",
            "https://campayn.com/api/v1/lists/123/contacts.json",
            {"email": "fixture@example.com", "first_name": "Fixture"},
        ),
        (
            "crowdpower",
            "/customers",
            "https://beacon.crowdpower.io/customers",
            {"user_id": "fixture", "custom_attributes": {"plan": "test"}},
        ),
        (
            "ecologi",
            "/impact/trees",
            "https://public.ecologi.com/impact/trees",
            {"number": 1, "test": True},
        ),
        (
            "google_calendar",
            "/calendars",
            "https://www.googleapis.com/calendar/v3/calendars",
            {"summary": "Fixture calendar"},
        ),
        (
            "google_drive",
            "/files",
            "https://www.googleapis.com/drive/v3/files",
            {"name": "Fixture folder", "mimeType": "application/vnd.google-apps.folder"},
        ),
        (
            "google_sheets",
            "/spreadsheets",
            "https://sheets.googleapis.com/v4/spreadsheets",
            {"properties": {"title": "Fixture spreadsheet"}},
        ),
        (
            "microsoft_excel",
            "/me/drive/items/fixture/workbook/worksheets/add",
            "https://graph.microsoft.com/v1.0/me/drive/items/fixture/workbook/worksheets/add",
            {"name": "Fixture"},
        ),
        (
            "microsoft_outlook",
            "/me/mailFolders",
            "https://graph.microsoft.com/v1.0/me/mailFolders",
            {"displayName": "Fixture folder", "isHidden": False},
        ),
        (
            "microsoft_teams",
            "/teams/fixture/channels",
            "https://graph.microsoft.com/v1.0/teams/fixture/channels",
            {"displayName": "Fixture channel", "membershipType": "standard"},
        ),
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


@pytest.mark.parametrize(
    "path,body,expected,content_type",
    [
        (
            "/list",
            {"name": "A + B & C", "locked": False},
            b"name=A+%2B+B+%26+C&locked=false",
            "application/x-www-form-urlencoded",
        ),
        (
            "/member",
            {"list_id": "fixture", "custom_fields": {"city": "Edinburgh"}, "tags": ["one", "two"]},
            b"list_id=fixture&custom_fields%5Bcity%5D=Edinburgh&tags%5B0%5D=one&tags%5B1%5D=two",
            "application/x-www-form-urlencoded",
        ),
        (
            "/list/fixture/members",
            {"actions": ["add"], "members": [{"email": "fixture@example.com"}]},
            None,
            "application/json",
        ),
        (
            "/list/fixture/members?mode=test",
            {"actions": ["add"], "members": []},
            None,
            "application/json",
        ),
    ],
)
def test_laposta_body_formats(tmp_path, path, body, expected, content_type):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("laposta", "Laposta", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({"ok": True})) as send:
        result = bound["laposta_request"][1]({"method": "POST", "path": path, "body": body})
    assert "HTTP 200" in result
    request = send.call_args.args[0]
    assert request.full_url == "https://api.laposta.org/v2" + path
    assert request.get_header("Authorization") == "Basic Zml4dHVyZS1rZXk6"
    assert request.get_header("Content-type") == content_type
    if expected is None:
        assert json.loads(request.data) == body
    else:
        assert request.data == expected


def test_laposta_form_secret_cannot_inject_fields(tmp_path):
    from urllib.parse import parse_qs

    from harness.redaction import register_secret

    secret = "fixture-secret&extra=bad+value"
    token = register_secret(secret, "LAPOSTA_FIXTURE")
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("laposta", "Laposta", secret=secret)
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    with patch("connectors.generic._open", return_value=_response({})) as send:
        result = generic._request(
            ctx, {"method": "POST", "path": "/list", "body": {"remarks": token}}
        )
    assert "HTTP 200" in result
    assert parse_qs(send.call_args.args[0].data.decode()) == {"remarks": [secret]}


def test_laposta_read_has_no_body(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("laposta", "Laposta", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["laposta_get"][1]({"path": "/list"})
    request = send.call_args.args[0]
    assert request.full_url == "https://api.laposta.org/v2/list"
    assert request.data is None
    assert request.get_header("Content-type") is None
    assert request.get_header("Authorization") == "Basic Zml4dHVyZS1rZXk6"


def test_sheets_repeated_range_query(tmp_path):
    from urllib.parse import parse_qs, urlparse

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("google_sheets", "Sheets", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({})) as send:
        result = bound["google_sheets_get"][1](
            {
                "path": "/spreadsheets/fixture",
                "query": {
                    "ranges": ["Sheet1!A1:B2", "Sheet2!C1:D2"],
                    "fields": "spreadsheetId,sheets",
                    "unused": None,
                },
            }
        )
    assert "HTTP 200" in result
    assert parse_qs(urlparse(send.call_args.args[0].full_url).query) == {
        "ranges": ["Sheet1!A1:B2", "Sheet2!C1:D2"],
        "fields": ["spreadsheetId,sheets"],
    }


@pytest.mark.parametrize("method", ["GET", "POST", "PUT"])
def test_enormail_request_contract(tmp_path, method):
    from urllib.parse import parse_qs

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("enormail", "Enormail", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    if method == "GET":
        name, args = "enormail_get", {"path": "/account.json"}
    else:
        name, args = (
            "enormail_request",
            {
                "method": method,
                "path": "/lists/fixture.json" if method == "PUT" else "/lists.json",
                "body": {"title": "Fixture & list"},
            },
        )
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound[name][1](args)
    request = send.call_args.args[0]
    assert request.full_url == "https://api.enormail.eu/api/1.0" + args["path"]
    assert request.get_header("Authorization") == "Basic Zml4dHVyZS1rZXk6"
    if method == "GET":
        assert request.data is None
    else:
        assert request.get_header("Content-type") == "application/x-www-form-urlencoded"
        assert parse_qs(request.data.decode()) == {"title": ["Fixture & list"]}


@pytest.mark.parametrize("type_,path", [("cometly", "/events"), ("benchmark_email", "/Contact/")])
def test_get_sends_required_content_type(tmp_path, type_, path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(type_, type_, secret="fixture-key")
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in generic._get(ctx, {"path": path})
    request = send.call_args.args[0]
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("Accept") == "application/json"
    assert request.data is None


def test_humanitix_check_in_without_body(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("humanitix", "Humanitix", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({"messages": []})) as send:
        result = bound["humanitix_request"][1](
            {"method": "POST", "path": "/events/fixture-event/tickets/fixture-ticket/check-in"}
        )
    assert "HTTP 200" in result
    request = send.call_args.args[0]
    assert (
        request.full_url
        == "https://api.humanitix.com/v1/events/fixture-event/tickets/fixture-ticket/check-in"
    )
    assert request.get_method() == "POST"
    assert request.data is None
    assert request.get_header("X-api-key") == "fixture-key"


def test_humanitix_required_page_query(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("humanitix", "Humanitix", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({"events": []})) as send:
        result = bound["humanitix_get"][1]({"path": "/events", "query": {"page": 1}})
    assert "HTTP 200" in result
    assert send.call_args.args[0].full_url == "https://api.humanitix.com/v1/events?page=1"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_giantcampaign_query_credential(tmp_path, method):
    from urllib.parse import parse_qs, urlsplit

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("giantcampaign", "GiantCampaign", secret="key&= +?")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({})) as send:
        name = "giantcampaign_get" if method == "GET" else "giantcampaign_request"
        result = bound[name][1]({"method": method, "path": "/lists?name=Fixture"})
    assert "HTTP 200" in result
    req = send.call_args.args[0]
    parts = urlsplit(req.full_url)
    assert parts.netloc == "acc.giantcampaign.com"
    assert parts.path == "/api/v1/lists"
    assert parse_qs(parts.query) == {"name": ["Fixture"], "api_token": ["key&= +?"]}
    assert req.get_header("Authorization") is None
    assert req.data is None


@pytest.mark.parametrize(
    "path,query,body",
    [
        ("/lists?api_token=override", None, None),
        ("/lists?%61pi_token=override", None, None),
        ("/lists?api_token[]=override", None, None),
        ("/lists", {"api_token": "override"}, None),
        ("/lists", None, {"api_token": "override"}),
        ("https://other.example/lists", None, None),
    ],
)
def test_giantcampaign_refuses_credential_override(tmp_path, path, query, body):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("giantcampaign", "GiantCampaign", secret="fixture-key")
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    with patch("connectors.generic._open") as send:
        assert generic._http(ctx, "POST", path, query=query, body=body).startswith("error:")
    send.assert_not_called()


def test_query_credential_is_not_exposed_by_connection_error(tmp_path):
    import urllib.error

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("giantcampaign", "GiantCampaign", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch(
        "connectors.generic._open",
        side_effect=urllib.error.URLError("URL includes api_token=fixture-key"),
    ):
        result = bound["giantcampaign_get"][1]({"path": "/lists"})
    assert result == "error: could not reach API"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_leaddyno_request_contract(tmp_path, method):
    from urllib.parse import parse_qs

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("leaddyno", "LeadDyno", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    args = {"path": "/visitors"}
    if method == "POST":
        args.update(method="POST", body={"url": "https://example.com/?afmc=fixture&x=1"})
    name = "leaddyno_get" if method == "GET" else "leaddyno_request"
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound[name][1](args)
    req = send.call_args.args[0]
    assert req.full_url == "https://api.leaddyno.com/v1/visitors"
    assert req.get_header("Key") == "fixture-key"
    assert req.get_method() == method
    if method == "POST":
        assert req.get_header("Content-type") == "application/x-www-form-urlencoded"
        assert parse_qs(req.data.decode()) == {"url": ["https://example.com/?afmc=fixture&x=1"]}
    else:
        assert req.data is None


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_acumbamail_parameter_authentication(tmp_path, method):
    from urllib.parse import parse_qs, urlsplit

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("acumbamail", "Acumbamail", secret="key&= +?")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    name = "acumbamail_get" if method == "GET" else "acumbamail_request"
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound[name][1]({"method": method, "path": "/getLists/"})
    req = send.call_args.args[0]
    parts = urlsplit(req.full_url)
    assert parts.netloc == "acumbamail.com"
    assert parts.path == "/api/1/getLists/"
    assert req.get_header("Authorization") is None
    if method == "GET":
        assert parse_qs(parts.query) == {"auth_token": ["key&= +?"]}
        assert req.data is None
    else:
        assert parts.query == ""
        assert parse_qs(req.data.decode()) == {"auth_token": ["key&= +?"]}
        assert req.get_header("Content-type") == "application/x-www-form-urlencoded"


def test_acumbamail_form_preserves_caller_body_and_refuses_override(tmp_path):
    from urllib.parse import parse_qs

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("acumbamail", "Acumbamail", secret="fixture-key")
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    body = {"merge_fields": {"EMAIL": "fixture@example.com"}}
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in generic._request(
            ctx, {"method": "POST", "path": "/addSubscriber/", "body": body}
        )
    assert body == {"merge_fields": {"EMAIL": "fixture@example.com"}}
    assert parse_qs(send.call_args.args[0].data.decode()) == {
        "merge_fields[EMAIL]": ["fixture@example.com"],
        "auth_token": ["fixture-key"],
    }
    with patch("connectors.generic._open") as send:
        assert "secret store" in generic._request(
            ctx, {"method": "POST", "path": "/getLists/", "body": {"auth_token": "override"}}
        )
    send.assert_not_called()


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_emailverify_parameter_authentication(tmp_path, method):
    from urllib.parse import parse_qs, urlsplit

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("emailverify_io", "EmailVerify", secret="key&= +?")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    body = {"title": "Fixture", "email_batch": [{"address": "fixture@example.com"}]}
    args = (
        {"path": "/v2/check-account-balance"}
        if method == "GET"
        else {
            "method": "POST",
            "path": "/v1/validate-batch",
            "body": body,
        }
    )
    name = "emailverify_io_get" if method == "GET" else "emailverify_io_request"
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound[name][1](args)
    req = send.call_args.args[0]
    parts = urlsplit(req.full_url)
    assert parts.netloc == "app.emailverify.io"
    assert parts.path == "/api" + args["path"]
    assert req.get_header("Authorization") is None
    if method == "GET":
        assert parse_qs(parts.query) == {"key": ["key&= +?"]}
        assert req.data is None
    else:
        assert parts.query == ""
        assert json.loads(req.data) == {**body, "key": "key&= +?"}
        assert "key" not in body
        assert req.get_header("Content-type") == "application/json"
        with patch("connectors.generic._open") as send:
            assert "secret store" in bound[name][1]({**args, "body": {"key": "override"}})
        send.assert_not_called()


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_dribbble_documented_encoding(tmp_path, method):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("dribbble", "Dribbble", secret="fixture-token")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    path = "/user" if method == "GET" else "/shots/123"
    args = {"path": path, "method": method}
    if method == "PUT":
        args["body"] = {"title": "Fixture"}
    name = "dribbble_get" if method == "GET" else "dribbble_request"
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound[name][1](args)
    req = send.call_args.args[0]
    assert req.full_url == "https://api.dribbble.com/v2" + path
    assert req.method == method
    assert req.get_header("Authorization") == "Bearer fixture-token"
    # The official v2 overview specifies JSON with this unusual media type.
    assert req.get_header("Content-type") == "application/x-www-form-urlencoded"
    if method == "PUT":
        assert json.loads(req.data) == {"title": "Fixture"}
    else:
        assert req.data is None
