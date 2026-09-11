"""Idle-gated rolling restarts of stale-code agents."""

from __future__ import annotations

import json
import time

import pytest

from harness.agent_updater import find_rollable, roll_once
from harness.orchestrator import Orchestrator
from harness.version import __version__

ROSTER = """
[[bots]]
name = "atlas"
role = "a terse research assistant"
provider = "echo"

[[bots]]
name = "nova"
role = "a friendly writing partner"
provider = "echo"
"""


@pytest.fixture
def orch(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    o = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    o.init()
    o.up()
    deadline = time.time() + 10
    while time.time() < deadline and not all(h.status.value == "running" for h in o.status()):
        time.sleep(0.2)
    try:
        yield o
    finally:
        o.down()


def _make_stale(orch, name):
    rf = orch.paths.run_file(name)
    data = json.loads(rf.read_text(encoding="utf-8"))
    data["version"] = "0.0.1"
    rf.write_text(json.dumps(data), encoding="utf-8")


def test_stale_idle_bot_is_rolled(orch):
    old_pid = orch._handle("atlas").pid
    _make_stale(orch, "atlas")
    nova_pid = orch._handle("nova").pid

    logs = []
    assert roll_once(orch, log=logs.append) == "atlas"
    handle = orch._handle("atlas")
    assert handle.status.value == "running"
    assert handle.pid != old_pid
    assert handle.meta.get("version") == __version__
    # up-to-date bot untouched
    assert orch._handle("nova").pid == nova_pid
    assert logs and "0.0.1" in logs[0]

    # nothing left to roll
    assert roll_once(orch) is None


def test_busy_bot_is_skipped(orch):
    _make_stale(orch, "atlas")
    orch.control.set_busy("atlas", "rid-1")  # our own (live) pid owns the claim
    assert find_rollable(orch) is None
    orch.control.clear_busy("atlas")
    assert find_rollable(orch) == "atlas"


def test_pending_inbox_is_skipped(orch, monkeypatch):
    import harness.agent_updater as au

    _make_stale(orch, "atlas")
    monkeypatch.setattr(
        au.messaging, "pending", lambda paths, name: ["queued"] if name == "atlas" else []
    )
    assert find_rollable(orch) is None


def test_human_control_is_skipped(orch):
    _make_stale(orch, "atlas")
    orch.control.take_over("atlas")
    assert find_rollable(orch) is None
    orch.control.return_control("atlas")
    assert find_rollable(orch) == "atlas"


def test_one_restart_per_tick(orch):
    _make_stale(orch, "atlas")
    _make_stale(orch, "nova")
    first = roll_once(orch)
    assert first in ("atlas", "nova")
    # exactly one was rolled; the other is still stale
    assert find_rollable(orch) is not None
    second = roll_once(orch)
    assert {first, second} == {"atlas", "nova"}
    assert roll_once(orch) is None


def test_update_is_visible_and_manual_restart_joins_it(orch, monkeypatch):
    import threading

    _make_stale(orch, "atlas")
    entered, release = threading.Event(), threading.Event()
    original = orch.backend.stop
    calls = []
    finished = threading.Event()

    def slow_stop(handle):
        calls.append(handle.bot)
        entered.set()
        assert release.wait(5)
        original(handle)

    monkeypatch.setattr(orch.backend, "stop", slow_stop)
    worker = threading.Thread(target=lambda: roll_once(orch))
    worker.start()
    try:
        assert entered.wait(1)
        assert next(h for h in orch.status() if h.bot == "atlas").status.value == "updating"
        assert orch.is_starting("atlas")
        assert find_rollable(orch) is None
        orch.restart("atlas", wait=False, on_finished=finished.set)
        assert calls == ["atlas"]
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert finished.wait(1), "A caller joining an update must receive completion"
    assert not orch.is_starting("atlas")
