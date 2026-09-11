"""Deleted bots retain private data for 24h, then purge without harming peers."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from harness import bot_cleanup
from harness.orchestrator import Orchestrator
from harness.roster import RosterError
from harness.statestore import StateStore
from isolation import IsolationUnavailable
from isolation.machines import OWNER_LABEL, SECRET_BOT_LABEL, MachineBackend


def make_orch(tmp_path):
    roster = tmp_path / "roster.json"
    roster.write_text(
        '{"bots":[{"name":"atlas","provider":"echo"},{"name":"nova","provider":"echo"}]}'
    )
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=roster)
    orch.init()
    return orch


def due(orch, bot="atlas"):
    return json.loads(bot_cleanup.job_path(orch.paths, bot).read_text())["purge_after"]


def test_retention_restart_and_complete_private_cleanup(tmp_path):
    orch = make_orch(tmp_path)
    paths = orch.paths
    store = StateStore(paths)
    for bot in ("atlas", "nova"):
        store.append_transcript(bot, "session", {"role": "user", "text": "private history"})
        (paths.bot_memory(bot) / "facts.jsonl").write_text("private memory")
        paths.bot_routines(bot).write_text("[]")
        paths.log_file(bot).write_text("private log")
        secret = paths.run / "bot-secrets" / bot / "key"
        secret.parent.mkdir(parents=True)
        secret.write_text("private credential copy")
    (paths.credentials / "KEY").write_text("account credential")
    (paths.canonical_home() / "shared.txt").write_text("shared home")
    (paths.workspace / "project.txt").write_text("shared workspace")
    orch.remove_bot("atlas")
    expiry = due(orch)
    assert bot_cleanup.sweep(orch, now=expiry - 1) == []
    assert store.transcript_records("atlas")
    assert paths.bot_memory("atlas").exists()
    # A new orchestrator after a restart picks up the same persisted job.
    restarted = Orchestrator.create(home=paths.home, roster_path=orch.roster_path)
    assert bot_cleanup.sweep(restarted, now=expiry) == ["atlas"]
    assert store.transcript_records("atlas") == []
    assert not paths.bot_memory("atlas").exists()
    assert not paths.bot_routines("atlas").exists()
    assert not paths.log_file("atlas").exists()
    assert not (paths.run / "bot-secrets" / "atlas").exists()
    assert store.transcript_records("nova")
    assert paths.bot_memory("nova").exists()
    assert paths.bot_routines("nova").exists()
    assert (paths.credentials / "KEY").exists()
    assert (paths.canonical_home() / "shared.txt").exists()
    assert (paths.workspace / "project.txt").exists()
    assert bot_cleanup.sweep(restarted, now=expiry + 1) == []
    assert (paths.run / "atlas.lifecycle.lock").exists()


def test_recreation_cancels_cleanup_and_preserves_history(tmp_path):
    orch = make_orch(tmp_path)
    orch.remove_bot("atlas")
    expiry = due(orch)
    bot_cleanup.shutdown(orch, "atlas")
    orch.add_bot(name="atlas", provider="echo", start=False)
    assert not bot_cleanup.job_path(orch.paths, "atlas").exists()
    assert bot_cleanup.sweep(orch, now=expiry) == []
    assert orch.paths.bot_memory("atlas").exists()


def test_partial_purge_retries_and_blocks_recreation(tmp_path, monkeypatch):
    orch = make_orch(tmp_path)
    orch.remove_bot("atlas")
    purge = bot_cleanup._purge_files
    monkeypatch.setattr(bot_cleanup, "_purge_files", lambda *a: (_ for _ in ()).throw(OSError()))
    expiry = due(orch)
    assert bot_cleanup.sweep(orch, now=expiry) == []
    with pytest.raises(RosterError, match="cleanup is in progress"):
        orch.add_bot(name="atlas", start=False)
    monkeypatch.setattr(bot_cleanup, "_purge_files", purge)
    assert bot_cleanup.sweep(orch, now=expiry + 1) == ["atlas"]


def test_live_disk_roster_prevents_stale_orchestrator_purge(tmp_path):
    orch = make_orch(tmp_path)
    orch.remove_bot("atlas")
    # Simulate an older client restoring the roster without knowing about jobs.
    orch.roster_path.write_text('{"bots":[{"name":"atlas","provider":"echo"}]}')
    assert bot_cleanup.sweep(orch, now=due(orch)) == []
    assert orch.paths.bot_memory("atlas").exists()


def test_no_automatic_deletion_of_old_orphans(tmp_path):
    orch = make_orch(tmp_path)
    orphan = orch.paths.bot_memory("old-orphan")
    orphan.mkdir()
    assert bot_cleanup.sweep(orch, now=10**12) == []
    assert orphan.exists()


def test_symlinks_cannot_delete_outside_home(tmp_path):
    orch = make_orch(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "valuable").write_text("keep")
    (orch.paths.bot_memory("atlas") / "link").symlink_to(outside, target_is_directory=True)
    orch.remove_bot("atlas")
    assert bot_cleanup.sweep(orch, now=due(orch)) == ["atlas"]
    assert (outside / "valuable").read_text() == "keep"


def test_machine_reserved_until_expiry_then_disk_removed(tmp_path, monkeypatch):
    orch = make_orch(tmp_path)
    backend = MachineBackend(orch.paths)
    machine = backend.pool.acquire("atlas", lambda n: False)
    bot_cleanup.schedule(orch.paths, "atlas", None, now=0)
    backend.pool.release("atlas")
    assert backend.pool.machines()[0].state == "retired"
    peer = backend.pool.acquire("nova", lambda n: False)
    assert peer.name != machine.name
    with pytest.raises(IsolationUnavailable, match="pending deletion"):
        backend.pool.acquire("atlas", lambda n: False)
    calls, state = fake_engine(monkeypatch, orch, machine)
    backend.purge_deleted("atlas")
    assert ("rm", machine.name) in calls
    assert ("volume", "rm", machine.volume) in calls
    assert not state.container and not state.volume
    assert backend.pool.machines()[0].state == "idle"
    assert backend.pool.machines()[1].bot == "nova"
    backend.purge_deleted("atlas")  # idempotent after successful cleanup


def fake_engine(monkeypatch, orch, machine, *, owner="atlas", running=False):
    calls = []
    state = SimpleNamespace(container=True, volume=True, fail_volume=False)
    info = {
        "Config": {"Labels": {OWNER_LABEL: "1", SECRET_BOT_LABEL: owner}},
        "State": {"Running": running},
        "Mounts": [
            {"Type": "volume", "Name": machine.volume, "Destination": "/home/agent"},
            {
                "Type": "bind",
                "Source": str(orch.paths.run / "bot-secrets" / "atlas"),
                "Destination": "/run/harness",
            },
        ],
    }

    def run(*args, **kw):
        calls.append(args)
        output, code = "", 0
        if args[0] == "ps":
            output = machine.name if state.container else ""
        elif args[0] == "inspect":
            output = json.dumps([info])
        elif args[0] == "rm":
            state.container = False
        elif args[:2] == ("volume", "ls"):
            output = machine.volume if state.volume else ""
        elif args[:2] == ("volume", "rm"):
            if state.fail_volume:
                code = 1
            else:
                state.volume = False
        return subprocess.CompletedProcess(args, code, output, "")

    monkeypatch.setattr("isolation.machines.engine.run", run)
    return calls, state


@pytest.mark.parametrize("owner,running", [("nova", False), ("atlas", True)])
def test_cleanup_refuses_reassigned_or_running_computer(tmp_path, monkeypatch, owner, running):
    orch = make_orch(tmp_path)
    backend = MachineBackend(orch.paths)
    machine = backend.pool.acquire("atlas", lambda n: False)
    bot_cleanup.schedule(orch.paths, "atlas", None)
    backend.pool.release("atlas")
    calls, _ = fake_engine(monkeypatch, orch, machine, owner=owner, running=running)
    with pytest.raises(IsolationUnavailable, match="ownership/state changed"):
        backend.purge_deleted("atlas")
    assert not any(c[0] == "rm" or c[:2] == ("volume", "rm") for c in calls)


def test_container_removed_disk_failure_is_retryable(tmp_path, monkeypatch):
    orch = make_orch(tmp_path)
    backend = MachineBackend(orch.paths)
    machine = backend.pool.acquire("atlas", lambda n: False)
    bot_cleanup.schedule(orch.paths, "atlas", None)
    backend.pool.release("atlas")
    _, state = fake_engine(monkeypatch, orch, machine)
    state.fail_volume = True
    with pytest.raises(IsolationUnavailable, match="disk"):
        backend.purge_deleted("atlas")
    assert backend.pool.machines()[0].state == "retired"
    state.fail_volume = False
    backend.purge_deleted("atlas")
    assert not state.volume


def test_stop_before_delete_reserves_saved_disk(tmp_path):
    orch = make_orch(tmp_path)
    backend = MachineBackend(orch.paths)
    machine = backend.pool.acquire("atlas", lambda n: False)
    backend.pool.release("atlas")
    bot_cleanup.schedule(orch.paths, "atlas", None)
    backend.reserve_deleted("atlas")
    assert backend.pool.machines()[0].state == "retired"
    assert backend.pool.acquire("nova", lambda n: False).name != machine.name


def test_private_prompts_answers_queue_and_streams_removed(tmp_path):
    from agent import messaging

    orch = make_orch(tmp_path)
    paths = orch.paths
    msg = messaging.Msg(to="atlas", frm="user", text="private")
    messaging.send(paths, msg)
    paths.stream_file(msg.id).write_text('{"type":"final"}\n')
    (paths.prompts / "prompt.json").write_text('{"bot":"atlas"}')
    (paths.answers / "prompt.json").write_text('{"value":"private answer"}')
    (paths.prompts / "peer.json").write_text('{"bot":"nova"}')
    orch.remove_bot("atlas")
    assert bot_cleanup.sweep(orch, now=due(orch)) == ["atlas"]
    assert not (paths.prompts / "prompt.json").exists()
    assert not (paths.answers / "prompt.json").exists()
    assert not paths.stream_file(msg.id).exists()
    assert not (paths.messages / "atlas").exists()
    assert (paths.prompts / "peer.json").exists()


def test_running_agent_defers_cleanup(tmp_path, monkeypatch):
    orch = make_orch(tmp_path)
    orch.remove_bot("atlas")
    # Finish the asynchronous job write before replacing its PID for this case.
    bot_cleanup.shutdown(orch, "atlas")
    path = bot_cleanup.job_path(orch.paths, "atlas")
    job = json.loads(path.read_text())
    job["pid"] = 12345
    path.write_text(json.dumps(job))
    monkeypatch.setattr(bot_cleanup, "pid_alive", lambda pid: True)
    monkeypatch.setattr("isolation.process_identity.read_cmdline", lambda pid: None)
    assert bot_cleanup.sweep(orch, now=due(orch)) == []
    assert orch.paths.bot_memory("atlas").exists()
    assert path.exists()


def test_failed_roster_write_does_not_reserve_live_bot_forever(tmp_path, monkeypatch):
    orch = make_orch(tmp_path)

    def fail_save():
        raise OSError("save failed")

    monkeypatch.setattr(orch, "_persist", fail_save)
    with pytest.raises(OSError, match="save failed"):
        orch.remove_bot("atlas")
    assert "atlas" in orch.roster.names()
    assert not bot_cleanup.job_path(orch.paths, "atlas").exists()


def test_corrupt_pool_defers_cleanup_instead_of_forgetting_disk(tmp_path):
    orch = make_orch(tmp_path)
    orch.remove_bot("atlas")
    orch.paths.machines_file().write_text("{bad json")
    assert bot_cleanup.sweep(orch, now=due(orch)) == []
    assert orch.paths.bot_memory("atlas").exists()
    assert bot_cleanup.job_path(orch.paths, "atlas").exists()
    with pytest.raises(IsolationUnavailable, match="ownership"):
        MachineBackend(orch.paths).pool.acquire("new-bot", lambda name: False)


def test_human_inbox_survives_bot_named_user(tmp_path):
    from agent import messaging

    orch = make_orch(tmp_path)
    orch.add_bot(name="user", provider="echo", start=False)
    paths = orch.paths
    message = messaging.send(paths, messaging.Msg(to="user", frm="nova", text="Shared inbox"))
    orch.remove_bot("user")
    assert bot_cleanup.sweep(orch, now=due(orch, "user")) == ["user"]
    assert message.exists()


def test_shared_room_stream_survives_participant_deletion(tmp_path):
    from agent import messaging

    orch = make_orch(tmp_path)
    paths = orch.paths
    msg = messaging.Msg(to="atlas", frm="nova", text="Group work", room="team")
    messaging.send(paths, msg)
    stream = paths.stream_file(msg.id)
    stream.write_text('{"type":"message","room":"team","text":"shared"}\n')
    orch.remove_bot("atlas")
    assert bot_cleanup.sweep(orch, now=due(orch)) == ["atlas"]
    assert stream.exists()


def test_resolved_prompt_answer_removed_after_prompt_file_expires(tmp_path):
    orch = make_orch(tmp_path)
    paths = orch.paths
    StateStore(paths).record_prompt_resolution(
        "old-prompt", bot="atlas", resolution={"state": "answered"}
    )
    answer = paths.answers / "old-prompt.json"
    answer.write_text('{"value":"private"}')
    orch.remove_bot("atlas")
    assert bot_cleanup.sweep(orch, now=due(orch)) == ["atlas"]
    assert not answer.exists()


def test_shared_pool_metadata_survives_bot_named_machines(tmp_path):
    orch = make_orch(tmp_path)
    orch.add_bot(name="machines", provider="echo", start=False)
    orch.remove_bot("machines")
    pool = orch.paths.machines_file()
    pool.write_text('{"machines": []}')
    assert bot_cleanup.sweep(orch, now=due(orch, "machines")) == ["machines"]
    assert json.loads(pool.read_text()) == {"machines": []}


def test_unrelated_malformed_prompt_does_not_block_cleanup(tmp_path):
    orch = make_orch(tmp_path)
    (orch.paths.prompts / "torn.json").write_text('{"bot":')
    (orch.paths.prompts / "invalid.json").write_text("[]")
    orch.remove_bot("atlas")
    assert bot_cleanup.sweep(orch, now=due(orch)) == ["atlas"]
    assert (orch.paths.prompts / "torn.json").exists()
    assert (orch.paths.prompts / "invalid.json").exists()
