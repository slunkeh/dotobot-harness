"""Fictional decision cards preserve action identity, routing and exact text."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent import blocks, govern, policy, tools
from agent.history import user_thread
from agent.memory import Memory
from agent.outgoing import submit
from agent.streaming import answer_prompt, get_prompt, list_prompts
from harness.paths import HarnessPaths
from harness.statestore import store_for


def button(label, action):
    return {"type": "button", "label": label, "action": action}


@pytest.mark.parametrize("extra", [{}, {"value": "yes"}])
def test_ambiguous_submit_decisions_are_rejected(extra):
    view = {
        "type": "row",
        "children": [
            button("Accept", {"kind": "submit", **extra}),
            button("Decline", {"kind": "submit", **extra}),
        ],
    }
    assert "error:" in blocks.validate_view(view)


def test_form_submit_named_actions_and_repeated_save_buttons_remain_valid():
    view = {
        "type": "column",
        "children": [
            button("Save", {"kind": "submit"}),
            {"type": "text_input", "name": "note"},
            button("Save", {"kind": "submit"}),
            button("Discard", {"kind": "action", "id": "discard"}),
        ],
    }
    assert blocks.validate_view(view) is None


@pytest.fixture
def context(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["sample-bot"])
    ctx = tools.ToolContext(
        paths=paths,
        bot="sample-bot",
        memory=Memory(paths, "sample-bot"),
        task_id="task-1",
        task_revision=1,
        task_conversation="peer:user",
        session_id="session-1",
        turn_id="request-1",
    )
    with store_for(paths)._tx() as conn:
        conn.execute(
            "INSERT INTO agent_tasks VALUES(?,?,?,?,?,?)",
            (
                ctx.bot,
                ctx.task_conversation,
                ctx.task_id,
                1,
                '{"status":"active"}',
                1,
            ),
        )
    return ctx


def approval(ctx, monkeypatch, answer="confirm"):
    def wait(ctx, done, **kwargs):
        row = list_prompts(ctx.paths)[0]
        answer_prompt(ctx.paths, row["id"], answer)
        return done()

    monkeypatch.setattr(tools, "_wait_for_human", wait)
    result = tools._confirm(
        ctx,
        {
            "question": "Post?",
            "allow_all": True,
            "outgoing_message": {
                "target_url": "https://forum.example.test/topic/42",
                "text": "Could you share the measurements?",
                "context": "Source: https://reference.example.test/report",
            },
        },
    )
    rows = store_for(ctx.paths).prompts(ctx.bot)
    return rows[0], result


def test_full_outgoing_proposal_is_reviewable_without_copying_metadata_into_text(
    context, monkeypatch
):
    row, result = approval(context, monkeypatch)
    assert row["id"] in result
    assert row["payload"]["question"] == "Post this exact message?"
    assert "forum.example.test/topic/42" in row["payload"]["detail"]
    assert "Could you share the measurements?" in row["payload"]["detail"]
    assert "Source:" not in row["subject"]["outgoing_message"]["text"]
    assert "context" not in row["subject"]["outgoing_message"]
    assert not row["payload"].get("allow_all")
    assert not row["payload"].get("allow_all_label")


@pytest.mark.parametrize(
    "change",
    ["declined", "bot", "task", "revision", "conversation", "forged", "text", "target", "stopped"],
)
def test_outgoing_gate_rejects_forged_stale_and_changed_proposals(context, monkeypatch, change):
    row, _ = approval(context, monkeypatch, "cancel" if change == "declined" else "confirm")
    args = {"approval_id": row["id"]}
    if change in {"bot", "task", "revision", "conversation"}:
        field = {
            "bot": "bot",
            "task": "task_id",
            "revision": "task_revision",
            "conversation": "task_conversation",
        }[change]
        setattr(context, field, 2 if change == "revision" else "other")
    if change == "forged":
        args["text"] = "Unapproved replacement"
    if change in {"text", "target"}:
        row["payload"]["outgoing_message"]["text" if change == "text" else "target_url"] += (
            "changed"
        )
        import json

        with store_for(context.paths)._tx() as conn:
            conn.execute(
                "UPDATE agent_prompts SET payload=? WHERE prompt_id=?", (json.dumps(row), row["id"])
            )
    if change == "stopped":
        with store_for(context.paths)._tx() as conn:
            conn.execute("UPDATE agent_tasks SET data=?", ('{"status":"stopped"}',))
    assert govern.govern(
        context, "computer_submit_approved", args, paths=context.paths, bot=context.bot
    )
    assert context.outgoing_approval is None


def test_outgoing_respects_policy_denial_and_never_prompts_twice(context, monkeypatch):
    row, _ = approval(context, monkeypatch)
    args = {"approval_id": row["id"]}
    forbidden = policy.parse({"deny": [{"tool": "computer_submit_approved"}]})
    assert govern.govern(
        context,
        "computer_submit_approved",
        args,
        paths=context.paths,
        bot=context.bot,
        policy=forbidden,
    )
    asks = policy.parse({"ask": [{"intent": "message"}]})
    assert (
        govern.govern(
            context,
            "computer_submit_approved",
            args,
            paths=context.paths,
            bot=context.bot,
            policy=asks,
            approver=lambda *a, **k: pytest.fail("second confirmation"),
        )
        is None
    )


def test_browser_submit_uses_only_saved_exact_message_and_replays_never_send(context, monkeypatch):
    row, _ = approval(context, monkeypatch)
    sent = []

    class Browser:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def prepare_outgoing(self, url, text):
            return "ready"

        def submit_outgoing(self, url, text):
            sent.append((url, text))
            return "ok"

    context.computer = SimpleNamespace(browser_session=Browser)
    args = {"approval_id": row["id"]}
    assert (
        govern.govern(
            context, "computer_submit_approved", args, paths=context.paths, bot=context.bot
        )
        is None
    )
    assert "dispatched once" in submit(context, args)
    assert sent == [("https://forum.example.test/topic/42", "Could you share the measurements?")]
    assert govern.govern(
        context, "computer_submit_approved", args, paths=context.paths, bot=context.bot
    )
    assert "error:" in submit(context, args)
    assert len(sent) == 1


def test_approval_execution_has_one_winner_across_workers(context, monkeypatch):
    row, _ = approval(context, monkeypatch)

    def claim(_):
        return store_for(context.paths).claim_prompt_execution(
            row["id"], bot=context.bot, task_id=context.task_id, revision=context.task_revision
        )

    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(claim, range(2))) == 1


def test_execution_claim_rechecks_answer_state_atomically(context, monkeypatch):
    import json

    row, _ = approval(context, monkeypatch)
    row["resolution"]["state"] = "skipped"
    with store_for(context.paths)._tx() as conn:
        conn.execute(
            "UPDATE agent_prompts SET payload=? WHERE prompt_id=?", (json.dumps(row), row["id"])
        )
    assert not store_for(context.paths).claim_prompt_execution(
        row["id"], bot=context.bot, task_id=context.task_id, revision=context.task_revision
    )


@pytest.mark.parametrize("ready", [False, True])
def test_unavailable_composer_or_uncertain_dispatch_never_replays(context, monkeypatch, ready):
    row, _ = approval(context, monkeypatch)

    class Browser:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def prepare_outgoing(self, url, text):
            return "ready" if ready else "unsupported"

        def submit_outgoing(self, url, text):
            raise TimeoutError("reply lost")

    context.computer = SimpleNamespace(browser_session=Browser)
    args = {"approval_id": row["id"]}
    assert (
        govern.govern(
            context, "computer_submit_approved", args, paths=context.paths, bot=context.bot
        )
        is None
    )
    assert "error:" in submit(context, args)
    recorded = get_prompt(context.paths, row["id"])
    assert bool(recorded.get("execution_started")) == ready


def test_card_history_preserves_thread_on_open_and_resolution(context):
    context.thread_id = "root-42"
    context.origin = "routine"
    payload = {"question": "Continue?"}
    row, _ = tools._decision_prompt(context, "confirm", payload)
    tools._persist_card(context, row["id"], "confirm", payload)
    tools._persist_card(
        context,
        row["id"],
        "confirm",
        payload,
        resolution={"state": "answered", "responded_value": "confirm"},
    )
    assert user_thread(context.memory, peer="user") == []
    history = user_thread(context.memory, peer="user", thread_id="root-42")
    assert len(history) == 1
    assert history[0]["thread_id"] == "root-42"
    assert history[0]["origin"] == "routine"
    assert history[0]["resolution"]["responded_value"] == "confirm"
    assert get_prompt(context.paths, row["id"])["thread_id"] == "root-42"


@pytest.mark.parametrize("routing", ["thread_id", "conversation", "room"])
def test_legacy_card_routes_from_resolved_authoritative_prompt(context, routing):
    row = {
        "id": "legacy-decision",
        "bot": context.bot,
        "type": "card",
        "card_type": "confirm",
        "payload": {"question": "Continue?"},
        "resolution": {"state": "answered", "responded_value": "confirm"},
    }
    if routing == "thread_id":
        row["thread_id"] = "root-42"
    elif routing == "conversation":
        row["task_conversation"] = "thread:root-42"
    else:
        row["room"] = "team-42"
    store_for(context.paths).open_prompt(row)
    context.memory.log_card(
        "session-1",
        card_id=row["id"],
        card_type="confirm",
        payload=row["payload"],
        peer="user",
        frm=context.bot,
    )
    assert user_thread(context.memory, peer="user") == []
    if routing != "room":
        history = user_thread(context.memory, peer="user", thread_id="root-42")
        assert len(history) == 1
        assert history[0]["thread_id"] == "root-42"
        assert history[0]["resolution"]["responded_value"] == "confirm"
