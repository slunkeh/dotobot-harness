"""Stream resume: a reconnecting client re-attaches to an in-flight request."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from types import SimpleNamespace

import pytest

from agent import messaging, recovery
from agent.runtime import build_agent
from agent.streaming import StreamReader, StreamWriter
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.roster import Bot
from harness.server import make_server
from providers.base import Message
from tests.test_ws import WSClient

ROSTER = """
[[bots]]
name = "atlas"
role = "a terse research assistant"
provider = "echo"
"""


def _paths(tmp_path):
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas"])
    return p


def test_events_carry_monotonic_offsets(tmp_path):
    paths = _paths(tmp_path)
    w = StreamWriter(paths, "r1")
    w.delta("hello ")
    w.delta("world")
    w.final("hello world", "atlas")
    events = list(StreamReader(paths, "r1").events(timeout=2.0))
    offsets = [e.offset for e in events]
    assert all(isinstance(o, int) and o > 0 for o in offsets)
    assert offsets == sorted(offsets)


def test_reader_resumes_from_offset(tmp_path):
    paths = _paths(tmp_path)
    w = StreamWriter(paths, "r2")
    w.delta("first ")
    w.delta("second ")
    w.final("first second", "atlas")

    all_events = list(StreamReader(paths, "r2").events(timeout=2.0))
    # Resume just past the first delta (the first delta is chased by the
    # unthrottled `appended` upsert): only the tail replays.
    assert all_events[0].type == "delta"
    tail = list(StreamReader(paths, "r2", offset=all_events[0].offset).events(timeout=2.0))
    assert [e.type for e in tail] == ["message", "delta", "message", "final"]
    deltas = [e for e in tail if e.type == "delta"]
    assert [e.text for e in deltas] == ["second "]
    # the replayed upserts each carry the whole text so far — a client that
    # resumes mid-stream converges without replaying the first delta, and the
    # closing frame is always the authoritative final text
    assert [e.text for e in tail if e.type == "message"] == [
        "first ",
        "first second",
    ]


@pytest.fixture
def server(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    orch.up()
    deadline = time.time() + 10
    while time.time() < deadline and not all(h.status.value == "running" for h in orch.status()):
        time.sleep(0.2)
    httpd = make_server(orch, "127.0.0.1", 0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield ("127.0.0.1", port, orch)
    finally:
        httpd.shutdown()
        httpd.server_close()
        orch.down()


def test_ws_resume_replays_tail_of_finished_turn(server):
    host, port, orch = server
    # Run a turn to completion on one connection.
    c1 = WSClient(host, port)
    try:
        c1.send({"type": "chat", "bot": "atlas", "text": "resume me"})
        frames = c1.recv_until("final")
        rid = next(f["request_id"] for f in frames if f["type"] == "accepted")
        # Offset just past the first stream event ("thinking" status).
        first_offset = next(f["offset"] for f in frames if f["type"] == "status")
    finally:
        c1.close()

    # A fresh connection (post-restart in real life) resumes mid-stream.
    c2 = WSClient(host, port)
    try:
        c2.send({"type": "resume", "bot": "atlas", "request_id": rid, "offset": first_offset})
        frames = c2.recv_until("final")
        types = [f["type"] for f in frames]
        assert "resumed" in types
        assert "accepted" not in types  # no preamble on resume
        assert frames[-1]["type"] == "final"
        assert "resume me" in frames[-1]["text"]
        # Only the tail replays: the "thinking" status before the offset is skipped.
        statuses = [f["value"] for f in frames if f["type"] == "status"]
        assert "thinking" not in statuses
        assert any(f["type"] == "delta" for f in frames)
    finally:
        c2.close()


def test_ws_resume_unknown_request_fails_fast(server):
    host, port, _orch = server
    c = WSClient(host, port)
    try:
        c.send({"type": "resume", "bot": "atlas", "request_id": "nope123", "offset": 0})
        frames = c.recv_until("final")
        assert any(f["type"] == "error" for f in frames)
        assert frames[-1]["type"] == "final"
    finally:
        c.close()


def test_http_stream_endpoint_replays(server):
    host, port, orch = server
    rid, reader = orch.chat_stream("atlas", "http resume")
    assert reader.collect(timeout=15.0)

    req = urllib.request.Request(f"http://{host}:{port}/api/streams/{rid}?bot=atlas&offset=0")
    with urllib.request.urlopen(req, timeout=15) as r:
        body = r.read().decode()
    events = [
        json.loads(line[len("data: ") :]) for line in body.splitlines() if line.startswith("data: ")
    ]
    types = [e["type"] for e in events]
    assert types[0] == "resumed"
    assert "final" in types


def _steer(tmp_path, *, before=None):
    paths = _paths(tmp_path)
    agent = build_agent(paths, Bot(name="atlas", role="terse", provider="echo"), stream_delay=0)
    first = messaging.Msg(to="atlas", frm="user", text="start")
    recovery.admit_turn(agent.statestore, bot="atlas", session=agent.session_id, msg=first)
    agent.control.set_busy("atlas", first.id)
    if before is not None:
        before(StreamWriter(paths, first.id))
    follow = messaging.Msg(to="atlas", frm="user", text="ask me again")
    messaging.send(paths, follow)
    assert agent._inject_followups(
        [Message(role="user", content="start")],
        turn_id=first.id,
        thread_peer="user",
        room=None,
    )
    return agent, paths, first, follow


def _resume(agent, paths, request_id, offset=0):
    from harness.server import _Handler

    handler = object.__new__(_Handler)
    handler.orch = SimpleNamespace(paths=paths, control=agent.control)
    frames = []
    handler._resume_frames("atlas", request_id, offset, frames.append, park_prompts=False)
    return frames


def test_consumed_followup_resumes_the_live_request_without_settling_its_question(tmp_path):
    agent, paths, first, follow = _steer(tmp_path)
    frames = _resume(agent, paths, follow.id)
    assert [f["type"] for f in frames] == ["resumed", "steered"]
    assert frames[-1]["target_request_id"] == first.id
    assert frames[-1]["request_id"] == follow.id
    assert agent.control.busy_request("atlas") == first.id


def test_steered_followup_replay_survives_completion_and_an_acknowledged_offset(tmp_path):
    agent, paths, first, follow = _steer(tmp_path)
    StreamWriter(paths, first.id).final("the completed reply", "atlas")
    agent.control.clear_busy("atlas")
    recovery.settle_turn(agent.statestore, first.id)
    frames = _resume(agent, paths, follow.id, offset=10_000)
    assert [f["type"] for f in frames] == ["resumed", "steered"]
    assert frames[-1]["target_request_id"] == first.id


def test_pre_fix_followup_can_resume_using_the_active_recovery_claim(tmp_path):
    agent, paths, first, follow = _steer(tmp_path)
    paths.stream_file(follow.id).unlink(missing_ok=True)
    frames = _resume(agent, paths, follow.id)
    assert [f["type"] for f in frames] == ["resumed", "steered"]
    assert frames[-1]["target_request_id"] == first.id


def test_followup_relay_finishes_without_repeating_the_canonical_stream(tmp_path, monkeypatch):
    from harness import server as server_module

    agent, paths, first, follow = _steer(tmp_path)
    StreamWriter(paths, first.id).choice("atlas", "q1", "Which one?", ["A", "B"])
    monkeypatch.setattr(server_module, "SSE_CHAT_TIMEOUT", 0.01)
    checks = []
    monkeypatch.setattr(agent.control, "is_busy", lambda name: checks.append(name) or False)
    frames = []
    server_module.stream_turns(
        SimpleNamespace(paths=paths, control=agent.control),
        [("atlas", follow.id, StreamReader(paths, follow.id))],
        write=frames.append,
        announce=False,
        park_prompts=False,
    )
    assert [f["type"] for f in frames] == ["steered"]
    assert not checks


def test_stream_collect_follows_a_consumed_followup_to_its_actual_reply(tmp_path):
    _agent, paths, first, follow = _steer(tmp_path)
    StreamWriter(paths, first.id).final("the actual reply", "atlas")
    assert StreamReader(paths, follow.id).collect(timeout=0.2) == "the actual reply"


def test_recovery_requeues_a_consumed_followup_without_its_old_stream_alias(tmp_path):
    agent, paths, first, follow = _steer(tmp_path)
    recovery.startup_sweep(agent.statestore, paths, "atlas", alive=lambda _pid: False)
    assert {m.id for m in messaging.pending(paths, "atlas")} == {first.id, follow.id}
    assert not paths.stream_file(follow.id).exists()


def test_independent_readmission_drops_a_pre_crash_steer_acknowledgment(tmp_path):
    agent, paths, first, follow = _steer(tmp_path)
    # Crash just before consuming the inbox entry / recording it in a claim:
    # the surviving message is admitted normally after the original settles.
    recovery.settle_turn(agent.statestore, first.id)
    agent.control.clear_busy("atlas")
    messaging.send(paths, follow)
    assert agent.process_inbox_once()
    events = list(StreamReader(paths, follow.id).events(timeout=0.2))
    assert "steered" not in [ev.type for ev in events]
    assert events[-1].type == "final"
    assert "ask me again" in events[-1].text


def test_steer_acknowledgment_is_visible_before_the_inbox_entry_disappears(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    follow = messaging.Msg(to="atlas", frm="user", text="ask me again")
    messaging.send(paths, follow)
    original = messaging.mark_processed
    seen = []

    def observe(paths, name, path):
        from agent.streaming import read_steered

        ack = read_steered(paths, follow.id)
        assert ack is not None and ack.target_request_id == "original"
        assert path.exists()
        seen.append(follow.id)
        original(paths, name, path)

    monkeypatch.setattr(messaging, "mark_processed", observe)
    assert messaging.take_steer(paths, "atlas", "original") == [follow]
    assert seen == [follow.id]


def test_resume_handles_steering_published_between_its_liveness_checks(tmp_path, monkeypatch):
    from harness import server as server_module

    agent, paths, first, follow = _steer(tmp_path)
    agent.control.clear_busy("atlas")
    recovery.settle_turn(agent.statestore, first.id)
    # The first check preceded publication; replay sees the durable event.
    monkeypatch.setattr(server_module, "read_steered", lambda *args: None)
    frames = _resume(agent, paths, follow.id)
    assert [f["type"] for f in frames] == ["resumed", "steered"]
    assert frames[-1]["target_request_id"] == first.id


def test_cli_followup_does_not_replay_a_question_from_before_it_joined(tmp_path):
    def old_question(writer):
        writer.choice("atlas", "answered", "Old question?", ["A", "B"])
        writer.card_resolution(
            "atlas",
            "answered",
            "choice",
            {"question": "Old question?"},
            {"state": "answered", "responded_value": "B"},
        )
        writer.close()

    _agent, paths, first, follow = _steer(tmp_path, before=old_question)
    writer = StreamWriter(paths, first.id)
    writer.choice("atlas", "new", "New question?", ["C", "D"])
    writer.final("the new reply", "atlas")
    events = list(StreamReader(paths, follow.id).events(timeout=0.2))
    assert [(ev.id, ev.question) for ev in events if ev.type == "choice"] == [
        ("new", "New question?")
    ]
    assert events[-1].text == "the new reply"


@pytest.mark.parametrize("starting", [True, False])
def test_stream_waits_through_startup_but_not_a_stopped_bot(tmp_path, monkeypatch, starting):
    from harness import server as server_module

    paths = _paths(tmp_path)
    checks = []

    def is_starting(name):
        checks.append(name)
        if starting:
            StreamWriter(paths, "queued").final("startup completed", "atlas")
        return starting

    orch = SimpleNamespace(
        paths=paths,
        control=SimpleNamespace(is_busy=lambda _name: False),
        is_starting=is_starting,
    )
    monkeypatch.setattr(server_module, "SSE_CHAT_TIMEOUT", 0.01)
    frames = []
    server_module.stream_turns(
        orch,
        [("atlas", "queued", StreamReader(paths, "queued"))],
        write=frames.append,
        announce=False,
        park_prompts=False,
    )
    assert checks == ["atlas"]
    if starting:
        assert not any(f["type"] == "error" for f in frames)
        assert frames[-1]["text"] == "startup completed"
    else:
        assert [f["type"] for f in frames] == ["error", "final"]


def test_timeout_does_not_mark_a_late_reply_delivered(tmp_path, monkeypatch):
    from harness import server

    paths = _paths(tmp_path)
    orch = SimpleNamespace(paths=paths, control=SimpleNamespace(is_busy=lambda name: False))
    monkeypatch.setattr(server, "SSE_CHAT_TIMEOUT", 0.01)
    frames = []
    server.stream_turns(
        orch,
        [("atlas", "late", StreamReader(paths, "late"))],
        write=frames.append,
        announce=False,
        park_prompts=False,
    )
    assert any(f["type"] == "error" for f in frames)
    assert "late" not in getattr(orch, "_delivered_streams", {})
    StreamWriter(paths, "late").final("Actually finished", "atlas")
    server.stream_turns(
        orch,
        [("atlas", "late", StreamReader(paths, "late"))],
        write=frames.append,
        announce=False,
        park_prompts=False,
    )
    assert frames[-1]["text"] == "Actually finished"
    assert "late" in orch._delivered_streams
