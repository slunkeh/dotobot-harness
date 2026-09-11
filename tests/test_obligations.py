"""Ack obligations: accepted user messages always get answered."""

from __future__ import annotations

import time

import pytest

from agent import messaging, obligations
from agent.runtime import build_agent
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.roster import Bot

ROSTER = """
[[bots]]
name = "atlas"
role = "a terse research assistant"
provider = "echo"
"""


def _setup(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    agent = build_agent(paths, Bot(name="atlas", role="terse", provider="echo"), stream_delay=0.0)
    return agent, paths


def _accept(paths, text="hello", now=None):
    """Server-side accept: inbox the message AND record the obligation."""
    msg = messaging.Msg(to="atlas", frm="user", text=text)
    messaging.send(paths, msg)
    obligations.record_send(paths, "atlas", msg.id, now=now)
    return msg


class _Boom(BaseException):
    """Simulates the process dying mid-turn (not caught by `except Exception`)."""


def test_dispatch_chat_records_an_obligation(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    turns = orch.dispatch_chat("hello", bot="atlas")
    record = obligations.get(orch.paths, "atlas")
    assert record is not None
    assert record["message_ids"] == [turns[0][1]]
    assert record["redrives"] == 0


def test_multiple_pending_messages_coalesce_into_one_obligation(tmp_path):
    _, paths = _setup(tmp_path)
    _accept(paths, "first")
    first = obligations.get(paths, "atlas")
    _accept(paths, "second")
    record = obligations.get(paths, "atlas")
    assert record["coalesced"] == 2
    assert len(record["message_ids"]) == 2
    assert record["created_ts"] == first["created_ts"]  # same obligation, not a new one


def test_obligation_cleared_when_the_bot_replies(tmp_path):
    agent, paths = _setup(tmp_path)
    _accept(paths)
    agent.process_inbox_once()
    assert obligations.get(paths, "atlas") is None
    replies = [m for _p, m in messaging.read_inbox(paths, "user")]
    assert len(replies) == 1 and "hello" in replies[0].text


def test_obligation_survives_a_mid_turn_crash(tmp_path, monkeypatch):
    agent, paths = _setup(tmp_path)
    _accept(paths)

    def _die(*a, **k):
        raise _Boom

    monkeypatch.setattr(agent, "_produce", _die)
    with pytest.raises(_Boom):
        agent.process_inbox_once()
    # No reply went out: the obligation must still stand for the boot check.
    assert obligations.get(paths, "atlas") is not None


def test_obligation_stays_while_more_user_chats_are_queued(tmp_path):
    agent, paths = _setup(tmp_path)
    _accept(paths, "older request")
    _accept(paths, "latest request")
    agent.process_inbox_once()
    # One reply went out, but a user chat is still queued: not settled yet.
    assert obligations.get(paths, "atlas") is not None
    agent.process_inbox_once()
    assert obligations.get(paths, "atlas") is None


def test_poison_drop_notice_settles_the_obligation(tmp_path, monkeypatch):
    """The existing crash-retry cap already answers the user; no redrive after."""
    agent, paths = _setup(tmp_path)
    _accept(paths, "bad input")

    def _die(*a, **k):
        raise _Boom

    monkeypatch.setattr(agent, "_produce", _die)
    for _ in range(2):
        with pytest.raises(_Boom):
            agent.process_inbox_once()
    agent.process_inbox_once()  # third attempt: dropped with an error reply
    replies = [m for _p, m in messaging.read_inbox(paths, "user")]
    assert len(replies) == 1 and "dropped" in replies[0].text
    assert obligations.get(paths, "atlas") is None


# -- redrive ----------------------------------------------------------------


def test_redrive_fires_after_the_idle_window(tmp_path):
    _, paths = _setup(tmp_path)
    obligations.record_send(paths, "atlas", "m1", now=time.time() - 100)
    out = obligations.maybe_redrive(paths, "atlas", idle=20)
    assert out == obligations.RECOVERY_PROMPT
    assert "may have missed" in out  # worded as recovery, never as completion
    queued = [m for m in messaging.pending(paths, "atlas") if (m.frm or "") == "user"]
    assert [m.text for m in queued] == [obligations.RECOVERY_PROMPT]
    assert obligations.get(paths, "atlas")["redrives"] == 1


def test_redrive_waits_out_the_idle_window(tmp_path):
    _, paths = _setup(tmp_path)
    obligations.record_send(paths, "atlas", "m1")
    assert obligations.maybe_redrive(paths, "atlas", idle=20) is None
    assert messaging.pending(paths, "atlas") == []


def test_redrive_skipped_while_a_user_message_is_still_queued(tmp_path):
    """A message the run loop will reach on its own is never double-sent."""
    _, paths = _setup(tmp_path)
    _accept(paths, "still queued", now=time.time() - 100)
    assert obligations.maybe_redrive(paths, "atlas", idle=20) is None
    assert [m.text for m in messaging.pending(paths, "atlas")] == ["still queued"]


def test_queued_background_work_does_not_hold_an_obligation(tmp_path):
    """Routine/dream ticks send as frm="user" but are not user chats.

    A queued routine must neither keep a settled obligation alive nor
    suppress a due redrive — the obligation is about a human's message.
    """
    _, paths = _setup(tmp_path)
    obligations.record_send(paths, "atlas", "m1", now=time.time() - 100)
    messaging.send(
        paths,
        messaging.Msg(to="atlas", frm="user", text="[Routine: nightly]", origin="routine"),
    )
    obligations.settle(paths, "atlas")
    assert obligations.get(paths, "atlas") is None
    obligations.record_send(paths, "atlas", "m2", now=time.time() - 100)
    assert obligations.maybe_redrive(paths, "atlas", idle=20) == obligations.RECOVERY_PROMPT


def test_redrive_stops_after_three_counted_attempts(tmp_path):
    _, paths = _setup(tmp_path)
    t0 = 1000.0
    obligations.record_send(paths, "atlas", "m1", now=t0)
    for i in range(1, obligations.MAX_REDRIVES + 1):
        messaging.drop_pending(paths, "atlas")  # bot never picked it up
        assert obligations.maybe_redrive(paths, "atlas", now=t0 + 100 * i, idle=20)
        assert obligations.get(paths, "atlas")["redrives"] == i
    messaging.drop_pending(paths, "atlas")
    # Fourth check: no more nagging — the obligation is dropped as lost.
    assert obligations.maybe_redrive(paths, "atlas", now=t0 + 1000, idle=20) is None
    assert obligations.get(paths, "atlas") is None
    assert messaging.pending(paths, "atlas") == []


def test_boot_check_redrives_then_reply_settles(tmp_path):
    """The bot-start path: stale obligation -> recovery prompt -> reply clears."""
    agent, paths = _setup(tmp_path)
    obligations.record_send(paths, "atlas", "lost-message", now=time.time() - 100)
    agent.check_obligations()
    assert [m.text for m in messaging.pending(paths, "atlas")] == [obligations.RECOVERY_PROMPT]
    agent.process_inbox_once()
    assert obligations.get(paths, "atlas") is None
    replies = [m for _p, m in messaging.read_inbox(paths, "user")]
    assert len(replies) == 1


def test_check_obligations_stays_quiet_while_busy(tmp_path):
    agent, paths = _setup(tmp_path)
    obligations.record_send(paths, "atlas", "m1", now=time.time() - 100)
    agent.control.set_busy("atlas", "req-1")
    try:
        agent.check_obligations()
        assert messaging.pending(paths, "atlas") == []
        assert obligations.get(paths, "atlas")["redrives"] == 0
    finally:
        agent.control.clear_busy("atlas")
