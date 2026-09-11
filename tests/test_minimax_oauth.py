"""MiniMax user-code OAuth (no live network)."""

from __future__ import annotations

from providers.minimax_oauth import (
    access_token,
    finish_login,
    oauth_configured,
    reset_sessions,
    start_login,
    status,
)


class FakeTransport:
    def __init__(self, pending_polls: int = 0) -> None:
        self.pending_polls = pending_polls
        self.posts: list[tuple[str, dict]] = []

    def post_form(self, url: str, data: dict, headers=None):
        self.posts.append((url, dict(data)))
        if url.endswith("/oauth/code"):
            return {
                "user_code": "MM-1234",
                "verification_uri": "https://www.minimax.io/connect",
                "expired_in": 90,
                "interval": 1,
                "state": data["state"],
            }
        if data.get("grant_type") == "urn:ietf:params:oauth:grant-type:user_code":
            if self.pending_polls > 0:
                self.pending_polls -= 1
                return {"status": "pending"}
            return {
                "status": "success",
                "access_token": "mm-at",
                "refresh_token": "mm-rt",
                "expired_in": 3600,
            }
        if data.get("grant_type") == "refresh_token":
            return {
                "status": "success",
                "access_token": "mm-at-2",
                "refresh_token": "mm-rt-2",
                "expired_in": 3600,
            }
        raise AssertionError(f"unexpected post {url} {data}")


def test_user_code_login_saves_tokens(tmp_path):
    reset_sessions()
    from harness.paths import HarnessPaths

    paths = HarnessPaths(tmp_path)
    paths.ensure_layout()
    transport = FakeTransport(pending_polls=1)
    started = start_login(paths, transport=transport, spawn=False)
    assert started["status"] == "pending"
    assert started["user_code"] == "MM-1234"
    assert "minimax.io" in started["verification_uri"]
    assert status("minimax", paths)["status"] == "pending"

    sleeps: list[float] = []
    out = finish_login(paths, transport=transport, sleep=sleeps.append)
    assert out["status"] == "complete"
    assert oauth_configured(paths)
    assert access_token(paths) == "mm-at"
    assert sleeps == [2]
