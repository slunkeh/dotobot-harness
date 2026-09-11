"""xAI Grok OAuth — Hermes device-code flow.

Copied from Nous Hermes Agent (`hermes_cli/auth.py`): same public Grok CLI
client id, OIDC discovery on auth.x.ai, device-code grant, refresh_token
rotation. Stdlib only. Tokens live in `$HARNESS_HOME/credentials/XAI_OAUTH`.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse


class _CredRoot(Protocol):
    @property
    def credentials(self) -> Path: ...

ISSUER = "https://auth.x.ai"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
DEVICE_CODE_URL = f"{ISSUER}/oauth2/device/code"
INFERENCE_BASE = "https://api.x.ai/v1"
SECRET_NAME = "XAI_OAUTH"
REFRESH_SKEW_SECONDS = 300
PROVIDER_IDS = frozenset({"grok", "xai", "xai-oauth"})


class OAuthError(RuntimeError):
    """Raised when the xAI device-code / refresh flow fails."""


class Transport:
    """Minimal HTTP surface so tests can stub the xAI auth server."""

    def get_json(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        req = urllib.request.Request(url, headers=_headers(headers), method="GET")
        return _read_json(req)

    def post_form(
        self, url: str, data: dict[str, str], headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        body = urllib.parse.urlencode(data).encode("utf-8")
        hdrs = _headers(headers)
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        return _read_json(req)


def _headers(extra: dict[str, str] | None) -> dict[str, str]:
    out = {"Accept": "application/json", "User-Agent": "dotobot/0.1"}
    if extra:
        out.update(extra)
    return out


def _read_json(req: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(detail) if detail else {}
        except json.JSONDecodeError:
            raise OAuthError(f"xAI HTTP {exc.code}: {detail}") from exc
        if isinstance(payload, dict) and payload.get("error"):
            payload["_http_status"] = exc.code
            return payload
        raise OAuthError(f"xAI HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise OAuthError(f"xAI request failed: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise OAuthError("xAI returned a non-object JSON payload")
    return payload


def _require_xai_https(url: str, field: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        raise OAuthError(f"xAI {field} is not HTTPS: {url!r}")
    if host != "x.ai" and not host.endswith(".x.ai"):
        raise OAuthError(f"xAI {field} host {host!r} is not on *.x.ai")
    return url


def discover(transport: Transport | None = None) -> dict[str, str]:
    t = transport or Transport()
    payload = t.get_json(DISCOVERY_URL)
    authorization = str(payload.get("authorization_endpoint") or "").strip()
    token = str(payload.get("token_endpoint") or "").strip()
    if not authorization or not token:
        raise OAuthError("xAI OIDC discovery was missing endpoints")
    return {
        "authorization_endpoint": _require_xai_https(authorization, "authorization_endpoint"),
        "token_endpoint": _require_xai_https(token, "token_endpoint"),
    }


def request_device_code(transport: Transport | None = None) -> dict[str, Any]:
    t = transport or Transport()
    payload = t.post_form(DEVICE_CODE_URL, {"client_id": CLIENT_ID, "scope": SCOPE})
    required = (
        "device_code",
        "user_code",
        "verification_uri",
        "verification_uri_complete",
        "expires_in",
        "interval",
    )
    missing = [k for k in required if k not in payload]
    if missing:
        raise OAuthError(f"xAI device-code response missing {', '.join(missing)}")
    return payload


def poll_device_token(
    *,
    token_endpoint: str,
    device_code: str,
    expires_in: int,
    interval: int,
    transport: Transport | None = None,
    sleep: Any = time.sleep,
    now: Any = time.monotonic,
) -> dict[str, Any]:
    t = transport or Transport()
    deadline = now() + max(1, int(expires_in))
    wait = max(1, int(interval))
    while now() < deadline:
        payload = t.post_form(
            token_endpoint,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": CLIENT_ID,
                "device_code": device_code,
            },
        )
        err = str(payload.get("error") or "")
        if payload.get("access_token") and payload.get("refresh_token"):
            return payload
        if err == "authorization_pending":
            sleep(wait)
            continue
        if err == "slow_down":
            wait = min(wait + 1, 30)
            sleep(wait)
            continue
        if err:
            desc = payload.get("error_description") or err
            raise OAuthError(f"xAI device-code polling failed: {desc}")
        raise OAuthError("xAI device-code token response was missing tokens")
    raise OAuthError("Timed out waiting for xAI device authorization")


def refresh_tokens(
    refresh_token: str,
    *,
    token_endpoint: str,
    transport: Transport | None = None,
) -> dict[str, Any]:
    if not refresh_token.strip():
        raise OAuthError("xAI OAuth is missing a refresh_token; sign in again")
    t = transport or Transport()
    payload = t.post_form(
        token_endpoint,
        {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
        },
    )
    if payload.get("_http_status") == 403 or payload.get("error"):
        desc = payload.get("error_description") or payload.get("error") or payload
        raise OAuthError(f"xAI token refresh failed: {desc}")
    access = str(payload.get("access_token") or "").strip()
    if not access:
        raise OAuthError("xAI token refresh response was missing access_token")
    return {
        "access_token": access,
        "refresh_token": str(payload.get("refresh_token") or refresh_token).strip(),
        "expires_in": payload.get("expires_in"),
        "token_type": str(payload.get("token_type") or "Bearer"),
        "id_token": str(payload.get("id_token") or ""),
    }


def _cred_path(paths: _CredRoot) -> Path:
    return paths.credentials / SECRET_NAME


def oauth_configured(paths: _CredRoot) -> bool:
    return _cred_path(paths).is_file()


def load_tokens(paths: _CredRoot) -> dict[str, Any] | None:
    path = _cred_path(paths)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def save_tokens(paths: _CredRoot, tokens: dict[str, Any], *, token_endpoint: str) -> None:
    paths.credentials.mkdir(parents=True, exist_ok=True)
    try:
        paths.credentials.chmod(0o700)
    except OSError:  # pragma: no cover - non-posix
        pass
    expires_in = tokens.get("expires_in")
    try:
        ttl = int(expires_in) if expires_in is not None else 21600
    except (TypeError, ValueError):
        ttl = 21600
    payload = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "token_endpoint": token_endpoint,
        "expires_at": time.time() + max(1, ttl),
        "token_type": tokens.get("token_type") or "Bearer",
    }
    from harness.fsutil import write_private
    from harness.redaction import register_secret

    for field in ("access_token", "refresh_token"):
        if isinstance(payload.get(field), str):
            register_secret(payload[field], f"XAI_OAUTH_{field.upper()}")
    write_private(_cred_path(paths), json.dumps(payload))


def clear_tokens(paths: _CredRoot) -> bool:
    path = _cred_path(paths)
    if path.is_file():
        path.unlink()
        return True
    return False


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
    endpoint = str(data.get("token_endpoint") or "").strip()
    expires_at = float(data.get("expires_at") or 0)
    stale = force or not access or not expires_at or time.time() >= expires_at - REFRESH_SKEW_SECONDS
    if stale:
        if not refresh:
            return access or None
        if not endpoint:
            endpoint = discover(transport)["token_endpoint"]
        updated = refresh_tokens(refresh, token_endpoint=endpoint, transport=transport)
        save_tokens(paths, updated, token_endpoint=endpoint)
        return updated["access_token"]
    return access


# -- in-flight device-code sessions (one per provider) ---------------------
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
            "user_code": sess.get("user_code"),
            "verification_uri": sess.get("verification_uri"),
            "verification_uri_complete": sess.get("verification_uri_complete"),
            "expires_in": sess.get("expires_in"),
            "message": sess.get("message"),
        }
        return {k: v for k, v in public.items() if v is not None}
    if oauth_configured(paths):
        return {"provider": name, "status": "complete"}
    return {"provider": name, "status": "idle"}


def start_login(
    paths: _CredRoot,
    *,
    provider: str = "grok",
    transport: Transport | None = None,
    spawn: bool = True,
) -> dict[str, Any]:
    name = provider.lower()
    if name not in PROVIDER_IDS:
        raise OAuthError(f"OAuth is not implemented for {provider}")
    t = transport or Transport()
    discovery = discover(t)
    device = request_device_code(t)
    session = {
        "status": "pending",
        "device_code": device["device_code"],
        "user_code": device["user_code"],
        "verification_uri": device["verification_uri"],
        "verification_uri_complete": device["verification_uri_complete"],
        "expires_in": int(device["expires_in"]),
        "interval": int(device["interval"]),
        "token_endpoint": discovery["token_endpoint"],
        "message": "Open the verification URL and approve access.",
    }
    with _lock:
        _sessions[name] = session
    if spawn:
        threading.Thread(
            target=_poll_until_done,
            args=(name, paths, t),
            daemon=True,
        ).start()
    return {
        "provider": name,
        "status": "pending",
        "user_code": session["user_code"],
        "verification_uri": session["verification_uri"],
        "verification_uri_complete": session["verification_uri_complete"],
        "expires_in": session["expires_in"],
        "message": session["message"],
    }


def finish_login(
    paths: _CredRoot,
    *,
    provider: str = "grok",
    transport: Transport | None = None,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Block until the in-flight device-code session completes (tests)."""
    return _poll_until_done(provider.lower(), paths, transport or Transport(), sleep=sleep)


def _poll_until_done(
    name: str,
    paths: _CredRoot,
    transport: Transport,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    with _lock:
        sess = dict(_sessions.get(name) or {})
    if not sess:
        raise OAuthError("no in-flight xAI OAuth session")
    try:
        tokens = poll_device_token(
            token_endpoint=sess["token_endpoint"],
            device_code=sess["device_code"],
            expires_in=int(sess["expires_in"]),
            interval=int(sess.get("interval") or 5),
            transport=transport,
            sleep=sleep,
        )
        save_tokens(paths, tokens, token_endpoint=sess["token_endpoint"])
        with _lock:
            current = _sessions.get(name)
            if current is not None:
                current["status"] = "complete"
                current["message"] = "Signed in to xAI Grok."
                current.pop("device_code", None)
        return status(name, paths)
    except Exception as exc:
        with _lock:
            current = _sessions.get(name)
            if current is not None:
                current["status"] = "error"
                current["message"] = str(exc)
        raise
