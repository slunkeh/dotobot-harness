"""Channel abstraction + desktop view-model.

Control ops (takeover/teach) need no running bot. Streaming and the takeover
*pause* are verified end-to-end against real bot processes (echo provider).
"""

from __future__ import annotations

import time

import pytest

from channels.base import available_channels
from channels.viewmodel import DesktopViewModel
from harness.orchestrator import Orchestrator

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


def _orch(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    return orch


def _wait_running(orch, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if all(h.status.value == "running" for h in orch.status()):
            return True
        time.sleep(0.2)
    return False


def test_available_channels_lists_desktop_and_cli():
    ch = available_channels()
    assert ch["desktop"].startswith("implemented")
    assert ch["cli"].startswith("implemented")
    assert "telegram" in ch


def test_viewmodel_mentions_slash_and_rooms(tmp_path):
    vm = DesktopViewModel(_orch(tmp_path), "atlas")
    assert vm.mention_candidates("at") == ["atlas"]
    names = {i["name"] for i in vm.slash_catalog()}
    assert "memory" in names
    assert "soul" in names
    room = vm.create_room("Pair", ["atlas", "nova"])
    assert room.members == ["atlas", "nova"]
    assert any(r.id == room.id for r in vm.rooms())


def test_viewmodel_control_ops_without_running_bot(tmp_path):
    vm = DesktopViewModel(_orch(tmp_path), "atlas")
    assert vm.bots() == ["atlas", "nova"]

    assert vm.take_over().mode == "takeover"
    assert vm.control_state().paused
    assert vm.return_control().mode == "bot"

    vm.start_teach()
    vm.record_step("open the report")
    vm.record_step("export as CSV")
    path = vm.save_teach("export-report")
    assert path.endswith("SKILL.md")
    assert vm.control_state().mode == "bot"


def test_viewmodel_streams_reply(tmp_path):
    orch = _orch(tmp_path)
    vm = DesktopViewModel(orch, "atlas")
    orch.up()
    try:
        assert _wait_running(orch)
        events = list(vm.stream("hello there", timeout=20.0))
        assert any(e.type == "status" for e in events)
        finals = [e for e in events if e.type == "final"]
        assert finals and "hello there" in (finals[-1].text or "")
    finally:
        orch.down()


def test_takeover_does_not_block_chat(tmp_path):
    """A user chat during leftover takeover still replies; no return-control card."""
    orch = _orch(tmp_path)
    orch.up()
    try:
        assert _wait_running(orch)
        orch.control.take_over("atlas")

        _rid, reader = orch.chat_stream("atlas", "do something while paused")
        events = list(reader.events(timeout=15.0))
        assert any(e.type == "final" for e in events), events
        assert not any(e.type == "card" and e.card_type == "control_return" for e in events)
    finally:
        orch.down()


def test_stop_and_queue_commands_do_not_enter_inbox(tmp_path):
    from agent import messaging

    orch = _orch(tmp_path)
    orch.init()
    messaging.send(orch.paths, messaging.Msg(to="atlas", frm="user", text="waiting"))
    turns = orch.dispatch_chat("/queue", bot="atlas")
    events = list(turns[0][2].events(timeout=2.0))
    assert any("queued" in (e.text or "") for e in events if e.type == "final")
    assert messaging.pending(orch.paths, "atlas")  # original still waiting

    turns = orch.dispatch_chat("/queue clear", bot="atlas")
    list(turns[0][2].events(timeout=2.0))
    assert messaging.pending(orch.paths, "atlas") == []

    turns = orch.dispatch_chat("/stop", bot="atlas")
    assert orch.control.stop_requested("atlas")
    finals = [e for e in turns[0][2].events(timeout=2.0) if e.type == "final"]
    assert finals and "Stopped" in (finals[-1].text or "")
    assert messaging.pending(orch.paths, "atlas") == []


def test_stop_lands_on_the_user_thread_before_the_ack(tmp_path):
    """`/stop` skips the inbox so it can interrupt, but the user still said
    it. If the session log never records that line, a history reload after
    `Stopped.` draws the live `/stop` bubble *below* the reply."""
    from agent.history import user_thread
    from agent.memory import Memory

    orch = _orch(tmp_path)
    mem = Memory(paths=orch.paths, bot="atlas")
    mem.log_turn("s1", "in:user", "run the weekday scan", peer="user")
    orch.dispatch_chat("/stop", bot="atlas")
    rows = user_thread(Memory(paths=orch.paths, bot="atlas"), peer="user")
    texts = [r["text"] for r in rows]
    assert texts == ["run the weekday scan", "/stop"]


@pytest.mark.parametrize("command", ["/stop", "/queue", "/queue clear", "/queue drop"])
def test_session_command_preserves_client_message_identity(tmp_path, command):
    from agent.history import user_thread

    orch = _orch(tmp_path)
    for ident in ("first-client-send", "second-client-send"):
        orch.dispatch_chat(command, bot="atlas", message_id=ident)
    rows = user_thread(orch.memory_for("atlas"), peer="user")
    assert [row["message_id"] for row in rows] == ["first-client-send", "second-client-send"]
    assert [row["text"] for row in rows] == [command, command]
