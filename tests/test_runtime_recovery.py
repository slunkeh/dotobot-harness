"""Crash-safety of the agent inbox loop (at-least-once turns, poison cap)."""

import json

import pytest

from agent import messaging
from agent.runtime import build_agent
from harness.control import Control
from harness.paths import HarnessPaths
from harness.roster import Bot


def _setup(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    agent = build_agent(paths, Bot(name="atlas", role="terse", provider="echo"), stream_delay=0.0)
    return agent, paths


def _send_user_msg(paths, text="hello"):
    return messaging.send(paths, messaging.Msg(to="atlas", frm="user", text=text))


def test_user_chat_while_paused_still_replies(tmp_path):
    """A send during leftover takeover is not parked and does not ask for control back."""
    agent, paths = _setup(tmp_path)
    agent.reply_timeout = 6.0
    agent.control.take_over("atlas")
    _send_user_msg(paths, "what's on the screen")

    agent.process_inbox_once()
    assert agent.control.state("atlas").paused  # leftover hold is ignored, not cleared
    assert messaging.pending(paths, "atlas") == []
    replies = [m.text for _p, m in messaging.read_inbox(paths, "user")]
    assert replies


class _Boom(BaseException):
    """Simulates the process dying mid-turn (not caught by `except Exception`)."""


def test_message_survives_mid_turn_crash(tmp_path, monkeypatch):
    agent, paths = _setup(tmp_path)
    _send_user_msg(paths)

    def _die(*a, **k):
        raise _Boom

    monkeypatch.setattr(agent, "_produce", _die)
    with pytest.raises(_Boom):
        agent.process_inbox_once()
    # The turn never completed: the message must still be pending for retry.
    assert [m.text for m in messaging.pending(paths, "atlas")] == ["hello"]

    monkeypatch.undo()
    agent.process_inbox_once()
    assert messaging.pending(paths, "atlas") == []
    replies = [m for _p, m in messaging.read_inbox(paths, "user")]
    assert len(replies) == 1 and "hello" in replies[0].text


def test_poison_message_dropped_after_two_crashes(tmp_path, monkeypatch):
    agent, paths = _setup(tmp_path)
    _send_user_msg(paths, "bad input")

    def _die(*a, **k):
        raise _Boom

    monkeypatch.setattr(agent, "_produce", _die)
    for _ in range(2):
        with pytest.raises(_Boom):
            agent.process_inbox_once()
    # Third attempt: dropped with an error reply instead of crashing again.
    agent.process_inbox_once()
    assert messaging.pending(paths, "atlas") == []
    replies = [m for _p, m in messaging.read_inbox(paths, "user")]
    assert len(replies) == 1 and "dropped" in replies[0].text
    assert not (paths.run / "atlas.attempt.json").is_file()


def test_attempt_marker_cleared_on_success(tmp_path):
    agent, paths = _setup(tmp_path)
    _send_user_msg(paths)
    agent.process_inbox_once()
    assert not (paths.run / "atlas.attempt.json").is_file()


def test_handled_error_still_marks_processed(tmp_path, monkeypatch):
    agent, paths = _setup(tmp_path)
    _send_user_msg(paths)

    def _fail(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(agent, "_produce", _fail)
    agent.process_inbox_once()
    assert messaging.pending(paths, "atlas") == []
    replies = [m for _p, m in messaging.read_inbox(paths, "user")]
    assert len(replies) == 1 and "provider down" in replies[0].text


def test_busy_flag_cleared_when_owner_pid_dies(tmp_path):
    _, paths = _setup(tmp_path)
    ctrl = Control(paths)
    # A busy claim from a pid that no longer exists must not report busy.
    (paths.run / "atlas.busy").write_text(
        json.dumps({"request_id": "r1", "pid": 2**22 + 12345, "ts": 0}), encoding="utf-8"
    )
    assert not ctrl.is_busy("atlas")
    assert not (paths.run / "atlas.busy").is_file()


def test_legacy_busy_flag_still_counts(tmp_path):
    _, paths = _setup(tmp_path)
    ctrl = Control(paths)
    (paths.run / "atlas.busy").write_text("some-request-id", encoding="utf-8")
    assert ctrl.is_busy("atlas")
    assert ctrl.busy_request("atlas") == "some-request-id"


def test_inbox_drains_user_messages_in_order(tmp_path):
    agent, paths = _setup(tmp_path)
    _send_user_msg(paths, "older request")
    _send_user_msg(paths, "latest request")
    agent.process_inbox_once()
    assert [m.text for m in messaging.pending(paths, "atlas")] == ["latest request"]
    replies = [m.text for _p, m in messaging.read_inbox(paths, "user")]
    assert any("older request" in t for t in replies)
    agent.process_inbox_once()
    assert messaging.pending(paths, "atlas") == []


def test_first_model_call_runs_before_a_send_now_jump(tmp_path, monkeypatch):
    """A follow-up Send-now during prompt setup must not skip the first job."""
    from providers.base import Completion, Provider

    class Once(Provider):
        def __init__(self, model="m"):
            super().__init__(model)
            self.n = 0

        def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
            self.n += 1
            return Completion(text="inspected the repo", finish_reason="stop")

    agent, paths = _setup(tmp_path)
    agent.provider = Once()
    _send_user_msg(paths, "check the latest proposal in the github repo")

    from agent import toolselect

    orig = toolselect.select

    def inject(granted, skills, text, *, floor=12):
        messaging.send(
            paths, messaging.Msg(to="atlas", frm="user", text="use GitHub", now=True)
        )
        return orig(granted, skills, text, floor=floor)

    monkeypatch.setattr(toolselect, "select", inject)
    agent.process_inbox_once()
    replies = [m.text for _p, m in messaging.read_inbox(paths, "user")]
    assert any("inspected the repo" in t for t in replies)
    assert not any("Jumping to the message" in t for t in replies)
    assert agent.provider.n == 1


def test_send_now_message_jumps_the_queue(tmp_path):
    agent, paths = _setup(tmp_path)
    _send_user_msg(paths, "first in line")
    promoted = messaging.send(
        paths, messaging.Msg(to="atlas", frm="user", text="jump the queue", now=True)
    )
    assert promoted
    agent.process_inbox_once()
    assert [m.text for m in messaging.pending(paths, "atlas")] == ["first in line"]
    replies = [m.text for _p, m in messaging.read_inbox(paths, "user")]
    assert any("jump the queue" in t for t in replies)


def test_deferred_send_now_does_not_block_a_later_send_now(tmp_path):
    """Two Send-now chats: the first actually runs and the later
    one stays queued — no jump-line loop."""
    agent, paths = _setup(tmp_path)
    messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="stop", now=True))
    messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="fix it", now=True))
    agent.process_inbox_once()
    pending = [m.text for m in messaging.pending(paths, "atlas")]
    replies = [m.text for _p, m in messaging.read_inbox(paths, "user")]
    assert pending == ["fix it"]
    assert any("stop" in t for t in replies)
    assert not any("Jumping to the message" in t for t in replies)
    agent.process_inbox_once()
    assert messaging.pending(paths, "atlas") == []


def test_in_flight_turn_steers_plain_follow_up(tmp_path, monkeypatch):
    """A plain send while busy joins the live turn after the current tool."""
    from agent import tools as toolsmod
    from providers.base import Completion, Provider, ToolCall

    class OnceTool(Provider):
        def __init__(self, model="m"):
            super().__init__(model)
            self.n = 0
            self.seen: list[list[str]] = []

        def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
            self.n += 1
            self.seen.append([getattr(m, "content", "") or "" for m in messages])
            if self.n == 1:
                return Completion(
                    tool_calls=[ToolCall(id="c1", name="remember", arguments={"text": "x"})],
                    finish_reason="tool_use",
                )
            return Completion(text="finished with the follow-up", finish_reason="stop")

    agent, paths = _setup(tmp_path)
    agent.provider = OnceTool()

    def inject(ctx, args):
        messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="queued follow-up"))
        return "ok: remembered"

    monkeypatch.setattr(toolsmod, "_remember", inject)
    toolsmod._DEFAULT_TOOLS = None
    _send_user_msg(paths, "original work")
    agent.process_inbox_once()
    replies = [m.text for _p, m in messaging.read_inbox(paths, "user")]
    assert any("finished with the follow-up" in t for t in replies)
    assert messaging.pending(paths, "atlas") == []
    assert any("queued follow-up" in c for round_ in agent.provider.seen for c in round_)
    toolsmod._DEFAULT_TOOLS = None


def test_in_flight_turn_queues_follow_up_with_attachments(tmp_path, monkeypatch):
    """Attachments cannot steer; they wait for the next turn."""
    from agent import tools as toolsmod
    from providers.base import Completion, Provider, ToolCall

    class OnceTool(Provider):
        def __init__(self, model="m"):
            super().__init__(model)
            self.n = 0

        def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
            self.n += 1
            if self.n == 1:
                return Completion(
                    tool_calls=[ToolCall(id="c1", name="remember", arguments={"text": "x"})],
                    finish_reason="tool_use",
                )
            return Completion(text="finished the original work", finish_reason="stop")

    agent, paths = _setup(tmp_path)
    agent.provider = OnceTool()

    def inject(ctx, args):
        messaging.send(
            paths,
            messaging.Msg(
                to="atlas",
                frm="user",
                text="see this",
                attachments=[{"name": "x.png", "path": "x.png"}],
            ),
        )
        return "ok: remembered"

    monkeypatch.setattr(toolsmod, "_remember", inject)
    toolsmod._DEFAULT_TOOLS = None
    _send_user_msg(paths, "original work")
    agent.process_inbox_once()
    assert [m.text for m in messaging.pending(paths, "atlas")] == ["see this"]
    toolsmod._DEFAULT_TOOLS = None


def test_in_flight_turn_preempts_for_send_now_chat(tmp_path, monkeypatch):
    from agent import tools as toolsmod
    from providers.base import Completion, Provider, ToolCall

    class OnceTool(Provider):
        def __init__(self, model="m"):
            super().__init__(model)
            self.n = 0

        def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
            self.n += 1
            if self.n == 1:
                return Completion(
                    tool_calls=[ToolCall(id="c1", name="remember", arguments={"text": "x"})],
                    finish_reason="tool_use",
                )
            return Completion(text="should not finish original", finish_reason="stop")

    agent, paths = _setup(tmp_path)
    agent.provider = OnceTool()

    def inject(ctx, args):
        messaging.send(
            paths, messaging.Msg(to="atlas", frm="user", text="do this instead", now=True)
        )
        return "ok: remembered"

    monkeypatch.setattr(toolsmod, "_remember", inject)
    toolsmod._DEFAULT_TOOLS = None
    _send_user_msg(paths, "original work")
    agent.process_inbox_once()
    pending = [m.text for m in messaging.pending(paths, "atlas")]
    assert "original work" in pending
    assert "do this instead" in pending
    replies = [m.text for _p, m in messaging.read_inbox(paths, "user")]
    assert any("sent now" in t for t in replies)
    agent.process_inbox_once()
    assert [m.text for m in messaging.pending(paths, "atlas")] == ["original work"]
    toolsmod._DEFAULT_TOOLS = None


def test_busy_request_roundtrip(tmp_path):
    _, paths = _setup(tmp_path)
    ctrl = Control(paths)
    ctrl.set_busy("atlas", "req-42")
    assert ctrl.is_busy("atlas")
    assert ctrl.busy_request("atlas") == "req-42"
    ctrl.clear_busy("atlas")
    assert ctrl.busy_request("atlas") is None


def test_reply_quote_reaches_the_bot_turn(tmp_path):
    """A quoted reply folds the quoted message into the bot's turn."""
    agent, paths = _setup(tmp_path)
    messaging.send(
        paths,
        messaging.Msg(
            to="atlas",
            frm="user",
            text="what about this one?",
            quote={"id": "abc123", "author": "atlas", "text": "We could ship on Friday"},
        ),
    )
    agent.process_inbox_once()
    replies = [m.text for _p, m in messaging.read_inbox(paths, "user")]
    assert any('Replying to atlas, message abc123: "We could ship on Friday"' in t for t in replies)
