"""Provider readiness probes.

Answers "can this provider serve a request right now?" without touching the
network or reading any secret. Per provider the probe reports
`{installed, authenticated, path}` plus a `status` string the app can render
verbatim: "Ready" / "Needs sign-in" / "Not installed".

Executable search order for a provider's companion CLI (first hit wins):

    1. env override (e.g. $CODEX_PATH, $CLAUDE_CODE_PATH)
    2. ~/.local/bin
    3. the tool's own home dir (~/.codex/bin, ~/.claude/local)
    4. $PATH

Auth checks are cheap and secret-free: a conventional env key being set, a
key in the harness credentials store, or a vendor credentials file existing.
File *contents* are never read — existence is the whole check.

The harness talks to providers over HTTP, so a companion CLI is a sign-in
convenience, not the request path: an authenticated provider is "Ready" even
when its CLI is missing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import HarnessPaths
from .secrets import secret_source

STATUS_READY = "Ready"
STATUS_NEEDS_SIGN_IN = "Needs sign-in"
STATUS_NOT_INSTALLED = "Not installed"


@dataclass(frozen=True)
class CliProbe:
    """Where a provider's companion CLI and login artifacts live."""

    cli: str | None = None  # executable name, None for API-only providers
    env_var: str | None = None  # explicit path override
    tool_homes: tuple[str, ...] = ()  # the tool's own bin dirs, ~-relative
    cred_files: tuple[str, ...] = ()  # existence-only checks, never read
    needs_auth: bool = True


_PROBES: dict[str, CliProbe] = {
    "claude": CliProbe(
        cli="claude",
        env_var="CLAUDE_CODE_PATH",
        tool_homes=("~/.claude/local",),
        cred_files=("~/.claude/.credentials.json",),
    ),
    "codex": CliProbe(
        cli="codex",
        env_var="CODEX_PATH",
        tool_homes=("~/.codex/bin",),
        cred_files=("~/.codex/auth.json",),
    ),
    "grok": CliProbe(),  # API-only: xAI key or OAuth, no companion CLI
    "echo": CliProbe(needs_auth=False),  # local, keyless
}


def find_cli(name: str, *, env_var: str | None = None, tool_homes: tuple[str, ...] = ()) -> str | None:
    """First executable found in the documented search order, else None."""
    candidates: list[Path] = []
    override = os.environ.get(env_var, "").strip() if env_var else ""
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(Path.home() / ".local" / "bin" / name)
    candidates.extend(Path(d).expanduser() / name for d in tool_homes)
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry:
            candidates.append(Path(entry) / name)
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def _authenticated(provider_id: str, probe: CliProbe, paths: HarnessPaths | None) -> bool:
    if secret_source(provider_id, paths) is not None:  # env key, stored key, or OAuth tokens
        return True
    return any(Path(f).expanduser().is_file() for f in probe.cred_files)


def provider_readiness(provider_id: str, paths: HarnessPaths | None = None) -> dict[str, Any]:
    """One provider's `{installed, authenticated, path, status}` snapshot."""
    probe = _PROBES.get(provider_id.lower(), CliProbe())
    path: str | None = None
    installed = True  # API-only providers have nothing to install
    if probe.cli:
        path = find_cli(probe.cli, env_var=probe.env_var, tool_homes=probe.tool_homes)
        installed = path is not None
    authenticated = not probe.needs_auth or _authenticated(provider_id, probe, paths)
    if authenticated:
        status = STATUS_READY
    elif not installed:
        status = STATUS_NOT_INSTALLED
    else:
        status = STATUS_NEEDS_SIGN_IN
    return {
        "installed": installed,
        "authenticated": authenticated,
        "path": path,
        "status": status,
    }
