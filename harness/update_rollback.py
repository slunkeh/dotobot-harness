"""Narrow rollback: previous agent code with the same state and machine contract.

Never restore an old data snapshot over work completed after the upgrade.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from harness.fsutil import write_atomic
from harness.runtime_identity import identity


def restore_agent(orch, row, target):
    previous = row.get("previous_identity") or {}
    target = target or {}
    root = Path(row.get("previous_release") or "/nonexistent")
    if (
        not previous
        or not target
        or previous.get("state") is None
        or previous.get("state") != target.get("state")
        or previous.get("machine") != target.get("machine")
        or not root.is_dir()
        or identity(root) != previous
    ):
        return False
    handle = orch._handle(row["name"])
    if not handle or handle.status.value != "running" or orch.control.is_busy(row["name"]):
        return False
    if orch.control.state(row["name"]).mode != "bot":
        return False
    record = json.loads(orch.paths.run_file(row["name"]).read_text())
    if handle.meta.get("version") == row.get("previous_version"):
        return True  # Failure occurred before the old agent was replaced.
    if handle.backend not in {"process", "machines"}:
        return False
    env = {**os.environ, "PYTHONPATH": str(root)}
    argv = orch._agent_argv(row["name"])
    if handle.backend == "machines":
        from isolation.process_identity import (
            GENERATION_TOKEN_ENV,
            mint_generation_token,
            token_argument,
        )

        token = mint_generation_token()
        env.update(HARNESS_MACHINE_NAME=handle.meta["machine"], HARNESS_SHARED_DISPLAY="0")
        env[GENERATION_TOKEN_ENV] = token
        argv = [*argv, token_argument(token)]
        orch.backend._stop_agent(handle.pid)
        record["token"] = token
    else:
        orch.backend.stop(handle)
    with orch.paths.log_file(row["name"]).open("a") as log:
        child = subprocess.Popen(
            [sys.executable, *argv],
            cwd=root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    record.update(
        pid=child.pid, version=row["previous_version"], identity=previous, release_root=str(root)
    )
    write_atomic(orch.paths.run_file(row["name"]), json.dumps(record))
    return True
