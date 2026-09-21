"""Feature opt-ins, conservative decisions and runtime ownership boundaries."""

import json
import sqlite3
from unittest.mock import Mock

import pytest

from agent import compaction
from agent.external import wrap_external
from agent.memory import Memory
from agent.streaming import StreamEvent, StreamReader, StreamWriter
from agent.tools import ToolContext, _recommend_handoff, _remember
from harness import jev, prefs
from harness import jev_features as features
from harness.paths import HarnessPaths
from harness.push import PushRelay
from harness.secrets import set_secret


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.delenv(jev.KEY, raising=False)
    p = HarnessPaths.resolve(tmp_path)
    p.ensure_layout(["atlas"])
    set_secret(jev.KEY, "test-customer-key", p)
    return p


def enable(paths, *names):
    features.configure(paths, {"enabled": True, "features": dict.fromkeys(names, True)})


def answer(monkeypatch, values, confidence=0.99):
    def request(key, state, questions):
        return {k: {"choice": values[k], "confidence": confidence} for k in questions}

    monkeypatch.setattr(jev, "_request", request)


def test_new_checks_default_off_and_old_enable_does_not_expand_consent(paths, monkeypatch):
    monkeypatch.setattr(jev, "_request", lambda *a: pytest.fail("unexpected paid call"))
    jev.set_enabled(paths, True)
    assert not any(features.settings(paths).values())
    assert features.summary_problem(paths, "source", "summary") == ""
    assert features.review_memory(paths, "fact", []) is None
    assert features.check_completion(paths, "Done", []) == "Done"
    assert features.notification_priority(paths, "urgent") == 0


def test_settings_validate_before_mutation_and_preserve_other_features(paths):
    prefs.save(paths, {"caveman": True})
    enable(paths, "memory")
    features.configure(paths, {"features": {"compaction": True}})
    before = prefs.load(paths)
    assert before["caveman"] and before["jev_features"]["memory"]
    for change in [{"features": {"fake": True}}, {"features": {"memory": 1}}, {"enabled": "yes"}]:
        with pytest.raises(jev.JevError):
            features.configure(paths, change)
        assert prefs.load(paths) == before
    jev.set_enabled(paths, False)
    assert not features.enabled(paths, "memory")


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"coverage": []},
        {"coverage": {"choice": [], "confidence": 1}},
        {"coverage": {"choice": "missing", "confidence": True}},
        {"coverage": {"choice": "missing", "confidence": float("nan")}},
    ],
)
def test_malformed_judgments_fall_back(paths, monkeypatch, bad):
    enable(paths, "compaction")
    monkeypatch.setattr(jev, "_request", lambda *a: bad)
    assert features.summary_problem(paths, "important", "summary") == ""


def test_oversize_and_missing_key_skip_network(paths, monkeypatch):
    enable(paths, "compaction")
    monkeypatch.setattr(jev, "_request", lambda *a: pytest.fail("unexpected paid call"))
    assert features.summary_problem(paths, "x" * jev.MAX_INPUT_BYTES, "summary") == ""
    jev.disconnect(paths)
    assert features.summary_problem(paths, "important", "summary") == ""


def test_compaction_missing_constraint_retries_without_committing(paths, monkeypatch):
    from tests.test_compaction import FakeSummarizer, _seed

    memory = Memory(paths, "atlas")
    _seed(memory)
    before = memory._session_records()
    enable(paths, "compaction")
    answer(monkeypatch, {"coverage": "missing"})
    provider = FakeSummarizer()
    assert not compaction.maybe_compact(
        memory,
        peer="user",
        provider=provider,
        budget=250,
        session_id="s1",
        paths=paths,
        bot="atlas",
    )
    assert len(provider.calls) == 3
    assert memory._session_records() == before
    assert "lost or contradicted" in provider.calls[1][0][0].content


def test_compaction_uncertain_or_unavailable_keeps_baseline(paths, monkeypatch):
    enable(paths, "compaction")
    answer(monkeypatch, {"coverage": "missing"}, confidence=0.6)
    assert features.summary_problem(paths, "source", "summary") == ""

    def offline(*a):
        raise jev.JevError("offline")

    monkeypatch.setattr(jev, "_request", offline)
    assert features.summary_problem(paths, "source", "summary") == ""


def test_memory_flags_conflicts_without_replacing_facts(paths, monkeypatch):
    memory = Memory(paths, "atlas")
    memory.remember("Use account A")
    enable(paths, "memory")
    answer(monkeypatch, {"value": "durable", "relation": "conflict"})
    result = _remember(ToolContext(paths, "atlas", memory), {"text": "Use account B"})
    assert "conflict" in result
    assert [f["text"] for f in memory.facts()] == ["Use account A", "Use account B"]
    assert memory.facts()[-1]["jev_review"]["relation"] == "conflict"


def search_result():
    return json.dumps(
        {
            "results": [
                {"id": str(i), "url": f"https://example.org/{i}", "text": str(i) * 1500}
                for i in range(3)
            ],
            "nextPageToken": "next",
            "total": 3,
        }
    )


def test_tool_filter_preserves_envelope_links_and_pagination(paths, monkeypatch):
    enable(paths, "tool_results")
    answer(monkeypatch, {"0": "keep", "1": "omit", "2": "uncertain"})
    original = wrap_external(search_result())
    selected = features.filter_tool_result(paths, "question", original)
    assert selected.splitlines()[0] == original.splitlines()[0]
    assert selected.splitlines()[-1] == original.splitlines()[-1]
    data = json.loads(selected.splitlines()[1])
    assert [r["id"] for r in data["results"]] == ["0", "2"]
    assert data["results"][0]["url"] == "https://example.org/0"
    assert data["nextPageToken"] == "next" and data["total"] == 3
    assert data["jev_selection"]["omitted"] == 1
    answer(monkeypatch, {"0": "omit", "1": "omit", "2": "omit"})
    assert features.filter_tool_result(paths, "question", original) == original


def test_tool_filter_never_changes_errors_or_unsupported_shapes(paths, monkeypatch):
    enable(paths, "tool_results")
    monkeypatch.setattr(jev, "_request", lambda *a: pytest.fail("must not filter"))
    for result in [
        "error: " + "x" * 5000,
        json.dumps({"error": "failure", "results": [{}] * 3, "detail": "x" * 5000}),
        "plain text " * 600,
    ]:
        assert features.filter_tool_result(paths, "question", result) == result


def test_completion_claim_is_flagged_not_rewritten_as_failure(paths, monkeypatch):
    enable(paths, "completion")
    answer(monkeypatch, {"supported": "unsupported"})
    text = features.check_completion(
        paths, "Installed.", [{"tool": "upload", "result": "uploaded"}]
    )
    assert text.startswith("Installed.") and "unverified" in text
    answer(monkeypatch, {"supported": "unsupported"}, confidence=0.5)
    assert features.check_completion(paths, "Installed.", []) == "Installed."


def test_handoff_advice_does_not_send_and_obeys_room_membership(paths, monkeypatch):
    from agent.messaging import Msg, send

    (paths.home / "roster.toml").write_text(
        '[[bots]]\nname="atlas"\nprovider="echo"\n[[bots]]\nname="web"\nrole="Websites"\nprovider="echo"\n[[bots]]\nname="private"\nprovider="echo"\n'
    )
    # No room: only this bot's own pending handoffs are shared.
    send(paths, Msg(to="web", frm="atlas", text="Update the website"))
    send(paths, Msg(to="web", frm="private", text="Do not share this other bot task"))
    enable(paths, "handoffs")
    captured = []

    def judge(key, state, questions):
        captured.append(state)
        return {
            "bot": {"choice": "0", "confidence": 0.99},
            "duplicate": {"choice": "yes", "confidence": 0.99},
        }

    monkeypatch.setattr(jev, "_request", judge)
    ctx = ToolContext(paths, "atlas", Memory(paths, "atlas"))
    result = json.loads(_recommend_handoff(ctx, {"task": "Update website"}))
    assert result["recommended_bot"] == "web" and result["possible_duplicate"]
    assert len(captured[0]["pending"]) == 1
    assert "Do not share" not in json.dumps(captured)
    # An invalid room must not fall back to the full roster.
    ctx.room = "missing"
    assert _recommend_handoff(ctx, {"task": "task"}).startswith("error:")
    assert len(captured) == 1


def test_priority_survives_stream_and_never_suppresses_notifications(paths, monkeypatch):
    enable(paths, "notifications")
    answer(monkeypatch, {"attention": "routine"})
    writer = StreamWriter(paths, "priority")
    writer.final(
        "Routine progress",
        "atlas",
        notification_priority=features.notification_priority(paths, "Routine progress"),
    )
    event = next(e for e in StreamReader(paths, "priority")._read_new() if e.type == "final")
    from harness.server import asdict_event

    forwarded = asdict_event(event)
    assert forwarded["notification_priority"] == -1
    assert event.notification_priority == -1
    assert StreamEvent.from_dict({"type": "final"}).notification_priority == 0
    relay = PushRelay(paths.home, "https://relay.example/push")
    relay.register({"id": "a" * 64, "secret": "b" * 64})
    relay.enqueue(
        {
            "bot": "atlas",
            "type": "final",
            "text": "progress",
            "request_id": "p",
            "notification_priority": -1,
        }
    )
    relay.enqueue(
        {
            "bot": "atlas",
            "type": "choice",
            "question": "Approve?",
            "id": "q",
            "notification_priority": -1,
        }
    )
    response = Mock(status=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr("urllib.request.build_opener", lambda *a: opener)
    assert relay.deliver_one()
    delivered = json.loads(opener.open.call_args.args[0].data)
    assert delivered["body"] == "Approve?" and "priority" not in delivered
    assert relay.deliver_one()
    with sqlite3.connect(relay.path) as db:
        assert db.execute("SELECT count(*) FROM outbox WHERE done=1").fetchone()[0] == 2


def test_runtime_filters_read_results_but_checks_original_receipt(tmp_path, monkeypatch):
    from agent import runtime
    from agent.tools import Tool, default_tools
    from providers.base import Completion, Provider, ToolCall
    from tests.test_compaction import _agent

    class Model(Provider):
        id = "test"

        def complete(self, messages, **kwargs):
            results = [m for m in messages if m.role == "tool"]
            if not results:
                return Completion(
                    text="",
                    tool_calls=[ToolCall(id="r", name="recall", arguments={"query": "find"})],
                )
            selected = json.loads(results[-1].content)
            assert len(selected["results"]) == 2
            return Completion(text="The app is installed.", finish_reason="stop")

    agent, paths = _agent(tmp_path, Model(model="test"))
    set_secret(jev.KEY, "test-customer-key", paths)
    enable(paths, "tool_results", "completion", "notifications")
    tools = dict(default_tools())
    tools["recall"] = Tool(tools["recall"].spec, lambda ctx, args: search_result())
    monkeypatch.setattr(runtime, "default_tools", lambda: tools)
    checked = []

    def judge(key, state, questions):
        if "supported" in questions:
            checked.append(state)
            return {"supported": {"choice": "unsupported", "confidence": 0.99}}
        if "attention" in questions:
            return {"attention": {"choice": "attention", "confidence": 0.99}}
        return {
            k: {"choice": "omit" if k == "1" else "keep", "confidence": 0.99} for k in questions
        }

    monkeypatch.setattr(jev, "_request", judge)
    writer = StreamWriter(paths, "runtime-check")
    reply = agent._produce("user", "Find the relevant documents", writer=writer)
    assert "unverified" in reply
    assert len(json.loads(checked[0]["tool_evidence"][0]["result"])["results"]) == 3
    final = next(e for e in StreamReader(paths, "runtime-check")._read_new() if e.type == "final")
    assert final.text == reply and final.notification_priority == 1
    assert any(r.get("text") == reply for r in agent.memory._session_records())


def test_extra_checks_scrub_registered_secret(paths, monkeypatch):
    from harness.redaction import register_secret

    enable(paths, "completion")
    register_secret("secret-for-jev-review")
    captured = []

    def judge(key, state, questions):
        captured.append(state)
        return {"supported": {"choice": "supported", "confidence": 1}}

    monkeypatch.setattr(jev, "_request", judge)
    features.check_completion(paths, "done", [{"result": "secret-for-jev-review"}])
    assert "secret-for-jev-review" not in json.dumps(captured)


def test_notifications_disabled_preserve_fifo(paths, monkeypatch):
    relay = PushRelay(paths.home, "https://relay.example/push")
    relay.register({"id": "a" * 64, "secret": "b" * 64})
    relay.enqueue(
        {
            "bot": "atlas",
            "type": "final",
            "text": "first",
            "request_id": "first",
            "notification_priority": -1,
        }
    )
    relay.enqueue({"bot": "atlas", "type": "choice", "question": "second", "id": "second"})
    with sqlite3.connect(relay.path) as db:
        rows = db.execute("SELECT payload FROM outbox ORDER BY created").fetchall()
    assert [json.loads(r[0])["priority"] for r in rows] == [0, 0]


def test_filter_never_grows_result_or_cuts_trust_boundary(paths, monkeypatch):
    enable(paths, "tool_results")
    answer(monkeypatch, {"0": "omit", "1": "keep", "2": "keep"})
    original = wrap_external(
        json.dumps({"items": [{"id": "tiny"}, {"text": "x" * 5000}, {"text": "y" * 5000}]})
    )
    assert features.filter_tool_result(paths, "query", original) == original
