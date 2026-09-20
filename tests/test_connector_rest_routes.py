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
        "google_ad_manager",
        {},
        "/networks",
        "https://admanager.googleapis.com/v1/networks",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "facebook",
        {},
        "/v20.0/me/accounts",
        "https://graph.facebook.com/v20.0/me/accounts",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "linkedin",
        {},
        "/v2/userinfo",
        "https://api.linkedin.com/v2/userinfo",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "dripcel",
        {},
        "/balance",
        "https://api.dripcel.com/balance",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "asters",
        {},
        "/workspaces",
        "https://api.asters.ai/api/external/v1.0/workspaces",
        "X-api-key",
        "fixture-key",
    ),
    (
        "chatrace",
        {},
        "/accounts/flows",
        "https://api.chatrace.com/accounts/flows",
        "X-access-token",
        "fixture-key",
    ),
    (
        "acymailing",
        {"api_domain": "fixture.example.com"},
        "/index.php?page=acymailing_front&option=com_acym&ctrl=api&task=getUsers",
        "https://fixture.example.com/index.php?page=acymailing_front&option=com_acym&ctrl=api&task=getUsers",
        "Api-key",
        "fixture-key",
    ),
    (
        "emailchef",
        {},
        "/lists",
        "https://app.emailchef.com/apps/api/v1/lists",
        "Authkey",
        "fixture-key",
    ),
    (
        "catch_all_verifier",
        {},
        "/credits",
        "https://app.catchallverifier.com/api/v1/credits",
        "Authorization",
        "fixture-key",
    ),
    (
        "add_to_calendar_pro",
        {},
        "/event/all",
        "https://api.add-to-calendar-pro.com/v1/event/all",
        "Authorization",
        "fixture-key",
    ),
    (
        "aimtell",
        {},
        "/sites/",
        "https://api.aimtell.com/prod/sites/",
        "X-authorization-api-key",
        "fixture-key",
    ),
    (
        "encharge",
        {},
        "/people/all",
        "https://api.encharge.io/v1/people/all",
        "X-encharge-token",
        "fixture-key",
    ),
    (
        "freshmarketer",
        {"subdomain": "fixture"},
        "/contacts",
        "https://fixture.freshmarketer.com/mas/api/v1/contacts",
        "Fm-token",
        "fixture-key",
    ),
    (
        "appsflyer",
        {},
        "/api/mng/apps",
        "https://hq1.appsflyer.com/api/mng/apps",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "demandbase",
        {},
        "/data/export/v1/jobs",
        "https://uapi.demandbase.com/data/export/v1/jobs",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "google_analytics",
        {},
        "/v1beta/properties/1234/metadata",
        "https://analyticsdata.googleapis.com/v1beta/properties/1234/metadata",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "enginemailer",
        {},
        "/campaign/emcampaign/GetCategoryList",
        "https://api.enginemailer.com/restapi/campaign/emcampaign/GetCategoryList",
        "Apikey",
        "fixture-key",
    ),
    (
        "adrapid",
        {},
        "/me",
        "https://api.adrapid.com/v1/api/me",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "contentdrips",
        {},
        "/queue/stats",
        "https://generate.contentdrips.com/queue/stats",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "callpage",
        {},
        "/v3/external/calls/history",
        "https://core.callpage.io/api/v3/external/calls/history",
        "Authorization",
        "fixture-key",
    ),
    (
        "funnelcockpit",
        {},
        "/me",
        "https://api.funnelcockpit.com/me",
        "Authorization",
        "fixture-key",
    ),
    (
        "kickofflabs",
        {},
        "/campaigns",
        "https://api.kickofflabs.com/v2/campaigns",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "flippingbook",
        {},
        "/fbonline/publication",
        "https://api-tc.is.flippingbook.com/api/v1/fbonline/publication",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "lagrowthmachine",
        {},
        "/members",
        "https://apiv2.lagrowthmachine.com/flow/members",
        "Authorization",
        "Bearer fixture-key",
    ),
    (
        "curated",
        {},
        "/publications",
        "https://api.curated.co/api/v3/publications",
        "Authorization",
        'Token token="fixture-key"',
    ),
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
        query_suffix = ("&limit=2" if "?" in url else "?limit=2") if method == "GET" else ""
        assert request.full_url == url + query_suffix
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
            "appsflyer",
            "/api/p360-click-signing/v2.0/test",
            "https://hq1.appsflyer.com/api/p360-click-signing/v2.0/test",
            {"url": "https://example.com/fixture", "signature_version": "v2"},
        ),
        (
            "demandbase",
            "/data/intent/v1/companies/intent/query",
            "https://uapi.demandbase.com/data/intent/v1/companies/intent/query",
            {
                "companyIds": ["726263"],
                "startDate": "2026-07-01",
                "endDate": "2026-07-22",
                "pageSize": 25,
            },
        ),
        (
            "google_analytics",
            "/v1beta/properties/1234:runReport",
            "https://analyticsdata.googleapis.com/v1beta/properties/1234:runReport",
            {
                "dimensions": [{"name": "city"}],
                "metrics": [{"name": "activeUsers"}],
                "dateRanges": [{"startDate": "7daysAgo", "endDate": "yesterday"}],
                "limit": "10",
            },
        ),
        (
            "enginemailer",
            "/Campaign/EMCampaign/CreateCampaign",
            "https://api.enginemailer.com/restapi/Campaign/EMCampaign/CreateCampaign",
            {
                "CampaignName": "Fixture campaign",
                "SenderName": "Fixture",
                "SenderEmail": "sender@example.com",
                "Subject": "Fixture",
                "Content": "Fixture content",
            },
        ),
        (
            "adrapid",
            "/banners",
            "https://api.adrapid.com/v1/api/banners",
            {"templateId": "fixture", "modes": {"png": True}},
        ),
        (
            "contentdrips",
            "/render",
            "https://generate.contentdrips.com/render",
            {
                "template_id": "fixture",
                "output": "png",
                "content_update": [{"type": "textbox", "label": "title", "value": "Fixture"}],
            },
        ),
        (
            "funnelcockpit",
            "/email/tag",
            "https://api.funnelcockpit.com/email/tag",
            {"contactId": "fixture-contact", "tagId": "fixture-tag"},
        ),
        (
            "kickofflabs",
            "/123/",
            "https://api.kickofflabs.com/v2/123/",
            {"email": "fixture@example.com", "first_name": "Fixture"},
        ),
        (
            "flippingbook",
            "/fbonline/publication",
            "https://api-tc.is.flippingbook.com/api/v1/fbonline/publication",
            {"name": "Fixture", "url": "https://example.com/fixture.pdf"},
        ),
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


def test_curated_quotes_stored_token_and_creates_draft(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    secret = 'fixture"\\token'
    record = Connectors(paths).add("curated", "Curated", secret=secret)
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["curated_request"][1](
            {"method": "POST", "path": "/publications/123/issues/"}
        )
    req = send.call_args.args[0]
    assert req.full_url == "https://api.curated.co/api/v3/publications/123/issues/"
    assert req.get_header("Authorization") == "Token token=" + json.dumps(secret)
    assert req.method == "POST"
    assert req.data is None


@pytest.mark.parametrize("endpoint", ["sendletter", "sendpostcard"])
def test_docupost_post_query_authentication(tmp_path, endpoint):
    from urllib.parse import parse_qs, urlsplit

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("docupost", "DocuPost", secret="key&= +?")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["docupost_request"][1](
            {"method": "POST", "path": "/" + endpoint + "?to_name=Fixture%20Test"}
        )
    req = send.call_args.args[0]
    parts = urlsplit(req.full_url)
    assert parts.netloc == "app.docupost.com"
    assert parts.path == "/api/1.1/wf/" + endpoint
    assert parse_qs(parts.query) == {"to_name": ["Fixture Test"], "api_token": ["key&= +?"]}
    assert req.method == "POST"
    assert req.data is None
    assert req.get_header("Authorization") is None


@pytest.mark.parametrize(
    "path,body,is_form",
    [
        ("/audiences/create", {"name": "Fixture"}, False),
        ("/leads", {"firstname": "Fixture", "proEmail": "fixture@example.com"}, False),
        ("/campaigns/123/status", {"status": "PAUSED"}, True),
        ("/campaigns/123/settings", {"name": "Fixture"}, True),
        ("/audiences", {"audience": "Fixture", "linkedinUrl": "https://example.com"}, True),
        ("/leads/status", {"status": "PAUSED"}, True),
    ],
)
def test_lagrowthmachine_body_formats(tmp_path, path, body, is_form):
    from urllib.parse import parse_qs

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("lagrowthmachine", "LGM", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["lagrowthmachine_request"][1](
            {"method": "POST", "path": path, "body": body}
        )
    req = send.call_args.args[0]
    assert req.full_url == "https://apiv2.lagrowthmachine.com/flow" + path
    assert req.get_header("Authorization") == "Bearer fixture-key"
    if is_form:
        assert parse_qs(req.data.decode()) == {k: [v] for k, v in body.items()}
        assert req.get_header("Content-type") == "application/x-www-form-urlencoded"
    else:
        assert json.loads(req.data) == body
        assert req.get_header("Content-type") == "application/json"


@pytest.mark.parametrize("client_id", ["12345", "", "12\r\nInjected: yes", "abc"])
def test_hypeauditor_client_id_and_token(tmp_path, client_id):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "hypeauditor", "HypeAuditor", config={"client_id": client_id}, secret="fixture-key"
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({})) as send:
        result = bound["hypeauditor_get"][1]({"path": "/api/v1/media-plan/plans"})
    if client_id != "12345":
        assert "client_id" in result
        send.assert_not_called()
        return
    assert "HTTP 200" in result
    req = send.call_args.args[0]
    assert req.full_url == "https://hypeauditor.com/api/v1/media-plan/plans"
    assert req.get_header("X-auth-id") == "12345"
    assert req.get_header("X-auth-hash") == "fixture-key"
    assert req.get_header("Authorization") is None


def test_hypeauditor_create_plan(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "hypeauditor", "HypeAuditor", config={"client_id": "12345"}, secret="fixture-key"
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["hypeauditor_request"][1](
            {"method": "POST", "path": "/api/v1/media-plan/plans", "body": {"title": "Fixture"}}
        )
    req = send.call_args.args[0]
    assert req.full_url == "https://hypeauditor.com/api/v1/media-plan/plans"
    assert req.get_header("X-auth-id") == "12345"
    assert req.get_header("X-auth-hash") == "fixture-key"
    assert json.loads(req.data) == {"title": "Fixture"}


def test_jvzoo_versioned_read_authentication(tmp_path):
    import base64

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("jvzoo", "JVZoo", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["jvzoo_get"][1](
            {
                "path": "/v3.0/transactions",
                "query": {"start_date": "2025-01-01", "end_date": "2025-01-31"},
            }
        )
    req = send.call_args.args[0]
    assert (
        req.full_url
        == "https://api.jvzoo.com/v3.0/transactions?start_date=2025-01-01&end_date=2025-01-31"
    )
    assert req.get_header("Authorization") == "Basic " + base64.b64encode(b"fixture-key:x").decode()
    assert req.data is None


@pytest.mark.parametrize(
    "domain,username",
    [
        ("forum.example.com", "fixture_user"),
        ("", "fixture_user"),
        ("https://forum.example.com", "fixture_user"),
        ("forum.example.com/evil", "fixture_user"),
        ("forum.example.com@evil.test", "fixture_user"),
        ("forum.example.com", ""),
        ("forum.example.com", "fixture\r\nInjected: yes"),
    ],
)
def test_discourse_host_and_identity(tmp_path, domain, username):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "discourse",
        "Discourse",
        config={"api_domain": domain, "api_username": username},
        secret="fixture-key",
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({})) as send:
        result = bound["discourse_get"][1]({"path": "/categories.json"})
    if (domain, username) != ("forum.example.com", "fixture_user"):
        assert result.startswith("error:")
        send.assert_not_called()
        return
    assert "HTTP 200" in result
    req = send.call_args.args[0]
    assert req.full_url == "https://forum.example.com/categories.json"
    assert req.get_header("Api-key") == "fixture-key"
    assert req.get_header("Api-username") == "fixture_user"
    assert req.get_header("Authorization") is None


def test_discourse_json_topic_request(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "discourse",
        "Discourse",
        config={"api_domain": "forum.example.com", "api_username": "fixture_user"},
        secret="fixture-key",
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    body = {"title": "Fixture topic", "raw": "Fixture body"}
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["discourse_request"][1](
            {"method": "POST", "path": "/posts.json", "body": body}
        )
    req = send.call_args.args[0]
    assert req.full_url == "https://forum.example.com/posts.json"
    assert req.get_header("Api-username") == "fixture_user"
    assert json.loads(req.data) == body


@pytest.mark.parametrize("command", ["GetRemainingCredits", "ListProjects"])
def test_brandmentions_command_query(tmp_path, command):
    from urllib.parse import parse_qs, urlsplit

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("brandmentions", "BrandMentions", secret="key&= +?")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["brandmentions_get"][1](
            {"path": "/command.php", "query": {"command": command}}
        )
    req = send.call_args.args[0]
    parts = urlsplit(req.full_url)
    assert parts.netloc == "api.brandmentions.com"
    assert parts.path == "/command.php"
    assert parse_qs(parts.query) == {"command": [command], "api_key": ["key&= +?"]}
    assert req.get_header("Authorization") is None
    assert req.data is None


def test_callpage_v1_field_update(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("callpage", "CallPage", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["callpage_request"][1](
            {"method": "PATCH", "path": "/v1/external/calls/13/fields/1421", "body": {"value": 12}}
        )
    req = send.call_args.args[0]
    assert req.full_url == "https://core.callpage.io/api/v1/external/calls/13/fields/1421"
    assert req.method == "PATCH"
    assert req.get_header("Authorization") == "fixture-key"
    assert json.loads(req.data) == {"value": 12}


@pytest.mark.parametrize(
    "method,path", [("GET", "/now/v3/overview"), ("POST", "/tracking/v1/event")]
)
def test_gosquared_query_key_and_project(tmp_path, method, path):
    from urllib.parse import parse_qs, urlsplit

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("gosquared", "GoSquared", secret="key&= +?")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    body = {"event": {"name": "Fixture", "data": {"test": True}}, "visitor_id": "fixture"}
    args = {"path": path, "query": {"site_token": "GSN-fixture"}}
    if method == "POST":
        args = {"method": method, "path": path + "?site_token=GSN-fixture", "body": body}
    with patch("connectors.generic._open", return_value=_response({})) as send:
        result = bound["gosquared_get" if method == "GET" else "gosquared_request"][1](args)
    assert "HTTP 200" in result
    req = send.call_args.args[0]
    parts = urlsplit(req.full_url)
    assert parts.netloc == "api.gosquared.com"
    assert parts.path == path
    assert parse_qs(parts.query) == {"api_key": ["key&= +?"], "site_token": ["GSN-fixture"]}
    assert req.get_header("Authorization") is None
    assert req.method == method
    if method == "POST":
        assert req.get_header("Content-type") == "application/json"
        assert json.loads(req.data) == body
    else:
        assert req.data is None


@pytest.mark.parametrize(
    "path,body", [("/webinars", {}), ("/webinar", {"webinar_id": 6, "timezone": "GMT+4:30"})]
)
def test_everwebinar_form_read(tmp_path, path, body):
    from urllib.parse import parse_qs

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("everwebinar", "EverWebinar", secret="key&= +?")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    original = dict(body)
    with patch("connectors.generic._open", return_value=_response({"status": "success"})) as send:
        result = bound["everwebinar_request"][1]({"method": "POST", "path": path, "body": body})
    assert "HTTP 200" in result
    req = send.call_args.args[0]
    assert req.full_url == "https://api.webinarjam.com/everwebinar" + path
    assert req.method == "POST"
    assert req.get_header("Authorization") is None
    assert req.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert parse_qs(req.data.decode()) == {
        "api_key": ["key&= +?"],
        **{k: [str(v)] for k, v in body.items()},
    }
    assert body == original


def test_airship_version_header(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("airship", "Airship", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({"ok": True})) as send:
        result = bound["airship_get"][1]({"path": "/api/channels", "query": {"limit": 1}})
    assert "HTTP 200" in result
    req = send.call_args.args[0]
    assert req.full_url == "https://go.urbanairship.com/api/channels?limit=1"
    assert req.get_header("Accept") == "application/vnd.urbanairship+json; version=3"
    assert req.get_header("Authorization") == "Bearer fixture-key"


@pytest.mark.parametrize(
    "region,mode,host",
    [
        ("us", "bearer", "go.urbanairship.com"),
        ("eu", "bearer", "go.airship.eu"),
        ("us", "basic", "go.urbanairship.com"),
        ("eu", "basic", "go.airship.eu"),
        ("us", "oauth", "api.asnapius.com"),
        ("eu", "oauth", "api.asnapieu.com"),
    ],
)
def test_airship_region_auth(tmp_path, region, mode, host):
    import base64

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "airship", "Airship", config={"region": region, "auth_mode": mode}, secret="app:key"
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({"ok": True})) as send:
        assert "HTTP 200" in bound["airship_get"][1]({"path": "/api/channels"})
    req = send.call_args.args[0]
    assert req.full_url == "https://" + host + "/api/channels"
    expected = (
        "Basic " + base64.b64encode(b"app:key").decode() if mode == "basic" else "Bearer app:key"
    )
    assert req.get_header("Authorization") == expected
    assert req.get_header("Accept") == "application/vnd.urbanairship+json; version=3"


@pytest.mark.parametrize(
    "config", [{"region": "other"}, {"region": "https://evil.example"}, {"auth_mode": "unknown"}]
)
def test_airship_invalid_config_blocks_transport(tmp_path, config):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("airship", "Airship", config=config, secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open") as send:
        assert "error:" in bound["airship_get"][1]({"path": "/api/channels"})
    send.assert_not_called()


def test_adroll_multipart_request(tmp_path):
    from email import policy
    from email.parser import BytesParser

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("adroll", "AdRoll", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    body = {
        "name": "Fixture café",
        "organization": "fixture-org",
        "country_code": "us",
        "url": "https://example.com",
    }
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["adroll_request"][1](
            {
                "method": "POST",
                "path": "/api/v1/advertisable/create?apikey=fixture-client",
                "body": body,
            }
        )
    req = send.call_args.args[0]
    assert (
        req.full_url
        == "https://services.adroll.com/api/v1/advertisable/create?apikey=fixture-client"
    )
    assert req.get_header("Authorization") == "Token fixture-key"
    msg = BytesParser(policy=policy.default).parsebytes(
        ("Content-Type: " + req.get_header("Content-type") + "\r\n\r\n").encode() + req.data
    )
    assert msg.get_content_type() == "multipart/form-data"
    assert {
        part.get_param("name", header="content-disposition"): part.get_payload(decode=True).decode()
        for part in msg.iter_parts()
    } == body


@pytest.mark.parametrize("body", [{"bad\r\nname": "x"}, {"file": {"path": "/tmp/example"}}])
def test_adroll_rejects_unsupported_multipart(tmp_path, body):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("adroll", "AdRoll", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open") as send:
        assert "error: multipart" in bound["adroll_request"][1](
            {"method": "POST", "path": "/api/v1/ad/create?apikey=fixture", "body": body}
        )
    send.assert_not_called()


@pytest.mark.parametrize("path", ["/me/videos/list", "/me/video/988"])
def test_hippo_video_query_auth(tmp_path, path):
    from urllib.parse import parse_qs, urlsplit

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("hippo_video", "Hippo Video", secret="key&= +?")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({"code": 200})) as send:
        assert "HTTP 200" in bound["hippo_video_get"][1](
            {"path": path, "query": {"email": "fixture@example.com", "page": 1}}
        )
    req = send.call_args.args[0]
    parts = urlsplit(req.full_url)
    assert parts.netloc == "www.hippovideo.io"
    assert parts.path == "/api/v1" + path
    assert parse_qs(parts.query) == {
        "email": ["fixture@example.com"],
        "page": ["1"],
        "authentication_token": ["key&= +?"],
    }
    assert req.get_header("Authorization") is None


def test_hippo_video_personalization_json(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("hippo_video", "Hippo Video", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    body = {
        "email": "fixture@example.com",
        "video_id": 988,
        "merge_fields": [{"first_name": "Fixture"}],
    }
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["hippo_video_request"][1](
            {"method": "POST", "path": "/me/video/personalize", "body": body}
        )
    req = send.call_args.args[0]
    assert req.full_url == "https://www.hippovideo.io/api/v1/me/video/personalize"
    assert json.loads(req.data) == {**body, "authentication_token": "fixture-key"}
    assert "authentication_token" not in body
    assert req.get_header("Content-type") == "application/json"


@pytest.mark.parametrize("method", ["GET", "POST", "PUT"])
def test_feedblitz_xml_transport(tmp_path, method):
    from urllib.parse import parse_qs, urlsplit

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("feedblitz", "FeedBlitz", secret="key&= +?")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    xml = "<fixture><name>Café &amp; test</name></fixture>"
    args = {"path": "/user"}
    if method != "GET":
        args.update(method=method, body={"xml": xml})
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["feedblitz_get" if method == "GET" else "feedblitz_request"][1](
            args
        )
    req = send.call_args.args[0]
    parts = urlsplit(req.full_url)
    assert parts.netloc == "app.feedblitz.com"
    assert parts.path == "/f.api/user"
    assert parse_qs(parts.query) == {"key": ["key&= +?"]}
    assert req.get_header("Accept") == "application/xml"
    assert req.get_header("User-agent")
    assert req.method == method
    if method != "GET":
        assert req.data == xml.encode()
        assert req.get_header("Content-type") == "application/xml; charset=utf-8"


@pytest.mark.parametrize("body", [{"xml": {}}, {"xml": "<x/>", "other": 1}, {}])
def test_feedblitz_rejects_wrong_body(tmp_path, body):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("feedblitz", "FeedBlitz", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open") as send:
        assert "error: XML body" in bound["feedblitz_request"][1](
            {"path": "/user", "method": "POST", "body": body}
        )
    send.assert_not_called()


def test_freshmarketer_subscription_type_json(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "freshmarketer", "Freshmarketer", config={"subdomain": "fixture"}, secret="fixture-key"
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    body = {"name": "Fixture type", "description": "Fixture description"}
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["freshmarketer_request"][1](
            {"method": "POST", "path": "/email-types", "body": body}
        )
    req = send.call_args.args[0]
    assert req.full_url == "https://fixture.freshmarketer.com/mas/api/v1/email-types"
    assert req.get_header("Fm-token") == "fixture-key"
    assert json.loads(req.data) == body


@pytest.mark.parametrize("subdomain", ["", "https://example.com", "x/evil", "x?evil", "x@evil"])
def test_freshmarketer_invalid_host(tmp_path, subdomain):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "freshmarketer", "Freshmarketer", config={"subdomain": subdomain}, secret="fixture-key"
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open") as send:
        assert "error:" in bound["freshmarketer_get"][1]({"path": "/contacts"})
    send.assert_not_called()


def test_encharge_tag_json(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("encharge", "Encharge", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    body = {"tag": "fixture", "email": "fixture@example.com"}
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["encharge_request"][1](
            {"method": "POST", "path": "/tags", "body": body}
        )
    req = send.call_args.args[0]
    assert req.full_url == "https://api.encharge.io/v1/tags"
    assert req.get_header("X-encharge-token") == "fixture-key"
    assert json.loads(req.data) == body


def test_aimtell_website_update_json(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("aimtell", "Aimtell", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    body = {"name": "Fixture site", "icon": "https://example.com/icon.png"}
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["aimtell_request"][1](
            {"method": "PUT", "path": "/site/123", "body": body}
        )
    req = send.call_args.args[0]
    assert req.full_url == "https://api.aimtell.com/prod/site/123"
    assert req.get_method() == "PUT"
    assert req.get_header("X-authorization-api-key") == "fixture-key"
    assert json.loads(req.data) == body


def test_add_to_calendar_pro_event_json(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("add_to_calendar_pro", "Calendar", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    body = {
        "new_event_group_name": "Fixture",
        "dates": [{"name": "Fixture date", "startDate": "2027-01-20", "timeZone": "Europe/London"}],
    }
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["add_to_calendar_pro_request"][1](
            {"method": "POST", "path": "/event", "body": body}
        )
    req = send.call_args.args[0]
    assert req.full_url == "https://api.add-to-calendar-pro.com/v1/event"
    assert req.get_header("Authorization") == "fixture-key"
    assert json.loads(req.data) == body


def test_catch_all_verifier_single_json(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("catch_all_verifier", "Verifier", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    body = {"email": "fixture@example.com"}
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["catch_all_verifier_request"][1](
            {"method": "POST", "path": "/verify/single", "body": body}
        )
    req = send.call_args.args[0]
    assert req.full_url == "https://app.catchallverifier.com/api/v1/verify/single"
    assert req.get_header("Authorization") == "fixture-key"
    assert json.loads(req.data) == body


def test_emailchef_list_json(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("emailchef", "Emailchef", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    body = {"instance_in": {"list_name": "Fixture", "list_description": "Test list"}}
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["emailchef_request"][1](
            {"method": "POST", "path": "/lists", "body": body}
        )
    req = send.call_args.args[0]
    assert req.full_url == "https://app.emailchef.com/apps/api/v1/lists"
    assert req.get_header("Authkey") == "fixture-key"
    assert json.loads(req.data) == body


def test_acymailing_subscription_json(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "acymailing",
        "AcyMailing",
        config={"api_domain": "fixture.example.com"},
        secret="fixture-key",
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    body = {
        "emails": ["fixture@example.com"],
        "listIds": ["1"],
        "sendWelcomeEmail": False,
        "trigger": False,
    }
    path = "/blog/index.php?page=acymailing_front&option=com_acym&ctrl=api&task=subscribeUsers"
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["acymailing_request"][1](
            {"method": "POST", "path": path, "body": body}
        )
    req = send.call_args.args[0]
    assert req.full_url == "https://fixture.example.com" + path
    assert req.get_header("Api-key") == "fixture-key"
    assert json.loads(req.data) == body


@pytest.mark.parametrize("domain", ["", "https://example.com", "x/evil", "x?evil", "x@evil"])
def test_acymailing_invalid_host(tmp_path, domain):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "acymailing", "AcyMailing", config={"api_domain": domain}, secret="fixture-key"
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open") as send:
        assert "error:" in bound["acymailing_get"][1]({"path": "/index.php"})
    send.assert_not_called()


@pytest.mark.parametrize(
    "path, body",
    [
        ("/subscribers_list/lists", {}),
        (
            "/subscriber/addMultiple",
            {
                "list": "fixture-list",
                "subscribers": [{"EMAIL": "fixture@example.com", "FNAME": "Fixture"}],
            },
        ),
    ],
)
def test_easysendy_json_api_key(tmp_path, path, body):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("easysendy", "EasySendy", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["easysendy_request"][1](
            {"method": "POST", "path": path, "body": body}
        )
    req = send.call_args.args[0]
    assert req.full_url == "https://api.easysendy.com/rest" + path
    assert req.get_header("Authorization") is None
    assert req.get_header("Content-type") == "application/json"
    assert json.loads(req.data) == {**body, "api_key": "fixture-key"}
    assert "api_key" not in body


@pytest.mark.parametrize(
    "path, body, expected",
    [
        ("/accounts/tags", {"name": "Fixture tag"}, b"name=Fixture+tag"),
        ("/contacts/123/send/456", {"value": "fixture"}, b"value=fixture"),
        ("/contacts/123/send/text", {"text": "Fixture"}, None),
        ("/contacts", {"phone": "+441234567890", "first_name": "Fixture"}, None),
    ],
)
def test_chatrace_body_encoding(tmp_path, path, body, expected):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("chatrace", "Chatrace", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["chatrace_request"][1](
            {"method": "POST", "path": path, "body": body}
        )
    req = send.call_args.args[0]
    assert req.full_url == "https://api.chatrace.com" + path
    assert req.get_header("X-access-token") == "fixture-key"
    if expected is None:
        assert req.get_header("Content-type") == "application/json"
        assert json.loads(req.data) == body
    else:
        assert req.get_header("Content-type") == "application/x-www-form-urlencoded"
        assert req.data == expected


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("GET", "/directory", None),
        ("POST", "/directory/123/users", {"email": "fixture@example.com", "first_name": "Fixture"}),
    ],
)
def test_coupontools_header_pair(tmp_path, method, path, body):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "coupontools", "Coupontools", secret='["fixture-key", "fixture-secret"]'
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    name = "coupontools_get" if method == "GET" else "coupontools_request"
    args = {"path": path} if method == "GET" else {"method": method, "path": path, "body": body}
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound[name][1](args)
    req = send.call_args.args[0]
    assert req.full_url == "https://api.coupontools.com/v4" + path
    assert req.get_header("X-api-key") == "fixture-key"
    assert req.get_header("X-api-secret") == "fixture-secret"
    assert req.get_header("Authorization") is None
    if body is not None:
        assert json.loads(req.data) == body


@pytest.mark.parametrize(
    "secret", ["plain", "{}", '["one"]', '["key", ""]', '["key", 3]', '["key", "bad\\r\\nheader"]']
)
def test_coupontools_invalid_pair(tmp_path, secret):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("coupontools", "Coupontools", secret=secret)
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open") as send:
        assert "error: store the credential" in bound["coupontools_get"][1]({"path": "/directory"})
    send.assert_not_called()


@pytest.mark.parametrize(
    "domain",
    [
        "api.clevertap.com",
        "in1.api.clevertap.com",
        "sg1.api.clevertap.com",
        "us1.api.clevertap.com",
        "aps3.api.clevertap.com",
        "mec1.api.clevertap.com",
    ],
)
def test_clevertap_region_read(tmp_path, domain):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "clevertap",
        "CleverTap",
        config={"api_domain": domain},
        secret='["fixture-id", "fixture-passcode"]',
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["clevertap_get"][1](
            {"path": "/1/profile.json", "query": {"identity": "fixture"}}
        )
    req = send.call_args.args[0]
    assert req.full_url == "https://" + domain + "/1/profile.json?identity=fixture"
    assert req.get_header("X-clevertap-account-id") == "fixture-id"
    assert req.get_header("X-clevertap-passcode") == "fixture-passcode"
    assert req.get_header("Content-type") is None


def test_clevertap_profile_dry_run_json(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "clevertap",
        "CleverTap",
        config={"api_domain": "in1.api.clevertap.com"},
        secret='["fixture-id", "fixture-passcode"]',
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    body = {"d": [{"identity": "fixture", "type": "profile", "profileData": {"Name": "Fixture"}}]}
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["clevertap_request"][1](
            {"method": "POST", "path": "/1/upload?dryRun=1", "body": body}
        )
    req = send.call_args.args[0]
    assert req.full_url == "https://in1.api.clevertap.com/1/upload?dryRun=1"
    assert req.get_header("X-clevertap-account-id") == "fixture-id"
    assert req.get_header("X-clevertap-passcode") == "fixture-passcode"
    assert json.loads(req.data) == body


@pytest.mark.parametrize(
    "path, body",
    [
        ("/balance/currentbalance", {"organizationIds": ["00000000-0000-0000-0000-000000000001"]}),
        (
            "/shorturl",
            {
                "organizationId": "00000000-0000-0000-0000-000000000001",
                "items": [{"url": "https://example.com"}],
            },
        ),
    ],
)
def test_arpoone_json_contract(tmp_path, path, body):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("arpoone", "Arpoone", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["arpoone_request"][1](
            {"method": "POST", "path": path, "body": body}
        )
    req = send.call_args.args[0]
    assert req.full_url == "https://api.arpoone.com/v1.2" + path
    assert req.get_header("Authorization") == "Bearer fixture-key"
    assert json.loads(req.data) == body


def test_asters_posts_json(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("asters", "Asters", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    body = {
        "socialAccountId": "fixture",
        "page": 1,
        "filters": {"date": {"from": "01-01-2026", "to": "01-31-2026"}},
    }
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound["asters_request"][1](
            {"method": "POST", "path": "/data/posts", "body": body}
        )
    req = send.call_args.args[0]
    assert req.full_url == "https://api.asters.ai/api/external/v1.0/data/posts"
    assert req.get_header("X-api-key") == "fixture-key"
    assert json.loads(req.data) == body
    with patch("connectors.generic._open", return_value=_response({})) as send:
        bound["asters_get"][1]({"path": "/workspaces"})
    assert send.call_args.args[0].get_header("Content-type") == "application/json"


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("GET", "/users?type=all", None),
        ("POST", "/users/query?type=all", {"name": "Fixture"}),
    ],
)
def test_instabot_master_key_pair(tmp_path, method, path, body):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "instabot", "Instabot", secret='["fixture-key", "fixture-master"]'
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    name = "instabot_get" if method == "GET" else "instabot_request"
    args = {"path": path} if method == "GET" else {"method": method, "path": path, "body": body}
    with patch("connectors.generic._open", return_value=_response({})) as send:
        assert "HTTP 200" in bound[name][1](args)
    req = send.call_args.args[0]
    assert req.full_url == "https://api.instabot.io/v1" + path
    assert req.get_header("X-instabot-api-key") == "fixture-key"
    assert req.get_header("Authorization") == "X-Instabot-Master-Api-Key fixture-master"
    if body is not None:
        assert json.loads(req.data) == body


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
def test_dux_signature_and_envelope(tmp_path, method):
    import base64
    import hashlib
    import hmac

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "dux_soup", "Dux", config={"userid": "123"}, secret="fixture-key"
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    path = "/xapi/remote/control/123/conversations/recent/batch"
    body = {"profileids": [], "direction": "all"}
    with (
        patch("time.time", return_value=1700000000),
        patch("connectors.generic._open", return_value=_response({})) as send,
    ):
        if method == "GET":
            result = bound["dux_soup_get"][1]({"path": path, "query": {"label": "a+b"}})
        else:
            result = bound["dux_soup_request"][1]({"method": method, "path": path, "body": body})
    assert "HTTP 200" in result
    req = send.call_args.args[0]
    url = "https://app.dux-soup.com" + path
    if method == "GET":
        url += "?label=a%2Bb"
        signed = url.encode()
        assert req.data is None
    else:
        expected = {**body, "targeturl": url, "timestamp": 1700000000000, "userid": "123"}
        assert json.loads(req.data) == expected
        signed = json.dumps(expected).encode()
        assert req.data == signed
    assert req.full_url == url
    assert req.get_header("Authorization") is None
    assert (
        req.get_header("X-dux-signature")
        == base64.b64encode(hmac.new(b"fixture-key", signed, hashlib.sha1).digest()).decode()
    )
    assert body == {"profileids": [], "direction": "all"}


@pytest.mark.parametrize(
    "userid, body",
    [
        ("", {}),
        ("abc", {}),
        ("456", {}),
        ("123", {"timestamp": 0}),
        ("123", {"userid": "456"}),
        ("123", {"targeturl": "https://example.com"}),
    ],
)
def test_dux_invalid_envelope_blocks_transport(tmp_path, userid, body):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "dux_soup", "Dux", config={"userid": userid}, secret="fixture-key"
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open") as send:
        result = bound["dux_soup_request"][1](
            {"method": "POST", "path": "/xapi/remote/control/123/queue", "body": body}
        )
    assert "error:" in result
    send.assert_not_called()


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_autoklose_query_token_and_json(tmp_path, method):
    from urllib.parse import parse_qs, urlsplit

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("autoklose", "Autoklose", secret="key&= +?")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    body = {"type": "base64", "name": "fixture.txt", "is_inline": False, "attachment": "aGk="}
    path = "/campaigns/123/emails" if method == "GET" else "/campaigns/123/emails/456/attachment"
    args = {"path": path}
    if method == "POST":
        args.update(method=method, body=body)
    else:
        args["query"] = {"expand[]": ["body", "statistics"]}
    with patch("connectors.generic._open", return_value=_response({})) as send:
        result = bound["autoklose_get" if method == "GET" else "autoklose_request"][1](args)
    assert "HTTP 200" in result
    req = send.call_args.args[0]
    url = urlsplit(req.full_url)
    assert url.netloc == "api.autoklose.com"
    assert url.path == "/api" + path
    expected = {"api_token": ["key&= +?"]}
    if method == "GET":
        expected["expand[]"] = ["body", "statistics"]
    assert parse_qs(url.query) == expected
    assert req.get_header("Authorization") is None
    if method == "POST":
        assert json.loads(req.data) == body
        assert req.get_header("Content-type") == "application/json"
    else:
        assert req.data is None


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_mailingboss_path_token(tmp_path, method):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("builderall_mailingboss", "MailingBoss", secret="key/+ ?&")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    path = "/lists" if method == "GET" else "/lists/fields"
    args = {"path": path + "?page=1", "method": method}
    if method == "POST":
        args["body"] = {"list_uid": "fixture-list"}
    with patch("connectors.generic._open", return_value=_response({})) as send:
        result = bound[
            "builderall_mailingboss_get" if method == "GET" else "builderall_mailingboss_request"
        ][1](args)
    assert "HTTP 200" in result
    req = send.call_args.args[0]
    assert (
        req.full_url
        == "https://member.mailingboss.com/integration/index.php"
        + path
        + "/key%2F%2B%20%3F%26?page=1"
    )
    assert req.get_header("Authorization") is None
    if method == "POST":
        assert json.loads(req.data) == {"list_uid": "fixture-list"}
    else:
        assert req.data is None


def test_mailingboss_transport_error_omits_token(tmp_path):
    import urllib.error

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("builderall_mailingboss", "MailingBoss", secret="fixture-secret")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", side_effect=urllib.error.URLError("fixture-secret")):
        result = bound["builderall_mailingboss_get"][1]({"path": "/lists"})
    assert result == "error: could not reach API"


@pytest.mark.parametrize(
    "path", ["/lineitems", "/daily-stats", "/creatives", "/creatives-daily-stats"]
)
def test_buysellads_reporting_query(tmp_path, path):
    from urllib.parse import parse_qs, urlsplit

    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("buysellads", "BuySellAds", secret="key&= +?")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    query = {"startDate": "2020-09-01", "endDate": "2020-09-30"}
    with patch("connectors.generic._open", return_value=_response([])) as send:
        result = bound["buysellads_get"][1]({"path": path, "query": query})
    assert "HTTP 200" in result
    req = send.call_args.args[0]
    url = urlsplit(req.full_url)
    assert url.netloc == "papi.buysellads.com"
    assert url.path == path
    assert parse_qs(url.query) == {
        "key": ["key&= +?"],
        "startDate": ["2020-09-01"],
        "endDate": ["2020-09-30"],
    }
    assert req.get_header("Authorization") is None
    assert req.data is None


def test_kartra_form_credentials_and_nested_read(tmp_path):
    from urllib.parse import parse_qs

    paths = HarnessPaths(home=tmp_path)
    creds = {"app_id": "fixture-app", "api_key": "key&=", "api_password": "pass+ ?"}
    record = Connectors(paths).add("kartra", "Kartra", secret=json.dumps(creds))
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({"status": "Success"})) as send:
        result = bound["kartra_request"][1](
            {"method": "POST", "path": "/", "body": {"get_lead": {"email": "fixture@example.com"}}}
        )
    assert "HTTP 200" in result
    req = send.call_args.args[0]
    assert req.full_url == "https://app.kartra.com/api"
    assert req.get_header("Authorization") is None
    assert req.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert parse_qs(req.data.decode()) == {
        **{k: [v] for k, v in creds.items()},
        "get_lead[email]": ["fixture@example.com"],
    }


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/", {}),
        ("POST", "/other", {}),
        ("POST", "/?api_key=x", {}),
        ("POST", "/", {"api_key": "override"}),
        ("POST", "/", {"api_password[]": "override"}),
    ],
)
def test_kartra_rejects_wrong_request_shape(tmp_path, method, path, body):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "kartra", "Kartra", secret=json.dumps({"app_id": "a", "api_key": "k", "api_password": "p"})
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open") as send:
        result = bound["kartra_request"][1]({"method": method, "path": path, "body": body})
    assert "error:" in result
    send.assert_not_called()


def test_lahar_conversion_json_token(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("lahar", "Lahar", secret="fixture&token")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    body = {"nome_formulario": "Dotobot fixture", "email_contato": "fixture@example.com"}
    with patch("connectors.generic._open", return_value=_response({})) as send:
        result = bound["lahar_request"][1]({"method": "POST", "path": "/conversions", "body": body})
    assert "HTTP 200" in result
    req = send.call_args.args[0]
    assert req.full_url == "https://app.lahar.com.br/api/conversions"
    assert req.get_header("Authorization") is None
    assert req.get_header("Content-type") == "application/json"
    assert json.loads(req.data) == {**body, "token_api_lahar": "fixture&token"}
    assert "token_api_lahar" not in body


def test_dripcel_contact_search_json(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("dripcel", "Dripcel", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    body = {
        "find": {"tag_ids": {"$in": ["fixture-tag"]}},
        "projection": {"cell": 1},
        "options": {"skip": 0, "limit": 1},
    }
    with patch("connectors.generic._open", return_value=_response({"ok": True})) as send:
        result = bound["dripcel_request"][1](
            {"method": "POST", "path": "/contacts/search", "body": body}
        )
    assert "HTTP 200" in result
    req = send.call_args.args[0]
    assert req.full_url == "https://api.dripcel.com/contacts/search"
    assert req.get_header("Authorization") == "Bearer fixture-key"
    assert json.loads(req.data) == body


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_icontact_headers_and_array_write(tmp_path, method):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "icontact", "iContact", secret=json.dumps(["app", "user", "password"])
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    body = [
        {
            "name": "Fixture",
            "welcomeMessageId": 0,
            "emailOwnerOnChange": 0,
            "welcomeOnManualAdd": 0,
            "welcomeOnSignupAdd": 0,
        }
    ]
    args = (
        {"path": "/a/"}
        if method == "GET"
        else {"method": method, "path": "/a/1/c/2/lists", "body": body}
    )
    with patch("connectors.generic._open", return_value=_response({})) as send:
        result = bound["icontact_get" if method == "GET" else "icontact_request"][1](args)
    assert "HTTP 200" in result
    req = send.call_args.args[0]
    assert req.full_url == "https://app.icontact.com/icp" + args["path"]
    for header, value in [
        ("Api-appid", "app"),
        ("Api-username", "user"),
        ("Api-password", "password"),
        ("Api-version", "2.2"),
        ("Content-type", "application/json"),
    ]:
        assert req.get_header(header) == value
    assert req.get_header("Authorization") is None
    if method == "POST":
        assert json.loads(req.data) == body


@pytest.mark.parametrize("version", [None, "202608"])
def test_linkedin_post_version_headers(tmp_path, version):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "linkedin",
        "LinkedIn",
        secret="fixture-key",
        config={"api_version": version} if version else {},
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    body = {
        "author": "urn:li:organization:1",
        "commentary": "Fixture",
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    with patch("connectors.generic._open", return_value=_response({})) as send:
        result = bound["linkedin_request"][1](
            {"method": "POST", "path": "/rest/posts", "body": body}
        )
    assert "HTTP 200" in result
    req = send.call_args.args[0]
    assert req.full_url == "https://api.linkedin.com/rest/posts"
    assert req.get_header("Linkedin-version") == (version or "202609")
    assert req.get_header("X-restli-protocol-version") == "2.0.0"
    assert json.loads(req.data) == body


def test_facebook_messenger_json(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("facebook", "Facebook", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    body = {"recipient": {"id": "fixture-person"}, "sender_action": "mark_seen"}
    with patch("connectors.generic._open", return_value=_response({})) as send:
        result = bound["facebook_request"][1](
            {"method": "POST", "path": "/v20.0/123/messages", "body": body}
        )
    assert "HTTP 200" in result
    req = send.call_args.args[0]
    assert req.full_url == "https://graph.facebook.com/v20.0/123/messages"
    assert req.get_header("Authorization") == "Bearer fixture-key"
    assert json.loads(req.data) == body


@pytest.mark.parametrize(
    "mode,host", [("instagram", "graph.instagram.com"), ("facebook", "graph.facebook.com")]
)
def test_instagram_login_hosts(tmp_path, mode, host):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "instagram", "Instagram", secret="fixture-key", config={"login_type": mode}
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({})) as send:
        result = bound["instagram_get"][1]({"path": "/v20.0/123", "query": {"fields": "id"}})
    assert "HTTP 200" in result
    assert send.call_args.args[0].full_url == "https://" + host + "/v20.0/123?fields=id"
    assert send.call_args.args[0].get_header("Authorization") == "Bearer fixture-key"


def test_instagram_default_json_message(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("instagram", "Instagram", secret="fixture-key")
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    body = {"recipient": {"id": "fixture-person"}, "message": {"text": "Fixture"}}
    with patch("connectors.generic._open", return_value=_response({})) as send:
        result = bound["instagram_request"][1](
            {"method": "POST", "path": "/v20.0/123/messages", "body": body}
        )
    assert "HTTP 200" in result
    assert send.call_args.args[0].full_url == "https://graph.instagram.com/v20.0/123/messages"
    assert json.loads(send.call_args.args[0].data) == body


def test_instagram_invalid_login_type(tmp_path):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "instagram", "Instagram", secret="fixture-key", config={"login_type": "other"}
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open") as send:
        assert "error" in bound["instagram_get"][1]({"path": "/me"})
    send.assert_not_called()


@pytest.mark.parametrize("project", [None, "fixture-project", "bad\r\nheader", ""])
def test_ad_manager_report_and_quota_project(tmp_path, project):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "google_ad_manager",
        "Ad Manager",
        secret="fixture-key",
        config={} if project is None else {"quota_project": project},
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    with patch("connectors.generic._open", return_value=_response({})) as send:
        result = bound["google_ad_manager_request"][1](
            {"method": "POST", "path": "/networks/123/reports/456:run", "body": {}}
        )
    if project in ("", "bad\r\nheader"):
        assert "invalid" in result
        send.assert_not_called()
        return
    assert "HTTP 200" in result
    req = send.call_args.args[0]
    assert req.full_url == "https://admanager.googleapis.com/v1/networks/123/reports/456:run"
    assert req.get_header("X-goog-user-project") == project
    assert req.get_header("Authorization") == "Bearer fixture-key"
    assert json.loads(req.data) == {}


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"login_customer_id": "1234567890", "linked_customer_id": "9876543210"},
        {"login_customer_id": "123-456-7890"},
        {"linked_customer_id": "bad\nheader"},
    ],
)
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_google_ads_credentials_and_customer_headers(tmp_path, config, method):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "google_ads",
        "Ads",
        secret=json.dumps(["fixture-token", "fixture-developer"]),
        config=config,
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    path = (
        "/v25/customers:listAccessibleCustomers"
        if method == "GET"
        else "/v25/customers/1234567890/googleAds:search"
    )
    body = {"query": "SELECT campaign.id FROM campaign LIMIT 1"}
    args = {"path": path} if method == "GET" else {"path": path, "method": method, "body": body}
    with patch("connectors.generic._open", return_value=_response({})) as send:
        result = bound["google_ads_get" if method == "GET" else "google_ads_request"][1](args)
    if any(not v.isdigit() for v in config.values()):
        assert "digits only" in result
        send.assert_not_called()
        return
    assert "HTTP 200" in result
    req = send.call_args.args[0]
    assert req.full_url == "https://googleads.googleapis.com" + path
    assert req.get_header("Authorization") == "Bearer fixture-token"
    assert req.get_header("Developer-token") == "fixture-developer"
    assert req.get_header("Login-customer-id") == config.get("login_customer_id")
    assert req.get_header("Linked-customer-id") == config.get("linked_customer_id")
    if method == "POST":
        assert json.loads(req.data) == body


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_demio_header_pair_and_registration(tmp_path, method):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add(
        "demio", "Demio", secret=json.dumps(["fixture-key", "fixture-secret"])
    )
    bound = tools_for_bot(paths, "atlas", record_ids={record["id"]})
    path = "/ping" if method == "GET" else "/event/register"
    body = {"id": 1, "date_id": 35, "name": "Fixture", "email": "fixture@example.com"}
    args = {"path": path} if method == "GET" else {"path": path, "method": method, "body": body}
    with patch("connectors.generic._open", return_value=_response({})) as send:
        result = bound["demio_get" if method == "GET" else "demio_request"][1](args)
    assert "HTTP 200" in result
    req = send.call_args.args[0]
    assert req.full_url == "https://my.demio.com/api/v1" + path
    assert req.get_header("Api-key") == "fixture-key"
    assert req.get_header("Api-secret") == "fixture-secret"
    assert req.get_header("Authorization") is None
    if method == "PUT":
        assert json.loads(req.data) == body
