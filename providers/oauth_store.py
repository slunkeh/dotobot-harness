"""Shared OAuth token persistence for provider adapters.

Tokens live in `$HARNESS_HOME/credentials/<SECRET>` as JSON, chmod 600.
Nothing here is logged. Each provider owns its own start/refresh flow and
calls these helpers for the on-disk shape.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Protocol


class _CredRoot(Protocol):
    @property
    def credentials(self) -> Path: ...


def pkce_pair() -> tuple[str, str]:
    """RFC 7636 S256 (verifier, challenge). Stdlib only."""
    import base64
    import hashlib
    import secrets

    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def cred_path(paths: _CredRoot, secret_name: str) -> Path:
    return paths.credentials / secret_name


def _register(secret_name: str, document: dict[str, Any]) -> None:
    """Tell the redaction registry about the bearer tokens in `document`.

    A token that ever reaches a log line, tool result, or error payload is
    then sentinelised like an API key; registration never changes
    what is written to disk.
    """
    from harness.redaction import register_secret

    for field in ("access_token", "refresh_token"):
        value = document.get(field)
        if isinstance(value, str) and value:
            register_secret(value, f"{secret_name}_{field.upper()}")


def load_tokens(paths: _CredRoot, secret_name: str) -> dict[str, Any] | None:
    path = cred_path(paths, secret_name)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    _register(secret_name, data)
    return data


def save_tokens(
    paths: _CredRoot,
    secret_name: str,
    tokens: dict[str, Any],
    *,
    extra: dict[str, Any] | None = None,
    default_ttl: int = 21600,
) -> None:
    paths.credentials.mkdir(parents=True, exist_ok=True)
    try:
        paths.credentials.chmod(0o700)
    except OSError:  # pragma: no cover - non-posix
        pass
    expires_in = tokens.get("expires_in")
    try:
        ttl = int(expires_in) if expires_in is not None else default_ttl
    except (TypeError, ValueError):
        ttl = default_ttl
    if "expires_at" in tokens:
        expires_at = float(tokens["expires_at"])
    else:
        expires_at = time.time() + max(1, ttl)
    payload: dict[str, Any] = {
        "access_token": tokens["access_token"],
        "refresh_token": str(tokens.get("refresh_token") or "").strip(),
        "expires_at": expires_at,
        "token_type": tokens.get("token_type") or "Bearer",
    }
    if extra:
        payload.update(extra)
    _register(secret_name, payload)
    path = cred_path(paths, secret_name)
    _write_private(path, payload)


def clear_tokens(paths: _CredRoot, secret_name: str) -> bool:
    path = cred_path(paths, secret_name)
    if path.is_file():
        path.unlink()
        return True
    return False


def _write_private(path: Path, document: dict[str, Any]) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(document) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover
        pass
