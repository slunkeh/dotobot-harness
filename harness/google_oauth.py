"""Google Workspace consent using registered Google clients and shared token storage.

Client secrets belong only on the operator's server, never in the public app.
The Google account-login client is independent of these API consent grants.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from . import mcp_oauth

SCOPES = {
    "gmail": ["https://mail.google.com/"],
    "google_calendar": ["https://www.googleapis.com/auth/calendar"],
    "google_drive": ["https://www.googleapis.com/auth/drive"],
    "google_sheets": ["https://www.googleapis.com/auth/spreadsheets"],
}
IOS_REDIRECT = "com.dotobot.ios:/oauth/google"


def supported(type_: str) -> bool:
    return type_ in SCOPES


def start_authorize(paths, record, redirect_uri, *, transport=None, client_id="", client_secret=""):
    type_ = str(record.get("type") or "")
    if not supported(type_):
        raise mcp_oauth.OAuthError("Unsupported Google Workspace connector")
    parsed = urlparse(redirect_uri)
    if redirect_uri == IOS_REDIRECT:
        prefix = "GOOGLE_IOS"
    elif parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}:
        prefix = "GOOGLE_DESKTOP"
    elif parsed.scheme == "https":
        prefix = "GOOGLE_WEB"
    else:
        raise mcp_oauth.OAuthError("Unsupported Google OAuth callback")
    if not client_id:
        client_id = os.environ.get(prefix + "_CLIENT_ID", "")
        client_secret = client_secret or os.environ.get(prefix + "_CLIENT_SECRET", "")
    if not client_id:
        raise mcp_oauth.OAuthError(
            "Google Workspace sign-in is not configured on this server: "
            f"the operator must set {prefix}_CLIENT_ID for a registered Google client."
        )
    return mcp_oauth.start_authorize(
        paths,
        record,
        "",
        redirect_uri,
        transport,
        client_id=client_id,
        client_secret=client_secret,
        google_scopes=SCOPES[type_],
    )


def token(paths, record):
    """Prefer OAuth once connected; never silently downgrade a failed grant."""
    if not supported(str(record.get("type") or "")):
        return None
    cid = str(record.get("id") or "")
    if not mcp_oauth.load_tokens(paths, cid):
        raise mcp_oauth.OAuthError("Connect this Google account in Manage > Plugins.")
    try:
        value = mcp_oauth.access_token(paths, cid)
    except mcp_oauth.OAuthError:
        raise mcp_oauth.OAuthError(
            "Google access expired or was revoked. Reconnect this Google account."
        ) from None
    if not value:
        raise mcp_oauth.OAuthError("Reconnect this Google account.")
    return value
