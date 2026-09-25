"""Scheduled decisions release the worker and preserve their exact occurrence."""

from datetime import UTC, datetime, timedelta

import pytest

from agent import messaging, tools
from agent.memory import Memory
from agent.runtime import build_agent
from agent.streaming import get_prompt, list_prompts, write_answer
from harness import routines, taskscope
from harness.paths import HarnessPaths
from harness.roster import Bot
from providers.base import Completion, Provider, ToolCall


def setup(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    return paths


class Replies(Provider):
    def __init__(self, *answers):
        super().__init__(model="echo")
        self.answers = list(answers)
        self.seen = []

    def complete(self, messages, **kwargs):
        self.seen.append((messages, kwargs))
        return self.answers.pop(0)


def test_routine_choice_preserves_open_decision_without_waiting(tmp_path):
    paths = setup(tmp_path)
    task = taskscope.begin_task(
        paths, "atlas", "generated:routine:first", text="", input_id="first", trusted_user=False
    )
    ctx = tools.ToolContext(
        paths,
        "atlas",
        Memory(paths, "atlas"),
        origin="routine",
        turn_id="first",
        task_id=task["task_id"],
        task_revision=task["revision"],
        task_conversation=task["conversation"],
        user_input_timeout=0.001,
    )
    try:
        tools._ask_user_choice(
            ctx, {"question": "Publish the reviewed draft?", "options": ["Accept", "Decline"]}
        )
    except Exception as exc:
        assert type(exc).__name__ == "RoutineSuspended"
    else:
        raise AssertionError(
            "A scheduled question must suspend, not consume the worker's input timeout"
        )
    assert len(list_prompts(paths, "atlas")) == 1
    assert not list_prompts(paths, "atlas")[0].get("resolution")


def test_occurrence_reports_enqueue_separately_from_completion(tmp_path):
    paths = setup(tmp_path)
    job = routines.add_routine(
        paths,
        "atlas",
        title="Daily digest",
        prompt="Summarize new notes",
        when="8am",
        enabled=True,
        timezone="Etc/UTC",
    )
    now = datetime.now(UTC).replace(hour=8, minute=0, second=0, microsecond=0)
    routines.fire_due(paths, ["atlas"], now=now)
    msg = messaging.pending(paths, "atlas")[0]
    assert msg.routine["scheduled_at"] == now.timestamp()
    assert msg.routine["expires_at"] == now.timestamp() + 3600
    row = routines.public(routines.list_routines(paths, "atlas")[0])
    assert row["last_run_status"] == "queued"
    assert row["history"][-1]["run_id"] == msg.routine["run_id"]
    assert row["id"] == job["id"]


def test_expired_recurring_occurrence_never_calls_model(tmp_path):
    paths = setup(tmp_path)
    routines.add_routine(
        paths,
        "atlas",
        title="Period check",
        prompt="Check the current period",
        when="8am",
        enabled=True,
        timezone="Etc/UTC",
    )
    old = (datetime.now(UTC) - timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    routines.fire_due(paths, ["atlas"], now=old)
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent.provider = Replies(Completion(text="Should not execute"))
    assert agent.process_inbox_once()
    assert not agent.provider.seen
    assert (
        routines.public(routines.list_routines(paths, "atlas")[0])["last_run_status"] == "expired"
    )


def test_run_tool_reports_queued_and_actual_enabled_state(tmp_path, monkeypatch):
    paths = setup(tmp_path)
    monkeypatch.setattr(
        tools,
        "_api_json",
        lambda *a, **k: {"title": "Daily digest", "enabled": True, "last_run_id": "occurrence"},
    )
    result = tools._run_routine(
        tools.ToolContext(paths, "atlas", Memory(paths, "atlas")), {"id": "job"}
    )
    assert "queued" in result.lower()
    assert "enabled" in result.lower()
    assert "disabled" not in result.lower()
    assert "test-ran" not in result


def question():
    return Completion(
        tool_calls=[
            ToolCall(
                id="choose",
                name="ask_user_choice",
                arguments={
                    "question": "Use the reviewed draft?",
                    "options": ["Accept", "Decline"],
                },
            )
        ],
        finish_reason="tool_use",
    )


@pytest.mark.parametrize("answer", ["Accept", "Decline"])
def test_waiting_routine_releases_queue_and_resumes_exact_task_once_after_restart(tmp_path, answer):
    paths = setup(tmp_path)
    first = routines.add_routine(
        paths, "atlas", title="Draft review", prompt="Review draft ALPHA", when="8am"
    )
    second = routines.add_routine(
        paths, "atlas", title="Notes digest", prompt="Summarize BETA notes", when="9am"
    )
    routines.run_now(paths, "atlas", first["id"])
    original = messaging.pending(paths, "atlas")[0]
    task = taskscope.scope_for_input(paths, "atlas", original.id)
    routines.run_now(paths, "atlas", second["id"])
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent.provider = Replies(question(), Completion(text="The notes digest is ready."))
    assert agent.process_inbox_once()
    prompt = list_prompts(paths, "atlas")[0]
    assert not prompt.get("resolution")
    assert taskscope.read_task(paths, "atlas", task["conversation"])["status"] == "waiting"
    assert not agent.control.is_busy("atlas")
    assert agent.process_inbox_once()
    assert len(agent.provider.seen) == 2
    assert len(list_prompts(paths, "atlas")) == 1
    write_answer(paths, prompt["id"], answer)
    restarted = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    restarted.provider = Replies(Completion(text="I used only the saved decision."))
    assert restarted.process_inbox_once()
    assert not restarted.process_inbox_once()
    context = str(restarted.provider.seen)
    assert "Review draft ALPHA" in context
    assert answer in context and task["task_id"] in context
    assert "original due time" in context
    assert get_prompt(paths, prompt["id"])["answer_consumed"]
    assert not [
        row
        for row in restarted.memory._session_records()
        if row.get("role") == "in:user"
        and "Continue the original task using" in row.get("text", "")
    ]


def test_later_occurrence_does_not_invalidate_waiting_earlier_decision(tmp_path):
    paths = setup(tmp_path)
    job = routines.add_routine(
        paths, "atlas", title="Draft review", prompt="Review draft", when="8am"
    )
    routines.run_now(paths, "atlas", job["id"])
    first = messaging.pending(paths, "atlas")[0]
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent.provider = Replies(question())
    assert agent.process_inbox_once()
    prompt = list_prompts(paths, "atlas")[0]
    routines.run_now(paths, "atlas", job["id"])
    second = messaging.pending(paths, "atlas")[0]
    assert (
        taskscope.scope_for_input(paths, "atlas", first.id)["conversation"]
        != taskscope.scope_for_input(paths, "atlas", second.id)["conversation"]
    )
    write_answer(paths, prompt["id"], "Accept")
    messaging.queue_prompt_answers(paths, "atlas")
    answers = [msg for msg in messaging.pending(paths, "atlas") if msg.origin == "prompt_answer"]
    assert len(answers) == 1
    assert messaging.continuation_scope(paths, "atlas", answers[0].resume)


def test_late_approval_for_expired_occurrence_does_not_run(tmp_path, monkeypatch):
    paths = setup(tmp_path)
    job = routines.add_routine(
        paths, "atlas", title="Period review", prompt="Review this period", when="8am"
    )
    routines.run_now(paths, "atlas", job["id"])
    original = messaging.pending(paths, "atlas")[0]
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent.provider = Replies(question())
    assert agent.process_inbox_once()
    prompt = list_prompts(paths, "atlas")[0]
    write_answer(paths, prompt["id"], "Accept")
    monkeypatch.setattr(routines, "occurrence_expired", lambda occurrence, **kw: True)
    restarted = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    restarted.provider = Replies()
    assert restarted.process_inbox_once()
    assert not restarted.provider.seen
    scope = taskscope.scope_for_input(paths, "atlas", original.id)
    assert taskscope.read_task(paths, "atlas", scope["conversation"])["status"] == "stopped"
    assert (
        routines.public(routines.list_routines(paths, "atlas")[0])["last_run_status"] == "expired"
    )


def test_one_shot_reminder_runs_late_and_recurring_can_opt_in(tmp_path):
    paths = setup(tmp_path)
    old = (datetime.now(UTC) - timedelta(days=2)).replace(hour=7, minute=0, second=0, microsecond=0)
    routines.add_routine(
        paths, "atlas", title="Reminder", prompt="Read the reminder", once_at=old.timestamp()
    )
    routines.add_routine(
        paths,
        "atlas",
        title="Digest",
        prompt="Build the digest",
        when="8am",
        enabled=True,
        timezone="Etc/UTC",
        missed_run_policy="run_late",
    )
    routines.fire_due(paths, ["atlas"], now=old.replace(hour=8, minute=0))
    queued = messaging.pending(paths, "atlas")
    assert len(queued) == 2
    assert all(msg.routine["expires_at"] is None for msg in queued)
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent.provider = Replies(
        Completion(text="Reminder delivered."), Completion(text="Digest prepared.")
    )
    assert agent.process_inbox_once() and agent.process_inbox_once()
    assert len(agent.provider.seen) == 2
    assert all(
        row["last_run_status"] == "completed" for row in routines.list_routines(paths, "atlas")
    )


def test_changed_or_deleted_occurrence_is_cancelled_before_execution(tmp_path):
    paths = setup(tmp_path)
    job = routines.add_routine(
        paths, "atlas", title="Review", prompt="Review version A", when="8am"
    )
    routines.run_now(paths, "atlas", job["id"])
    routines.update_routine(paths, "atlas", job["id"], prompt="Review version B")
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent.provider = Replies()
    assert agent.process_inbox_once()
    assert not agent.provider.seen
    assert routines.list_routines(paths, "atlas")[0]["last_run_status"] == "cancelled"
    routines.run_now(paths, "atlas", job["id"])
    routines.remove_routine(paths, "atlas", job["id"])
    assert agent.process_inbox_once()
    assert routines.list_routines(paths, "atlas") == []


def test_recovery_work_is_internal_and_cannot_select_connections(tmp_path):
    from agent import obligations
    from agent.history import user_thread

    paths = setup(tmp_path)
    obligations.record_send(paths, "atlas", "missing", now=1)
    obligations.maybe_redrive(paths, "atlas", now=1000, idle=1)
    msg = messaging.pending(paths, "atlas")[0]
    assert msg.origin == "recovery"
    assert messaging.lane_of(msg) == messaging.LANE_BACKGROUND
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent.provider = Replies(Completion(text="I may have missed your last message."))
    assert agent.process_inbox_once()
    scope = taskscope.scope_for_input(paths, "atlas", msg.id)
    assert scope["objective"] == "" and scope["connector_ids"] == []
    assert not [row for row in user_thread(agent.memory, peer="user") if row.get("role") == "user"]


def allow_fake_commands(monkeypatch, calls):
    from agent import runtime

    original = runtime.default_tools

    def catalogue():
        available = original()
        command = available["run_command"]
        available["run_command"] = tools.Tool(
            command.spec, lambda ctx, args: calls.append(args["command"]) or "done"
        )
        return available

    monkeypatch.setattr(runtime, "default_tools", catalogue)
    monkeypatch.setattr(runtime, "require_approval", lambda *a, **k: None)


def command():
    return Completion(
        tool_calls=[
            ToolCall(id="command", name="run_command", arguments={"command": "example-action"})
        ]
    )


def test_prior_action_checkpoint_survives_restart_without_replay_or_optional_review(
    tmp_path, monkeypatch
):
    paths = setup(tmp_path)
    calls = []
    allow_fake_commands(monkeypatch, calls)
    job = routines.add_routine(
        paths, "atlas", title="Review", prompt="Run a command then request a review", when="8am"
    )
    routines.run_now(paths, "atlas", job["id"])
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent.provider = Replies(command(), question())
    assert agent.process_inbox_once()
    prompt = list_prompts(paths, "atlas")[0]
    receipts = prompt["routine_receipts"]
    assert len(receipts) == 1 and receipts[0]["tool"] == "run_command"
    assert "example-action" not in str(receipts)
    assert prompt["protected_context"]
    assert calls == ["example-action"]
    write_answer(paths, prompt["id"], "Accept")
    restarted = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    restarted.provider = Replies(
        command(), Completion(text="The previous action was not repeated.")
    )

    def no_review(*a, **k):
        raise AssertionError(
            "a resume with incomplete earlier evidence must not call optional review"
        )

    monkeypatch.setattr("harness.jev_features.check_completion", no_review)
    assert restarted.process_inbox_once()
    assert calls == ["example-action"]
    assert "already ran before the scheduled pause" in str(restarted.provider.seen)
    assert "Protected routine checkpoint" in str(restarted.provider.seen)


def test_expiry_rechecked_after_model_before_mutation(tmp_path, monkeypatch):
    paths = setup(tmp_path)
    calls = []
    allow_fake_commands(monkeypatch, calls)
    job = routines.add_routine(paths, "atlas", title="Review", prompt="Run a command", when="8am")
    routines.run_now(paths, "atlas", job["id"])
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent.provider = Replies(command())
    original = agent.provider.complete

    def complete(*a, **k):
        result = original(*a, **k)
        monkeypatch.setattr(routines, "occurrence_expired", lambda *a, **k: True)
        return result

    agent.provider.complete = complete
    assert agent.process_inbox_once()
    assert calls == []
    assert routines.list_routines(paths, "atlas")[0]["last_run_status"] == "expired"


@pytest.mark.parametrize("change", [None, "expire", "configure"])
def test_outgoing_revalidates_after_browser_preparation_without_claiming_twice(
    tmp_path, monkeypatch, change
):
    from agent.computer import GatedComputer
    from harness.delivery import Ledger

    paths = setup(tmp_path)
    job = routines.add_routine(
        paths,
        "atlas",
        title="Review message",
        prompt="Review the message before posting",
        when="8am",
    )
    routines.run_now(paths, "atlas", job["id"])
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent.provider = Replies(
        Completion(
            tool_calls=[
                ToolCall(
                    id="review",
                    name="confirm",
                    arguments={
                        "question": "Review?",
                        "outgoing_message": {
                            "target_url": "https://forum.example.test/topic/42",
                            "text": "Please share the dimensions.",
                        },
                    },
                )
            ]
        )
    )
    assert agent.process_inbox_once()
    prompt = list_prompts(paths, "atlas")[0]
    write_answer(paths, prompt["id"], "confirm")
    sends, claims = [], []
    start = Ledger.start_action

    def claim(self, *args, **kwargs):
        claims.append(args)
        return start(self, *args, **kwargs)

    monkeypatch.setattr(Ledger, "start_action", claim)

    class Browser:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def prepare_outgoing(self, url, text):
            if change == "expire":
                monkeypatch.setattr(routines, "occurrence_expired", lambda *a, **kw: True)
            elif change == "configure":
                routines.update_routine(
                    paths, "atlas", job["id"], prompt="Use the revised instruction"
                )
            return "ready"

        def submit_outgoing(self, url, text):
            sends.append((url, text))
            return "ok"

    monkeypatch.setattr(GatedComputer, "browser_session", lambda self: Browser())
    restarted = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    restarted.provider = Replies(
        Completion(
            tool_calls=[
                ToolCall(
                    id="submit",
                    name="computer_submit_approved",
                    arguments={"approval_id": prompt["id"]},
                )
            ]
        ),
        Completion(text="I inspected the submitted result."),
    )
    assert restarted.process_inbox_once()
    assert len(claims) == 1
    row = get_prompt(paths, prompt["id"])
    if change:
        assert sends == []
        assert not row.get("execution_started")
        assert routines.list_routines(paths, "atlas")[0]["last_run_status"] == (
            "expired" if change == "expire" else "cancelled"
        )
    else:
        assert sends == [("https://forum.example.test/topic/42", "Please share the dimensions.")]
        assert row["execution_started"]


def test_routine_secret_prompt_resumes_with_availability_only(tmp_path):
    from agent.streaming import resolve_secret_prompts
    from harness.secrets import set_secret

    paths = setup(tmp_path)
    job = routines.add_routine(
        paths, "atlas", title="Digest", prompt="Use the requested integration", when="8am"
    )
    routines.run_now(paths, "atlas", job["id"])
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent.provider = Replies(
        Completion(
            tool_calls=[
                ToolCall(id="secret", name="request_secret", arguments={"name": "DEMO_SERVICE_KEY"})
            ]
        )
    )
    assert agent.process_inbox_once()
    prompt = list_prompts(paths, "atlas")[0]
    assert prompt["type"] == "secret_request" and not prompt.get("resolution")
    set_secret("DEMO_SERVICE_KEY", "fictional-test-key", paths)
    resolve_secret_prompts(paths, "DEMO_SERVICE_KEY")
    restarted = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    restarted.provider = Replies(Completion(text="The requested key is available."))
    assert restarted.process_inbox_once()
    assert "secret_provided" in str(restarted.provider.seen)
    assert "fictional-test-key" not in str(restarted.provider.seen)
    assert get_prompt(paths, prompt["id"])["answer_consumed"]


def test_run_history_does_not_overwrite_newer_occurrence(tmp_path):
    paths = setup(tmp_path)
    job = routines.add_routine(
        paths, "atlas", title="Digest", prompt="Prepare the digest", when="8am"
    )
    routines.run_now(paths, "atlas", job["id"])
    first = messaging.pending(paths, "atlas")[0].routine
    routines.run_now(paths, "atlas", job["id"])
    second = messaging.pending(paths, "atlas")[1].routine
    routines.record_run_state(paths, "atlas", first, "completed")
    row = routines.list_routines(paths, "atlas")[0]
    assert row["last_run_id"] == second["run_id"] and row["last_run_status"] == "queued"
    assert row["history"][0]["status"] == "completed"


def test_recovery_reports_exact_completed_source_without_reopening_it(tmp_path, monkeypatch):
    from agent import obligations

    paths = setup(tmp_path)
    source = taskscope.begin_task(
        paths, "atlas", "peer:user", text="Prepare the sample digest", input_id="source"
    )
    taskscope.mark_task(
        paths,
        "atlas",
        source["conversation"],
        source["task_id"],
        source["revision"],
        "completed",
        outcome="The sample digest is ready.",
    )
    obligations.record_send(paths, "atlas", "source", now=1)
    obligations.maybe_redrive(paths, "atlas", now=1000, idle=1)
    msg = messaging.pending(paths, "atlas")[0]
    assert msg.recovery_input_id == "source"
    assert "The sample digest is ready." in msg.text
    calls = []
    allow_fake_commands(monkeypatch, calls)
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent.provider = Replies(command(), Completion(text="The sample digest is ready."))
    assert agent.process_inbox_once()
    assert calls == []
    assert taskscope.read_task(paths, "atlas", source["conversation"])["status"] == "completed"


def test_recovery_preserves_current_unfinished_task_authorization(tmp_path, monkeypatch):
    from agent import obligations

    paths = setup(tmp_path)
    source = taskscope.begin_task(
        paths, "atlas", "peer:user", text="Run the sample command", input_id="source"
    )
    obligations.record_send(paths, "atlas", "source", now=1)
    obligations.maybe_redrive(paths, "atlas", now=1000, idle=1)
    calls = []
    allow_fake_commands(monkeypatch, calls)
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent.provider = Replies(command(), Completion(text="The sample command returned."))
    assert agent.process_inbox_once()
    assert calls == ["example-action"]
    assert source["task_id"] in str(agent.provider.seen)


@pytest.mark.parametrize("age,executed", [(3700, False), (10, True)])
def test_upgrade_handles_legacy_queue_without_guessing_its_schedule(tmp_path, age, executed):
    import time

    paths = setup(tmp_path)
    messaging.send(
        paths,
        messaging.Msg(
            to="atlas",
            frm="user",
            origin="routine",
            text="[Routine: reminder]\nDeliver a note",
            ts=time.time() - age,
        ),
    )
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent.provider = Replies(Completion(text="The note is ready."))
    assert agent.process_inbox_once()
    assert bool(agent.provider.seen) == executed
    if not executed:
        replies = [msg for _, msg in messaging.read_inbox(paths, "user")]
        assert len(replies) == 1 and "no saved occurrence identity" in replies[0].text
    assert not agent.process_inbox_once()


def test_checkpoint_survives_crash_before_source_request_is_archived(tmp_path, monkeypatch):
    paths = setup(tmp_path)
    calls = []
    allow_fake_commands(monkeypatch, calls)
    job = routines.add_routine(
        paths, "atlas", title="Review", prompt="Run a command then request review", when="8am"
    )
    routines.run_now(paths, "atlas", job["id"])
    original = messaging.pending(paths, "atlas")[0]
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent.provider = Replies(command(), question())
    # Produce directly: a crash after the durable question would leave the
    # inbox request present but no terminal reply or archive to deduplicate it.
    agent._produce(
        "user", original.text, origin="routine", routine=original.routine, turn_id=original.id
    )
    assert calls == ["example-action"]
    assert len(list_prompts(paths, "atlas")) == 1
    restarted = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    restarted.provider = Replies(command(), question())
    assert restarted.process_inbox_once()
    assert calls == ["example-action"]
    assert len(list_prompts(paths, "atlas")) == 1


def test_scheduler_callback_relay_preserves_occurrence_and_idempotent_archive(
    tmp_path, monkeypatch
):
    from agent import obligations
    from harness.orchestrator import Orchestrator
    from harness.server import relay_bot_turn

    roster = tmp_path / "roster.toml"
    roster.write_text('[[bots]]\nname = "atlas"\nprovider = "echo"\n')
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=roster, backend="process")
    orch.init()
    monkeypatch.setattr("harness.server.stream_turns", lambda *a, **k: None)
    paths = orch.paths
    routines.add_routine(
        paths,
        "atlas",
        title="Digest",
        prompt="Prepare a digest",
        when="8am",
        enabled=True,
        timezone="Etc/UTC",
    )
    now = datetime.now(UTC).replace(hour=8, minute=0, second=0, microsecond=0)
    sent = []

    def send(bot, text, **kwargs):
        sent.append(relay_bot_turn(orch, bot, text, **kwargs))

    routines.fire_due(paths, ["atlas"], now=now, send=send)
    path, msg = messaging.read_inbox(paths, "atlas")[0]
    assert msg.id == sent[0] == msg.routine["run_id"]
    assert msg.ts == msg.routine["scheduled_at"] == now.timestamp()
    assert taskscope.scope_for_input(paths, "atlas", msg.id)["conversation"].endswith(msg.id)
    assert obligations.get(paths, "atlas") is None
    messaging.mark_processed(paths, "atlas", path)
    routines.record_run_state(paths, "atlas", msg.routine, "completed")
    rows = routines.list_routines(paths, "atlas")
    rows[0]["last_run"] = None  # replay a lost scheduler acknowledgement
    routines._save(paths, "atlas", rows)
    routines.fire_due(paths, ["atlas"], now=now, send=send)
    assert len(sent) == 1
    assert messaging.pending(paths, "atlas") == []
    assert routines.list_routines(paths, "atlas")[0]["last_run_status"] == "completed"


def test_unrelated_routine_result_does_not_clear_missing_human_reply(tmp_path):
    from agent import obligations

    paths = setup(tmp_path)
    obligations.record_send(paths, "atlas", "missing-human-message")
    job = routines.add_routine(
        paths, "atlas", title="Digest", prompt="Prepare the digest", when="8am"
    )
    routines.run_now(paths, "atlas", job["id"])
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    agent.provider = Replies(Completion(text="The digest is ready."))
    assert agent.process_inbox_once()
    assert obligations.get(paths, "atlas")["message_ids"] == ["missing-human-message"]
