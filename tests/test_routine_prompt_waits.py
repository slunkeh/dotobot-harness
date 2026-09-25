"""Only an unresolved resumable prompt can release a scheduled worker."""

import pytest

from agent import tools
from agent.memory import Memory
from agent.streaming import get_prompt, list_prompts, write_answer
from harness import taskscope
from harness.control import Control
from harness.paths import HarnessPaths


@pytest.fixture
def context(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["sample-bot"])
    task = taskscope.begin_task(
        paths,
        "sample-bot",
        "generated:routine:review",
        text="",
        input_id="review",
        trusted_user=False,
    )
    return tools.ToolContext(
        paths=paths,
        bot="sample-bot",
        memory=Memory(paths, "sample-bot"),
        control=Control(paths),
        session_id="session-1",
        origin="routine",
        turn_id="review",
        task_id=task["task_id"],
        task_revision=task["revision"],
        task_conversation=task["conversation"],
        user_input_timeout=1,
    )


def answered_confirmation(ctx):
    with pytest.raises(tools.RoutineSuspended):
        tools._confirm(ctx, {"question": "Continue the reviewed task?"})
    row = list_prompts(ctx.paths, ctx.bot)[0]
    write_answer(ctx.paths, row["id"], "confirm")
    result = tools._confirm(ctx, {"question": "Continue the reviewed task?"})
    assert not result.startswith("error:")
    assert get_prompt(ctx.paths, row["id"])["resolution"]["state"] == "answered"
    return row["id"]


def test_resolved_confirmation_then_control_return_keeps_existing_wait(context, monkeypatch):
    answered_confirmation(context)
    context.control.take_over(context.bot)
    waiting = []

    def return_computer(_duration):
        row = next(
            row
            for row in list_prompts(context.paths, context.bot)
            if row.get("card_type") == "control_return"
        )
        waiting.append(row["id"])
        context.control.return_control(context.bot)

    monkeypatch.setattr(tools.time, "sleep", return_computer)
    result = tools._request_control(context, {"reason": "Continue the reviewed task"})
    assert result.startswith("ok: the user returned control")
    assert len(waiting) == 1
    assert context.pending_prompt_id is None
    assert get_prompt(context.paths, waiting[0])["resolution"]["state"] == "answered"
    assert not context.control.state(context.bot).paused


def test_stale_resolved_prompt_id_does_not_suspend_an_unrelated_wait(context):
    context.pending_prompt_id = answered_confirmation(context)
    observations = iter([False, True])
    assert tools._wait_for_human(context, lambda: next(observations))
    assert context.pending_prompt_id is None
