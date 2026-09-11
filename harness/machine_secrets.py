"""Explicitly granted script credentials, outside all synced bot files.

The tool gate authorizes use_secret_file. This module only validates the
machine mount and stages the selected value; it never makes policy decisions.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path

from harness.paths import HarnessPaths
from harness.roster import valid_bot_name
from isolation.base import IsolationUnavailable

MOUNT = "/run/harness"


class MachineSecretError(ValueError):
    """A script credential cannot be supplied to this machine."""


def directory_for_bot(paths: HarnessPaths, bot: str) -> Path:
    if not valid_bot_name(bot):
        raise MachineSecretError("Invalid bot name")
    root = paths.run / "bot-secrets"
    directory = root / bot
    if root.is_symlink() or directory.is_symlink():
        raise MachineSecretError("Unsafe script credential directory")
    return directory


def _owner(path: Path) -> None:
    # Hosted machines run as the image's uid 1000, never as the host root.
    if os.geteuid() == 0:
        os.chown(path, 1000, 1000)


def prepare_directory(paths: HarnessPaths, bot: str) -> Path:
    directory = directory_for_bot(paths, bot)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    _owner(directory)
    return directory


@contextlib.contextmanager
def _lock(paths: HarnessPaths):
    paths.run.mkdir(parents=True, exist_ok=True)
    fd = os.open(paths.run / "bot-secret-grants.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def stage(paths: HarnessPaths, bot: str, name: str, value: str) -> Path:
    from harness.secrets import valid_secret_name
    from isolation.machines import stage_secret_file

    if not valid_secret_name(name):
        raise MachineSecretError("Invalid secret name")
    directory = prepare_directory(paths, bot)
    if (directory / name).is_symlink():
        raise MachineSecretError("Unsafe script credential file")
    target = stage_secret_file(directory, name, value)
    _owner(target)
    return target


def mounted_for_bot(paths: HarnessPaths, bot: str, machine: str) -> bool:
    from isolation import engine

    try:
        result = engine.run("inspect", "-f", "{{json .Mounts}}", machine)
        if result.returncode:
            return False
        expected = directory_for_bot(paths, bot).resolve()
        return any(
            m.get("Type") == "bind"
            and m.get("Destination") == MOUNT
            and m.get("RW") is False
            and Path(m.get("Source") or "").resolve() == expected
            for m in json.loads(result.stdout)
        )
    except (OSError, ValueError, TypeError, AttributeError, IsolationUnavailable):
        return False


def grant(paths: HarnessPaths, bot: str, name: str) -> str:
    from harness.machine_view import machine_for_bot
    from harness.redaction import resolve_outbound
    from harness.secrets import get_secret, resolve_env_name, valid_secret_name

    if not valid_secret_name(name):
        raise MachineSecretError("Invalid secret name")
    name = resolve_env_name(name)
    machine = machine_for_bot(paths, bot)
    if not machine:
        raise MachineSecretError("Script credential files require a bot machine")
    if not mounted_for_bot(paths, bot, machine):
        raise MachineSecretError(
            "Restart this bot in Bot settings to load its script credential mount"
        )
    with _lock(paths):
        value = get_secret(name, paths)
        if not value:
            raise MachineSecretError(f"Call request_secret with name {name!r} first")
        value = resolve_outbound(value, where="a bot script credential file")
        stage(paths, bot, name, value)
    return f"{MOUNT}/{name}"


def refresh_grants(paths: HarnessPaths, name: str, *, remove: bool = False) -> None:
    """Rotate or revoke copies already granted; never grant to another bot."""
    from harness.redaction import resolve_outbound
    from harness.secrets import get_secret, valid_secret_name

    root = paths.run / "bot-secrets"
    if not valid_secret_name(name) or not root.is_dir() or root.is_symlink():
        return
    with _lock(paths):
        value = None if remove else get_secret(name, paths)
        if value:
            value = resolve_outbound(value, where="a rotated bot script credential file")
        for directory in root.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            target = directory / name
            if remove:
                target.unlink(missing_ok=True)
            elif target.is_file() and not target.is_symlink():
                if value:
                    stage(paths, directory.name, name, value)
                else:
                    target.unlink(missing_ok=True)


def _register_grants(paths: HarnessPaths, bot: str) -> None:
    from harness.redaction import register_secret
    from harness.secrets import valid_secret_name

    directory = directory_for_bot(paths, bot)
    if not directory.is_dir():
        return
    for target in directory.iterdir():
        if valid_secret_name(target.name) and target.is_file() and not target.is_symlink():
            register_secret(target.read_text(encoding="utf-8"), target.name)


@contextlib.contextmanager
def script_secret_scope(paths: HarnessPaths, bot: str):
    """Rehydrate redaction after restart and catch rotations during a tool.

    The grant outlives the agent's process-local redaction registry. Read only
    this bot's grants before a handler can emit events and again before its
    returned output is scrubbed. This is presentation, never authorization.
    """
    _register_grants(paths, bot)
    try:
        yield
    finally:
        _register_grants(paths, bot)
