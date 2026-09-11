"""Generation-token identity for harness-spawned agent processes.

Every agent ("brain") process the harness spawns is minted a fresh random
generation token, passed BOTH ways at spawn time:

* environment: ``HARNESS_GENERATION_TOKEN=<token>``
* argv:        ``--generation-token=<token>``

The child refuses to boot unless both agree (`boot_identity_error`): a
missing half or a mismatch means a mangled spawn, a copy-pasted command, or
an impostor, and a bot brain must never run under a stolen identity.

The parent side is the other half of the contract: before signaling a pid it
recorded earlier, it verifies the pid is alive AND that the live process's
``/proc/<pid>/cmdline`` still carries the exact whitespace-bounded token
(`verify_process_token`). PIDs get recycled; an innocent bystander process
that happens to wear a recorded pid must never receive our SIGTERM.
"""

from __future__ import annotations

import hmac
import os
import secrets
from collections.abc import Mapping
from pathlib import Path

from harness.fsutil import pid_alive

GENERATION_TOKEN_ENV = "HARNESS_GENERATION_TOKEN"
GENERATION_TOKEN_FLAG = "--generation-token"


def mint_generation_token() -> str:
    """A fresh random token for one spawned agent process."""
    return secrets.token_hex(16)


def token_argument(token: str) -> str:
    """The argv form of the token (`--generation-token=<token>`)."""
    return f"{GENERATION_TOKEN_FLAG}={token}"


def command_carries_token(command: str, token: str) -> bool:
    """True when `command` contains the exact, whitespace-bounded token flag.

    Substring hits inside a longer argument (e.g. a token that happens to be
    a prefix of another) do not count — the flag must stand alone.
    """
    if not token:
        return False
    needle = token_argument(token)
    offset = command.find(needle)
    while offset >= 0:
        before_ok = offset == 0 or command[offset - 1].isspace()
        end = offset + len(needle)
        after_ok = end == len(command) or command[end].isspace()
        if before_ok and after_ok:
            return True
        offset = command.find(needle, offset + 1)
    return False


def read_cmdline(pid: int) -> str | None:
    """The process's command line from /proc, NULs collapsed to spaces.

    None when the process is gone or /proc is unavailable (non-Linux).
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    parts = [part for part in raw.decode(errors="replace").split("\0") if part]
    return " ".join(parts)


def verify_process_token(
    pid: int | None,
    token: str,
    *,
    alive=pid_alive,
    read=read_cmdline,
) -> bool:
    """True only when `pid` is alive and its cmdline carries our exact token."""
    if not pid or not token or not alive(pid):
        return False
    command = read(pid)
    if not command:
        return False
    return command_carries_token(command, token)


def boot_identity_error(
    flag_token: str | None, env: Mapping[str, str] | None = None
) -> str | None:
    """Child-side boot check: the argv flag and the env var must agree.

    Returns a human-readable refusal reason, or None when identity is sound
    (both halves agree, or neither is present — a direct manual run).
    """
    if env is None:
        env = os.environ
    env_token = env.get(GENERATION_TOKEN_ENV) or None
    flag_token = flag_token or None
    if flag_token is None and env_token is None:
        return None
    if flag_token is None:
        return (
            f"generation token set in {GENERATION_TOKEN_ENV} but missing from argv "
            f"({GENERATION_TOKEN_FLAG}); refusing to boot"
        )
    if env_token is None:
        return (
            f"generation token passed via {GENERATION_TOKEN_FLAG} but missing from "
            f"{GENERATION_TOKEN_ENV}; refusing to boot"
        )
    if not hmac.compare_digest(flag_token, env_token):
        return "generation token mismatch between argv and environment; refusing to boot"
    return None
