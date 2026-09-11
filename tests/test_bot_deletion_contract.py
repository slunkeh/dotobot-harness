"""Regression contract uses only the existing public deletion entry point."""

import json
import time

from harness.orchestrator import Orchestrator


def test_deleting_bot_schedules_durable_cleanup_after_24_hours(tmp_path):
    roster = tmp_path / "roster.json"
    roster.write_text('{"bots":[{"name":"atlas","provider":"echo"}]}')
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=roster)
    orch.init()
    before = time.time()
    orch.remove_bot("atlas")
    assert "atlas" not in orch.roster.names()
    path = orch.paths.home / "deleted-bots" / "atlas.json"
    assert path.exists(), "Deletion must durably schedule the retained bot data for cleanup"
    job = json.loads(path.read_text())
    assert before + 86400 <= job["purge_after"] <= time.time() + 86400
    assert orch.paths.bot_memory("atlas").exists()
