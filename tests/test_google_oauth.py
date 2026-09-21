"""Google Workspace consent, scoped grants, refresh and removal of legacy auth."""

import base64
import hashlib
import time
from urllib.parse import parse_qs, urlparse

import pytest

from connectors.base import ConnectorContext
from harness import google_oauth, mcp_oauth
from harness.connectors import Connectors, catalog
from harness.paths import HarnessPaths


@pytest.fixture
def paths(tmp_path):
    mcp_oauth.reset_flows()
    yield HarnessPaths(home=tmp_path)
    mcp_oauth.reset_flows()


class Google(mcp_oauth.Transport):
    def __init__(self, scope):
        self.scope = scope
        self.forms = []
        self.payload = None

    def get_json(self, url):
        pytest.fail("Google must not use MCP discovery")

    def post_form(self, url, form):
        assert url == "https://oauth2.googleapis.com/token"
        assert "resource" not in form
        self.forms.append(form)
        return (
            self.payload
            if self.payload is not None
            else {
                "access_token": f"access-{len(self.forms)}",
                "refresh_token": "refresh",
                "scope": self.scope,
                "expires_in": 3600,
            }
        )


@pytest.mark.parametrize("kind", google_oauth.SCOPES)
def test_consent_exchange_refresh_and_disconnect(paths, kind):
    record = Connectors(paths).add(kind, kind)
    transport = Google(" ".join(google_oauth.SCOPES[kind]))
    start = google_oauth.start_authorize(
        paths,
        record,
        "http://127.0.0.1:18765/callback",
        client_id="desktop",
        client_secret="client-secret",
        transport=transport,
    )
    query = parse_qs(urlparse(start["authorize_url"]).query)
    assert query["scope"] == [transport.scope]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["code_challenge_method"] == ["S256"]
    assert "resource" not in query
    assert "client-secret" not in start["authorize_url"]
    assert mcp_oauth.exchange(paths, start["state"], "code", transport)["status"] == "connected"
    verifier = transport.forms[0]["code_verifier"]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    assert query["code_challenge"] == [challenge]
    assert google_oauth.token(paths, record) == "access-1"
    with pytest.raises(mcp_oauth.OAuthError, match="unknown or expired"):
        mcp_oauth.exchange(paths, start["state"], "code", transport)
    bundle = mcp_oauth.load_tokens(paths, record["id"])
    bundle["expires_at"] = time.time() - 1
    mcp_oauth.save_tokens(paths, record["id"], bundle)
    transport.payload = {"access_token": "renewed", "expires_in": 3600}
    assert mcp_oauth.access_token(paths, record["id"], transport=transport) == "renewed"
    assert mcp_oauth.load_tokens(paths, record["id"])["refresh_token"] == "refresh"
    mcp_oauth.clear_tokens(paths, record["id"])
    with pytest.raises(mcp_oauth.OAuthError, match="Connect this Google account"):
        google_oauth.token(paths, record)


@pytest.mark.parametrize("kind", google_oauth.SCOPES)
def test_old_pasted_credentials_are_never_used(paths, kind):
    record = Connectors(paths).add(kind, kind, secret="old-secret")
    assert record["secret_configured"] is False
    with pytest.raises(mcp_oauth.OAuthError, match="Connect this Google account"):
        ConnectorContext(paths, "bot", record).secret()


def test_ios_uses_registered_custom_scheme_and_own_client(paths, monkeypatch):
    monkeypatch.setenv("GOOGLE_IOS_CLIENT_ID", "ios")
    monkeypatch.setenv("GOOGLE_DESKTOP_CLIENT_ID", "desktop")
    record = Connectors(paths).add("gmail", "Gmail")
    start = google_oauth.start_authorize(paths, record, google_oauth.IOS_REDIRECT)
    assert parse_qs(urlparse(start["authorize_url"]).query)["client_id"] == ["ios"]
    with pytest.raises(mcp_oauth.OAuthError, match="Unsupported"):
        google_oauth.start_authorize(paths, record, "http://evil.example/callback")


@pytest.mark.parametrize(
    "payload",
    [
        {"access_token": "partial", "refresh_token": "refresh", "scope": "openid"},
        {"access_token": "temporary"},
    ],
)
def test_incomplete_consent_is_not_saved(paths, payload):
    record = Connectors(paths).add("google_drive", "Drive")
    transport = Google(google_oauth.SCOPES["google_drive"][0])
    transport.payload = payload
    start = google_oauth.start_authorize(paths, record, google_oauth.IOS_REDIRECT, client_id="ios")
    with pytest.raises(mcp_oauth.OAuthError):
        mcp_oauth.exchange(paths, start["state"], "code", transport)
    assert not mcp_oauth.connected(paths, record["id"])


def test_expired_token_does_not_fall_back_to_old_password(paths, monkeypatch):
    record = Connectors(paths).add("gmail", "Gmail", secret="old-password")
    mcp_oauth.save_tokens(
        paths,
        record["id"],
        {
            "access_token": "expired",
            "expires_at": 1,
            "token_endpoint": "https://oauth2.googleapis.com/token",
        },
    )
    with pytest.raises(mcp_oauth.OAuthError, match="Reconnect"):
        google_oauth.token(paths, record)


def test_catalog_offers_workspace_oauth_instead_of_obsolete_google_placeholder():
    entries = {entry["type"]: entry for entry in catalog()}
    assert "google" not in entries
    for kind in google_oauth.SCOPES:
        entry = entries[kind]
        assert entry["auth"] == "oauth"
        assert entry["oauth_supported"] is True
        assert entry["multi_account"] is True
        assert "planned" not in entry.get("description", "").lower()
