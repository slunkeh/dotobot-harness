"""Conservative content identities, independent of the release's version label."""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path

# These modules cannot run in an agent. Everything else is included, so a
# changed dependency defaults to restarting agents rather than guessing.
# Bump these explicitly when mixed-version controller/agent or persisted-state
# compatibility changes. Unknown older releases are never rollback evidence.
UPDATE_PROTOCOL = 1
STATE_SCHEMA = 1

CONTROLLER_ONLY = {"harness/server.py", "harness/agent_updater.py", "harness/updates.py"}


def identity(root: Path) -> dict:
    agent = hashlib.sha256()
    machine = hashlib.sha256()
    files = []
    for directory in ("agent", "harness", "providers", "connectors", "isolation", "channels"):
        files.extend((root / directory).rglob("*.py"))
    for path in sorted(files):
        rel = path.relative_to(root).as_posix()
        if rel in CONTROLLER_ONLY or rel == "harness/version.py":
            continue
        agent.update(rel.encode() + b"\0" + path.read_bytes() + b"\0")
    for rel in (
        "deploy/Dockerfile.machine",
        "deploy/machine_rm.py",
        "deploy/machine-wallpaper.jpg",
        "isolation/machine_supervisor.py",
    ):
        path = root / rel
        machine.update(rel.encode() + b"\0" + (path.read_bytes() if path.exists() else b"missing"))
    controller = root / "deploy/Dockerfile"
    declaration = root / "harness/runtime_identity.py"
    text = declaration.read_text() if declaration.exists() else ""

    def epoch(name):
        match = re.search(rf"^{name} = ([0-9]+)$", text, re.M)
        return int(match[1]) if match else None

    return {
        "agent": agent.hexdigest(),
        "machine": machine.hexdigest(),
        "controller": hashlib.sha256(
            controller.read_bytes() if controller.exists() else b""
        ).hexdigest(),
        "protocol": epoch("UPDATE_PROTOCOL"),
        "state": epoch("STATE_SCHEMA"),
    }


@lru_cache(maxsize=1)
def current_identity():
    return identity(Path(__file__).resolve().parents[1])


def compatible(old, new):
    return bool(
        old
        and new
        and old.get("protocol") is not None
        and old.get("state") is not None
        and old.get("protocol") == new.get("protocol")
        and old.get("state") == new.get("state")
        and old.get("agent") == new.get("agent")
        and old.get("machine") == new.get("machine")
    )
