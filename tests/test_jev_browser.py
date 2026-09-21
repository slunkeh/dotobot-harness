"""No paid API calls: exercise consent, batched choices, guarded execution and recovery."""

import copy
import json
from types import SimpleNamespace

import pytest

from agent import govern, jev_browser, policy
from agent.memory import Memory
from agent.tools import ToolContext
from harness import jev, jev_features
from harness.paths import HarnessPaths
from harness.redaction import register_secret
from harness.secrets import set_secret


def page():
    return {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Find a place",
        "elements": [
            {"id": "1", "role": "button", "label": "Search", "operations": ["CLICK"]},
            {"id": "2", "role": "input", "label": "City", "value": "", "operations": ["TYPE_TEXT"]},
            {
                "id": "3",
                "role": "select",
                "label": "Class",
                "operations": ["SELECT"],
                "options": [{"id": "0", "label": "Economy"}, {"id": "1", "label": "Business"}],
            },
        ],
        "scroll_down": True,
        "scroll_up": False,
        "unsupported": False,
    }


class Browser:
    def __init__(self):
        self.page = page()
        self.actions = []
        self.observations = 0
        self.result = "ok"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def observe(self):
        self.observations += 1
        return copy.deepcopy(self.page)

    def act(self, *args):
        self.actions.append(args)
        if isinstance(self.result, Exception):
            raise self.result
        self.page["text"] += " changed"
        return self.result


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.delenv(jev.KEY, raising=False)
    p = HarnessPaths.resolve(tmp_path)
    p.ensure_layout(["atlas"])
    set_secret(jev.KEY, "customer-test-key", p)
    jev_features.configure(p, {"enabled": True, "features": {"browser": True}})
    browser = Browser()
    ctx = ToolContext(
        paths=p,
        bot="atlas",
        memory=Memory(p, "atlas"),
        computer=SimpleNamespace(browser_session=lambda: browser),
        browser_check=lambda: None,
        browser_authorize=lambda *a: None,
        browser_text=lambda context: '{"text":"London"}',
    )
    ctx.test_browser = browser
    monkeypatch.setattr(jev_browser.time, "sleep", lambda _: None)
    return ctx


def judgment(monkeypatch, operation="CLICK", target="1", callback=None):
    calls = []

    def request(key, state, questions):
        calls.append((state, questions))
        if callback:
            callback()
        return {
            k: {
                "choice": operation
                if k == "operation"
                else target
                if k == operation
                else "uncertain",
                "confidence": 0.99,
            }
            for k in questions
        }

    monkeypatch.setattr(jev, "_request", request)
    return calls


def test_batches_only_compatible_targets_and_ignores_unused_heads(ctx, monkeypatch):
    calls = judgment(monkeypatch)
    result = json.loads(jev_browser.run(ctx, {"goal": "Find London", "max_steps": 1}))
    assert result["status"] == "fallback" and "budget" in result["reason"]
    assert len(calls) == 1
    questions = calls[0][1]
    assert set(questions["CLICK"]["criteria"]) == {"1", "uncertain"}
    assert set(questions["TYPE_TEXT"]["criteria"]) == {"2", "uncertain"}
    assert set(questions["SELECT"]["criteria"]) == {"3:0", "3:1", "uncertain"}
    assert ctx.test_browser.actions == [("CLICK", "1", None)]
    assert ctx.test_browser.observations == 2
    assert ctx.web_exposed
    assert "EXTERNAL_UNTRUSTED_CONTENT" in result["page"]


@pytest.mark.parametrize("change", [{"enabled": False}, {"features": {"browser": False}}])
def test_disabled_never_observes_or_calls_jev(ctx, monkeypatch, change):
    jev_features.configure(ctx.paths, change)
    monkeypatch.setattr(jev, "_request", lambda *a: pytest.fail("paid request"))
    assert jev_browser.run(ctx, {"goal": "Search"}).startswith("error:")
    assert not ctx.test_browser.observations


@pytest.mark.parametrize(
    "operation,target", [("CLICK", "2"), ("CLICK", "uncertain"), ("FAKE", "1")]
)
def test_invalid_or_uncertain_choices_do_not_act(ctx, monkeypatch, operation, target):
    judgment(monkeypatch, operation, target)
    result = json.loads(jev_browser.run(ctx, {"goal": "Search"}))
    assert result["status"] == "fallback"
    assert not ctx.test_browser.actions


def test_type_uses_literal_text_and_existing_type_permission(ctx, monkeypatch):
    calls = judgment(monkeypatch, "TYPE_TEXT", "2")
    checked = []
    ctx.browser_authorize = lambda name, args: checked.append((name, args))
    jev_browser.run(ctx, {"goal": "Find London", "max_steps": 1})
    assert len(calls) == 1
    assert checked[-1][0] == "computer_type"
    assert checked[-1][1]["node"] == "2" and checked[-1][1]["text"] == "London"
    assert checked[-1][1]["label"] == "City"
    assert checked[-1][1]["snapshot"]
    assert ctx.test_browser.actions == [("TYPE_TEXT", "2", "London")]


@pytest.mark.parametrize(
    "raw", ["not json", '{"text":""}', '{"text":true}', '{"text":"x","code":"bad"}']
)
def test_invalid_generated_text_never_reaches_browser(ctx, monkeypatch, raw):
    judgment(monkeypatch, "TYPE_TEXT", "2")
    ctx.browser_text = lambda context: raw
    jev_browser.run(ctx, {"goal": "Search"})
    assert not ctx.test_browser.actions


def test_select_uses_observed_option(ctx, monkeypatch):
    judgment(monkeypatch, "SELECT", "3:1")
    jev_browser.run(ctx, {"goal": "Choose Business", "max_steps": 1})
    assert ctx.test_browser.actions == [("SELECT", "3:1", None)]


def test_stop_or_disable_during_request_prevents_action(ctx, monkeypatch):
    judgment(monkeypatch, callback=lambda: jev_features.configure(ctx.paths, {"enabled": False}))
    result = json.loads(jev_browser.run(ctx, {"goal": "Search"}))
    assert "disabled" in result["reason"]
    assert not ctx.test_browser.actions


def test_new_instructions_while_generating_text_prevent_input(ctx, monkeypatch):
    judgment(monkeypatch, "TYPE_TEXT", "2")

    def text(context):
        ctx.browser_check = lambda: "New instructions arrived"
        return '{"text":"London"}'

    ctx.browser_text = text
    result = json.loads(jev_browser.run(ctx, {"goal": "Search"}))
    assert "New instructions" in result["reason"]
    assert not ctx.test_browser.actions


@pytest.mark.parametrize("result", ["stale", None, TimeoutError("may have clicked")])
def test_stale_and_unknown_actions_are_not_replayed(ctx, monkeypatch, result):
    judgment(monkeypatch)
    ctx.test_browser.result = result
    output = json.loads(jev_browser.run(ctx, {"goal": "Search"}))
    assert len(ctx.test_browser.actions) == 1
    assert output["status"] == ("fallback" if result == "stale" else "uncertain")
    assert ctx.computer_batch_failed == (result != "stale")


def test_done_is_verification_request_not_success(ctx, monkeypatch):
    judgment(monkeypatch, "DONE")
    result = json.loads(jev_browser.run(ctx, {"goal": "Search"}))
    assert result["status"] == "verify" and "Independently verify" in result["reason"]
    assert not ctx.test_browser.actions


def test_unsupported_page_and_missing_key_make_no_paid_call(ctx, monkeypatch):
    monkeypatch.setattr(jev, "_request", lambda *a: pytest.fail("paid call"))
    ctx.test_browser.page["unsupported"] = True
    jev_browser.run(ctx, {"goal": "Search"})
    assert not ctx.test_browser.actions
    ctx.test_browser.page["unsupported"] = False
    jev.disconnect(ctx.paths)
    jev_browser.run(ctx, {"goal": "Search"})
    assert not ctx.test_browser.actions


def test_existing_policy_denial_applies_to_inner_action(ctx, monkeypatch):
    judgment(monkeypatch, "TYPE_TEXT", "2")
    rules = policy.Policy(deny=(policy.Rule(tool="computer_type", source="test deny"),))
    ctx.browser_authorize = lambda name, args: govern.govern(
        ctx, name, args, paths=ctx.paths, bot=ctx.bot, policy=rules
    )
    output = json.loads(jev_browser.run(ctx, {"goal": "Search"}))
    assert "denied" in output["reason"].lower() or "refused" in output["reason"].lower()
    assert not ctx.test_browser.actions


def test_redacts_state_and_dynamic_question_labels(ctx, monkeypatch):
    secret = "registered-secret-in-a-label"
    register_secret(secret)
    ctx.test_browser.page["elements"][0]["label"] = secret
    calls = judgment(monkeypatch)
    jev_browser.run(ctx, {"goal": "Search", "max_steps": 1})
    assert secret not in json.dumps(calls)


@pytest.mark.parametrize("enabled", [False, True])
def test_runtime_offers_only_when_enabled_and_accounts_for_text_model(
    tmp_path, monkeypatch, enabled
):
    from agent import runtime
    from harness.usage import usage_file
    from providers.base import Completion, Provider, ToolCall
    from tests.test_compaction import _agent

    browser = Browser()

    class Model(Provider):
        id = "test"

        def complete(self, messages, **kwargs):
            if (kwargs.get("system") or "").startswith("Write the literal value"):
                assert kwargs["tools"] == []
                return Completion(text='{"text":"London"}', usage={"output_tokens": 7})
            offered = {t.name for t in kwargs.get("tools", [])}
            assert ("computer_browser" in offered) == enabled
            if enabled and not any(m.role == "tool" for m in messages):
                return Completion(
                    tool_calls=[
                        ToolCall(
                            id="browser-1",
                            name="computer_browser",
                            arguments={"goal": "Search London", "max_steps": 1},
                        )
                    ]
                )
            if enabled:
                assert browser.actions == [("TYPE_TEXT", "2", "London")]
            return Completion(text="Checked browser result", finish_reason="stop")

    agent, paths = _agent(tmp_path, Model(model="test"))
    set_secret(jev.KEY, "runtime-browser-test-key", paths)
    jev_features.configure(paths, {"enabled": True, "features": {"browser": enabled}})
    monkeypatch.setattr(runtime.HostComputer, "display_ready", lambda self: True)
    monkeypatch.setattr(runtime.HostComputer, "browser_session", lambda self, **kw: browser)
    judgment(monkeypatch, "TYPE_TEXT", "2")
    assert agent._produce("user", "Use the browser to search London") == "Checked browser result"
    records = [json.loads(line) for line in usage_file(paths).read_text().splitlines()]
    assert sum(r["requests"] for r in records) == (3 if enabled else 1)
    if enabled:
        assert sum(r["output_tokens"] for r in records) == 7
