"""Hermes-style xAI device-code OAuth (no live network)."""

from __future__ import annotations

from providers.xai_oauth import (
    OAuthError,
    access_token,
    finish_login,
    oauth_configured,
    reset_sessions,
    save_tokens,
    start_login,
    status,
)


class FakeTransport:
    def __init__(self, pending_polls: int = 0) -> None:
        self.pending_polls = pending_polls
        self.posts: list[tuple[str, dict]] = []
        self.refresh_count = 0

    def get_json(self, url: str, headers=None):
        assert "openid-configuration" in url
        return {
            "authorization_endpoint": "https://auth.x.ai/oauth2/auth",
            "token_endpoint": "https://auth.x.ai/oauth2/token",
        }

    def post_form(self, url: str, data: dict, headers=None):
        self.posts.append((url, dict(data)))
        if url.endswith("/oauth2/device/code"):
            return {
                "device_code": "dev-1",
                "user_code": "WDJB-MJHT",
                "verification_uri": "https://accounts.x.ai/connect/device",
                "verification_uri_complete": "https://accounts.x.ai/connect/device?user_code=WDJB-MJHT",
                "expires_in": 90,
                "interval": 1,
            }
        if data.get("grant_type") == "urn:ietf:params:oauth:grant-type:device_code":
            if self.pending_polls > 0:
                self.pending_polls -= 1
                return {"error": "authorization_pending"}
            return {
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in": 3600,
                "token_type": "Bearer",
            }
        if data.get("grant_type") == "refresh_token":
            self.refresh_count += 1
            return {
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "expires_in": 3600,
                "token_type": "Bearer",
            }
        raise AssertionError(f"unexpected post {url} {data}")


def test_device_code_login_saves_tokens(tmp_path):
    reset_sessions()
    from harness.paths import HarnessPaths

    paths = HarnessPaths(tmp_path)
    paths.ensure_layout()
    transport = FakeTransport(pending_polls=1)
    started = start_login(paths, transport=transport, spawn=False)
    assert started["status"] == "pending"
    assert started["user_code"] == "WDJB-MJHT"
    assert "accounts.x.ai" in started["verification_uri_complete"]
    assert status("grok", paths)["status"] == "pending"

    sleeps: list[int] = []
    out = finish_login(paths, transport=transport, sleep=sleeps.append)
    assert out["status"] == "complete"
    assert oauth_configured(paths)
    assert access_token(paths) == "access-1"
    assert sleeps == [1]


def test_refresh_when_expired(tmp_path):
    from harness.paths import HarnessPaths

    paths = HarnessPaths(tmp_path)
    paths.ensure_layout()
    save_tokens(
        paths,
        {
            "access_token": "old",
            "refresh_token": "refresh-1",
            "expires_in": 1,
        },
        token_endpoint="https://auth.x.ai/oauth2/token",
    )
    # Force expiry
    import json
    import time

    path = paths.credentials / "XAI_OAUTH"
    data = json.loads(path.read_text())
    data["expires_at"] = time.time() - 10
    path.write_text(json.dumps(data))

    transport = FakeTransport()
    token = access_token(paths, transport=transport)
    assert token == "access-2"
    assert transport.refresh_count == 1


def test_start_login_rejects_unknown_provider(tmp_path):
    from harness.paths import HarnessPaths

    paths = HarnessPaths(tmp_path)
    paths.ensure_layout()
    try:
        start_login(paths, provider="claude", spawn=False)
        raise AssertionError("expected OAuthError")
    except OAuthError:
        pass
