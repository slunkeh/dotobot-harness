import json
import os
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import messaging
from agent.runtime import build_agent
from harness import update_state, updates
from harness.control import Control
from harness.paths import HarnessPaths
from harness.roster import Bot
from harness.runtime_identity import current_identity, identity
from harness.version import __version__
from isolation.base import BotHandle, Status


@pytest.fixture
def fleet(tmp_path):
    paths = HarnessPaths.resolve(tmp_path)
    paths.ensure_layout(["atlas", "nova"])
    handles = {
        name: BotHandle(name, "process", os.getpid(), Status.RUNNING, {"version": "0.0.1"})
        for name in ["atlas", "nova"]
    }
    backend = Mock()
    backend.update_healthy.return_value = True
    orch = SimpleNamespace(
        paths=paths,
        roster=SimpleNamespace(names=lambda: list(handles)),
        _handle=handles.get,
        _lifecycle_lock=lambda n: nullcontext(),
        _agent_argv=lambda n: [],
        control=Control(paths),
        backend=backend,
    )
    return orch, handles


def ack(orch, name, version="0.0.1"):
    with messaging.queue_lock(orch.paths, name):
        update_state.acknowledge(orch.paths, name)
    p = orch.paths.run / f"{name}.update-ready.json"
    data = json.loads(p.read_text())
    data["version"] = version
    p.write_text(json.dumps(data))


def test_hold_keeps_all_lanes_and_order_until_release(tmp_path):
    paths = HarnessPaths.resolve(tmp_path)
    paths.ensure_layout(["atlas"])
    bot = build_agent(paths, Bot(name="atlas", role="test", provider="echo"), stream_delay=0)
    msgs = [
        messaging.Msg(to="atlas", frm=frm, text="hello", origin=origin)
        for frm, origin in [("user", None), ("nova", None), ("user", "routine")]
    ]
    for msg in msgs:
        messaging.send(paths, msg)
    with messaging.queue_lock(paths, "atlas"):
        messaging.reorder_queue(paths, "atlas", [m.id for m in reversed(msgs)])
        update_state.set_hold(paths, "atlas", "op")
    assert bot.process_inbox_once() is False
    assert len(messaging.pending(paths, "atlas")) == 3
    assert messaging.queue_order(paths, "atlas") == [m.id for m in reversed(msgs)]
    assert not messaging.mark_now(paths, "atlas", msgs[0].id)
    assert not messaging.take_steer(paths, "atlas", "current", after_ts=0)
    update_state.release_hold(paths, "atlas", "wrong-operation")
    assert update_state.held(paths, "atlas")
    update_state.release_hold(paths, "atlas", "op")
    assert bot.process_inbox_once()
    assert len(messaging.pending(paths, "atlas")) == 2


def test_serial_hold_health_and_duplicate_start(fleet):
    orch, handles = fleet
    op = updates.begin(orch)
    assert updates.begin(orch)["id"] == op["id"]
    updates.tick(orch)
    assert update_state.held(orch.paths, "atlas")
    assert not update_state.held(orch.paths, "nova")
    ack(orch, "atlas")
    updates.tick(orch)
    assert orch.backend.restart_for_update.call_count == 1
    assert updates.snapshot(orch)["bots"][0]["stage"] == "checking_health"
    ack(orch, "atlas", __version__)
    updates.tick(orch)
    assert not update_state.held(orch.paths, "atlas")
    assert not update_state.held(orch.paths, "nova")
    updates.tick(orch)
    assert update_state.held(orch.paths, "nova")


@pytest.mark.parametrize("block", ["busy", "takeover", "teach", "legacy"])
def test_wait_without_interrupting(fleet, block):
    orch, _ = fleet
    updates.begin(orch)
    updates.tick(orch)
    if block != "legacy":
        ack(orch, "atlas")
    if block == "busy":
        orch.control.set_busy("atlas", "task")
    elif block == "takeover":
        orch.control.take_over("atlas")
    elif block == "teach":
        orch.control.start_teach("atlas")
    updates.tick(orch)
    orch.backend.restart_for_update.assert_not_called()
    assert updates.snapshot(orch)["bots"][0]["stage"] == "waiting"


def test_failure_pauses_fleet_and_preserves_hold(fleet):
    orch, _ = fleet
    updates.begin(orch)
    updates.tick(orch)
    ack(orch, "atlas")
    orch.backend.restart_for_update.side_effect = RuntimeError("snapshot failed")
    updates.tick(orch)
    assert updates.snapshot(orch)["stage"] == "needs_attention"
    updates.tick(orch)
    assert orch.backend.restart_for_update.call_count == 1
    assert not update_state.held(orch.paths, "nova")
    assert update_state.held(orch.paths, "atlas")


@pytest.mark.parametrize("stage", ["saving_state", "restarting", "checking_health"])
def test_interrupted_step_never_blindly_restarts(fleet, stage):
    orch, _ = fleet
    op = updates.begin(orch)
    op["bots"][0]["stage"] = stage
    op["bots"][0]["check_started"] = 10**12
    update_state.save(orch.paths, op)
    updates.tick(orch)
    orch.backend.restart_for_update.assert_not_called()
    assert updates.snapshot(orch)["bots"][0]["stage"] == "checking_health"


def test_stopped_and_deleted_bots_not_resurrected(fleet):
    orch, handles = fleet
    handles["nova"].status = Status.STOPPED
    op = updates.begin(orch)
    assert [b["name"] for b in op["bots"]] == ["atlas"]
    handles.pop("atlas")
    updates.tick(orch)
    assert updates.snapshot(orch)["bots"][0]["stage"] == "stopped"
    orch.backend.restart_for_update.assert_not_called()


def test_compatible_controller_update_no_restart(fleet):
    orch, handles = fleet
    for h in handles.values():
        h.meta["identity"] = current_identity()
    assert updates.begin(orch)["stage"] == "complete"
    updates.tick(orch)
    orch.backend.restart_for_update.assert_not_called()


def test_identity_controller_only_and_agent_change(tmp_path):
    (tmp_path / "harness").mkdir()
    server = tmp_path / "harness/server.py"
    server.write_text("before")
    before = identity(tmp_path)
    server.write_text("after")
    assert identity(tmp_path) == before
    (tmp_path / "harness/control.py").write_text("changed")
    assert identity(tmp_path)["agent"] != before["agent"]


def test_bridge_only_restarts_a_verified_idle_empty_queue(fleet):
    orch, handles = fleet
    handles["atlas"].meta["version"] = "0.2.110"
    # The bridge is relevant only when the target is a newer controller.
    op = updates.begin(orch)
    op["bots"] = [{"name": "atlas", "stage": "pending"}]
    update_state.save(orch.paths, op)
    msg = messaging.Msg(to="atlas", frm="user", text="finish first")
    messaging.send(orch.paths, msg)
    updates.tick(orch)
    orch.backend.restart_for_update.assert_not_called()
    for path, _ in messaging.read_inbox(orch.paths, "atlas"):
        messaging.mark_processed(orch.paths, "atlas", path)
    updates.tick(orch)
    assert orch.backend.restart_for_update.call_count == 1


def test_dead_replacement_is_not_success(fleet, monkeypatch):
    orch, handles = fleet
    op = updates.begin(orch)
    op["bots"][0].update(stage="checking_health", check_started=0)
    update_state.save(orch.paths, op)
    handles["atlas"].status = Status.DEAD
    updates.tick(orch)
    assert updates.snapshot(orch)["stage"] == "needs_attention"


def test_queue_hold_and_claim_are_serialized(tmp_path):
    import threading

    paths = HarnessPaths.resolve(tmp_path)
    paths.ensure_layout(["atlas"])
    bot = build_agent(paths, Bot(name="atlas", role="test", provider="echo"), stream_delay=0)
    messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="queued"))
    result = []
    with messaging.queue_lock(paths, "atlas"):
        thread = threading.Thread(target=lambda: result.append(bot.process_inbox_once()))
        thread.start()
        update_state.set_hold(paths, "atlas", "op")
    thread.join(5)
    assert result == [False]
    assert len(messaging.pending(paths, "atlas")) == 1


def test_preview_without_worker_never_offers_install(fleet):
    from harness import update_api

    orch, _ = fleet
    result = update_api.preview(orch)
    assert not result["can_start"]
    assert result["reason"]


def test_update_api_routes_require_auth(fleet):
    import threading
    import urllib.error
    import urllib.request

    from harness.orchestrator import Orchestrator
    from harness.server import make_server

    orch, _ = fleet
    roster = orch.paths.home / "roster.toml"
    roster.write_text('[[bots]]\nname="atlas"\nrole="test"\nprovider="echo"\n')
    real = Orchestrator.create(home=orch.paths.home, roster_path=roster, backend="process")
    server = make_server(real, "127.0.0.1", 0, token="test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for route, method in [
            ("/api/updates", "GET"),
            ("/api/updates/preview", "GET"),
            ("/api/updates/start", "POST"),
            ("/api/updates/retry", "POST"),
        ]:
            request = urllib.request.Request(base + route, method=method)
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            assert error.value.code == 401
        request = urllib.request.Request(
            base + "/api/updates", headers={"Authorization": "Bearer test-token"}
        )
        with urllib.request.urlopen(request) as response:
            assert json.load(response)["stage"] == "idle"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_agent_only_machine_update_never_syncs_or_stops_computer(tmp_path, monkeypatch):
    from isolation.machines import MachineBackend

    paths = HarnessPaths.resolve(tmp_path)
    paths.ensure_layout(["atlas"])
    backend = MachineBackend(paths)
    machine = SimpleNamespace(id=0, name="test-machine", bot="atlas")
    monkeypatch.setattr(backend.pool, "machines", lambda: [machine])
    monkeypatch.setattr(backend, "_stop_agent", Mock())
    monkeypatch.setattr(backend, "_spawn_agent", Mock(return_value=(4567, "new-token")))
    monkeypatch.setattr(backend, "_record", Mock())
    for method in [
        "_sync_up",
        "_sync_session_start",
        "_flush_running_peers",
        "_ensure_container",
        "_quiesce_chrome",
    ]:
        monkeypatch.setattr(
            backend,
            method,
            Mock(side_effect=AssertionError("unnecessary state copy or computer restart")),
        )
    handle = BotHandle(
        "atlas", "machines", 1234, Status.RUNNING, {"machine_id": 0, "identity": current_identity()}
    )
    result = backend.restart_for_update(handle, [], current_identity())
    assert result.pid == 4567
    backend._stop_agent.assert_called_once_with(1234)


def test_failed_snapshot_retains_original_machine_and_agent(tmp_path, monkeypatch):
    from isolation.base import IsolationUnavailable
    from isolation.machines import MachineBackend

    paths = HarnessPaths.resolve(tmp_path)
    paths.ensure_layout(["atlas"])
    backend = MachineBackend(paths)
    machine = SimpleNamespace(id=0, name="test-machine", bot="atlas")
    monkeypatch.setattr(backend.pool, "machines", lambda: [machine])
    monkeypatch.setattr(backend, "_quiesce_chrome", Mock())
    monkeypatch.setattr(backend, "_image_id", Mock(return_value="image-id"))
    monkeypatch.setattr(backend, "_sync_up", Mock(return_value={"walk_complete": False}))
    monkeypatch.setattr(backend, "_stop_agent", Mock())
    handle = BotHandle("atlas", "machines", 1234, Status.RUNNING, {"machine_id": 0})
    with pytest.raises(IsolationUnavailable, match="snapshot incomplete"):
        backend.restart_for_update(handle, [], current_identity())
    backend._stop_agent.assert_not_called()


def test_rollback_refuses_missing_or_changed_previous_source(fleet):
    from harness.update_rollback import restore_agent

    orch, _ = fleet
    row = {
        "name": "atlas",
        "previous_identity": current_identity(),
        "previous_release": "/nonexistent",
    }
    assert not restore_agent(orch, row, current_identity())
    orch.backend.stop.assert_not_called()


def test_dead_pid_with_fresh_ack_is_not_ready(fleet):
    orch, handles = fleet
    ack(orch, "atlas", __version__)
    handles["atlas"].status = Status.DEAD
    assert not update_state.ready(orch.paths, "atlas", handles["atlas"])


def test_compatible_rollback_uses_verified_immutable_source(fleet, tmp_path, monkeypatch):
    from harness import update_rollback

    orch, handles = fleet
    previous = tmp_path / "previous"
    (previous / "harness").mkdir(parents=True)
    (previous / "harness/runtime_identity.py").write_text("UPDATE_PROTOCOL = 1\nSTATE_SCHEMA = 1\n")
    old_identity = identity(previous)
    handles["atlas"].meta["version"] = "2.0.0"
    orch.paths.run_file("atlas").write_text(json.dumps({"bot": "atlas", "pid": 1234}))
    child = Mock(pid=6789)
    spawn = Mock(return_value=child)
    monkeypatch.setattr(update_rollback.subprocess, "Popen", spawn)
    row = {
        "name": "atlas",
        "previous_identity": old_identity,
        "previous_release": str(previous),
        "previous_version": "1.0.0",
    }
    assert update_rollback.restore_agent(orch, row, old_identity)
    assert spawn.call_args.kwargs["cwd"] == previous
    assert spawn.call_args.kwargs["env"]["PYTHONPATH"] == str(previous)
    restored = json.loads(orch.paths.run_file("atlas").read_text())
    assert restored["version"] == "1.0.0" and restored["pid"] == 6789
    # A changed persistent-state contract never authorizes replaying old code.
    assert not update_rollback.restore_agent(orch, row, {**old_identity, "state": 2})


def test_hold_does_not_acknowledge_an_escaped_worker(tmp_path):
    paths = HarnessPaths.resolve(tmp_path)
    paths.ensure_layout(["atlas"])
    bot = build_agent(paths, Bot(name="atlas", role="test", provider="echo"), stream_delay=0)
    with messaging.queue_lock(paths, "atlas"):
        update_state.set_hold(paths, "atlas", "op")
    bot.scheduler.zombies.append(object())
    assert not bot.process_inbox_once()
    assert not (paths.run / "atlas.update-ready.json").exists()
