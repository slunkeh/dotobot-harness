"""Codex / ChatGPT subscription PKCE OAuth (no live network)."""

from __future__ import annotations

import base64
import json

from providers.codex_oauth import (
    OAuthError,
    access_token,
    account_id,
    authorization_url,
    chatgpt_account_id,
    exchange,
    reset_sessions,
    save_tokens,
    start_login,
)


def _jwt(payload: dict) -> str:
    def seg(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg(payload)}.sig"


class FakeTransport:
    def __init__(self):
        self.posts: list[tuple[str, dict]] = []

    def post_form(self, url, data):
        self.posts.append((url, dict(data)))
        if data.get("grant_type") == "authorization_code":
            return {
                "access_token": "at-1",
                "refresh_token": "rt-1",
                "id_token": _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-9"}}),
                "expires_in": 3600,
            }
        if data.get("grant_type") == "refresh_token":
            return {
                "access_token": "at-2",
                "refresh_token": "rt-2",
                "expires_in": 3600,
            }
        raise AssertionError(data)


def test_chatgpt_account_id_from_id_token():
    token = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-1"}})
    assert chatgpt_account_id(token) == "acct-1"


def test_authorization_url_uses_codex_cli_client():
    url = authorization_url("chal", "st")
    assert url.startswith("https://auth.openai.com/oauth/authorize?")
    assert "client_id=app_EMoamEEZ73f0CkXaXp7hrann" in url
    assert "localhost%3A1455" in url or "localhost:1455" in url
    assert "codex_cli_simplified_flow=true" in url


def test_start_and_exchange_saves_account(tmp_path):
    reset_sessions()
    from harness.paths import HarnessPaths

    paths = HarnessPaths(tmp_path)
    paths.ensure_layout()
    started = start_login(paths)
    assert started["needs_loopback"] is True
    assert started["redirect_uri"] == "http://localhost:1455/auth/callback"

    # The redirect always carries the state we issued, so the exchange
    # must present it (a stateless exchange is refused; see
    # tests/test_hardening_storage.py).
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(started["verification_uri"]).query)["state"][0]
    out = exchange(paths, "the-code", state=state, transport=FakeTransport())
    assert out["status"] == "complete"
    assert access_token(paths) == "at-1"
    assert account_id(paths) == "acct-9"


def test_refresh_when_expired(tmp_path):
    import time

    from harness.paths import HarnessPaths

    paths = HarnessPaths(tmp_path)
    paths.ensure_layout()
    save_tokens(
        paths,
        {
            "access_token": "old",
            "refresh_token": "rt-1",
            "id_token": _jwt({"aud": "app_EMoamEEZ73f0CkXaXp7hrann"}),
            "account_id": "acct-9",
            "expires_in": 1,
        },
    )
    path = paths.credentials / "CODEX_OAUTH"
    data = json.loads(path.read_text())
    data["expires_at"] = time.time() - 10
    path.write_text(json.dumps(data))

    token = access_token(paths, transport=FakeTransport())
    assert token == "at-2"
    assert account_id(paths) == "acct-9"


def test_exchange_rejects_state_mismatch(tmp_path):
    reset_sessions()
    from harness.paths import HarnessPaths

    paths = HarnessPaths(tmp_path)
    paths.ensure_layout()
    start_login(paths)
    try:
        exchange(paths, "code", state="wrong", transport=FakeTransport())
        raise AssertionError("expected OAuthError")
    except OAuthError:
        pass
