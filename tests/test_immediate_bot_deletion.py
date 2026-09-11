"""Deletion receipts and roster changes cannot wait for computer shutdown."""

import json
import subprocess
import sys
import threading
import urllib.request

import pytest

from harness import bot_cleanup
from harness.orchestrator import Orchestrator
from harness.roster import RosterError, load_roster
from harness.server import make_server
from isolation import BotHandle
from isolation.machines import MachineBackend


@pytest.fixture
def orch(tmp_path, monkeypatch):
    roster = tmp_path / "roster.json"
    roster.write_text(
        '{"bots":[{"name":"atlas","provider":"echo"},{"name":"nova","provider":"echo"}]}'
    )
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=roster)
    orch.init()
    orch.paths.run_file("atlas").write_text('{"bot":"atlas","backend":"process","pid":12345}')
    monkeypatch.setattr(bot_cleanup, "pid_alive", lambda pid: False)
    return orch


def test_delete_receipt_and_roster_arrive_while_shutdown_is_blocked(orch, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def stop(handle):
        entered.set()
        assert release.wait(5)
        finished.set()

    monkeypatch.setattr(orch.backend, "stop", stop)
    server = make_server(orch, host="127.0.0.1", port=0, token="test-key")
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/api/bots"
    try:
        req = urllib.request.Request(
            base + "/atlas", method="DELETE", headers={"Authorization": "Bearer test-key"}
        )
        with urllib.request.urlopen(req, timeout=1) as response:
            assert json.load(response)["removed"] == "atlas"
        assert entered.wait(1)
        assert not finished.is_set()
        assert "atlas" not in load_roster(orch.roster_path).names()
        req = urllib.request.Request(base, headers={"Authorization": "Bearer test-key"})
        with urllib.request.urlopen(req, timeout=1) as response:
            assert [b["name"] for b in json.load(response)] == ["nova"]
        assert orch.paths.bot_memory("atlas").exists()
    finally:
        release.set()
        if hasattr(bot_cleanup, "shutdown"):
            bot_cleanup.shutdown(orch, "atlas")
        server.shutdown()
        server.server_close()
        worker.join(2)


def test_shutdown_failure_retries_before_24h_after_server_restart(orch, monkeypatch):
    monkeypatch.setattr(bot_cleanup, "start_shutdown", lambda *a: None)
    orch.remove_bot("atlas")
    restarted = Orchestrator.create(home=orch.paths.home, roster_path=orch.roster_path)
    attempts = []

    def stop(handle):
        attempts.append(handle.bot)
        if len(attempts) == 1:
            raise OSError("engine unavailable")

    monkeypatch.setattr(restarted.backend, "stop", stop)
    path = bot_cleanup.job_path(orch.paths, "atlas")
    now = json.loads(path.read_text())["deleted_at"] + 1
    assert bot_cleanup.sweep(restarted, now=now) == []
    assert json.loads(path.read_text())["stopping"]
    assert "atlas" not in restarted.roster.names()
    assert bot_cleanup.sweep(restarted, now=now + 1) == []
    assert attempts == ["atlas", "atlas"]
    assert not json.loads(path.read_text())["stopping"]


def test_real_process_shutdown_completes_off_request_path(orch, monkeypatch):
    from harness.fsutil import pid_alive

    monkeypatch.setattr(bot_cleanup, "pid_alive", pid_alive)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
        start_new_session=True,
        stdout=subprocess.PIPE,
    )
    try:
        assert child.stdout.readline() == b"ready\n"
        orch.paths.run_file("atlas").write_text(
            json.dumps({"bot": "atlas", "backend": "process", "pid": child.pid})
        )
        orch.remove_bot("atlas")
        bot_cleanup.shutdown(orch, "atlas")
        assert not pid_alive(child.pid)
        assert not json.loads(bot_cleanup.job_path(orch.paths, "atlas").read_text())["stopping"]
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
    assert orch.paths.bot_memory("atlas").exists()


def test_recreation_cannot_cancel_unfinished_shutdown(orch, monkeypatch):
    monkeypatch.setattr(bot_cleanup, "start_shutdown", lambda *a: None)
    monkeypatch.setattr(orch.backend, "stop", lambda handle: None)
    orch.remove_bot("atlas")
    with pytest.raises(RosterError, match="cleanup is in progress"):
        orch.add_bot(name="atlas", start=False)
    bot_cleanup.shutdown(orch, "atlas")
    orch.add_bot(name="atlas", start=False)
    assert "atlas" in orch.roster.names()
    assert not bot_cleanup.job_path(orch.paths, "atlas").exists()


def test_idle_disk_cannot_be_reassigned_before_shutdown_worker_starts(orch, monkeypatch):
    monkeypatch.setattr(bot_cleanup, "start_shutdown", lambda *a: None)
    pool = MachineBackend(orch.paths).pool
    old = pool.acquire("atlas", lambda name: False)
    pool.release("atlas")
    orch.remove_bot("atlas")
    new = pool.acquire("nova", lambda name: False)
    assert old.name != new.name
    assert pool.machines()[0].state == "retired"


def test_deletion_does_not_probe_machine_status(orch, monkeypatch):
    monkeypatch.setattr(bot_cleanup, "start_shutdown", lambda *a: None)

    def slow_load(name):
        pytest.fail("DELETE must not call backend.load and its Docker status probe")

    monkeypatch.setattr(orch.backend, "load", slow_load)
    orch.remove_bot("atlas")
    assert "atlas" not in orch.roster.names()


def test_background_shutdown_does_not_signal_recycled_pid(orch, monkeypatch):
    monkeypatch.setattr(bot_cleanup, "start_shutdown", lambda *a: None)
    monkeypatch.setattr("isolation.process_identity.read_cmdline", lambda pid: "original process")
    orch.remove_bot("atlas")
    monkeypatch.setattr(bot_cleanup, "pid_alive", lambda pid: True)
    monkeypatch.setattr("isolation.process_identity.read_cmdline", lambda pid: "another process")
    monkeypatch.setattr(
        orch.backend, "stop", lambda handle: pytest.fail("recycled process signalled")
    )
    bot_cleanup.shutdown(orch, "atlas")
    assert not json.loads(bot_cleanup.job_path(orch.paths, "atlas").read_text())["stopping"]


def test_running_machine_keeps_shutdown_queued_even_if_stop_swallows_failure(orch, monkeypatch):
    monkeypatch.setattr(bot_cleanup, "start_shutdown", lambda *a: None)
    orch.backend_name = "machines"
    orch.backend._record(BotHandle("atlas", "machines", meta={"machine": "harness-machine-0"}))
    monkeypatch.setattr(orch.backend, "stop", lambda handle: None)
    state = ["running"]
    monkeypatch.setattr(orch.backend, "_container_state", lambda name: state[0])
    orch.remove_bot("atlas")
    bot_cleanup.shutdown(orch, "atlas")
    path = bot_cleanup.job_path(orch.paths, "atlas")
    assert json.loads(path.read_text())["stopping"]
    state[0] = "exited"
    bot_cleanup.shutdown(orch, "atlas")
    assert not json.loads(path.read_text())["stopping"]
