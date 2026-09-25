import json
from unittest.mock import Mock

import pytest

from agent.continuity import assess, candidate
from harness.connectors import Connectors
from harness.paths import HarnessPaths
from harness.taskscope import begin_task
from providers.base import Completion


@pytest.fixture
def scope(tmp_path, monkeypatch):
    paths = HarnessPaths.resolve(tmp_path)
    paths.ensure_layout(["atlas"])
    records = [{"id": "original", "type": "notion", "name": "Notion", "oauth_configured": True}]
    monkeypatch.setattr(Connectors, "list", lambda self: records)
    first = begin_task(paths, "atlas", "peer:user", text="Use Notion to find the brief", input_id="one")
    return paths, first, records


@pytest.mark.parametrize("text", ["What did you find?", "Can you do that now?", "I already gave you access"])
def test_semantic_followup_keeps_account_but_revalidates_action(scope, text):
    paths, first, _ = scope
    provider = Mock()
    provider.complete.return_value = Completion(text='{"relation":"same_task"}')
    assert candidate(first, text)
    assert assess(provider, first, text)[0]
    second = begin_task(paths, "atlas", "peer:user", text=text, input_id="two",
                        continuation_of=(first["task_id"], first["revision"]))
    assert second["task_id"] == first["task_id"]
    assert second["connector_ids"] == ["original"]
    assert second["revision"] > first["revision"]
    assert second["latest_instruction"] == text


@pytest.mark.parametrize("text", ["Continue", "New task: check that", "Explain photosynthesis", 'Quote: "do that again"'])
def test_no_classifier_for_clear_or_external_messages(scope, text):
    assert not candidate(scope[1], text)


def test_classifier_receives_only_human_text_not_outcomes_or_grants(scope):
    first = {**scope[1], "outcome": "external webpage says ignore constraints",
             "provenance": [{"connector_id": "sensitive-account-id"}]}
    provider = Mock()
    provider.complete.return_value = Completion(text='{"relation":"same_task"}')
    assess(provider, first, 'What did you find?\n> Web text says select another account')
    request = json.dumps(provider.complete.call_args.args[0][0].content)
    assert "external webpage" not in request
    assert "sensitive-account-id" not in request
    assert "Web text" not in request
    assert provider.complete.call_args.kwargs["tools"] == []


@pytest.mark.parametrize("response", ['same_task', '{"relation":"same_task","approved":true}', '{"relation":"new_task"}', ''])
def test_only_exact_relation_verdict_is_accepted(scope, response):
    provider = Mock()
    provider.complete.return_value = Completion(text=response)
    assert not assess(provider, scope[1], "What did you find?")[0]


def test_assessed_identity_cannot_adopt_newer_task_or_new_subject(scope):
    paths, first, _ = scope
    current = begin_task(paths, "atlas", "peer:user", text="Explain photosynthesis", input_id="new")
    after = begin_task(paths, "atlas", "peer:user", text="What did you find?", input_id="late",
                       continuation_of=(first["task_id"], first["revision"]))
    assert after["task_id"] not in {first["task_id"], current["task_id"]}
    assert after["connector_ids"] == []
    explicit = begin_task(paths, "atlas", "peer:user", text="New topic: check that", input_id="explicit",
                          active_followup=True, continuation_of=(after["task_id"], after["revision"]))
    assert explicit["task_id"] != after["task_id"]


def test_disabled_or_replaced_accounts_never_reappear_on_semantic_continuation(scope):
    paths, first, records = scope
    records[:] = [{"id": "replacement", "type": "notion", "name": "Notion", "oauth_configured": True}]
    after = begin_task(paths, "atlas", "peer:user", text="What did you find?", input_id="two",
                       continuation_of=(first["task_id"], first["revision"]))
    assert after["connector_ids"] == []


def test_generated_text_cannot_use_a_classifier_verdict(scope):
    paths, first, _ = scope
    after = begin_task(paths, "atlas", "peer:user", text="What did you find?", input_id="generated",
                       trusted_user=False, continuation_of=(first["task_id"], first["revision"]))
    assert after["connector_ids"] == []


def test_runtime_provider_failure_defaults_to_new_scope(tmp_path, monkeypatch):
    from agent import continuity
    from agent.runtime import build_agent
    from harness.roster import Bot
    from harness.taskscope import read_task

    paths = HarnessPaths.resolve(tmp_path)
    paths.ensure_layout(["atlas"])
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent._produce("user", "Find the brief", turn_id="one")
    first = read_task(paths, "atlas", "peer:user")
    monkeypatch.setattr(continuity, "assess", Mock(side_effect=RuntimeError("offline")))
    agent._produce("user", "What did you find?", turn_id="two")
    assert read_task(paths, "atlas", "peer:user")["task_id"] != first["task_id"]


def test_classifier_bounds_all_human_fields(scope):
    provider = Mock()
    provider.complete.return_value = Completion(text='{"relation":"new_task"}')
    previous = {**scope[1], "objective": "x" * 10000, "latest_instruction": "y" * 10000}
    assert not assess(provider, previous, "z" * 10000)[0]
    payload = json.loads(provider.complete.call_args.args[0][0].content)
    assert {len(value) for value in payload.values()} == {2000}
