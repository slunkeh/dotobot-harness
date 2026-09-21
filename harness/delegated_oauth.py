"""Account-scoped credentials delegated by an authenticated control plane.

The runtime never performs provider consent or stores provider refresh tokens.
Only a revocable capability and the public broker URL are persisted locally.
"""
from __future__ import annotations

import json
import urllib.request
from urllib.parse import urlsplit

from . import mcp_oauth, netguard
from .redaction import register_secret
from .secrets import delete_secret


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(bundle, connector_id, service, action="token"):
    url = str(bundle.get("broker_url") or "").rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise mcp_oauth.OAuthError("Invalid connection service URL.")
    try:
        netguard.check_destination(url, https_only=True)
        req = urllib.request.Request(url + "/" + action, data=json.dumps({
            "grant_id": bundle["grant_id"], "connector_id": connector_id, "service": service,
        }).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + bundle["capability"]})
        with urllib.request.build_opener(NoRedirect).open(req, timeout=20) as response:
            value = json.loads(response.read(65537))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError, KeyError):
        raise mcp_oauth.OAuthError("Connection unavailable. Reconnect this account through your app.") from None


def connected(paths, connector_id):
    bundle = mcp_oauth.load_tokens(paths, connector_id) or {}
    return bundle.get("mode") == "delegated" and bool(bundle.get("capability"))


def install(paths, record, value):
    fields = ("broker_url", "grant_id", "capability")
    if any(not isinstance(value.get(k), str) or not 1 <= len(value[k]) <= 2048 for k in fields):
        raise mcp_oauth.OAuthError("Invalid delegated connection.")
    bundle = {k: value[k] for k in fields}
    # Probe before persisting; the issuer verifies connector and service binding.
    result = request(bundle, record["id"], record["type"])
    if result.get("service") != record["type"] or not result.get("access_token"):
        raise mcp_oauth.OAuthError("Connection does not match this app.")
    bundle.update(mode="delegated", email=str(result.get("email") or ""))
    previous = mcp_oauth.load_tokens(paths, record["id"]) or {}
    if previous.get("mode") == "delegated" and previous.get("grant_id") != bundle["grant_id"]:
        request(previous, record["id"], record["type"], "revoke")
    mcp_oauth.clear_tokens(paths, record["id"])
    mcp_oauth.save_tokens(paths, record["id"], bundle)
    delete_secret(f"connector_{record['id']}", paths)
    return bundle["email"]


def token(paths, record):
    bundle = mcp_oauth.load_tokens(paths, record["id"]) or {}
    if bundle.get("mode") != "delegated":
        raise mcp_oauth.OAuthError("Reconnect this account through Dotobot. The previous sign-in is no longer supported.")
    value = request(bundle, record["id"], record["type"])
    access = value.get("access_token")
    if not isinstance(access, str) or not access or value.get("service") != record["type"]:
        raise mcp_oauth.OAuthError("Reconnect this account through your app.")
    register_secret(access, "connector_access_token")
    return access


def disconnect(paths, record):
    bundle = mcp_oauth.load_tokens(paths, record["id"]) or {}
    if bundle.get("mode") == "delegated":
        request(bundle, record["id"], record["type"], "revoke")
    return mcp_oauth.clear_tokens(paths, record["id"])
