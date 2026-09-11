"""Anthropic Claude Pro/Max OAuth (PKCE, Hermes/Claude Code client).

Users sign in with a Claude subscription in the browser. Anthropic shows a
`code#state` string on `console.anthropic.com/oauth/code/callback`; the Mac
app pastes that back into `POST /api/providers/claude/oauth/exchange`.

Copied from Hermes (`agent/anthropic_adapter.py` + OpenCode's anthropic-auth):
same public Claude Code client id, S256 PKCE, token host
`platform.claude.com` with a console.anthropic.com fallback. Tokens live in
`$HARNESS_HOME/credentials/ANTHROPIC_OAUTH`. Stdlib only.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from urllib.parse import urlparse

from . import oauth_store
from .oauth_store import _CredRoot

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
SCOPE = "org:create_api_key user:profile user:inference"
TOKEN_URLS = (
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
)
SECRET_NAME = "ANTHROPIC_OAUTH"
REFRESH_SKEW_SECONDS = 300
PROVIDER_IDS = frozenset({"claude", "anthropic"})
CLAUDE_CODE_VERSION = "2.1.74"
# Match Claude Code on the token host. A bare "anthropic" UA plus a
# datacenter IP is a common 429 from this endpoint.
TOKEN_USER_AGENT = f"claude-cli/{CLAUDE_CODE_VERSION} (external, cli)"
CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."
OAUTH_BETAS = (
    "interleaved-thinking-2025-05-14",
    "fine-grained-tool-streaming-2025-05-14",
    "claude-code-20250219",
    "oauth-2025-04-20",
)
WRONG_PASTE = (
    "That's not the Claude authorization code. After you click Allow, the page "
    "shows a code like abc#xyz — paste that whole string, not the long URL."
)
RATE_LIMITED = (
    "Anthropic rate-limited sign-in. Wait a minute, click Sign in with OAuth "
    "again, then paste the code from the Claude page (it looks like abc#xyz)."
)


class OAuthError(RuntimeError):
    """Raised when the Anthropic PKCE flow fails."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Transport:
    """Minimal HTTP surface so tests can stub Anthropic's token host."""

    def post_json(self, url: str, data: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": TOKEN_USER_AGENT,
            },
            method="POST",
        )
        return _read_json(req)

    def post_form(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        body = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": TOKEN_USER_AGENT,
            },
            method="POST",
        )
        return _read_json(req)


def _read_json(req: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        if exc.code == 429:
            raise OAuthError(RATE_LIMITED, status=429) from exc
        raise OAuthError(f"Anthropic HTTP {exc.code}: {detail}", status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise OAuthError(f"Anthropic request failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise OAuthError("Anthropic returned non-JSON") from exc
    if not isinstance(payload, dict):
        raise OAuthError("Anthropic returned a non-object JSON payload")
    return payload


def _require_https(url: str, field: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        raise OAuthError(f"Anthropic {field} is not HTTPS: {url!r}")
    return url


def authorization_url(challenge: str, state: str) -> str:
    params = urllib.parse.urlencode(
        {
            "code": "true",
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{params}"


def parse_callback(raw: str) -> tuple[str, str | None]:
    """Split the `code#state` string Anthropic shows after approval."""
    text = (raw or "").strip()
    if not text:
        raise OAuthError("Paste the authorization code from the Claude page.")
    if "#" in text:
        code, state = text.split("#", 1)
        return code.strip(), state.strip() or None
    if "code=" in text:
        parsed = urlparse(text)
        qs = urllib.parse.parse_qs(parsed.query)
        fragment = urllib.parse.parse_qs(parsed.fragment)
        code = (qs.get("code") or fragment.get("code") or [""])[0]
        state = (qs.get("state") or fragment.get("state") or [""])[0]
        if code:
            return code, state or None
    return text, None


def reject_wrong_paste(raw: str, *, verifier: str = "", state: str = "") -> None:
    """Catch the common miss: pasting the authorize URL or PKCE verifier."""
    text = (raw or "").strip()
    if not text:
        raise OAuthError(WRONG_PASTE)
    if text in {verifier, state} or "oauth/authorize" in text or "code_challenge=" in text:
        raise OAuthError(WRONG_PASTE)
    if "#" not in text and "code=" not in text:
        raise OAuthError(WRONG_PASTE)


def exchange_code(
    raw_code: str,
    verifier: str,
    *,
    expected_state: str | None = None,
    transport: Transport | None = None,
) -> dict[str, Any]:
    code, state = parse_callback(raw_code)
    if expected_state and state and state != expected_state:
        raise OAuthError("Anthropic OAuth state mismatch; start sign-in again.")
    state = state or expected_state or verifier
    t = transport or Transport()
    body = {
        "code": code,
        "state": state,
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    }
    last_error: Exception | None = None
    for url in TOKEN_URLS:
        try:
            payload = t.post_json(url, body)
        except OAuthError as exc:
            # 429/400 from the live host is the answer; don't also hammer the
            # fallback (that is how a VPS IP gets stuck rate-limited).
            if exc.status in {400, 401, 403, 429}:
                raise
            last_error = exc
            continue
        access = str(payload.get("access_token") or "").strip()
        if access:
            return payload
        last_error = OAuthError("Anthropic token response was missing access_token")
    raise last_error or OAuthError("Anthropic token exchange failed")


def refresh_tokens(
    refresh_token: str, *, transport: Transport | None = None
) -> dict[str, Any]:
    if not refresh_token.strip():
        raise OAuthError("Anthropic OAuth is missing a refresh_token; sign in again")
    t = transport or Transport()
    last_error: Exception | None = None
    for url in TOKEN_URLS:
        try:
            payload = t.post_form(
                url,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CLIENT_ID,
                },
            )
        except OAuthError as exc:
            last_error = exc
            continue
        access = str(payload.get("access_token") or "").strip()
        if access:
            return {
                "access_token": access,
                "refresh_token": str(payload.get("refresh_token") or refresh_token).strip(),
                "expires_in": payload.get("expires_in"),
                "token_type": str(payload.get("token_type") or "Bearer"),
            }
        last_error = OAuthError("Anthropic token refresh response was missing access_token")
    raise last_error or OAuthError("Anthropic token refresh failed")


def oauth_configured(paths: _CredRoot) -> bool:
    return oauth_store.cred_path(paths, SECRET_NAME).is_file()


def load_tokens(paths: _CredRoot) -> dict[str, Any] | None:
    return oauth_store.load_tokens(paths, SECRET_NAME)


def save_tokens(paths: _CredRoot, tokens: dict[str, Any]) -> None:
    oauth_store.save_tokens(paths, SECRET_NAME, tokens, default_ttl=3600)


def clear_tokens(paths: _CredRoot) -> bool:
    reset_sessions()
    return oauth_store.clear_tokens(paths, SECRET_NAME)


def access_token(
    paths: _CredRoot,
    *,
    force: bool = False,
    transport: Transport | None = None,
) -> str | None:
    data = load_tokens(paths)
    if not data:
        return None
    access = str(data.get("access_token") or "").strip()
    refresh = str(data.get("refresh_token") or "").strip()
    expires_at = float(data.get("expires_at") or 0)
    stale = force or not access or not expires_at or time.time() >= expires_at - REFRESH_SKEW_SECONDS
    if stale:
        if not refresh:
            return access or None
        updated = refresh_tokens(refresh, transport=transport)
        save_tokens(paths, updated)
        return updated["access_token"]
    return access


_sessions: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def reset_sessions() -> None:
    with _lock:
        _sessions.clear()


def status(provider: str, paths: _CredRoot) -> dict[str, Any]:
    name = provider.lower()
    with _lock:
        sess = dict(_sessions.get(name) or {})
    if sess:
        public = {
            "provider": name,
            "status": sess.get("status", "pending"),
            "verification_uri": sess.get("verification_uri"),
            "verification_uri_complete": sess.get("verification_uri"),
            "needs_code": True,
            "expires_in": sess.get("expires_in"),
            "message": sess.get("message"),
        }
        return {k: v for k, v in public.items() if v is not None}
    if oauth_configured(paths):
        return {"provider": name, "status": "complete"}
    return {"provider": name, "status": "idle"}


def start_login(paths: _CredRoot, *, provider: str = "claude") -> dict[str, Any]:
    name = provider.lower()
    if name not in PROVIDER_IDS:
        raise OAuthError(f"OAuth is not implemented for {provider}")
    verifier, challenge = oauth_store.pkce_pair()
    url = authorization_url(challenge, verifier)
    _require_https(url, "authorization_url")
    session = {
        "status": "pending",
        "code_verifier": verifier,
        "state": verifier,
        "verification_uri": url,
        "expires_in": 600,
        "started_at": time.time(),
        "message": (
            "Approve in the browser, then paste the code Claude shows "
            "(it looks like abc#xyz — two pieces with a #)."
        ),
    }
    with _lock:
        _sessions[name] = session
    return {
        "provider": name,
        "status": "pending",
        "verification_uri": url,
        "verification_uri_complete": url,
        "needs_code": True,
        "expires_in": 600,
        "message": session["message"],
        # So the Mac can exchange the code from the user's IP (Anthropic
        # rate-limits this token host from many datacenter ranges).
        "code_verifier": verifier,
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "token_urls": list(TOKEN_URLS),
    }


def _finish_session(name: str, paths: _CredRoot, tokens: dict[str, Any]) -> dict[str, Any]:
    save_tokens(paths, tokens)
    with _lock:
        current = _sessions.get(name)
        if current is not None:
            current["status"] = "complete"
            current["message"] = "Signed in to Claude."
            current.pop("code_verifier", None)
    return status(name, paths)


def exchange(
    paths: _CredRoot,
    raw_code: str = "",
    *,
    tokens: dict[str, Any] | None = None,
    provider: str = "claude",
    transport: Transport | None = None,
) -> dict[str, Any]:
    name = provider.lower()
    with _lock:
        sess = dict(_sessions.get(name) or {})
    if not sess:
        raise OAuthError("no in-flight Anthropic OAuth session; start sign-in first")
    if isinstance(tokens, dict) and str(tokens.get("access_token") or "").strip():
        return _finish_session(name, paths, tokens)
    reject_wrong_paste(
        raw_code,
        verifier=str(sess.get("code_verifier") or ""),
        state=str(sess.get("state") or ""),
    )
    exchanged = exchange_code(
        raw_code,
        sess["code_verifier"],
        expected_state=str(sess.get("state") or ""),
        transport=transport,
    )
    return _finish_session(name, paths, exchanged)
