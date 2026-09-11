"""Connector sign-in for remote MCP servers (OAuth 2.1 authorization code).

Implements the MCP authorization spec against any compliant server, so the
user connects a service (Linear, Notion, ...) through that service's own
consent page instead of pasting an API key:

    RFC 9728  protected-resource metadata (find the authorization server)
    RFC 8414  authorization-server metadata (find the endpoints)
    RFC 7591  dynamic client registration (no pre-provisioned OAuth app)
    RFC 7636  PKCE (S256) on the authorization-code grant
    RFC 8707  resource indicator, so tokens are scoped to the MCP server

Stdlib only. Token bundles live in
`$HARNESS_HOME/credentials/connector_<id>_oauth` (JSON, chmod 600), next to
the api-key files the connector store already uses. In-flight flows are held
in memory keyed by the OAuth `state` value; the browser redirect (or the Mac
app's loopback relay) finishes them via `exchange`.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import secrets as pysecrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode, urlparse

from harness import netguard
from harness.fsutil import write_private
from harness.redaction import register_secret


class _CredRoot(Protocol):
    @property
    def credentials(self) -> Path: ...


CLIENT_NAME = "dotobot"
FLOW_TTL_SECONDS = 900
REFRESH_SKEW_SECONDS = 60


class OAuthError(RuntimeError):
    """An MCP connector OAuth step failed (discovery, registration, token)."""


class Transport:
    """Minimal HTTP surface so tests can stub the service's auth server."""

    def get_json(self, url: str) -> dict[str, Any]:
        # Discovery GETs go wherever the vendor's metadata points. Resolve
        # first and refuse loopback / link-local / private space so a
        # hostile MCP host cannot aim the serve process at the operator's
        # own network. Stubbed transports (tests) never reach this.
        _require_public_issuer(url, "discovery URL")
        req = urllib.request.Request(url, headers=_headers(), method="GET")
        return _read_json(req)

    def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        hdrs = _headers()
        hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        return _read_json(req)

    def post_form(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        body = urlencode(data).encode("utf-8")
        hdrs = _headers()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        return _read_json(req)


def _headers() -> dict[str, str]:
    return {"Accept": "application/json", "User-Agent": "dotobot/0.1"}


def _read_json(req: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        try:
            payload = json.loads(detail) if detail else {}
        except json.JSONDecodeError:
            raise OAuthError(f"HTTP {exc.code} from {req.full_url}: {detail}") from exc
        if isinstance(payload, dict) and payload.get("error"):
            payload["_http_status"] = exc.code
            return payload
        raise OAuthError(f"HTTP {exc.code} from {req.full_url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OAuthError(f"could not reach {req.full_url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise OAuthError(f"{req.full_url} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise OAuthError(f"{req.full_url} returned a non-object JSON payload")
    return payload


def _require_safe_url(url: str, field: str) -> str:
    """OAuth endpoints must be HTTPS (loopback HTTP allowed for tests/dev)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise OAuthError(f"{field} is not a valid URL: {url!r}")
    if parsed.scheme == "https" or host in {"localhost", "127.0.0.1", "::1"}:
        return url
    raise OAuthError(f"{field} is not HTTPS: {url!r}")


def _require_issuer_shape(url: str, field: str) -> str:
    """Literal check on a vendor-supplied issuer: HTTPS, and not a loopback
    or private address written out as a host. No resolution here — that is
    `_require_public_issuer`, on the real network path."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise OAuthError(f"{field} is not a valid URL: {url!r}")
    if parsed.scheme != "https":
        raise OAuthError(f"{field} is not HTTPS: {url!r}")
    if host == "localhost" or host.endswith(".localhost"):
        raise OAuthError(f"{field} is not a public host: {url!r}")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        return url
    if not netguard.is_public_address(literal):
        raise OAuthError(f"{field} is not a public host: {url!r}")
    return url


def _require_public_issuer(url: str, field: str) -> str:
    """`_require_issuer_shape` plus DNS: every resolved address must be public."""
    _require_issuer_shape(url, field)
    try:
        netguard.check_destination(url, https_only=True)
    except netguard.UnsafeDestination as exc:
        raise OAuthError(f"{field} {exc}") from exc
    except OSError as exc:
        raise OAuthError(f"could not resolve {field} {url!r}: {exc}") from exc
    return url


# -- discovery --------------------------------------------------------------


def discover(mcp_url: str, transport: Transport | None = None) -> dict[str, Any]:
    """Resolve an MCP server's authorization endpoints per the MCP auth spec.

    Returns authorization_endpoint / token_endpoint (required),
    registration_endpoint (may be empty) and scopes (space-joined, may be
    empty) from the protected-resource metadata.
    """
    t = transport or Transport()
    parsed = urlparse(mcp_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")

    issuer = origin
    scopes: list[str] = []
    candidates = [f"{origin}/.well-known/oauth-protected-resource"]
    if path:
        candidates.insert(0, f"{origin}/.well-known/oauth-protected-resource{path}")
    for url in candidates:
        try:
            prm = t.get_json(url)
        except OAuthError:
            continue
        servers = prm.get("authorization_servers")
        if isinstance(servers, list) and servers:
            # The issuer is the vendor's word, and the next three GETs are
            # built from it: an `http://127.0.0.1:<port>` or metadata
            # address here was a request from the serve process to wherever
            # the MCP host said. HTTPS to a public host, or no discovery.
            issuer = _require_issuer_shape(str(servers[0]).rstrip("/"), "authorization_servers[0]")
            raw = prm.get("scopes_supported")
            if isinstance(raw, list):
                scopes = [str(s) for s in raw if str(s).strip()]
            break

    iparsed = urlparse(issuer)
    iorigin = f"{iparsed.scheme}://{iparsed.netloc}"
    ipath = iparsed.path.rstrip("/")
    meta: dict[str, Any] | None = None
    for url in (
        f"{iorigin}/.well-known/oauth-authorization-server{ipath}",
        f"{iorigin}/.well-known/openid-configuration{ipath}",
        # origin + path, not the raw issuer string: a query string on the
        # vendor's issuer must not ride along into the well-known GET.
        f"{iorigin}{ipath}/.well-known/openid-configuration",
    ):
        try:
            candidate = t.get_json(url)
        except OAuthError:
            continue
        if candidate.get("authorization_endpoint") and candidate.get("token_endpoint"):
            meta = candidate
            break
    if meta is None:
        raise OAuthError(
            f"no OAuth authorization-server metadata found for {mcp_url} "
            "(the service may not support MCP OAuth sign-in)"
        )
    return {
        "authorization_endpoint": _require_safe_url(
            str(meta["authorization_endpoint"]), "authorization_endpoint"
        ),
        "token_endpoint": _require_safe_url(str(meta["token_endpoint"]), "token_endpoint"),
        "registration_endpoint": str(meta.get("registration_endpoint") or ""),
        "scopes": " ".join(scopes),
    }


def _oauth_client_path(paths: _CredRoot, connector_id: str) -> Path:
    return paths.credentials / f"connector_{connector_id}_oauth_client"


def load_oauth_client(paths: _CredRoot, connector_id: str) -> dict[str, str] | None:
    path = _oauth_client_path(paths, connector_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    client_id = str(data.get("client_id") or "").strip()
    if not client_id:
        return None
    return {
        "client_id": client_id,
        "client_secret": str(data.get("client_secret") or ""),
    }


def save_oauth_client(paths: _CredRoot, connector_id: str, client: dict[str, str]) -> None:
    paths.credentials.mkdir(parents=True, exist_ok=True)
    try:
        paths.credentials.chmod(0o700)
    except OSError:  # pragma: no cover - non-posix
        pass
    document = {
        "client_id": client["client_id"],
        "client_secret": client.get("client_secret") or "",
    }
    if document["client_secret"]:
        register_secret(document["client_secret"], f"connector_{connector_id}_client_secret")
    # Born 0600 and atomic (fsutil.write_private): never a wide or torn
    # credential file, not even between write and chmod.
    write_private(_oauth_client_path(paths, connector_id), json.dumps(document))


def _env_oauth_client(type_: str) -> dict[str, str] | None:
    key = str(type_ or "").strip().upper()
    if not key:
        return None
    client_id = (os.environ.get(f"HARNESS_MCP_OAUTH_{key}_CLIENT_ID") or "").strip()
    if not client_id:
        return None
    return {
        "client_id": client_id,
        "client_secret": (os.environ.get(f"HARNESS_MCP_OAUTH_{key}_CLIENT_SECRET") or "").strip(),
    }


def resolve_oauth_client(
    paths: _CredRoot,
    record: dict[str, Any],
    *,
    client_id: str = "",
    client_secret: str = "",
) -> dict[str, str] | None:
    """Pre-registered OAuth app (Slack and other non-DCR servers)."""
    connector_id = str(record.get("id") or "")
    config = record.get("config") if isinstance(record.get("config"), dict) else {}
    cid = (client_id or "").strip() or str(config.get("client_id") or "").strip()
    secret = (client_secret or "").strip()
    stored = load_oauth_client(paths, connector_id) if connector_id else None
    if cid:
        if not secret and stored and stored.get("client_id") == cid:
            secret = stored.get("client_secret") or ""
        client = {"client_id": cid, "client_secret": secret}
        if connector_id and (secret or stored):
            save_oauth_client(paths, connector_id, client)
        return client
    if stored:
        return stored
    return _env_oauth_client(str(record.get("type") or ""))


def register_client(
    metadata: dict[str, Any], redirect_uri: str, transport: Transport | None = None
) -> dict[str, str]:
    """Dynamically register this harness as an OAuth client (RFC 7591)."""
    endpoint = str(metadata.get("registration_endpoint") or "")
    if not endpoint:
        raise OAuthError(
            "the service's authorization server does not support dynamic client "
            "registration. Paste the app's Client ID and Client Secret, add this "
            f"redirect URL on the app: {redirect_uri}, then Retry"
        )
    t = transport or Transport()
    payload = t.post_json(
        _require_safe_url(endpoint, "registration_endpoint"),
        {
            "client_name": CLIENT_NAME,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )
    client_id = str(payload.get("client_id") or "").strip()
    if not client_id:
        desc = payload.get("error_description") or payload.get("error") or payload
        raise OAuthError(f"client registration failed: {desc}")
    return {"client_id": client_id, "client_secret": str(payload.get("client_secret") or "")}


# -- token store ------------------------------------------------------------


def _cred_path(paths: _CredRoot, connector_id: str) -> Path:
    return paths.credentials / f"connector_{connector_id}_oauth"


def connected(paths: _CredRoot, connector_id: str) -> bool:
    return _cred_path(paths, connector_id).is_file()


def load_tokens(paths: _CredRoot, connector_id: str) -> dict[str, Any] | None:
    path = _cred_path(paths, connector_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    _register_tokens(connector_id, data)
    return data


def _register_tokens(connector_id: str, bundle: dict[str, Any]) -> None:
    """Tell the redaction registry about a connector's bearer tokens so a
    token that reaches a log line or error payload is sentinelised.
    Registration never changes what is written to disk."""
    for field in ("access_token", "refresh_token"):
        value = bundle.get(field)
        if isinstance(value, str) and value:
            register_secret(value, f"connector_{connector_id}_{field}")


def save_tokens(paths: _CredRoot, connector_id: str, bundle: dict[str, Any]) -> None:
    paths.credentials.mkdir(parents=True, exist_ok=True)
    try:
        paths.credentials.chmod(0o700)
    except OSError:  # pragma: no cover - non-posix
        pass
    _register_tokens(connector_id, bundle)
    write_private(_cred_path(paths, connector_id), json.dumps(bundle))


def clear_tokens(paths: _CredRoot, connector_id: str) -> bool:
    """Sign a connector out: drop tokens and any in-flight flow."""
    with _lock:
        for state in [s for s, f in _flows.items() if f["connector_id"] == connector_id]:
            del _flows[state]
        _errors.pop(connector_id, None)
    removed = False
    for path in (
        _cred_path(paths, connector_id),
        _oauth_client_path(paths, connector_id),
    ):
        if path.is_file():
            path.unlink()
            removed = True
    return removed


def _ttl(expires_in: Any, default: int = 3600) -> int:
    try:
        return max(1, int(expires_in)) if expires_in is not None else default
    except (TypeError, ValueError):
        return default


def access_token(
    paths: _CredRoot,
    connector_id: str,
    *,
    force: bool = False,
    transport: Transport | None = None,
) -> str | None:
    """The connector's current access token, refreshing when stale."""
    data = load_tokens(paths, connector_id)
    if not data:
        return None
    access = str(data.get("access_token") or "").strip()
    refresh = str(data.get("refresh_token") or "").strip()
    expires_at = float(data.get("expires_at") or 0)
    stale = force or not access or (expires_at and time.time() >= expires_at - REFRESH_SKEW_SECONDS)
    if not stale:
        return access
    if not refresh:
        return access or None
    t = transport or Transport()
    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": str(data.get("client_id") or ""),
    }
    if data.get("client_secret"):
        form["client_secret"] = str(data["client_secret"])
    if data.get("resource"):
        form["resource"] = str(data["resource"])
    payload = t.post_form(str(data.get("token_endpoint") or ""), form)
    if payload.get("error"):
        desc = payload.get("error_description") or payload.get("error")
        raise OAuthError(f"token refresh failed: {desc}")
    new_access = str(payload.get("access_token") or "").strip()
    if not new_access:
        raise OAuthError("token refresh response was missing access_token")
    data.update(
        access_token=new_access,
        refresh_token=str(payload.get("refresh_token") or refresh).strip(),
        expires_at=time.time() + _ttl(payload.get("expires_in")),
    )
    save_tokens(paths, connector_id, data)
    return new_access


# -- in-flight flows (state -> pending exchange) ----------------------------

_flows: dict[str, dict[str, Any]] = {}
_errors: dict[str, str] = {}
_lock = threading.Lock()


def reset_flows() -> None:
    with _lock:
        _flows.clear()
        _errors.clear()


def _prune_locked(now: float) -> None:
    for state in [s for s, f in _flows.items() if now - f["created"] > FLOW_TTL_SECONDS]:
        del _flows[state]


def start_authorize(
    paths: _CredRoot,
    record: dict[str, Any],
    mcp_url: str,
    redirect_uri: str,
    transport: Transport | None = None,
    *,
    client_id: str = "",
    client_secret: str = "",
) -> dict[str, Any]:
    """Begin the connect flow: discovery + registration + authorize URL.

    Returns a payload the client uses to open the service's consent page in
    the user's browser; the redirect back carries `code` + `state`, which
    `exchange` (via /oauth/callback or the exchange endpoint) turns into
    stored tokens.
    """
    connector_id = str(record.get("id") or "")
    if not connector_id:
        raise OAuthError("connector record has no id")
    _require_safe_url(redirect_uri, "redirect_uri")
    t = transport or Transport()
    metadata = discover(mcp_url, t)

    static = resolve_oauth_client(paths, record, client_id=client_id, client_secret=client_secret)
    # Reuse the client registration from a previous connect when it targeted
    # the same token endpoint and redirect; register a fresh one otherwise.
    prior = load_tokens(paths, connector_id) or {}
    if static:
        client = static
    elif (
        prior.get("client_id")
        and prior.get("token_endpoint") == metadata["token_endpoint"]
        and prior.get("redirect_uri") == redirect_uri
    ):
        client = {
            "client_id": str(prior["client_id"]),
            "client_secret": str(prior.get("client_secret") or ""),
        }
    else:
        client = register_client(metadata, redirect_uri, t)

    verifier = base64.urlsafe_b64encode(pysecrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    state = pysecrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": mcp_url,
    }
    if metadata["scopes"]:
        params["scope"] = metadata["scopes"]
    try:
        from harness.connectors import CATALOG

        entry = next(
            (c for c in CATALOG if c.get("type") == str(record.get("type") or "")),
            {},
        )
        extra = entry.get("oauth_scopes")
        if extra:
            params["scope"] = " ".join(str(s) for s in extra if str(s).strip())
    except Exception:
        pass
    authz = str(metadata.get("authorization_endpoint") or "")
    if "accounts.google.com" in authz or "google.com/o/oauth2" in authz:
        # Google only issues a refresh token with offline + consent.
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    sep = "&" if "?" in metadata["authorization_endpoint"] else "?"
    authorize_url = metadata["authorization_endpoint"] + sep + urlencode(params)

    now = time.time()
    with _lock:
        _prune_locked(now)
        # One flow per connector: a re-click supersedes the previous attempt.
        for old in [s for s, f in _flows.items() if f["connector_id"] == connector_id]:
            del _flows[old]
        _errors.pop(connector_id, None)
        _flows[state] = {
            "connector_id": connector_id,
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "token_endpoint": metadata["token_endpoint"],
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "resource": mcp_url,
            "created": now,
        }
    return {
        "connector": connector_id,
        "status": "pending",
        "authorize_url": authorize_url,
        "state": state,
        "redirect_uri": redirect_uri,
        "message": "Approve access in your browser to connect.",
    }


def fail(state: str, message: str) -> str | None:
    """Record a denied/failed authorization for its connector; returns the id."""
    with _lock:
        flow = _flows.pop(state, None)
        if flow is None:
            return None
        _errors[flow["connector_id"]] = message or "authorization failed"
        return flow["connector_id"]


def exchange(
    paths: _CredRoot,
    state: str,
    code: str,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Finish a flow: swap the authorization code for tokens and store them.

    `state` is single-use and must match an in-flight flow — it is the CSRF
    check that lets the browser-facing callback stay unauthenticated.
    """
    if not code.strip():
        raise OAuthError("authorization response had no code")
    with _lock:
        _prune_locked(time.time())
        flow = _flows.pop(state, None)
    if flow is None:
        raise OAuthError("unknown or expired OAuth state; start the connect flow again")
    connector_id = flow["connector_id"]
    t = transport or Transport()
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": flow["redirect_uri"],
        "client_id": flow["client_id"],
        "code_verifier": flow["verifier"],
        "resource": flow["resource"],
    }
    if flow["client_secret"]:
        form["client_secret"] = flow["client_secret"]
    try:
        payload = t.post_form(flow["token_endpoint"], form)
    except OAuthError as exc:
        with _lock:
            _errors[connector_id] = str(exc)
        raise
    if payload.get("error") or not str(payload.get("access_token") or "").strip():
        desc = payload.get("error_description") or payload.get("error") or "no access_token"
        with _lock:
            _errors[connector_id] = f"token exchange failed: {desc}"
        raise OAuthError(f"token exchange failed: {desc}")
    save_tokens(
        paths,
        connector_id,
        {
            "access_token": str(payload["access_token"]).strip(),
            "refresh_token": str(payload.get("refresh_token") or "").strip(),
            "expires_at": time.time() + _ttl(payload.get("expires_in")),
            "token_type": str(payload.get("token_type") or "Bearer"),
            "token_endpoint": flow["token_endpoint"],
            "client_id": flow["client_id"],
            "client_secret": flow["client_secret"],
            "resource": flow["resource"],
            "redirect_uri": flow["redirect_uri"],
            "scope": str(payload.get("scope") or ""),
        },
    )
    with _lock:
        _errors.pop(connector_id, None)
    return status(paths, connector_id)


def status(paths: _CredRoot, connector_id: str) -> dict[str, Any]:
    """idle | pending | connected | error, for the UI to poll."""
    with _lock:
        _prune_locked(time.time())
        pending = any(f["connector_id"] == connector_id for f in _flows.values())
        error = _errors.get(connector_id)
    if connected(paths, connector_id):
        return {"connector": connector_id, "status": "connected"}
    if pending:
        return {
            "connector": connector_id,
            "status": "pending",
            "message": "Waiting for browser approval.",
        }
    if error:
        return {"connector": connector_id, "status": "error", "message": error}
    return {"connector": connector_id, "status": "idle"}
