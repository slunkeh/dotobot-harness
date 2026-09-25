"""Advisory review cannot hide decisions or contradict evidence it cannot see."""

import json

import pytest

from agent.streaming import StreamReader, StreamWriter
from harness import jev, jev_features, prefs
from harness.paths import HarnessPaths
from harness.secrets import set_secret


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.delenv(jev.KEY, raising=False)
    paths = HarnessPaths.resolve(tmp_path)
    paths.ensure_layout(["atlas"])
    set_secret(jev.KEY, "test-review-key", paths)
    prefs.save(paths, {"jev_enabled": True, "jev_features": {"completion": True}})
    return paths


def test_all_omitted_restores_baseline_and_records_only_identifiers(paths, monkeypatch):
    monkeypatch.setattr(jev, "_request", lambda key, state, qs: {
        k: {"choice": "omit", "confidence": 1} for k in qs
    })
    items = [{"id": "record-a", "text": "private preference"}, {"text": "private fact"}]
    writer = StreamWriter(paths, "selection")
    assert jev.select_context(paths, "question", items, writer=writer) == items
    event = [e for e in StreamReader(paths, "selection")._read_new() if e.type == "tool"][-1]
    diagnostic = json.loads(event.detail)
    assert diagnostic["fallback"] == "all_omitted"
    assert len(diagnostic["kept"]) == 2 and not diagnostic["omitted"]
    assert "private" not in event.detail and "record-a" not in event.detail


def test_authoritative_decisions_survive_semantic_omission(paths, monkeypatch):
    items = [
        {"text": "Declined exact publication", "protected_context": True},
        {"text": "Relevant background"},
        {"text": "Unrelated background"},
    ]
    monkeypatch.setattr(jev, "_request", lambda key, state, qs: {
        k: {"choice": "keep" if k == "1" else "omit", "confidence": 1} for k in qs
    })
    assert jev.select_context(paths, "publication", items) == items[:2]


def test_protected_decision_content_stays_out_of_external_selection(paths, monkeypatch):
    captured = []

    def request(key, state, questions):
        captured.append((state, questions))
        return {k: {"choice": "keep", "confidence": 1} for k in questions}

    monkeypatch.setattr(jev, "_request", request)
    items = [{"text": "saved decision details", "protected_context": True}, {"text": "background"}]
    assert jev.select_context(paths, "question", items) == items
    assert "saved decision details" not in json.dumps(captured)
    assert "0" not in captured[0][1]


def test_completion_abstains_when_reviewer_lacks_images_or_prior_receipts(paths, monkeypatch):
    monkeypatch.setattr(jev, "_request", lambda *args: pytest.fail("incomplete review context"))
    text = "The item is published."
    assert jev_features.check_completion(
        paths, text, [], evidence_complete=False,
    ) == text


def test_unsupported_answer_is_repaired_once_without_conflicting_footer(paths, monkeypatch):
    reviews, repairs = [], []

    def request(key, state, questions):
        reviews.append(state)
        return {"supported": {
            "choice": "unsupported" if len(reviews) == 1 else "supported", "confidence": 1,
        }}

    def repair(instruction):
        repairs.append(instruction)
        return "The upload completed; installation is not yet verified."

    monkeypatch.setattr(jev, "_request", request)
    result = jev_features.check_completion(
        paths, "Installed.", [{"tool": "upload", "result": "uploaded"}], repair=repair,
    )
    assert result == "The upload completed; installation is not yet verified."
    assert len(repairs) == 1 and len(reviews) == 2
    assert "Do not retry" in repairs[0]
    assert "Verification note" not in result


def test_failed_repair_does_not_assert_failure_or_repeat_side_effect(paths, monkeypatch):
    monkeypatch.setattr(jev, "_request", lambda *args: {
        "supported": {"choice": "unsupported", "confidence": 1},
    })
    calls = []

    def repair(instruction):
        calls.append(instruction)
        return "Installed."

    result = jev_features.check_completion(paths, "Installed.", [], repair=repair)
    assert len(calls) == 1
    assert not result.startswith("Installed.")
    assert "may have" in result and "failed" not in result


def test_authoritative_history_result_never_filtered(paths, monkeypatch):
    prefs.save(paths, {"jev_enabled": True, "jev_features": {"tool_results": True}})
    monkeypatch.setattr(jev, "_request", lambda *args: pytest.fail("authoritative history filtered"))
    result = json.dumps({"results": [
        {"text": "x" * 1500, "protected_context": True},
        {"text": "y" * 1500}, {"text": "z" * 1500},
    ]})
    assert jev_features.filter_tool_result(paths, "request", result) == result


def test_runtime_revises_only_answer_without_repeating_tool(tmp_path, monkeypatch):
    from agent import runtime
    from agent.tools import Tool, default_tools
    from providers.base import Completion, Provider, ToolCall
    from tests.test_compaction import _agent

    calls, actions = [], []

    class Model(Provider):
        id = "test"

        def complete(self, messages, **kwargs):
            calls.append(kwargs)
            if "Draft answer:" in messages[-1].content:
                assert kwargs["tools"] == []
                return Completion(text="Uploaded; installation remains unchecked.")
            if not any(m.role == "tool" for m in messages):
                return Completion(text="", tool_calls=[ToolCall(id="read", name="recall", arguments={"query": "status"})])
            return Completion(text="Installed.")

    agent, paths = _agent(tmp_path, Model(model="test"))
    set_secret(jev.KEY, "test-review-key", paths)
    prefs.save(paths, {"jev_enabled": True, "jev_features": {"completion": True}})
    tools = dict(default_tools())

    def read(ctx, args):
        actions.append(args)
        return "Uploaded; not installed."

    tools["recall"] = Tool(tools["recall"].spec, read)
    monkeypatch.setattr(runtime, "default_tools", lambda: tools)
    monkeypatch.setattr(jev, "_request", lambda key, state, qs: {
        "supported": {"choice": "unsupported" if state["reply"] == "Installed." else "supported", "confidence": 1},
    })
    assert agent._produce("user", "Check the upload status") == "Uploaded; installation remains unchecked."
    assert len(calls) == 3 and len(actions) == 1


def test_runtime_keeps_visual_result_with_main_agent(tmp_path, monkeypatch):
    from agent import runtime
    from agent.tools import Tool, default_tools
    from providers.base import Completion, Provider, ToolCall
    from tests.test_compaction import _agent

    class Model(Provider):
        id = "test"

        def complete(self, messages, **kwargs):
            if not any(m.role == "tool" for m in messages):
                return Completion(text="", tool_calls=[ToolCall(id="read", name="recall", arguments={"query": "status"})])
            assert any(m.images for m in messages)
            return Completion(text="The item appears in the published list.")

    agent, paths = _agent(tmp_path, Model(model="test"))
    set_secret(jev.KEY, "test-review-key", paths)
    prefs.save(paths, {"jev_enabled": True, "jev_features": {"completion": True}})
    tools = dict(default_tools())

    def read(ctx, args):
        ctx.images.append(("image/png", b"synthetic image evidence"))
        return "Image attached."

    tools["recall"] = Tool(tools["recall"].spec, read)
    monkeypatch.setattr(runtime, "default_tools", lambda: tools)
    monkeypatch.setattr(jev, "_request", lambda *args: pytest.fail("image evidence must not be externally reviewed"))
    assert agent._produce("user", "Check publication") == "The item appears in the published list."


def test_runtime_does_not_discredit_prior_receipt_or_send_it_to_reviewer(tmp_path, monkeypatch):
    from harness.delivery import Ledger
    from providers.base import Completion, Provider
    from tests.test_compaction import _agent

    class Model(Provider):
        id = "test"

        def complete(self, messages, **kwargs):
            assert "saved-receipt" in kwargs["system"]
            return Completion(text="The earlier publication has a saved receipt.")

    agent, paths = _agent(tmp_path, Model(model="test"))
    set_secret(jev.KEY, "test-review-key", paths)
    prefs.save(paths, {"jev_enabled": True, "jev_features": {"completion": True}})
    monkeypatch.setattr(Ledger, "turn_rows", lambda *args: [
        {"target": "publish", "status": "sent", "detail": "saved-receipt"},
    ])
    monkeypatch.setattr(jev, "_request", lambda *args: pytest.fail("prior receipts stay local"))
    assert agent._produce("user", "What was completed?") == "The earlier publication has a saved receipt."
