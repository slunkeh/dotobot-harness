"""Anthropic Claude subscription PKCE OAuth (no live network)."""

from __future__ import annotations

from providers.anthropic import AnthropicProvider
from providers.anthropic_oauth import (
    OAuthError,
    access_token,
    authorization_url,
    exchange,
    exchange_code,
    parse_callback,
    reset_sessions,
    save_tokens,
    start_login,
)
from providers.base import Auth, Message


class FakeTransport:
    def __init__(self, payload=None):
        self.payload = payload or {
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "expires_in": 3600,
        }
        self.posts: list[tuple[str, dict]] = []

    def post_json(self, url, data):
        self.posts.append((url, dict(data)))
        return dict(self.payload)

    def post_form(self, url, data):
        self.posts.append((url, dict(data)))
        return {
            "access_token": "at-2",
            "refresh_token": "rt-2",
            "expires_in": 3600,
        }


def test_parse_callback_code_hash_state():
    code, state = parse_callback("abc#xyz")
    assert code == "abc"
    assert state == "xyz"


def test_authorization_url_is_claude_pkce():
    url = authorization_url("chal", "st")
    assert url.startswith("https://claude.ai/oauth/authorize?")
    assert "client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e" in url
    assert "code_challenge=chal" in url
    assert "code_challenge_method=S256" in url


def test_start_and_exchange_saves_tokens(tmp_path):
    reset_sessions()
    from harness.paths import HarnessPaths

    paths = HarnessPaths(tmp_path)
    paths.ensure_layout()
    started = start_login(paths)
    assert started["status"] == "pending"
    assert started["needs_code"] is True
    assert "claude.ai" in started["verification_uri"]

    assert started.get("code_verifier")
    out = exchange(paths, "the-code#" + started["code_verifier"], transport=FakeTransport())
    assert out["status"] == "complete"
    assert access_token(paths) == "at-1"


def test_refresh_when_expired(tmp_path):
    import json
    import time

    from harness.paths import HarnessPaths

    paths = HarnessPaths(tmp_path)
    paths.ensure_layout()
    save_tokens(
        paths,
        {"access_token": "old", "refresh_token": "rt-1", "expires_in": 1},
    )
    path = paths.credentials / "ANTHROPIC_OAUTH"
    data = json.loads(path.read_text())
    data["expires_at"] = time.time() - 10
    path.write_text(json.dumps(data))

    token = access_token(paths, transport=FakeTransport())
    assert token == "at-2"


def test_rejects_pasting_the_authorize_url_or_verifier(tmp_path):
    from providers.anthropic_oauth import WRONG_PASTE, exchange

    reset_sessions()
    from harness.paths import HarnessPaths

    paths = HarnessPaths(tmp_path)
    paths.ensure_layout()
    started = start_login(paths)
    for paste in (
        started["code_verifier"],
        started["verification_uri"],
        "just-a-long-pkce-looking-string-without-a-hash",
    ):
        try:
            exchange(paths, paste, transport=FakeTransport())
            raise AssertionError(f"expected OAuthError for {paste!r}")
        except OAuthError as exc:
            assert str(exc) == WRONG_PASTE


def test_429_does_not_try_the_fallback_host():
    from providers.anthropic_oauth import RATE_LIMITED, exchange_code

    class Limited:
        def __init__(self):
            self.urls = []

        def post_json(self, url, data):
            self.urls.append(url)
            raise OAuthError(RATE_LIMITED, status=429)

        def post_form(self, url, data):
            raise AssertionError("must not form-post after 429")

    t = Limited()
    try:
        exchange_code("abc#ver", "ver", expected_state="ver", transport=t)
        raise AssertionError("expected 429")
    except OAuthError as exc:
        assert exc.status == 429
    assert len(t.urls) == 1
    assert "platform.claude.com" in t.urls[0]


def test_client_supplied_tokens_skip_anthropic(tmp_path):
    reset_sessions()
    from harness.paths import HarnessPaths

    paths = HarnessPaths(tmp_path)
    paths.ensure_layout()
    start_login(paths)
    out = exchange(
        paths,
        tokens={"access_token": "from-mac", "refresh_token": "rt", "expires_in": 3600},
        transport=FakeTransport(),
    )
    assert out["status"] == "complete"
    assert access_token(paths) == "from-mac"


def test_exchange_without_session_fails(tmp_path):
    reset_sessions()
    from harness.paths import HarnessPaths

    paths = HarnessPaths(tmp_path)
    paths.ensure_layout()
    try:
        exchange(paths, "code", transport=FakeTransport())
        raise AssertionError("expected OAuthError")
    except OAuthError:
        pass


def test_exchange_code_posts_json():
    t = FakeTransport()
    payload = exchange_code("abc", "verifier", expected_state="verifier", transport=t)
    assert payload["access_token"] == "at-1"
    assert t.posts[0][1]["code_verifier"] == "verifier"
    assert t.posts[0][0].endswith("/v1/oauth/token")


def test_claude_oauth_headers_and_system_prefix():
    p = AnthropicProvider(auth=Auth(oauth_token="eyJabc"), claude_oauth=True)
    headers = p._headers()
    assert headers["authorization"] == "Bearer eyJabc"
    assert "oauth-2025-04-20" in headers["anthropic-beta"]
    assert headers["user-agent"].startswith("claude-cli/")
    assert headers["x-app"] == "cli"
    body = p._body([Message(role="user", content="hi")], "be helpful", None, 16, 0.2, False)
    assert isinstance(body["system"], list)
    assert body["system"][0]["text"].startswith("You are Claude Code")
    assert body["system"][1]["text"] == "be helpful"
