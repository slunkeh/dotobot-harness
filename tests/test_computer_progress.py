"""Auto progress card while the bot drives Chrome / the desktop."""

from __future__ import annotations

import json

import pytest

from agent import messaging
from agent.history import user_thread
from agent.memory import Memory
from agent.runtime import (
    Agent,
    ComputerProgress,
    _computer_stage_label,
    _tool_step_label,
    build_agent,
)
from agent.streaming import StreamReader, StreamWriter
from harness.control import Control
from harness.paths import HarnessPaths
from harness.roster import Bot


class _Writer:
    def __init__(self) -> None:
        self.cards: list[tuple[str, str, dict]] = []

    def card(self, bot: str, card_id: str, card_type: str, payload: dict) -> None:
        self.cards.append((card_id, card_type, payload))


def test_stage_labels():
    assert _computer_stage_label("computer_open", {"app": "browser"}) == "Opening Chrome"
    assert _computer_stage_label("computer_screenshot", {}) == "Looking at the screen"
    assert _computer_stage_label("computer_click", {"node": "12"}) == "Clicking a control"
    assert _computer_stage_label("computer_click", {"x": 0.4, "y": 0.5}) == "Clicking"
    assert _tool_step_label("computer_open", {"app": "chrome"}) == "Opening Chrome"
    assert _tool_step_label("computer_move", {"x": 0.4, "y": 0.5}) == "Moving the pointer"
    assert _tool_step_label("computer_drag", {"path": []}) == "Dragging"
    assert _tool_step_label("computer_click", {"clicks": 2}) == "Double-clicking"


def test_progress_card_tracks_chrome_steps():
    w = _Writer()
    prog = ComputerProgress("site-helper", "req-1")
    assert prog.card_id == "computer-req-1"
    prog.on_tool(w, None, "computer_open", {"app": "browser"}, "active")
    prog.on_tool(w, None, "computer_open", {"app": "browser"}, "done")
    prog.on_tool(w, None, "computer_screenshot", {}, "active")
    prog.on_tool(w, None, "computer_screenshot", {}, "done")
    prog.on_tool(w, None, "computer_click", {"x": 0.2, "y": 0.3}, "active")
    prog.on_tool(w, None, "computer_click", {"x": 0.2, "y": 0.3}, "done")
    prog.finish(w, None)
    last = w.cards[-1]
    assert last[0] == "computer-req-1"
    assert last[1] == "progress"
    payload = last[2]
    assert payload["title"] == "Using Chrome"
    assert payload["state"] == "done"
    assert [s["label"] for s in payload["steps"]] == [
        "Opening Chrome",
        "Looking at the screen",
        "Clicking",
    ]
    assert all(s["status"] == "done" for s in payload["steps"])


def test_repeat_screenshots_collapse():
    w = _Writer()
    prog = ComputerProgress("atlas", "r")
    for _ in range(5):
        prog.on_tool(w, None, "computer_screenshot", {}, "active")
        prog.on_tool(w, None, "computer_screenshot", {}, "done")
    assert [s["label"] for s in prog.steps] == ["Looking at the screen"]
    assert prog.steps[0]["status"] == "done"


def test_cap_keeps_latest_stages():
    w = _Writer()
    prog = ComputerProgress("atlas", "r")
    names = [
        "computer_open",
        "computer_screenshot",
        "computer_click",
        "computer_type",
        "computer_key",
        "computer_scroll",
        "computer_screenshot",
        "computer_click",
        "computer_type",
        "computer_key",
    ]
    for name in names:
        prog.on_tool(w, None, name, {"app": "files", "key": "Return"}, "active")
        prog.on_tool(w, None, name, {"app": "files", "key": "Return"}, "done")
    assert len(prog.steps) == 8
    assert prog.steps[0]["label"] != "Opening files"


def test_show_progress_stops_auto_card():
    w = _Writer()
    prog = ComputerProgress("atlas", "r")
    prog.on_tool(w, None, "computer_screenshot", {}, "active")
    prog.on_tool(w, None, "computer_screenshot", {}, "done")
    n = len(w.cards)
    prog.note_show_progress(w, None)
    assert w.cards[-1][2]["state"] == "done"
    prog.on_tool(w, None, "computer_click", {}, "active")
    assert len(w.cards) == n + 1  # finish only, no new computer steps


def test_ignores_non_computer_tools():
    w = _Writer()
    prog = ComputerProgress("atlas", "r")
    prog.on_tool(w, None, "run_command", {"command": "ls"}, "active")
    assert w.cards == []


def test_persists_to_stream(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    writer = StreamWriter(paths, "rid-prog")
    prog = ComputerProgress("atlas", writer.request_id)
    prog.on_tool(writer, None, "computer_open", {"app": "browser"}, "active")
    prog.finish(writer, None)
    events = StreamReader(paths, "rid-prog")._read_new()
    cards = [e for e in events if e.type == "card"]
    assert cards
    assert cards[0].card_type == "progress"
    assert cards[0].id == "computer-rid-prog"
    assert cards[-1].payload["state"] == "done"


def test_finish_error_marks_active_steps():
    w = _Writer()
    prog = ComputerProgress("atlas", "r")
    prog.on_tool(w, None, "computer_click", {}, "active")
    prog.finish(w, None, error=True)
    assert w.cards[-1][2]["state"] == "error"
    assert w.cards[-1][2]["steps"][0]["status"] == "error"


def _agent(tmp_path, provider):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    agent = Agent(
        paths=paths,
        bot=Bot(name="atlas", role="terse", provider="echo"),
        provider=provider,
        memory=Memory(paths=paths, bot="atlas"),
        control=Control(paths),
        stream_delay=0.0,
    )
    return agent, paths


def _progress_cards(paths, request_id):
    return [e for e in StreamReader(paths, request_id)._read_new() if e.type == "card"]


def test_progress_card_settles_when_model_raises(tmp_path):
    from providers.base import Completion, Provider, ToolCall

    class BoomAfterShot(Provider):
        def __init__(self, model="m"):
            super().__init__(model)
            self.n = 0

        def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
            self.n += 1
            if self.n == 1:
                return Completion(
                    tool_calls=[ToolCall(id="c1", name="computer_screenshot", arguments={})],
                    finish_reason="tool_use",
                )
            raise RuntimeError("provider down")

    agent, paths = _agent(tmp_path, BoomAfterShot())
    with pytest.raises(RuntimeError, match="provider down"):
        agent._produce("user", "look at the screen", writer=StreamWriter(paths, "crash-prog"))
    cards = _progress_cards(paths, "crash-prog")
    assert cards
    assert cards[-1].id == "computer-crash-prog"
    assert cards[-1].payload["state"] == "error"
    hist = [r for r in user_thread(agent.memory, peer="user") if r.get("type") == "card"]
    assert hist
    assert hist[-1]["payload"]["state"] == "error"


def test_failed_show_progress_keeps_auto_card(tmp_path):
    from providers.base import Completion, Provider, ToolCall

    class ShotThenBadProgress(Provider):
        def __init__(self, model="m"):
            super().__init__(model)
            self.n = 0

        def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
            self.n += 1
            if self.n == 1:
                return Completion(
                    tool_calls=[ToolCall(id="c1", name="computer_screenshot", arguments={})],
                    finish_reason="tool_use",
                )
            if self.n == 2:
                return Completion(
                    tool_calls=[ToolCall(id="c2", name="show_progress", arguments={})],
                    finish_reason="tool_use",
                )
            if self.n == 3:
                return Completion(
                    tool_calls=[
                        ToolCall(id="c3", name="computer_click", arguments={"x": 0.2, "y": 0.3})
                    ],
                    finish_reason="tool_use",
                )
            return Completion(text="done", finish_reason="stop")

    agent, paths = _agent(tmp_path, ShotThenBadProgress())
    agent._produce("user", "drive chrome", writer=StreamWriter(paths, "bad-show"))
    cards = [e for e in _progress_cards(paths, "bad-show") if e.id == "computer-bad-show"]
    assert cards
    assert cards[-1].payload["state"] == "done"
    labels = [s["label"] for s in cards[-1].payload["steps"]]
    assert "Looking at the screen" in labels
    assert "Clicking" in labels


def test_poison_drop_settles_running_computer_card(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    agent = build_agent(paths, Bot(name="atlas", role="terse", provider="echo"), stream_delay=0.0)
    msg = messaging.Msg(to="atlas", frm="user", text="bad input")
    messaging.send(paths, msg)
    agent.memory.log_turn(
        agent.session_id,
        "card",
        "",
        peer="user",
        card_id=f"computer-{msg.id}",
        card_type="progress",
        payload={
            "title": "Using Chrome",
            "state": "running",
            "steps": [{"label": "Clicking", "status": "active"}],
        },
        frm="atlas",
    )
    paths.run.mkdir(parents=True, exist_ok=True)
    (paths.run / "atlas.attempt.json").write_text(
        json.dumps({"id": msg.id, "count": 2}), encoding="utf-8"
    )
    agent.process_inbox_once()
    hist = [
        r
        for r in user_thread(agent.memory, peer="user")
        if r.get("card_id") == f"computer-{msg.id}"
    ]
    assert hist
    assert hist[-1]["payload"]["state"] == "error"
    assert hist[-1]["payload"]["steps"][0]["status"] == "error"
    stream_cards = _progress_cards(paths, msg.id)
    assert stream_cards
    assert stream_cards[-1].payload["state"] == "error"
