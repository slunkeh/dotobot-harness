"""Authenticated route helpers; release locations come from host configuration."""

from __future__ import annotations

import time
import uuid

from deploy.updater import load_manifest, parse_version
from harness import update_state as state
from harness import updates
from harness.version import __version__


def host_status(paths):
    host = state.read_json(paths.home / "update-host.json")
    if time.time() - host.get("time", 0) > 90:
        return {"available": False, "reason": "The host updater is not connected."}
    return host


def preview(orch):
    host = host_status(orch.paths)
    result = {
        "supported": True,
        "can_start": False,
        "current_version": __version__,
        "reason": host.get("reason"),
        "bots": [],
    }
    if not host.get("available"):
        return result
    manifest = load_manifest(host["manifest_url"])
    target = {"version": manifest["version"], "identity": manifest.get("runtime_identity")}
    result.update(
        target_version=target["version"],
        bots=updates.affected(orch, target),
        can_start=parse_version(target["version"]) > parse_version(__version__),
        interruption="The server will reconnect. Bots needing new code update one at a time after their current task finishes.",
    )
    return result


def start(orch, expected_version):
    existing = updates.snapshot(orch)
    if existing.get("stage") not in {"idle", "complete", "cancelled"}:
        return existing
    candidate = preview(orch)
    if not candidate["can_start"] or candidate.get("target_version") != expected_version:
        raise updates.UpdateError(
            candidate.get("reason") or "Available release changed. Check for updates again."
        )
    with state.operation_lock(orch.paths):
        existing = state.read(orch.paths)
        if existing and existing.get("stage") not in updates.TERMINAL:
            return existing
        running = [
            n for n in orch.roster.names() if (h := orch._handle(n)) and h.status.value == "running"
        ]
        op = {
            "id": uuid.uuid4().hex,
            "stage": "preparing",
            "target": {"version": expected_version},
            "previous_version": __version__,
            "started_at": time.time(),
            "running_before": running,
            "bots": candidate["bots"],
        }
        state.save(orch.paths, op)
        return op
