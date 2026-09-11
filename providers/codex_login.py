"""Codex CLI (ChatGPT) login reuse.

The harness never runs its own ChatGPT OAuth flow. If the host's Codex CLI is
signed in with a ChatGPT plan (`codex login`), we reuse `$CODEX_HOME/auth.json`
(default `~/.codex/auth.json`): read the tokens, refresh them against
auth.openai.com when they expire (the OAuth client id is recovered from the
`id_token`'s `aud` claim — no client secret is ever stored), and write the
rotated file back atomically so the CLI and the harness share one login.

Reimplements grok-bot's `codexCredentials` / `codexAuthenticatedFetch` /
`jwtAudience` in repo style; stdlib only. The permission gate runs *before*
any read: `auth.json` must be a regular non-symlink file with no group/other
permission bits, or we refuse with an actionable error.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import stat
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from .base import ProviderError

TOKEN_URL = "https://auth.openai.com/oauth/token"
SIGN_IN_HINT = (
    "Codex is not signed in with ChatGPT. Run `codex login` on the harness host, then retry."
)
EXPIRED_HINT = (
    "Codex login expired and could not be refreshed. "
    "Run `codex login` on the harness host, then retry."
)
T = TypeVar("T")


class CodexLoginError(ProviderError):
    """Raised when the Codex CLI login is missing, unsafe to read, or expired."""


def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME", "").strip()
    return Path(env) if env else Path.home() / ".codex"


def auth_path(home: Path | None = None) -> Path:
    return (home or codex_home()) / "auth.json"


@dataclass
class CodexCredentials:
    """The ChatGPT tokens the Codex CLI stored, plus the file they came from."""

    access_token: str
    refresh_token: str
    id_token: str
    account_id: str
    path: Path
    document: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:  # never leak tokens in logs / tracebacks
        return f"CodexCredentials(path={str(self.path)!r}, tokens=set)"


def _gate(path: Path) -> None:
    """Refuse to read credentials that are not a private regular file.

    Runs before any open(): symlinks and group/other-accessible files are
    rejected so the harness cannot be pointed at (or leak through) a file the
    Codex CLI would itself refuse to trust.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise CodexLoginError(SIGN_IN_HINT) from None
    except OSError as exc:
        raise CodexLoginError(f"Cannot read Codex login at {path}: {exc}") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise CodexLoginError(
            f"Refusing to read Codex login: {path} must be a regular file, not a symlink."
        )
    if st.st_mode & 0o077:
        raise CodexLoginError(
            f"Refusing to read Codex login: {path} is group/world accessible "
            f"(mode {stat.S_IMODE(st.st_mode):04o}). Run `chmod 600 {path}`, then retry."
        )


def load_credentials(home: Path | None = None) -> CodexCredentials:
    """Read `$CODEX_HOME/auth.json` through the permission gate.

    Requires a ChatGPT sign-in (`auth_mode == "chatgpt"`) with all four token
    fields present; API-key-only Codex configs raise the sign-in hint.
    """
    path = auth_path(home)
    _gate(path)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CodexLoginError(f"Cannot read Codex login at {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CodexLoginError(SIGN_IN_HINT) from exc
    if not isinstance(parsed, dict):
        raise CodexLoginError(SIGN_IN_HINT)
    tokens = parsed.get("tokens")
    if not isinstance(tokens, dict):
        tokens = {}
    fields = {
        name: str(tokens.get(name) or "").strip()
        for name in ("access_token", "refresh_token", "id_token", "account_id")
    }
    if parsed.get("auth_mode") != "chatgpt" or not all(fields.values()):
        raise CodexLoginError(SIGN_IN_HINT)
    return CodexCredentials(path=path, document=parsed, **fields)


def login_available(home: Path | None = None) -> bool:
    """Non-raising probe: is a usable ChatGPT login on disk?"""
    try:
        load_credentials(home)
    except CodexLoginError:
        return False
    return True


def status(home: Path | None = None) -> dict[str, Any]:
    """Status payload for the server's provider-OAuth endpoints."""
    try:
        load_credentials(home)
    except CodexLoginError as exc:
        return {"provider": "codex", "status": "cli", "message": str(exc)}
    return {
        "provider": "codex",
        "status": "complete",
        "message": "Signed in by reusing the Codex CLI ChatGPT login.",
    }


def jwt_audience(token: str) -> str | None:
    """The `aud` claim of a JWT's payload — the OAuth client id Codex used.

    Base64url-decodes the middle segment only; no signature check (we merely
    recover the public client id so the refresh grant needs no stored secret).
    """
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload_b64 = parts[1]
    try:
        raw = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    aud = payload.get("aud") if isinstance(payload, dict) else None
    if isinstance(aud, str) and aud:
        return aud
    if isinstance(aud, list):
        for item in aud:
            if isinstance(item, str) and item:
                return item
    return None


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
                "User-Agent": "dotobot/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise CodexLoginError(EXPIRED_HINT) from exc
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise CodexLoginError(f"Codex token refresh failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise CodexLoginError(EXPIRED_HINT)
        return payload


def refresh_credentials(
    current: CodexCredentials, *, transport: Transport | None = None
) -> CodexCredentials:
    """Refresh the ChatGPT tokens and rewrite auth.json atomically.

    The rotated file keeps the old refresh/id token whenever the response
    omits them, and lands via a 0600 O_EXCL temp file + os.replace so the
    Codex CLI never observes a partial write.
    """
    client_id = jwt_audience(current.id_token)
    if not client_id:
        raise CodexLoginError(
            "Codex login expired and its refresh identity is invalid. "
            "Run `codex login` on the harness host, then retry."
        )
    t = transport or Transport()
    payload = t.post_form(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": current.refresh_token,
            "client_id": client_id,
        },
    )
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise CodexLoginError(
            "Codex returned an invalid refreshed login. "
            "Run `codex login` on the harness host, then retry."
        )
    tokens = dict(current.document.get("tokens") or {})
    tokens["access_token"] = access
    for name in ("refresh_token", "id_token"):
        value = payload.get(name)
        if isinstance(value, str) and value:
            tokens[name] = value  # else: preserve what the CLI stored
    document = {
        **current.document,
        "tokens": tokens,
        "last_refresh": datetime.now(UTC).isoformat(),
    }
    _write_private(current.path, document)
    return load_credentials(current.path.parent)


def _write_private(path: Path, document: dict[str, Any]) -> None:
    """Atomic private write: same-directory temp opened O_EXCL at 0600, then
    os.replace over the original."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(document, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def retry_once_on_401(call: Callable[[], T], refresh: Callable[[], None]) -> T:
    """Run a provider HTTP call; on HTTP 401 refresh the login and retry once.

    Anything other than a 401 (including a 401 on the retried call) propagates
    unchanged — refresh happens at most once per wrapped call.
    """
    try:
        return call()
    except ProviderError as exc:
        if not _is_unauthorized(exc):
            raise
        refresh()
        return call()


def _is_unauthorized(exc: BaseException) -> bool:
    cause = exc.__cause__
    return isinstance(cause, urllib.error.HTTPError) and cause.code == 401


# -- optional niceties from ~/.codex/config.toml ----------------------------
def cli_config(home: Path | None = None) -> dict[str, Any]:
    path = (home or codex_home()) / "config.toml"
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def configured_model(home: Path | None = None) -> str | None:
    value = cli_config(home).get("model")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def configured_reasoning_effort(home: Path | None = None) -> str | None:
    value = cli_config(home).get("model_reasoning_effort")
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return None
