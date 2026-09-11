"""Shared container-engine plumbing for the container and machines backends.

Resolution: $HARNESS_CONTAINER_ENGINE, else docker, else podman. All calls
shell out to the engine CLI so the runtime stays stdlib-only.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from .base import IsolationUnavailable

#: (PATH value, resolved engine) — the which() scan stats every PATH entry
#: and ran once per engine subprocess. Keyed on PATH so changing it (tests
#: inject fake engines that way) re-resolves.
_found_engine: tuple[str, str] | None = None


def resolve_engine() -> str:
    engine = os.environ.get("HARNESS_CONTAINER_ENGINE")
    if engine:
        return engine
    global _found_engine
    path_env = os.environ.get("PATH", "")
    if _found_engine is not None and _found_engine[0] == path_env:
        return _found_engine[1]
    found = shutil.which("docker") or shutil.which("podman")
    if not found:
        # Not cached: installing the engine mid-session should be picked up.
        raise IsolationUnavailable(
            "container backend needs docker or podman on PATH "
            "(or set HARNESS_CONTAINER_ENGINE)."
        )
    _found_engine = (path_env, found)
    return found


def run(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run an engine command capturing text output."""
    return subprocess.run(
        [resolve_engine(), *args], capture_output=True, text=True, timeout=timeout
    )


def run_raw(
    args: list[str],
    *,
    stdin=None,
    stdout=None,
    timeout: int = 600,
    preexec_fn=None,
) -> subprocess.CompletedProcess:
    """Run an engine command with binary stdio (for `cp -` tar streams).

    `preexec_fn` runs in the child before exec (e.g. an RLIMIT_FSIZE so a
    tar stream cannot fill the host disk); it never touches this process.
    """
    return subprocess.run(
        [resolve_engine(), *args],
        stdin=stdin,
        stdout=stdout,
        stderr=subprocess.PIPE,
        timeout=timeout,
        preexec_fn=preexec_fn,
    )
