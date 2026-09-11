"""Human decisions survive retries, worker restarts and changing model call ids."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent import tools
from agent.memory import Memory
from agent.streaming import (
    StreamReader,
    StreamWriter,
    answer_prompt,
    decision_context,
    get_prompt,
    list_prompts,
    sweep_skipped_prompts,
    sync_stale_prompts,
    write_answer,
    write_prompt,
)
from harness.approvals import ApprovalStore
from harness.paths import HarnessPaths
from harness.server import _Handler
from harness.statestore import store_for


@pytest.fixture
def paths(tmp_path):
    result = HarnessPaths.resolve(tmp_path / "home")
    result.ensure_layout(["atlas"])
    return result


def _handler(paths, payload, commits):
    handler = _Handler.__new__(_Handler)
    handler.orch = SimpleNamespace(paths=paths)
    handler._read_json = lambda: payload
    handler._send_json = lambda body, status=200: (status, body)
    handler._answer_control_return = lambda *a: None
    handler._commit_prompt_resolution = lambda *a: commits.append(a)
    return handler


def _prompt(paths, **extra):
    write_prompt(
        paths,
        {
            "id": "p1",
            "bot": "atlas",
            "type": "card",
            "card_type": "confirm",
            "payload": {"question": "Post this reply?", "detail": "exact text"},
            **extra,
        },
    )


def test_answer_retry_returns_original_resolution_without_new_chat(paths, monkeypatch):
    _prompt(paths)
    commits = []
    dispatched = []
    monkeypatch.setattr("harness.server.relay_bot_turn", lambda *a, **k: dispatched.append(a))
    handler = _handler(paths, {"id": "p1", "bot": "atlas", "value": "confirm"}, commits)
    first = handler._provide_answer()
    second = handler._provide_answer()
    assert first[0] == second[0] == 200
    assert first[1]["resolution"] == second[1]["resolution"]
    assert second[1]["replayed"] is True
    assert len(commits) == 1
    assert dispatched == []


@pytest.mark.parametrize("values", [("confirm", "confirm"), ("confirm", "cancel")])
def test_concurrent_answers_have_one_atomic_winner(paths, values):
    _prompt(paths)
    commits = []

    def submit(value):
        return _handler(
            paths, {"id": "p1", "bot": "atlas", "value": value}, commits
        )._provide_answer()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, values))
    assert sorted(result[0] for result in results) == (
        [200, 200] if values[0] == values[1] else [200, 409]
    )
    assert len(commits) == 1
    assert get_prompt(paths, "p1")["resolution"]["responded_value"] in values


def test_expired_confirmation_is_never_unscoped_chat(paths, monkeypatch):
    calls = []
    monkeypatch.setattr("harness.server.relay_bot_turn", lambda *a, **k: calls.append(a))
    response = _handler(
        paths, {"id": "missing", "bot": "atlas", "value": "confirm"}, []
    )._provide_answer()
    assert response[0] == 410
    assert calls == []


def test_prompt_bot_mismatch_is_rejected(paths):
    _prompt(paths)
    status, _ = answer_prompt(paths, "p1", "confirm", bot="different")
    assert status == "conflict"
    assert "resolution" not in get_prompt(paths, "p1")


def test_changed_task_revision_and_stopped_task_reject_answer(paths):
    store = store_for(paths)
    with store._tx() as conn:
        conn.execute(
            "INSERT INTO agent_tasks VALUES(?,?,?,?,?,?)",
            ("atlas", "user", "task1", 2, '{"status":"active"}', 1),
        )
    _prompt(paths, task_id="task1", task_revision=1, task_conversation="user")
    assert answer_prompt(paths, "p1", "confirm")[0] == "stale"
    assert list_prompts(paths) == []
    with store._tx() as conn:
        conn.execute("UPDATE agent_tasks SET revision=1,data=?", ('{"status":"stopped"}',))
    assert answer_prompt(paths, "p1", "confirm")[0] == "stale"


def _context(paths, *, call="c1", revision=1, writer=None):
    return tools.ToolContext(
        paths=paths,
        bot="atlas",
        memory=Memory(paths, "atlas"),
        task_id="task1",
        task_revision=revision,
        turn_id="r1",
        tool_call_id=call,
        writer=writer,
        approvals=ApprovalStore(paths, "atlas"),
    )


def _auto_answer(monkeypatch, value):
    waits = []

    def wait(ctx, done, **kwargs):
        prompt = list_prompts(ctx.paths)[0]
        waits.append(prompt["id"])
        assert write_answer(ctx.paths, prompt["id"], value)
        return done()

    monkeypatch.setattr(tools, "_wait_for_human", wait)
    return waits


@pytest.mark.parametrize(
    "name,args,value,expected",
    [
        ("confirm", {"question": "Post?", "detail": "exact reply"}, "confirm", "user confirmed"),
        ("confirm", {"question": "Post?", "detail": "exact reply"}, "cancel", "user cancelled"),
        (
            "ask_user_choice",
            {"question": "Which?", "options": ["A", "B"]},
            "Custom",
            "user chose: Custom",
        ),
    ],
)
def test_decision_replays_across_worker_and_provider_call_changes(
    paths, monkeypatch, name, args, value, expected
):
    waits = _auto_answer(monkeypatch, value)
    first = _context(paths, writer=StreamWriter(paths, "r1"))
    assert tools.default_tools()[name].handler(first, args) == expected
    recovered = _context(paths, call="new-provider-call", writer=StreamWriter(paths, "recovery"))
    assert tools.default_tools()[name].handler(recovered, args) == expected
    assert len(waits) == 1
    assert StreamReader(paths, "recovery")._read_new() == []
    block = decision_context(paths, "atlas", "task1", 1)
    assert args["question"] in block and value in block


def test_changed_payload_or_task_revision_requires_new_confirmation(paths, monkeypatch):
    waits = _auto_answer(monkeypatch, "confirm")
    ctx = _context(paths)
    tools._confirm(ctx, {"question": "Post?", "detail": "first reply"})
    tools._confirm(ctx, {"question": "Post?", "detail": "different reply"})
    tools._confirm(_context(paths, revision=2), {"question": "Post?", "detail": "first reply"})
    assert len(set(waits)) == 3


def test_pending_question_reuses_card_after_restart_without_appending_again(paths, monkeypatch):
    ctx = _context(paths, writer=StreamWriter(paths, "r1"))
    payload = {"question": "Post?", "detail": "exact reply"}
    row, created = tools._decision_prompt(ctx, "confirm", payload)
    assert created
    tools._emit_card(ctx, "confirm", payload, card_id=row["id"])
    assert sweep_skipped_prompts(paths, "atlas") == []
    waits = _auto_answer(monkeypatch, "confirm")
    recovered = _context(paths, call="after-restart", writer=StreamWriter(paths, "recovery"))
    assert tools._confirm(recovered, payload) == "user confirmed"
    events = StreamReader(paths, "recovery")._read_new()
    assert not any(event.type == "card" and event.mutation == "appended" for event in events)
    assert waits == [row["id"]]


def test_answer_survives_mailbox_consumption_before_worker_crash(paths, monkeypatch):
    ctx = _context(paths)
    row, _ = tools._decision_prompt(ctx, "confirm", {"question": "Post?"})
    assert write_answer(paths, row["id"], "confirm")
    assert tools.read_answer(paths, row["id"]) == "confirm"
    assert tools.read_answer(paths, row["id"]) is None
    monkeypatch.setattr(
        tools, "_wait_for_human", lambda *a, **k: pytest.fail("must replay saved decision")
    )
    assert (
        tools._confirm(_context(paths, call="recovered"), {"question": "Post?"}) == "user confirmed"
    )


def test_durable_resolution_outlives_client_reseed_ttl(paths, monkeypatch):
    _prompt(paths)
    answer_prompt(paths, "p1", "confirm")
    monkeypatch.setattr("agent.streaming.RESOLVED_PROMPT_TTL", -1)
    assert list_prompts(paths, include_resolved=True) == []
    assert not (paths.prompts / "p1.json").exists()
    assert answer_prompt(paths, "p1", "confirm")[0] == "replayed"


def test_worker_shutdown_preserves_pending_task_question(paths, monkeypatch):
    class Shutdown(BaseException):
        pass

    def stopped(*args, **kwargs):
        raise Shutdown()

    monkeypatch.setattr(tools, "_wait_for_human", stopped)
    with pytest.raises(Shutdown):
        tools._confirm(_context(paths), {"question": "Post?"})
    pending = list_prompts(paths)
    assert len(pending) == 1 and "resolution" not in pending[0]
    waits = _auto_answer(monkeypatch, "confirm")
    assert (
        tools._confirm(_context(paths, call="restarted"), {"question": "Post?"}) == "user confirmed"
    )
    assert waits == [pending[0]["id"]]


def test_decision_projection_is_bounded_and_prioritizes_pending_details(paths):
    ctx = _context(paths)
    for n in range(12):
        row, _ = tools._decision_prompt(
            ctx, "confirm", {"question": f"Action {n}?", "detail": "large payload " * 1000}
        )
        answer_prompt(paths, row["id"], "confirm")
    pending, _ = tools._decision_prompt(
        ctx, "confirm", {"question": "Pending action?", "detail": "exact pending payload " * 1000}
    )
    block = decision_context(paths, "atlas", "task1", 1, max_chars=800)
    assert len(block) <= 800
    assert pending["id"] in block and "Pending action?" in block
    assert '"details_omitted": true' in block
    assert get_prompt(paths, pending["id"])["payload"]["detail"] == "exact pending payload " * 1000


def test_registered_secrets_are_scrubbed_before_decision_storage_and_replay(paths, monkeypatch):
    import json

    from harness import redaction

    secret = 'secret-token-with-"quotes"-and-slash\\12345'
    sentinel = redaction.register_secret(secret, "decision-test")
    try:
        waits = _auto_answer(monkeypatch, "confirm")
        ctx = _context(paths)
        proposal = {
            "question": "Send this?",
            "proposed_action": {
                "tool": "message_agent",
                "arguments": {"to": "nova", "text": secret},
            },
        }
        assert tools._confirm(ctx, proposal) == "user confirmed"
        # Raw and sealed representations have one canonical decision identity.
        proposal["proposed_action"]["arguments"]["text"] = sentinel
        assert tools._confirm(_context(paths, call="restarted"), proposal) == "user confirmed"
        assert len(waits) == 1
        row, _ = tools._decision_prompt(ctx, "choice", {"question": secret, "options": [secret]})
        first_status, first = answer_prompt(paths, row["id"], secret)
        retry_status, retry = answer_prompt(paths, row["id"], secret)
        assert first_status == "applied" and retry_status == "replayed"
        assert first["resolution"] == retry["resolution"]
        assert first["resolution"]["responded_value"] == sentinel
        with store_for(paths)._connect() as conn:
            stored = " ".join(row[0] for row in conn.execute("SELECT payload FROM agent_prompts"))
            stored += " ".join(
                row[0] for row in conn.execute("SELECT payload FROM prompt_resolutions")
            )
        mirrored = " ".join(path.read_text() for path in paths.prompts.glob("*.json"))
        assert secret not in stored + mirrored
        assert json.dumps(secret)[1:-1] not in stored + mirrored
        assert sentinel in stored and sentinel in mirrored
    finally:
        redaction.registry().clear()


def test_governance_replay_is_bound_to_exact_tool_arguments(paths, monkeypatch):
    waits = _auto_answer(monkeypatch, "confirm")
    ctx = _context(paths)
    assert (
        tools.require_approval(
            ctx,
            "write",
            "github_comment",
            tool_name="github_comment",
            tool_arguments={"body": "first", "issue": 1},
        )
        is None
    )

    ctx.approvals.end_scope("c1")
    ctx.approvals.begin_turn()
    recovered = _context(paths, call="new-id")
    assert (
        tools.require_approval(
            recovered,
            "write",
            "github_comment",
            tool_name="github_comment",
            tool_arguments={"body": "first", "issue": 1},
        )
        is None
    )
    assert len(waits) == 1
    assert (
        tools.require_approval(
            recovered,
            "write",
            "github_comment",
            tool_name="github_comment",
            tool_arguments={"body": "second", "issue": 1},
        )
        is None
    )
    assert len(waits) == 2


def _save_task(paths, *, revision=1, status="active"):
    import json

    task = {
        "task_id": "task1",
        "conversation": "user",
        "revision": revision,
        "status": status,
        "connector_ids": ["github"],
        "provenance": [
            {
                "connector_id": "github",
                "source_kind": "user",
                "source_id": "human1",
                "matched_name": "GitHub",
            }
        ],
    }
    with store_for(paths)._tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO agent_tasks VALUES(?,?,?,?,?,?)",
            ("atlas", "user", "task1", revision, json.dumps(task), 1),
        )
    return task


def test_superseded_prompt_settles_history_and_stream_without_changing_approval(paths):
    from agent.history import user_thread

    _save_task(paths)
    ctx = _context(paths)
    ctx.task_conversation = "user"
    pending, _ = tools._decision_prompt(ctx, "confirm", {"question": "Pending?"})
    approved, _ = tools._decision_prompt(ctx, "confirm", {"question": "Approved?"})
    answer_prompt(paths, approved["id"], "confirm")
    _save_task(paths, revision=2)
    writer = StreamWriter(paths, "superseded")
    changed = sync_stale_prompts(paths, "atlas", writer=writer)
    assert [row["id"] for row in changed] == [pending["id"]]
    assert changed[0]["resolution"]["reason"] == "task_changed"
    assert get_prompt(paths, approved["id"])["resolution"]["approved"] is True
    events = StreamReader(paths, "superseded")._read_new()
    assert len(events) == 1 and events[0].mutation == "updated"
    history = user_thread(Memory(paths, "atlas"), peer="user")
    assert any(
        row.get("card_id") == pending["id"] and row["resolution"]["skipped"] for row in history
    )
    assert sync_stale_prompts(paths, "atlas") == []


def test_superseded_projection_retries_after_history_write_failure(paths, monkeypatch):
    _save_task(paths)
    ctx = _context(paths)
    ctx.task_conversation = "user"
    pending, _ = tools._decision_prompt(ctx, "confirm", {"question": "Pending?"})
    _save_task(paths, revision=2)
    original = Memory.log_card

    def failed(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(Memory, "log_card", failed)
    with pytest.raises(OSError, match="disk unavailable"):
        sync_stale_prompts(paths, "atlas")
    assert get_prompt(paths, pending["id"])["projection_pending"] is True
    monkeypatch.setattr(Memory, "log_card", original)
    assert [row["id"] for row in sync_stale_prompts(paths, "atlas")] == [pending["id"]]
    assert not get_prompt(paths, pending["id"]).get("projection_pending")


@pytest.mark.parametrize("custom_handler", [False, True])
def test_block_generated_names_and_prefilled_values_cannot_expand_scope(
    paths, monkeypatch, custom_handler
):
    from agent.blocks import read_block, write_block
    from harness.server import handle_block_action

    _save_task(paths)
    ctx = _context(paths)
    ctx.task_conversation = "user"
    out = tools._show_block(
        ctx,
        {
            "title": "Notion Webflow",
            "view": {"type": "progress", "value": 0.2},
            "task_id": "forged",
            "task_revision": 900,
        },
    )
    bid = out.split()[2]
    inst = read_block(paths, bid)
    assert inst["task_id"] == "task1" and inst["task_revision"] == 1
    captures = []
    monkeypatch.setattr("harness.server.relay_bot_turn", lambda *a, **kw: captures.append((a, kw)))
    if custom_handler:
        inst["block_type"] = "custom"
        write_block(paths, inst)
        monkeypatch.setattr(
            "harness.server.blocklib.find_block_def", lambda *a: SimpleNamespace(has_handler=True)
        )
        monkeypatch.setattr(
            "harness.server.blocklib.run_handler",
            lambda *a, **kw: {"message": "Use Notion and Webflow"},
        )
    handle_block_action(
        SimpleNamespace(paths=paths), bid, "Use Gmail", {"prefilled": "Notion", "Webflow": "Gmail"}
    )
    assert captures[0][1]["origin"] == "block_action"
    assert captures[0][1]["task_scope"]["connector_ids"] == ["github"]
    assert captures[0][1]["task_scope"]["conversation"] == "block:" + bid
    _save_task(paths, revision=2)
    if custom_handler:
        monkeypatch.setattr(
            "harness.server.blocklib.run_handler", lambda *a, **kw: pytest.fail("stale handler")
        )
    result = handle_block_action(SimpleNamespace(paths=paths), bid, "submit", {})
    assert "superseded" in result["error"]
    assert len(captures) == 1


def test_block_from_stopped_task_cannot_run_handler_or_dispatch(paths, monkeypatch):
    from agent.blocks import write_block
    from harness.server import handle_block_action

    _save_task(paths, status="stopped")
    write_block(
        paths,
        {
            "id": "blk_stopped",
            "bot": "atlas",
            "task_id": "task1",
            "task_revision": 1,
            "task_conversation": "user",
            "block_type": "custom",
            "title": "Restart",
            "blocking": False,
        },
    )
    monkeypatch.setattr(
        "harness.server.blocklib.find_block_def", lambda *a: SimpleNamespace(has_handler=True)
    )
    monkeypatch.setattr(
        "harness.server.blocklib.run_handler", lambda *a, **kw: pytest.fail("stopped handler")
    )
    monkeypatch.setattr(
        "harness.server.relay_bot_turn", lambda *a, **kw: pytest.fail("must not dispatch")
    )
    result = handle_block_action(SimpleNamespace(paths=paths), "blk_stopped", "submit", {})
    assert "stopped" in result["error"]


def test_block_from_unknown_source_cannot_start_fresh_actions(paths, monkeypatch):
    from agent.blocks import write_block
    from harness.server import handle_block_action

    _save_task(paths, status="unknown")
    write_block(
        paths,
        {
            "id": "blk_unknown",
            "bot": "atlas",
            "task_id": "task1",
            "task_revision": 1,
            "task_conversation": "user",
            "title": "Retry",
            "blocking": False,
        },
    )
    monkeypatch.setattr(
        "harness.server.relay_bot_turn", lambda *a, **kw: pytest.fail("must not dispatch")
    )
    result = handle_block_action(SimpleNamespace(paths=paths), "blk_unknown", "submit", {})
    assert "unresolved" in result["error"]


@pytest.mark.parametrize("ledger_state", ["inflight", "uncertain", "unavailable"])
def test_active_block_checks_delivery_hold_before_task_status_updates(
    paths, monkeypatch, ledger_state
):
    from agent.blocks import write_block
    from harness.delivery import DeliveryUnavailable
    from harness.server import handle_block_action

    _save_task(paths, status="active")
    write_block(
        paths,
        {
            "id": "blk_active",
            "bot": "atlas",
            "task_id": "task1",
            "task_revision": 1,
            "task_conversation": "user",
            "blocking": False,
            "block_type": "custom",
        },
    )

    def unresolved(self, bot, task):
        assert (bot, task) == ("atlas", "task1")
        if ledger_state == "unavailable":
            raise DeliveryUnavailable("disk unavailable")
        return [{"status": ledger_state}]

    monkeypatch.setattr("harness.delivery.Ledger.unresolved_actions", unresolved)
    monkeypatch.setattr(
        "harness.server.blocklib.run_handler", lambda *a, **kw: pytest.fail("held handler")
    )
    monkeypatch.setattr(
        "harness.server.relay_bot_turn", lambda *a, **kw: pytest.fail("must not dispatch")
    )
    result = handle_block_action(SimpleNamespace(paths=paths), "blk_active", "submit", {})
    assert "error" in result


def test_proposed_action_confirmation_is_reused_by_governance(paths, monkeypatch):
    from agent.govern import _action_name, classify

    waits = _auto_answer(monkeypatch, "confirm")
    ctx = _context(paths)
    args = {"to": "nova", "text": "exact message"}
    assert (
        tools._confirm(
            ctx,
            {
                "question": "Send this message?",
                "proposed_action": {
                    "tool": "message_agent",
                    "arguments": args,
                },
            },
        )
        == "user confirmed"
    )
    assert ctx.approvals.approvals() == []  # presentation never grants permission
    intent, target, _ = classify("message_agent", args)
    assert (
        tools.require_approval(
            ctx,
            _action_name(intent, "message_agent"),
            target,
            tool_name="message_agent",
            tool_arguments=args,
        )
        is None
    )
    assert len(waits) == 1
    assert (
        tools.require_approval(
            ctx,
            _action_name(intent, "message_agent"),
            target,
            tool_name="message_agent",
            tool_arguments={**args, "text": "changed"},
        )
        is None
    )
    assert len(waits) == 2
    row = get_prompt(paths, waits[0])
    assert row["payload"]["proposed_action"]["arguments"] == args
    assert "exact message" in row["payload"]["detail"]


def test_unknown_proposed_tool_does_not_open_a_prompt(paths):
    out = tools._confirm(
        _context(paths),
        {
            "question": "Send?",
            "proposed_action": {
                "tool": "invented_plugin_send",
                "arguments": {},
            },
        },
    )
    assert out.startswith("error:")
    assert list_prompts(paths) == []


def test_control_answer_keeps_holder_check_and_cannot_return_a_new_takeover(paths):
    from harness.control import Control

    control = Control(paths)
    control.take_over("atlas", holder="owner")
    control.request_return("atlas", "finish work", "return1")
    write_prompt(
        paths,
        {
            "id": "return1",
            "bot": "atlas",
            "type": "card",
            "card_type": "control_return",
            "payload": {"question": "Return?"},
        },
    )
    handler = _handler(paths, {}, [])
    handler.orch.control = control
    live = get_prompt(paths, "return1")
    response = _Handler._answer_control_return(
        handler, "return1", "accept", "atlas", live, "stranger"
    )
    assert response[0] == 403
    assert "resolution" not in get_prompt(paths, "return1")
    assert (
        _Handler._answer_control_return(handler, "return1", "accept", "atlas", live, "owner")[0]
        == 200
    )
    control.take_over("atlas", holder="owner")
    replay = _Handler._answer_control_return(
        handler, "return1", "accept", "atlas", get_prompt(paths, "return1"), "owner"
    )
    assert replay[0] == 200
    assert control.state("atlas").paused


def test_secret_values_cannot_enter_decision_store(paths):
    write_prompt(
        paths,
        {
            "id": "secret1",
            "bot": "atlas",
            "type": "secret_request",
            "name": "TOKEN",
            "task_id": "task1",
            "task_revision": 1,
        },
    )
    assert answer_prompt(paths, "secret1", "sensitive-value")[0] == "conflict"
    assert "sensitive-value" not in str(store_for(paths).prompts())
    assert decision_context(paths, "atlas", "task1", 1) == ""


def test_answer_store_failure_cannot_acknowledge_or_dispatch(paths, monkeypatch):
    _prompt(paths)
    monkeypatch.setattr(
        store_for(paths),
        "settle_prompt",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    with pytest.raises(OSError, match="disk unavailable"):
        _handler(paths, {"id": "p1", "value": "confirm"}, [])._provide_answer()
    assert "resolution" not in get_prompt(paths, "p1")


def test_websocket_hello_precedes_immediate_hub_broadcast(paths, monkeypatch):
    frames = []
    handler = _Handler.__new__(_Handler)
    handler.orch = SimpleNamespace(
        paths=paths,
        ws_hub=SimpleNamespace(
            add=lambda h: h._ws_send_json({"type": "bots"}), discard=lambda h: None
        ),
    )
    handler.headers = {"Sec-WebSocket-Key": "x", "Upgrade": "websocket"}
    handler.connection = SimpleNamespace(settimeout=lambda _: None)
    handler._authed = lambda: True
    handler.send_response = lambda *a: None
    handler.send_header = lambda *a: None
    handler.end_headers = lambda: None
    handler._ws_send_json = lambda frame: frames.append(frame)
    handler._ws_reseed = lambda: None
    handler._stop_stream = lambda: None
    handler.rfile = None
    monkeypatch.setattr("harness.server.wsproto.read_frame", lambda _: None)
    handler._websocket()
    assert [frame["type"] for frame in frames] == ["hello", "bots"]


@pytest.mark.parametrize(
    "kind,value",
    [
        ("confirm", "confirm"),
        ("choice", "Accept"),
        ("choice", "Decline"),
        ("choice", "ok post it now using Gmail"),
    ],
)
def test_late_answer_resumes_original_task_once_after_restart(paths, kind, value):
    from agent import messaging
    from agent.runtime import build_agent
    from harness import taskscope
    from harness.roster import Bot
    from providers.base import Completion, Provider

    task = taskscope.begin_task(
        paths, "atlas", "peer:user", text="Review a Reddit reply", input_id="original"
    )
    _prompt(
        paths,
        request_id="original",
        task_id=task["task_id"],
        task_revision=task["revision"],
        task_conversation="peer:user",
        **(
            {
                "type": "choice",
                "card_type": None,
                "question": "Post this reply?",
                "options": ["Accept", "Decline"],
            }
            if kind == "choice"
            else {}
        ),
    )
    handler = _handler(paths, {"id": "p1", "bot": "atlas", "value": value}, [])
    assert handler._provide_answer()[0] == 200

    class Recorder(Provider):
        def __init__(self):
            super().__init__(model="echo")
            self.calls = []

        def complete(self, messages, **kwargs):
            self.calls.append((messages, kwargs))
            return Completion(text="The approved reply is ready.", finish_reason="stop")

    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    provider = Recorder()
    agent.provider = provider
    assert agent.process_inbox_once()
    assert len(provider.calls) == 1
    assert taskscope.read_task(paths, "atlas", "peer:user")["task_id"] == task["task_id"]
    context = str(provider.calls)
    assert "Post this reply?" in context
    assert value in context
    assert task["task_id"] in context
    assert taskscope.read_task(paths, "atlas", "peer:user")["connector_ids"] == []
    assert handler._provide_answer()[1]["replayed"]
    assert not agent.process_inbox_once()
    restarted = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    assert not restarted.process_inbox_once()
    assert len(messaging.read_inbox(paths, "user")) == 1
    assert not [r for r in agent.memory._session_records() if r.get("role") == "in:user"]


def _answered_task(paths, *, conversation="peer:user", **extra):
    from harness import taskscope

    task = taskscope.begin_task(
        paths, "atlas", conversation, text="Review a reply", input_id="original"
    )
    _prompt(
        paths,
        request_id="original",
        task_id=task["task_id"],
        task_revision=task["revision"],
        task_conversation=conversation,
        **extra,
    )
    assert answer_prompt(paths, "p1", "confirm")[0] == "applied"
    return task


@pytest.mark.parametrize("status", ["stopped", "completed", "failed", "unknown"])
def test_answer_does_not_wake_inactive_task(paths, status):
    from agent import messaging
    from harness import taskscope

    task = _answered_task(paths)
    taskscope.mark_task(paths, "atlas", "peer:user", task["task_id"], task["revision"], status)
    messaging.queue_prompt_answers(paths, "atlas")
    assert messaging.pending(paths, "atlas") == []


def test_answer_consumed_by_live_waiter_does_not_queue_another_turn(paths):
    from agent import messaging

    _answered_task(paths)
    assert store_for(paths).read_prompt_answer("p1", consume=True) == "confirm"
    messaging.queue_prompt_answers(paths, "atlas")
    assert messaging.pending(paths, "atlas") == []


def test_original_recovery_request_keeps_ownership_of_answer(paths):
    from agent import messaging

    _answered_task(paths)
    messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="Review", id="original"))
    messaging.queue_prompt_answers(paths, "atlas")
    assert [m.id for m in messaging.pending(paths, "atlas")] == ["original"]
    assert not get_prompt(paths, "p1").get("answer_consumed")


def test_failed_answer_dispatch_retries_same_request(paths, monkeypatch):
    from agent import messaging
    from agent.runtime import build_agent
    from harness.roster import Bot

    _answered_task(paths)
    real_send = messaging.send
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)

    def interrupted_send(*args):
        result = real_send(*args)
        if args[1].origin == "prompt_answer":
            raise OSError("crash after inbox write, before decision commit")
        return result

    monkeypatch.setattr(messaging, "send", interrupted_send)
    assert not agent.process_inbox_once()
    pending = messaging.pending(paths, "atlas")
    assert len(pending) == 1  # A partial dispatch must not be archived as stale.
    first = pending[0]
    assert not get_prompt(paths, "p1").get("answer_consumed")
    assert not list(paths.processed("atlas").glob("*.json"))
    assert not agent.process_inbox_once()  # A repeated failure still retains it.
    assert [m.id for m in messaging.pending(paths, "atlas")] == [first.id]
    monkeypatch.setattr(messaging, "send", real_send)
    assert agent.process_inbox_once()
    assert get_prompt(paths, "p1")["answer_consumed"]
    assert not agent.process_inbox_once()
    assert len(messaging.read_inbox(paths, "user")) == 1


def test_answer_queued_before_task_change_is_archived_without_model_call(paths):
    from agent import messaging
    from agent.runtime import build_agent
    from harness import taskscope
    from harness.roster import Bot

    task = _answered_task(paths)
    messaging.queue_prompt_answers(paths, "atlas")
    taskscope.mark_task(paths, "atlas", "peer:user", task["task_id"], task["revision"], "stopped")
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    assert not agent.process_inbox_once()
    assert messaging.pending(paths, "atlas") == []
    assert not agent.memory._session_records()


@pytest.mark.parametrize(
    "conversation,extra",
    [
        ("thread:root-message", {}),
        ("room:team", {"room": "team"}),
        ("generated:dream:original", {"origin": "dream"}),
        ("generated:routine:original", {"origin": "routine"}),
    ],
)
def test_answer_preserves_conversation_and_origin(paths, conversation, extra):
    from agent import messaging

    task = _answered_task(paths, conversation=conversation, **extra)
    messaging.queue_prompt_answers(paths, "atlas")
    msg = messaging.pending(paths, "atlas")[0]
    assert messaging.continuation_scope(paths, "atlas", msg.resume)["task_id"] == task["task_id"]
    assert msg.room == extra.get("room")
    assert msg.thread_id == ("root-message" if conversation.startswith("thread:") else None)
    assert msg.resume["origin"] == extra.get("origin")
    from agent.runtime import Agent

    reply = Agent._response(msg, "Finished")
    assert reply.thread_id == msg.thread_id
    assert reply.room == msg.room
    assert reply.origin == extra.get("origin")


def test_replayed_decision_is_consumed_without_extra_turn(paths):
    from agent import messaging

    task = _answered_task(paths)
    ctx = _context(paths)
    ctx.task_id = task["task_id"]
    ctx.task_conversation = "peer:user"
    # Recreate the exact question with its stable task decision identity.
    row, _ = tools._decision_prompt(ctx, "choice", {"question": "Which?", "options": ["A", "B"]})
    assert answer_prompt(paths, row["id"], "A")[0] == "applied"
    assert (
        tools._ask_user_choice(ctx, {"question": "Which?", "options": ["A", "B"]})
        == "user chose: A"
    )
    assert store_for(paths).prompt(row["id"])["answer_consumed"]
    store_for(paths).read_prompt_answer("p1", consume=True)
    messaging.queue_prompt_answers(paths, "atlas")
    assert messaging.pending(paths, "atlas") == []
