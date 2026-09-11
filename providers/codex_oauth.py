"""OpenAI Codex / ChatGPT subscription OAuth (PKCE, Codex CLI client).

Users sign in with a ChatGPT/Codex plan. The Codex CLI's public client id
redirects to `http://localhost:1455/auth/callback`; the Mac app hosts that
loopback (same pattern as connector MCP OAuth) and POSTs the code to
`/api/providers/codex/oauth/exchange`.

Copied from Hermes (`hermes_cli/auth.py`) and the Codex CLI: client
`app_EMoamEEZ73f0CkXaXp7hrann`, token host `auth.openai.com`, inference at
`chatgpt.com/backend-api/codex/responses`. Tokens live in
`$HARNESS_HOME/credentials/CODEX_OAUTH`. Stdlib only.

A host Codex CLI login (`~/.codex/auth.json`) still counts as configured when
no harness tokens are stored.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from urllib.parse import urlparse

from . import oauth_store
from .oauth_store import _CredRoot

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPE = "openid profile email offline_access"
SECRET_NAME = "CODEX_OAUTH"
REFRESH_SKEW_SECONDS = 120
PROVIDER_IDS = frozenset({"codex", "openai", "codex-chatgpt", "chatgpt"})
USER_AGENT = "codex_cli_rs/0.1.0"


class OAuthError(RuntimeError):
    """Raised when the Codex PKCE flow fails."""


class Transport:
    """Minimal HTTP surface so tests can stub auth.openai.com."""

    def post_form(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        body = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
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
        raise OAuthError(f"Codex HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise OAuthError(f"Codex request failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise OAuthError("Codex returned non-JSON") from exc
    if not isinstance(payload, dict):
        raise OAuthError("Codex returned a non-object JSON payload")
    return payload


def authorization_url(challenge: str, state: str) -> str:
    params = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": "codex_cli_rs",
        }
    )
    return f"{AUTHORIZE_URL}?{params}"


def chatgpt_account_id(id_token: str) -> str:
    """ChatGPT account id from the id_token companion claim."""
    if not id_token:
        return ""
    parts = id_token.split(".")
    if len(parts) < 2:
        return ""
    import base64
    import binascii

    payload_b64 = parts[1]
    try:
        raw = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    auth = payload.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        for key in ("chatgpt_account_id", "account_id"):
            value = auth.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for key in ("chatgpt_account_id", "account_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def exchange_code(
    code: str,
    verifier: str,
    *,
    transport: Transport | None = None,
) -> dict[str, Any]:
    if not (code or "").strip():
        raise OAuthError("Codex OAuth did not return an authorization code")
    t = transport or Transport()
    payload = t.post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code.strip(),
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        },
    )
    access = str(payload.get("access_token") or "").strip()
    if not access:
        raise OAuthError("Codex token response was missing access_token")
    return payload


def refresh_tokens(
    refresh_token: str,
    *,
    id_token: str = "",
    transport: Transport | None = None,
) -> dict[str, Any]:
    if not refresh_token.strip():
        raise OAuthError("Codex OAuth is missing a refresh_token; sign in again")
    from .codex_login import jwt_audience

    client_id = jwt_audience(id_token) if id_token else CLIENT_ID
    t = transport or Transport()
    payload = t.post_form(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id or CLIENT_ID,
        },
    )
    access = str(payload.get("access_token") or "").strip()
    if not access:
        raise OAuthError("Codex token refresh response was missing access_token")
    return {
        "access_token": access,
        "refresh_token": str(payload.get("refresh_token") or refresh_token).strip(),
        "id_token": str(payload.get("id_token") or id_token).strip(),
        "expires_in": payload.get("expires_in"),
        "token_type": str(payload.get("token_type") or "Bearer"),
    }


def oauth_configured(paths: _CredRoot) -> bool:
    return oauth_store.cred_path(paths, SECRET_NAME).is_file()


def load_tokens(paths: _CredRoot) -> dict[str, Any] | None:
    return oauth_store.load_tokens(paths, SECRET_NAME)


def save_tokens(paths: _CredRoot, tokens: dict[str, Any]) -> None:
    id_token = str(tokens.get("id_token") or "").strip()
    account = str(tokens.get("account_id") or "").strip() or chatgpt_account_id(id_token)
    extra = {"id_token": id_token, "account_id": account}
    oauth_store.save_tokens(paths, SECRET_NAME, tokens, extra=extra, default_ttl=3600)


def clear_tokens(paths: _CredRoot) -> bool:
    reset_sessions()
    return oauth_store.clear_tokens(paths, SECRET_NAME)


def account_id(paths: _CredRoot) -> str:
    data = load_tokens(paths) or {}
    value = str(data.get("account_id") or "").strip()
    if value:
        return value
    return chatgpt_account_id(str(data.get("id_token") or ""))


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
    stale = (
        force or not access or not expires_at or time.time() >= expires_at - REFRESH_SKEW_SECONDS
    )
    if stale:
        if not refresh:
            return access or None
        updated = refresh_tokens(
            refresh,
            id_token=str(data.get("id_token") or ""),
            transport=transport,
        )
        if not updated.get("id_token"):
            updated["id_token"] = data.get("id_token") or ""
        if not updated.get("account_id"):
            updated["account_id"] = data.get("account_id") or chatgpt_account_id(
                str(updated.get("id_token") or "")
            )
        save_tokens(paths, updated)
        return updated["access_token"]
    return access


_sessions: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def reset_sessions() -> None:
    with _lock:
        _sessions.clear()


def _cli_complete() -> bool:
    from . import codex_login

    return codex_login.login_available()


def status(provider: str, paths: _CredRoot) -> dict[str, Any]:
    name = "codex"
    with _lock:
        sess = dict(_sessions.get(name) or {})
    if sess:
        public = {
            "provider": name,
            "status": sess.get("status", "pending"),
            "verification_uri": sess.get("verification_uri"),
            "verification_uri_complete": sess.get("verification_uri"),
            "needs_loopback": True,
            "redirect_uri": REDIRECT_URI,
            "expires_in": sess.get("expires_in"),
            "message": sess.get("message"),
        }
        return {k: v for k, v in public.items() if v is not None}
    if oauth_configured(paths):
        return {"provider": name, "status": "complete"}
    if _cli_complete():
        return {
            "provider": name,
            "status": "complete",
            "message": "Signed in by reusing the Codex CLI ChatGPT login.",
        }
    return {"provider": name, "status": "idle"}


def start_login(paths: _CredRoot, *, provider: str = "codex") -> dict[str, Any]:
    name = provider.lower()
    if name not in PROVIDER_IDS:
        raise OAuthError(f"OAuth is not implemented for {provider}")
    verifier, challenge = oauth_store.pkce_pair()
    # `state` rides the authorize URL and comes back on the plaintext
    # localhost:1455 redirect (browser history, URL logs, any local listener
    # that wins the port). It must therefore be its own random value: using
    # the PKCE verifier here hands whoever sees that redirect both halves of
    # the token exchange. The verifier stays server-side in the session.
    state = secrets.token_urlsafe(32)
    url = authorization_url(challenge, state)
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "auth.openai.com":
        raise OAuthError(f"Codex authorization URL host is not auth.openai.com: {url!r}")
    session = {
        "status": "pending",
        "code_verifier": verifier,
        "state": state,
        "verification_uri": url,
        "expires_in": 600,
        "started_at": time.time(),
        "message": (
            "Approve ChatGPT / Codex in the browser. The app catches the localhost:1455 redirect."
        ),
    }
    with _lock:
        _sessions["codex"] = session
    return {
        "provider": "codex",
        "status": "pending",
        "verification_uri": url,
        "verification_uri_complete": url,
        "needs_loopback": True,
        "redirect_uri": REDIRECT_URI,
        "expires_in": 600,
        "message": session["message"],
    }


def exchange(
    paths: _CredRoot,
    code: str,
    *,
    state: str | None = None,
    provider: str = "codex",
    transport: Transport | None = None,
) -> dict[str, Any]:
    with _lock:
        sess = dict(_sessions.get("codex") or {})
    if not sess:
        raise OAuthError("no in-flight Codex OAuth session; start sign-in first")
    expected = str(sess.get("state") or "")
    # A missing state is a mismatch too: the redirect always carries the one
    # we issued, so an exchange without it did not come from our redirect.
    if not state or state != expected:
        raise OAuthError("Codex OAuth state mismatch; start sign-in again.")
    tokens = exchange_code(code, sess["code_verifier"], transport=transport)
    save_tokens(paths, tokens)
    with _lock:
        current = _sessions.get("codex")
        if current is not None:
            current["status"] = "complete"
            current["message"] = "Signed in to Codex."
            current.pop("code_verifier", None)
    return status("codex", paths)
