#!/usr/bin/env python3
"""Persistent Docker PID 1: reload the controller, preserve its agent children.

Requires the host release root mounted at the same path and HARNESS_HOME mounted
at the same absolute path. Installed during an explicit bridge migration.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def heartbeat(home: Path, pid: int) -> bool:
    # A full/read-only disk must not kill PID 1 and all its agent children.
    # The old timestamp naturally expires, so host health checks fail closed.
    tmp = home / ".controller-supervisor.tmp"
    try:
        tmp.write_text(json.dumps({"time": time.time(), "controller_pid": pid}))
        tmp.replace(home / "controller-supervisor.json")
    except OSError:
        return False
    return True


def main():
    root = Path(os.environ.get("HARNESS_RELEASE_ROOT", "/opt/harness"))
    home = Path(os.environ["HARNESS_HOME"])
    stop = False

    def shutdown(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    child = None
    reloading = False
    heartbeat_ok = True
    mailbox = home / "controller-reload"
    stamp = mailbox.read_text() if mailbox.exists() else None
    while not stop:
        if child is None or child.poll() is not None:
            release = (root / "current").resolve()
            env = {**os.environ, "PYTHONPATH": str(release)}
            child = subprocess.Popen(
                [sys.executable, "-m", "harness", *sys.argv[1:]],
                cwd=release,
                env=env,
                start_new_session=True,
            )
            reloading = False
        current = mailbox.read_text() if mailbox.exists() else None
        if current != stamp:
            stamp = current
            if current and not reloading:
                child.terminate()
                reloading = True
        written = heartbeat(home, child.pid)
        if not written and heartbeat_ok:
            print("controller heartbeat unavailable; keeping agents alive", file=sys.stderr)
        heartbeat_ok = written
        time.sleep(1)
    if child and child.poll() is None:
        # An explicit container stop retains the existing final-sync lifecycle.
        # Reloads above never enter this path or stop any agent.
        arguments = sys.argv[1:]
        prefix = arguments[: arguments.index("serve")] if "serve" in arguments else []
        subprocess.run(
            [sys.executable, "-m", "harness", *prefix, "down"],
            cwd=(root / "current").resolve(),
            env=env,
            check=False,
        )
        child.terminate()
        child.wait()


if __name__ == "__main__":
    main()
