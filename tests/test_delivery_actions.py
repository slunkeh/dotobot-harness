"""Task-scoped sends require durable intent and never replay unknown outcomes."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent import govern, policy
from harness import audit, delivery
from harness.paths import HarnessPaths


@pytest.fixture
def led(tmp_path):
    return delivery.Ledger(HarnessPaths.resolve(tmp_path))


def _prepare(led, *, task="task-1", text="hello"):
    digest = delivery.args_digest("message_agent", {"to": "nova", "text": text})
    row = led.prepare_action("atlas", task, "peer:nova", digest=digest)
    return ("atlas", task, "peer:nova", row["seq"]), digest, row


def test_same_action_reuses_its_receipt_in_current_task_and_after_restart(led):
    key, digest, _ = _prepare(led)
    assert led.start_action(*key, digest=digest)
    led.finish_action(*key, digest=digest, outcome="sent", detail="remote receipt 123")
    for reopened in (led, delivery.Ledger(HarnessPaths.resolve(led.path.parent))):
        next_key, _, row = _prepare(reopened)
        assert next_key == key
        assert row["status"] == "sent"
        assert row["detail"] == "remote receipt 123"
        assert not reopened.start_action(*key, digest=digest)


@pytest.mark.parametrize("outcome", ["inflight", "uncertain"])
def test_unknown_action_is_not_retryable_without_restart(led, outcome):
    key, digest, _ = _prepare(led)
    assert led.start_action(*key, digest=digest)
    if outcome == "uncertain":
        led.finish_action(*key, digest=digest, outcome=outcome, detail="request timed out")
    repeated_key, _, row = _prepare(led)
    assert repeated_key == key
    assert row["status"] == outcome
    assert not led.start_action(*key, digest=digest)


def test_new_task_can_deliberately_repeat_same_send(led):
    key, digest, _ = _prepare(led)
    assert led.start_action(*key, digest=digest)
    led.finish_action(*key, digest=digest, outcome="sent")
    new_key, new_digest, row = _prepare(led, task="new-user-task")
    assert row["status"] == "pending"
    assert led.start_action(*new_key, digest=new_digest)


def test_distinct_payloads_to_same_target_remain_distinct(led):
    key, digest, _ = _prepare(led, text="first")
    assert led.start_action(*key, digest=digest)
    led.finish_action(*key, digest=digest, outcome="sent")
    other_key, other_digest, _ = _prepare(led, text="second")
    assert key != other_key
    assert led.start_action(*other_key, digest=other_digest)


def test_only_definitely_unsent_result_reopens_execution(led):
    key, digest, _ = _prepare(led)
    assert led.start_action(*key, digest=digest)
    led.finish_action(*key, digest=digest, outcome="unsent", detail="connection refused")
    assert led.action_state(*key[:3], digest=digest)["status"] == "pending"
    assert led.start_action(*key, digest=digest)
    led.finish_action(*key, digest=digest, outcome="uncertain")
    with pytest.raises(delivery.DeliveryUnavailable):
        led.finish_action(*key, digest=digest, outcome="unsent")
    assert led.action_state(*key[:3], digest=digest)["status"] == "uncertain"


def test_preparing_and_claiming_same_action_concurrently_only_starts_once(led):
    # Harness startup creates the database before agent processes are admitted.
    assert led.action_state("atlas", "task-1", "peer:nova", digest="not-yet") is None

    def attempt(_):
        key, digest, _ = _prepare(led)
        return led.start_action(*key, digest=digest)

    with ThreadPoolExecutor(max_workers=4) as pool:
        started = list(pool.map(attempt, range(8)))
    assert started.count(True) == 1
    assert len(led.turn_rows("atlas", "task-1")) == 1


def test_missing_intent_or_changed_payload_cannot_claim_execution(led):
    assert not led.start_action("atlas", "task", "peer:nova", digest="missing")
    key, _, _ = _prepare(led)
    assert not led.start_action(*key, digest="different payload")


def test_other_process_cannot_claim_different_action_after_stale_unresolved_read(led):
    other = delivery.Ledger(HarnessPaths.resolve(led.path.parent))
    first_key, first_digest, _ = _prepare(led, text="first")
    second_key, second_digest, _ = _prepare(other, text="second")
    assert other.unresolved_actions("atlas", "task-1") == []
    assert led.start_action(*first_key, digest=first_digest)
    assert not other.start_action(*second_key, digest=second_digest)
    assert other.unresolved_actions("atlas", "task-1")[0]["digest"] == first_digest
    led.finish_action(*first_key, digest=first_digest, outcome="sent")
    assert other.start_action(*second_key, digest=second_digest)


def test_broken_store_is_not_treated_as_no_previous_action(led):
    led.path.parent.mkdir(parents=True, exist_ok=True)
    led.path.write_text("corrupt SQLite file")
    with pytest.raises(delivery.DeliveryUnavailable):
        _prepare(led)
    with pytest.raises(delivery.DeliveryUnavailable):
        led.action_state("atlas", "task", "peer:nova", digest="digest")
    with pytest.raises(delivery.DeliveryUnavailable):
        led.start_action("atlas", "task", "peer:nova", digest="digest")
    with pytest.raises(delivery.DeliveryUnavailable):
        led.unresolved_actions("atlas", "task")


def test_failed_receipt_write_leaves_durable_inflight_action(led, monkeypatch):
    key, digest, _ = _prepare(led)
    assert led.start_action(*key, digest=digest)
    connect = led._connect

    def unavailable():
        raise OSError("disk unavailable")

    monkeypatch.setattr(led, "_connect", unavailable)
    with pytest.raises(delivery.DeliveryUnavailable):
        led.finish_action(*key, digest=digest, outcome="sent")
    monkeypatch.setattr(led, "_connect", connect)
    assert led.action_state(*key[:3], digest=digest)["status"] == "inflight"
    assert not led.start_action(*key, digest=digest)


def test_expiry_preserves_active_receipts_and_all_unknown_outcomes(led, monkeypatch):
    monkeypatch.setattr(delivery.time, "time", lambda: 100.0)
    for task, status in (("closed", "sent"), ("active", "sent"), ("unknown", "uncertain")):
        key, digest, _ = _prepare(led, task=task)
        assert led.start_action(*key, digest=digest)
        led.finish_action(*key, digest=digest, outcome=status)
    key, digest, _ = _prepare(led, task="inflight")
    assert led.start_action(*key, digest=digest)
    _prepare(led, task="unstarted-closed")
    _prepare(led, task="unstarted-active")
    monkeypatch.setattr(delivery.time, "time", lambda: 100.0 + delivery.RECEIPT_TTL + 1)
    assert led.prune_expired(active_tasks={("atlas", "active"), ("atlas", "unstarted-active")}) == 2
    assert not led.turn_rows("atlas", "closed")
    assert led.turn_rows("atlas", "active")[0]["status"] == "sent"
    assert led.turn_rows("atlas", "unknown")[0]["status"] == "uncertain"
    assert led.turn_rows("atlas", "inflight")[0]["status"] == "inflight"
    assert led.turn_rows("atlas", "unstarted-active")[0]["status"] == "pending"
    assert not led.turn_rows("atlas", "unstarted-closed")


def test_legacy_receipt_bridges_once_and_protects_later_current_task_repeats(led):
    digest = delivery.args_digest("message_agent", {"to": "nova", "text": "hello"})
    led.receipt("atlas", "old-turn", "peer:nova", digest=digest, detail="receipt from old version")
    consumed = set()
    exact, ambiguous = led.legacy_action(
        "atlas", "old-turn", "peer:nova", digest, consumed, task_id="task-1"
    )
    assert exact["detail"] == "receipt from old version"
    assert ambiguous is None
    assert led.legacy_action("atlas", "old-turn", "peer:nova", digest, consumed) == (None, None)
    key, repeated_digest, row = _prepare(led)
    assert row["status"] == "sent"
    assert not led.start_action(*key, digest=repeated_digest)


def test_legacy_mismatch_is_held_without_claiming_other_content_was_sent(led):
    led.receipt("atlas", "old-turn", "peer:nova", digest="different payload", detail="sent old")
    consumed = set()
    exact, ambiguous = led.legacy_action(
        "atlas", "old-turn", "peer:nova", "new payload", consumed, task_id="task-1"
    )
    assert exact is None
    assert ambiguous["digest"] == "different payload"
    assert led.action_state("atlas", "task-1", "peer:nova", digest="new payload") is None
    assert led.legacy_action(
        "atlas", "old-turn", "peer:nova", "new payload", consumed, task_id="task-1"
    ) == (None, ambiguous)


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("first_outcome", ["sent", "uncertain", "unsent"])
def test_runtime_repeated_action_uses_recorded_outcome(tmp_path, monkeypatch, batch, first_outcome):
    from agent.runtime import build_agent
    from agent.tools import default_tools
    from harness import taskscope
    from harness.roster import Bot
    from providers.base import Completion, Provider, ToolCall

    class Repeating(Provider):
        context_window = 128_000

        def __init__(self):
            super().__init__("test")
            self.calls = 0
            self.results = []

        def complete(self, messages, **kwargs):
            self.calls += 1
            self.results.extend(m.content for m in messages if m.role == "tool")
            if self.calls == 1 or (self.calls == 2 and not batch):
                count = 2 if batch else 1
                return Completion(
                    tool_calls=[
                        ToolCall(
                            id=f"provider-{self.calls}-{i}",
                            name="message_agent",
                            arguments={"to": "nova", "text": "hello"},
                        )
                        for i in range(count)
                    ]
                )
            return Completion(text="The recorded action was checked.")

    calls = []

    def send(ctx, args):
        calls.append(args)
        if len(calls) == 1 and first_outcome == "uncertain":
            return "error: request timed out after submission"
        if len(calls) == 1 and first_outcome == "unsent":
            return "error: connection refused"
        return "remote receipt 123"

    monkeypatch.setattr(default_tools()["message_agent"], "handler", send)
    paths = HarnessPaths.resolve(tmp_path)
    paths.ensure_layout(["atlas"])
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    provider = Repeating()
    agent.provider = provider
    agent._produce("user", "Ask nova to help", turn_id="turn-1")
    assert len(calls) == (2 if first_outcome == "unsent" else 1)
    task = taskscope.read_task(paths, "atlas", "peer:user")
    digest = delivery.args_digest("message_agent", {"to": "nova", "text": "hello"})
    row = delivery.Ledger(paths).action_state("atlas", task["task_id"], "peer:nova", digest=digest)
    assert row["status"] == ("uncertain" if first_outcome == "uncertain" else "sent")
    if first_outcome == "sent":
        assert any("already delivered" in result for result in provider.results)
    elif first_outcome == "uncertain":
        assert any("held back" in result for result in provider.results)


@pytest.mark.parametrize("field", ["task_state_error", "task_revision_changed", "delivery_error"])
def test_gate_holds_mutations_when_task_or_delivery_state_is_unavailable(tmp_path, field):
    paths = HarnessPaths.resolve(tmp_path)
    ctx = SimpleNamespace(**{field: True})
    approved = []
    claimed = []
    refusal = govern.govern(
        ctx,
        "message_agent",
        {"to": "nova", "text": "hello"},
        paths=paths,
        bot="atlas",
        policy=policy.parse({"ask": [{"intent": "message"}]}),
        approver=lambda *a, **k: approved.append(True),
        delivery_guard=lambda: claimed.append(True),
    )
    assert refusal.startswith("error:")
    assert not approved and not claimed
    assert audit.read(paths, "atlas")[-1]["source"] == "task-state"
    for tool in ("computer_screenshot", "recall", "show_table", "load_connector_tools"):
        assert govern.govern(ctx, tool, {}, paths=paths, bot="atlas") is None


@pytest.mark.parametrize(
    "failed_write", ["task", "prepare_action", "start_action", "finish_action"]
)
def test_runtime_delivery_storage_failures_do_not_repeat_send_and_keep_reads(
    tmp_path, monkeypatch, failed_write
):
    from agent.runtime import build_agent
    from agent.tools import default_tools
    from harness import taskscope
    from harness.roster import Bot
    from providers.base import Completion, Provider, ToolCall

    class ReadAfterSend(Provider):
        context_window = 128_000

        def __init__(self):
            super().__init__("test")
            self.called = False

        def complete(self, messages, **kwargs):
            if self.called:
                return Completion(text="Storage prevented completing the task.")
            self.called = True
            return Completion(
                tool_calls=[
                    ToolCall(id="a", name="message_agent", arguments={"to": "nova", "text": "hi"}),
                    ToolCall(id="b", name="recall", arguments={"query": "what happened"}),
                    ToolCall(id="c", name="message_agent", arguments={"to": "nova", "text": "hi"}),
                ]
            )

    sends = []
    reads = []
    monkeypatch.setattr(
        default_tools()["message_agent"],
        "handler",
        lambda ctx, args: sends.append(args) or "remote receipt 123",
    )
    monkeypatch.setattr(
        default_tools()["recall"], "handler", lambda ctx, args: reads.append(args) or "read works"
    )

    def unavailable(*args, **kwargs):
        raise delivery.DeliveryUnavailable("disk unavailable")

    if failed_write == "task":
        monkeypatch.setattr(taskscope, "begin_task", unavailable)
    else:
        monkeypatch.setattr(delivery.Ledger, failed_write, unavailable)
    paths = HarnessPaths.resolve(tmp_path)
    paths.ensure_layout(["atlas"])
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent.provider = ReadAfterSend()
    agent._produce("user", "Ask nova to help", turn_id="turn-1")
    assert len(sends) == (1 if failed_write == "finish_action" else 0)
    assert len(reads) == 1
    if failed_write == "finish_action":
        task = taskscope.read_task(paths, "atlas", "peer:user")
        assert task["status"] == "unknown"
        assert delivery.Ledger(paths).turn_rows("atlas", task["task_id"])[0]["status"] == "inflight"


@pytest.mark.parametrize("first_outcome", ["uncertain", "inflight"])
@pytest.mark.parametrize("replayed_tool", ["message_agent", "run_command", "computer_key"])
def test_resumed_task_with_unknown_send_cannot_resend_by_changing_payload(
    tmp_path, monkeypatch, first_outcome, replayed_tool
):
    from agent.runtime import build_agent
    from agent.tools import default_tools
    from harness.roster import Bot
    from providers.base import Completion, Provider, ToolCall

    class ProcessDied(BaseException):
        pass

    class SendOnce(Provider):
        context_window = 128_000

        def __init__(self, text, tool="message_agent"):
            super().__init__("test")
            self.text = text
            self.tool = tool
            self.called = False

        def complete(self, messages, **kwargs):
            if self.called:
                return Completion(text="Checked the action state.")
            self.called = True
            args = (
                {"command": "echo retry"}
                if self.tool == "run_command"
                else {"key": "ENTER"}
                if self.tool == "computer_key"
                else {"to": "nova", "text": self.text}
            )
            return Completion(tool_calls=[ToolCall("send", self.tool, args)])

    calls = []

    def send(ctx, args):
        calls.append(args)
        if len(calls) == 1:
            if first_outcome == "inflight":
                raise ProcessDied()
            return "error: request timed out after submission"
        return "second send was delivered"

    monkeypatch.setattr(default_tools()["message_agent"], "handler", send)
    monkeypatch.setattr(default_tools()["run_command"], "handler", send)
    monkeypatch.setattr(default_tools()["computer_key"], "handler", send)
    paths = HarnessPaths.resolve(tmp_path)
    paths.ensure_layout(["atlas"])
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent.provider = SendOnce("original payload")
    if first_outcome == "inflight":
        with pytest.raises(ProcessDied):
            agent._produce("user", "Ask nova to help", turn_id="turn-1")
    else:
        agent._produce("user", "Ask nova to help", turn_id="turn-1")
    restarted = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    restarted.provider = SendOnce("reworded payload", replayed_tool)
    restarted._produce("user", "continue", turn_id="turn-2")
    assert len(calls) == 1


def test_handler_cannot_mutate_preserved_tool_call_arguments(tmp_path, monkeypatch):
    from agent.runtime import build_agent
    from agent.tools import default_tools
    from harness.roster import Bot
    from providers.base import Completion, Provider, ToolCall

    class Script(Provider):
        context_window = 128_000

        def __init__(self):
            super().__init__("test")
            self.called = False

        def complete(self, messages, **kwargs):
            if self.called:
                call = next(call for msg in messages for call in (msg.tool_calls or []))
                assert call.arguments == {
                    "to": "nova",
                    "text": "approved text",
                    "metadata": {"source": "user"},
                }
                return Completion(text="Done.")
            self.called = True
            return Completion(
                tool_calls=[
                    ToolCall(
                        "send",
                        "message_agent",
                        {"to": "nova", "text": "approved text", "metadata": {"source": "user"}},
                    )
                ]
            )

    def handler(ctx, args):
        args["text"] = "handler scratch value"
        args["metadata"]["source"] = "handler scratch source"
        return "remote receipt 123"

    monkeypatch.setattr(default_tools()["message_agent"], "handler", handler)
    paths = HarnessPaths.resolve(tmp_path)
    paths.ensure_layout(["atlas"])
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent.provider = Script()
    agent._produce("user", "Ask nova to help", turn_id="turn-1")


def test_gate_receives_exact_action_and_claims_only_after_approval(tmp_path):
    paths = HarnessPaths.resolve(tmp_path)
    stages = []
    args = {"to": "nova", "text": "the approved text"}

    def approve(ctx, action, target, **kwargs):
        assert kwargs["tool_name"] == "message_agent"
        assert kwargs["tool_arguments"] == args
        stages.append("approved")

    def claim():
        assert stages == ["approved"]
        stages.append("claimed")

    assert (
        govern.govern(
            SimpleNamespace(),
            "message_agent",
            args,
            paths=paths,
            bot="atlas",
            policy=policy.parse({"ask": [{"intent": "message"}]}),
            approver=approve,
            delivery_guard=claim,
        )
        is None
    )
    assert stages == ["approved", "claimed"]


def test_delivery_claim_failure_is_a_gate_refusal(tmp_path):
    paths = HarnessPaths.resolve(tmp_path)

    def unavailable():
        raise delivery.DeliveryUnavailable("disk unavailable")

    refusal = govern.govern(
        SimpleNamespace(),
        "message_agent",
        {"to": "nova", "text": "hello"},
        paths=paths,
        bot="atlas",
        delivery_guard=unavailable,
    )
    assert refusal.startswith("error:")
    assert "not started" in refusal
    assert any(row["source"] == "delivery-state" for row in audit.read(paths, "atlas"))


def test_instruction_change_while_approval_waits_does_not_start_stale_action(tmp_path):
    ctx = SimpleNamespace()
    claimed = []

    def answer_old_card(ctx, *args, **kwargs):
        ctx.task_revision_changed = True
        return None

    refusal = govern.govern(
        ctx,
        "message_agent",
        {"to": "nova", "text": "old instruction"},
        paths=HarnessPaths.resolve(tmp_path),
        bot="atlas",
        policy=policy.parse({"ask": [{"intent": "message"}]}),
        approver=answer_old_card,
        delivery_guard=lambda: claimed.append(True),
    )
    assert refusal.startswith("error:")
    assert "new user instructions" in refusal
    assert not claimed


def test_policy_deny_and_human_decline_never_claim_delivery(tmp_path):
    paths = HarnessPaths.resolve(tmp_path)
    claimed = []
    for rule in ("deny", "ask"):
        refusal = govern.govern(
            SimpleNamespace(),
            "message_agent",
            {"to": "nova", "text": "hello"},
            paths=paths,
            bot="atlas",
            policy=policy.parse({rule: [{"intent": "message"}]}),
            approver=lambda *a, **k: "error: declined",
            delivery_guard=lambda: claimed.append(True),
        )
        assert refusal.startswith("error:")
    assert claimed == []
