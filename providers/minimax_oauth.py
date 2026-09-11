"""MiniMax subscription OAuth (user-code + PKCE, Hermes flow).

POST `{portal}/oauth/code` for a user_code + verification URL, then poll
`{portal}/oauth/token` with `urn:ietf:params:oauth:grant-type:user_code`
until the user approves. Same UI shape as Grok device-code.

Copied from Hermes (`hermes_cli/auth.py` MiniMax Portal OAuth). Tokens live
in `$HARNESS_HOME/credentials/MINIMAX_OAUTH`. Stdlib only. Inference is the
Anthropic-compatible MiniMax endpoint.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any
from urllib.parse import urlparse

from . import oauth_store
from .oauth_store import _CredRoot

CLIENT_ID = "78257093-7e40-4613-99e0-527b14b39113"
SCOPE = "group_id profile model.completion"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:user_code"
GLOBAL_PORTAL = "https://api.minimax.io"
CN_PORTAL = "https://api.minimaxi.com"
GLOBAL_INFERENCE = "https://api.minimax.io/anthropic"
CN_INFERENCE = "https://api.minimaxi.com/anthropic"
SECRET_NAME = "MINIMAX_OAUTH"
REFRESH_SKEW_SECONDS = 60
PROVIDER_IDS = frozenset({"minimax", "minimax-oauth"})


class OAuthError(RuntimeError):
    """Raised when the MiniMax user-code flow fails."""


class Transport:
    """Minimal HTTP surface so tests can stub MiniMax OAuth."""

    def post_form(self, url: str, data: dict[str, str], headers: dict[str, str] | None = None) -> dict[str, Any]:
        body = urllib.parse.urlencode(data).encode("utf-8")
        hdrs = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "dotobot/0.1",
        }
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        return _read_json(req)


def _read_json(req: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(detail) if detail else {}
        except json.JSONDecodeError:
            raise OAuthError(f"MiniMax HTTP {exc.code}: {detail}") from exc
        if isinstance(payload, dict):
            payload["_http_status"] = exc.code
            return payload
        raise OAuthError(f"MiniMax HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise OAuthError(f"MiniMax request failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise OAuthError("MiniMax returned non-JSON") from exc
    if not isinstance(payload, dict):
        raise OAuthError("MiniMax returned a non-object JSON payload")
    return payload


def _require_minimax_https(url: str, field: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        raise OAuthError(f"MiniMax {field} is not HTTPS: {url!r}")
    allowed = ("minimax.io", "minimaxi.com")
    if host not in allowed and not any(host.endswith("." + a) for a in allowed):
        raise OAuthError(f"MiniMax {field} host {host!r} is not on minimax.io / minimaxi.com")
    return url


def _portals(region: str) -> tuple[str, str]:
    if region == "cn":
        return CN_PORTAL, CN_INFERENCE
    return GLOBAL_PORTAL, GLOBAL_INFERENCE


def request_user_code(
    *,
    portal_base_url: str,
    code_challenge: str,
    state: str,
    transport: Transport | None = None,
) -> dict[str, Any]:
    t = transport or Transport()
    payload = t.post_form(
        f"{portal_base_url}/oauth/code",
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "scope": SCOPE,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        },
        headers={"x-request-id": str(uuid.uuid4())},
    )
    if payload.get("_http_status"):
        raise OAuthError(f"MiniMax OAuth authorization failed: {payload}")
    for field in ("user_code", "verification_uri", "expired_in"):
        if field not in payload:
            raise OAuthError(f"MiniMax OAuth response missing {field}")
    if payload.get("state") not in (None, "", state):
        raise OAuthError("MiniMax OAuth state mismatch")
    _require_minimax_https(str(payload["verification_uri"]), "verification_uri")
    return payload


def _deadline(expired_in: int) -> float:
    now_ms = int(time.time() * 1000)
    raw = int(expired_in)
    if raw > now_ms // 2:
        return raw / 1000.0
    return time.time() + max(1, raw)


def poll_token(
    *,
    portal_base_url: str,
    user_code: str,
    code_verifier: str,
    expired_in: int,
    interval: float = 2.0,
    transport: Transport | None = None,
    sleep: Any = time.sleep,
    now: Any = time.time,
) -> dict[str, Any]:
    t = transport or Transport()
    deadline = _deadline(expired_in)
    wait = max(2.0, float(interval))
    while now() < deadline:
        payload = t.post_form(
            f"{portal_base_url}/oauth/token",
            {
                "grant_type": GRANT_TYPE,
                "client_id": CLIENT_ID,
                "user_code": user_code,
                "code_verifier": code_verifier,
            },
        )
        if payload.get("_http_status"):
            msg = ((payload.get("base_resp") or {}) if isinstance(payload.get("base_resp"), dict) else {}).get(
                "status_msg"
            ) or payload.get("error") or payload
            raise OAuthError(f"MiniMax OAuth error: {msg}")
        status = payload.get("status")
        if status == "error":
            raise OAuthError("MiniMax OAuth reported an error. Please try again later.")
        if status == "success":
            if not payload.get("access_token") or not payload.get("refresh_token"):
                raise OAuthError("MiniMax OAuth success payload missing tokens")
            return payload
        sleep(wait)
    raise OAuthError("Timed out waiting for MiniMax authorization")


def refresh_tokens(
    refresh_token: str,
    *,
    portal_base_url: str,
    transport: Transport | None = None,
) -> dict[str, Any]:
    if not refresh_token.strip():
        raise OAuthError("MiniMax OAuth is missing a refresh_token; sign in again")
    t = transport or Transport()
    payload = t.post_form(
        f"{portal_base_url}/oauth/token",
        {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
        },
    )
    if payload.get("_http_status") or payload.get("status") not in (None, "success"):
        raise OAuthError(f"MiniMax token refresh failed: {payload}")
    access = str(payload.get("access_token") or "").strip()
    if not access:
        raise OAuthError("MiniMax token refresh response was missing access_token")
    return payload


def oauth_configured(paths: _CredRoot) -> bool:
    return oauth_store.cred_path(paths, SECRET_NAME).is_file()


def load_tokens(paths: _CredRoot) -> dict[str, Any] | None:
    return oauth_store.load_tokens(paths, SECRET_NAME)


def save_tokens(paths: _CredRoot, tokens: dict[str, Any], *, extra: dict[str, Any]) -> None:
    expires_in = tokens.get("expires_in")
    expired_in = tokens.get("expired_in")
    if expires_in is None and expired_in is not None:
        deadline = _deadline(int(expired_in))
        tokens = {**tokens, "expires_at": deadline, "expires_in": max(1, int(deadline - time.time()))}
    oauth_store.save_tokens(paths, SECRET_NAME, tokens, extra=extra, default_ttl=21600)


def clear_tokens(paths: _CredRoot) -> bool:
    reset_sessions()
    return oauth_store.clear_tokens(paths, SECRET_NAME)


def inference_base(paths: _CredRoot) -> str:
    data = load_tokens(paths) or {}
    url = str(data.get("inference_base_url") or "").strip()
    return url or GLOBAL_INFERENCE


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
        portal = str(data.get("portal_base_url") or GLOBAL_PORTAL)
        updated = refresh_tokens(refresh, portal_base_url=portal, transport=transport)
        extra = {
            "portal_base_url": portal,
            "inference_base_url": data.get("inference_base_url") or GLOBAL_INFERENCE,
            "region": data.get("region") or "global",
            "client_id": CLIENT_ID,
        }
        save_tokens(paths, updated, extra=extra)
        return str(updated["access_token"])
    return access


_sessions: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def reset_sessions() -> None:
    with _lock:
        _sessions.clear()


def status(provider: str, paths: _CredRoot) -> dict[str, Any]:
    name = "minimax"
    with _lock:
        sess = dict(_sessions.get(name) or {})
    if sess:
        public = {
            "provider": name,
            "status": sess.get("status", "pending"),
            "user_code": sess.get("user_code"),
            "verification_uri": sess.get("verification_uri"),
            "verification_uri_complete": sess.get("verification_uri"),
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
    provider: str = "minimax",
    region: str = "global",
    transport: Transport | None = None,
    spawn: bool = True,
) -> dict[str, Any]:
    name = provider.lower()
    if name not in PROVIDER_IDS:
        raise OAuthError(f"OAuth is not implemented for {provider}")
    portal, inference = _portals(region)
    verifier, challenge = oauth_store.pkce_pair()
    t = transport or Transport()
    device = request_user_code(
        portal_base_url=portal, code_challenge=challenge, state=verifier, transport=t
    )
    interval_raw = device.get("interval")
    try:
        interval = float(interval_raw) if interval_raw is not None else 2.0
    except (TypeError, ValueError):
        interval = 2.0
    if interval > 20:
        interval = interval / 1000.0  # MiniMax sometimes sends milliseconds
    session = {
        "status": "pending",
        "user_code": device["user_code"],
        "verification_uri": device["verification_uri"],
        "expired_in": int(device["expired_in"]),
        "interval": interval,
        "code_verifier": verifier,
        "portal_base_url": portal,
        "inference_base_url": inference,
        "region": region,
        "message": "Open the verification URL and approve access.",
    }
    with _lock:
        _sessions["minimax"] = session
    if spawn:
        threading.Thread(
            target=_poll_until_done,
            args=("minimax", paths, t),
            daemon=True,
        ).start()
    return {
        "provider": "minimax",
        "status": "pending",
        "user_code": session["user_code"],
        "verification_uri": session["verification_uri"],
        "verification_uri_complete": session["verification_uri"],
        "expires_in": 900,
        "message": session["message"],
    }


def finish_login(
    paths: _CredRoot,
    *,
    provider: str = "minimax",
    transport: Transport | None = None,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    return _poll_until_done("minimax", paths, transport or Transport(), sleep=sleep)


def _poll_until_done(
    name: str,
    paths: _CredRoot,
    transport: Transport,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    with _lock:
        sess = dict(_sessions.get(name) or {})
    if not sess:
        raise OAuthError("no in-flight MiniMax OAuth session")
    try:
        tokens = poll_token(
            portal_base_url=sess["portal_base_url"],
            user_code=sess["user_code"],
            code_verifier=sess["code_verifier"],
            expired_in=int(sess["expired_in"]),
            interval=float(sess.get("interval") or 2),
            transport=transport,
            sleep=sleep,
        )
        extra = {
            "portal_base_url": sess["portal_base_url"],
            "inference_base_url": sess["inference_base_url"],
            "region": sess.get("region") or "global",
            "client_id": CLIENT_ID,
        }
        save_tokens(paths, tokens, extra=extra)
        with _lock:
            current = _sessions.get(name)
            if current is not None:
                current["status"] = "complete"
                current["message"] = "Signed in to MiniMax."
                current.pop("code_verifier", None)
        return status(name, paths)
    except Exception as exc:
        with _lock:
            current = _sessions.get(name)
            if current is not None:
                current["status"] = "error"
                current["message"] = str(exc)
        raise
