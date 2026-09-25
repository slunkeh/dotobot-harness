"""Minimal secrets access (thin; encryption-at-rest is later).

Lookup order for a named secret:
    1. Environment variable (e.g. ANTHROPIC_API_KEY).
    2. A file `credentials/<name>` under the harness home (chmod 600 expected).

Secrets are never printed or logged. The orchestrator/agent request secrets by
name; keys are not injected as env into every bot by default.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .fsutil import write_private
from .paths import HarnessPaths
from .redaction import register_secret

#: A secret name is one bare filename under credentials/: it starts with a
#: letter or digit and is otherwise limited to [A-Za-z0-9._-]. That rules
#: out separators, NUL, hidden files, `.` and `..` — a name can never be a
#: path. Names reach this module from the API (`/api/providers/<id>/key`),
#: the roster (`auth_ref`) and the model (`get_secret` / `request_secret` /
#: `computer_type_secret`), and every one of them used to be joined straight
#: onto credentials/ (`../serve.json` read the linking key; `../../x` wrote
#: outside the home).
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

# OAuth token files (JSON) that count as "configured" alongside API keys.
_OAUTH_FILES = {
    "grok": "XAI_OAUTH",
    "xai": "XAI_OAUTH",
    "xai-oauth": "XAI_OAUTH",
    "claude": "ANTHROPIC_OAUTH",
    "anthropic": "ANTHROPIC_OAUTH",
    "codex": "CODEX_OAUTH",
    "openai": "CODEX_OAUTH",
    "codex-chatgpt": "CODEX_OAUTH",
    "chatgpt": "CODEX_OAUTH",
    "minimax": "MINIMAX_OAUTH",
    "minimax-oauth": "MINIMAX_OAUTH",
}

# Roster auth-ref -> conventional environment variable name.
_ENV_ALIASES = {
    "anthropic": "ANTHROPIC_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "codex": "OPENAI_API_KEY",
    "grok": "XAI_API_KEY",
    "xai": "XAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "glm": "GLM_API_KEY",
    "zai": "GLM_API_KEY",
    "kimi": "KIMI_API_KEY",
    "minimax": "MINIMAX_API_KEY",
}


def resolve_env_name(ref: str) -> str:
    return _ENV_ALIASES.get(ref.lower(), ref)


class SecretNameError(ValueError):
    """A secret name that is not a bare filename (path-shaped, hidden, empty)."""


def valid_secret_name(name: object) -> bool:
    """True when `name` can only ever be one file directly under credentials/."""
    return isinstance(name, str) and _NAME_RE.fullmatch(name) is not None


def _cred_file(paths: HarnessPaths, candidate: str) -> Path | None:
    """credentials/<candidate>, or None unless it is a bare name that resolves
    to a direct child of the credentials directory (no traversal, no symlink
    pointing out of it)."""
    if not valid_secret_name(candidate):
        return None
    root = paths.credentials
    path = root / candidate
    try:
        if path.resolve().parent != root.resolve():
            return None
    except OSError:
        return None
    return path


# ENV fragments kept uppercase when humanizing a store key into a card title.
_ACRONYMS = {
    "API",
    "AWS",
    "DB",
    "ID",
    "IMAP",
    "URL",
    "URI",
    "SSH",
    "SMTP",
    "PAT",
    "OTP",
    "SQL",
    "HTTP",
    "HTTPS",
    "TLS",
    "JWT",
    "VPN",
}

_TAIL_WORDS = {"token", "key", "password", "secret", "passphrase"}


def secret_display_title(name: str, title: str | None = None) -> str:
    """Human label for any secret card. Prefer an explicit title; never log values."""
    raw = (title or "").strip()
    if raw:
        return raw
    key = (name or "").strip()
    if not key:
        return "Secret"
    words: list[str] = []
    for part in key.replace("-", "_").split("_"):
        if not part:
            continue
        upper = part.upper()
        if upper in _ACRONYMS:
            words.append(upper)
        else:
            words.append(part[:1].upper() + part[1:].lower())
    if not words:
        return "Secret"
    if words[-1].lower() in _TAIL_WORDS:
        words[-1] = words[-1].lower()
    return " ".join(words)


def get_secret(name: str, paths: HarnessPaths | None = None) -> str | None:
    """Return a secret value by name, or None if not found. Never logs it.

    A name that is not a bare filename is simply "not found": the store
    fails closed rather than resolving a path somebody typed.
    """
    if not valid_secret_name(name):
        return None
    env_name = resolve_env_name(name)
    value = os.environ.get(env_name) or os.environ.get(name)
    if value:
        register_secret(value.strip(), env_name)
        return value.strip()

    if paths is not None:
        for candidate in (name, env_name):
            path = _cred_file(paths, candidate)
            if path is not None and path.is_file():
                value = path.read_text(encoding="utf-8").strip()
                # Every resolution (re-)registers the value so the scrubber
                # knows it even when the store predates this process.
                register_secret(value, env_name)
                return value
    return None


def has_secret(name: str, paths: HarnessPaths | None = None) -> bool:
    return get_secret(name, paths) is not None


def credential_fingerprint(name: str, paths: HarnessPaths) -> str:
    """Private consent version; never return this digest through a tool or API."""
    import hashlib

    value = get_secret(name, paths)
    if not value:
        return ""
    return hashlib.sha256((resolve_env_name(name) + "\0" + value).encode()).hexdigest()


def secret_source(name: str, paths: HarnessPaths | None = None) -> str | None:
    """Return where a secret comes from ('env' | 'file') without revealing it."""
    if not valid_secret_name(name):
        return None
    env_name = resolve_env_name(name)
    if os.environ.get(env_name) or os.environ.get(name):
        return "env"
    if paths is not None:
        for candidate in (name, env_name):
            path = _cred_file(paths, candidate)
            if path is not None and path.is_file():
                return "file"
        oauth_name = _OAUTH_FILES.get(name.lower())
        if oauth_name and (paths.credentials / oauth_name).is_file():
            return "file"
    return None


def set_secret(name: str, value: str, paths: HarnessPaths) -> None:
    """Store a secret in the credentials store (born 0600, atomic). Never logged.

    Raises SecretNameError for a name that is not a bare filename; the
    caller (an API route, a tool handler) turns that into a 400 / a tool
    error. Nothing is written, registered, or created for a bad name.
    """
    if not valid_secret_name(name):
        raise SecretNameError(f"invalid secret name {name!r}")
    env_name = resolve_env_name(name)
    paths.credentials.mkdir(parents=True, exist_ok=True)
    try:
        paths.credentials.chmod(0o700)
    except OSError:  # pragma: no cover - non-posix
        pass
    path = _cred_file(paths, env_name)
    if path is None:
        raise SecretNameError(f"invalid secret name {name!r}")
    if get_secret(name, paths) != value.strip():
        _revoke_routine_consents(name, paths)
    # Store-write is the registration point: from here on the
    # scrubber replaces the value (raw / URL-encoded / JSON-escaped) with
    # its sentinel in logs, streams, and error payloads.
    register_secret(value.strip(), env_name)
    write_private(path, value.strip())
    from .machine_secrets import refresh_grants

    refresh_grants(paths, env_name)


def _revoke_routine_consents(name: str, paths: HarnessPaths) -> None:
    env_name = resolve_env_name(name)
    from .approvals import ApprovalStore

    for approval_file in paths.control.glob("approvals-*.json"):
        bot = approval_file.stem.removeprefix("approvals-")
        store = ApprovalStore(paths, bot)
        store.revoke_credential(env_name)
        if name != env_name:
            store.revoke_credential(name)


def delete_secret(name: str, paths: HarnessPaths) -> bool:
    """Remove a stored API-key file. Returns True if a file was removed.

    OAuth token files are left alone; use delete_oauth for those.
    """
    if not valid_secret_name(name):
        return False
    env_name = resolve_env_name(name)
    # Remove consent first; a storage failure must not revive it on re-add.
    _revoke_routine_consents(name, paths)
    removed = False
    for candidate in (name, env_name):
        path = _cred_file(paths, candidate)
        if path is not None and path.is_file():
            path.unlink()
            removed = True
        from .machine_secrets import refresh_grants

        refresh_grants(paths, candidate, remove=True)
    return removed


def delete_oauth(name: str, paths: HarnessPaths) -> bool:
    """Remove a provider's stored OAuth tokens. Returns True if any were removed."""
    key = name.lower()
    oauth_name = _OAUTH_FILES.get(key)
    if oauth_name == "XAI_OAUTH":
        from providers import xai_oauth

        xai_oauth.reset_sessions()
        return xai_oauth.clear_tokens(paths)
    if oauth_name == "ANTHROPIC_OAUTH":
        from providers import anthropic_oauth

        return anthropic_oauth.clear_tokens(paths)
    if oauth_name == "CODEX_OAUTH":
        from providers import codex_oauth

        return codex_oauth.clear_tokens(paths)
    if oauth_name == "MINIMAX_OAUTH":
        from providers import minimax_oauth

        return minimax_oauth.clear_tokens(paths)
    if oauth_name:
        path = paths.credentials / oauth_name
        if path.is_file():
            path.unlink()
            return True
    return False
