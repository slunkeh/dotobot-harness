"""Durable update journal and queue holds shared by controller and agents."""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager

from harness.fsutil import write_atomic


@contextmanager
def operation_lock(paths):
    paths.home.mkdir(parents=True, exist_ok=True)
    with (paths.home / "update.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def read_json(path):
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError(f"Invalid update state: {path.name}")
        return value
    except FileNotFoundError:
        return {}


def read(paths):
    return read_json(paths.home / "update-operation.json")


def save(paths, operation):
    operation["updated_at"] = time.time()
    write_atomic(paths.home / "update-operation.json", json.dumps(operation))


def hold_path(paths, bot):
    return paths.inbox(bot).parent / "update-hold.json"


def held(paths, bot):
    # Presence fails closed even when a damaged file cannot be decoded.
    return hold_path(paths, bot).exists()


def set_hold(paths, bot, operation_id):
    """Caller holds messaging.queue_lock, the same lock used by admission."""
    write_atomic(hold_path(paths, bot), json.dumps({"id": operation_id}))


def release_hold(paths, bot, operation_id):
    from agent.messaging import queue_lock

    with queue_lock(paths, bot):
        if read_json(hold_path(paths, bot)).get("id") == operation_id:
            hold_path(paths, bot).unlink(missing_ok=True)


def acknowledge(paths, bot):
    """Called at the admission boundary, after the preceding turn fully settles."""
    from harness.runtime_identity import current_identity
    from harness.version import __version__

    hold = read_json(hold_path(paths, bot))
    destination = paths.run / f"{bot}.update-ready.json"
    previous = read_json(destination)
    if (
        previous.get("pid") == os.getpid()
        and previous.get("hold") == hold.get("id")
        and time.time() - previous.get("time", 0) < 2
    ):
        return bool(hold)
    write_atomic(
        destination,
        json.dumps(
            {
                "pid": os.getpid(),
                "hold": hold.get("id"),
                "version": __version__,
                "identity": current_identity(),
                "time": time.time(),
            }
        ),
    )
    return bool(hold)


def ready(paths, bot, handle, operation_id=None):
    data = read_json(paths.run / f"{bot}.update-ready.json")
    return (
        data
        if handle
        and handle.status.value == "running"
        and data.get("pid") == handle.pid
        and data.get("hold") == operation_id
        and time.time() - data.get("time", 0) < 10
        else {}
    )
