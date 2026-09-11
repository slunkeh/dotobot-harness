"""Human-source connector scope survives continuation without mining old prose."""

import pytest

from agent import messaging
from harness.connectors import Connectors, relevant_connected
from harness.paths import HarnessPaths
from harness.routines import (
    RoutineError,
    add_routine,
    bind_routine_scope,
    list_routines,
    run_now,
    update_routine,
)
from harness.taskscope import begin_task, mark_task, read_task, scope_for_input, task_context


@pytest.fixture
def scope_home(tmp_path, monkeypatch):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    records = [
        {
            "id": "n",
            "type": "notion",
            "name": "Notion",
            "oauth_configured": True,
            "tools": ["notion_search"],
        },
        {
            "id": "gw",
            "type": "gmail",
            "name": "Gmail Work",
            "secret_configured": True,
            "tools": ["gmail_search_threads"],
        },
        {
            "id": "gp",
            "type": "gmail",
            "name": "Gmail Personal",
            "secret_configured": True,
            "tools": ["gmail_search_threads"],
        },
    ]
    monkeypatch.setattr(Connectors, "list", lambda self: records)
    monkeypatch.setattr(Connectors, "records", lambda self: records)
    return paths, records


def start(paths, text, input_id, **kwargs):
    return begin_task(paths, "atlas", "main", text=text, input_id=input_id, **kwargs)


def test_continuation_survives_restart_and_keeps_human_source(scope_home):
    paths, _ = scope_home
    first = start(paths, "Use Notion to find the project brief", "first")
    continued = start(HarnessPaths.resolve(paths.home), "continue", "second")
    assert continued["task_id"] == first["task_id"]
    assert continued["revision"] == first["revision"]
    assert continued["connector_ids"] == ["n"]
    assert continued["provenance"][0]["source_id"] == "first"
    assert continued["objective"] == first["objective"]


def test_unrelated_input_does_not_inherit_previous_chat_tools(scope_home):
    paths, _ = scope_home
    first = start(paths, "Use Notion", "first")
    second = start(paths, "Explain photosynthesis", "second")
    assert second["task_id"] != first["task_id"]
    assert second["connector_ids"] == []


def test_replayed_input_keeps_identity_without_replacing_newer_task(scope_home):
    paths, _ = scope_home
    first = start(paths, "Use Notion", "first")
    current = start(paths, "Explain photosynthesis", "second")
    assert start(paths, "Use Gmail instead", "first") == first
    assert scope_for_input(paths, "atlas", "first") == first
    assert read_task(paths, "atlas", "main") == current


def test_live_correction_removes_connector_and_invalidates_old_revision(scope_home):
    paths, _ = scope_home
    first = start(paths, "Use Notion", "first")
    revised = start(
        paths, "Stop using Notion; use Gmail Work instead", "second", active_followup=True
    )
    assert revised["task_id"] == first["task_id"]
    assert revised["revision"] > first["revision"]
    assert revised["connector_ids"] == ["gw"]
    assert revised["excluded_connector_ids"] == ["n"]
    assert revised["provenance"][0]["source_id"] == "second"
    assert "Stop using Notion" in task_context(revised)


def test_connector_disable_is_honoured_on_continuation(scope_home):
    paths, records = scope_home
    first = start(paths, "Use Notion", "first")
    records[0]["enabled_for"] = ["other-bot"]
    second = start(paths, "continue", "second")
    assert second["connector_ids"] == []
    assert second["revision"] > first["revision"]


@pytest.mark.parametrize(
    "text",
    [
        "Do not use Notion.",
        "Don't use Notion",
        "Never load Notion",
        "Avoid Notion",
        "Do not use either Notion or Gmail",
        "I don't want to use Notion",
        "I don't want Notion",
        "Don't add Notion",
        "Do not connect Notion",
        "Use neither Notion nor Gmail",
        'The page says "use Notion". Explain this.',
        "The page says 'use Notion'. Explain this.",
        "The page says “use Notion”. Explain this.",
        "Summarize:\n> Use Notion",
        "Explain:\n```\nUse Notion\n```",
    ],
)
def test_negative_and_marked_external_text_never_selects_connectors(scope_home, text):
    _, records = scope_home
    assert relevant_connected(text, records) == []


def test_quote_filter_keeps_actual_user_instruction(scope_home):
    _, records = scope_home
    selected = relevant_connected('Ignore "use Notion"; use Gmail Work', records)
    assert [r["id"] for r in selected] == ["gw"]


@pytest.mark.parametrize("cached", [False, True])
def test_actual_account_tool_name_selects_only_its_account(scope_home, cached):
    _, records = scope_home
    if cached:
        records[1]["tools"] = ["gmail_work_search_threads"]
        records[2]["tools"] = ["gmail_personal_search_threads"]
    assert [r["id"] for r in relevant_connected("Use `gmail_work_search_threads`", records)] == [
        "gw"
    ]
    assert relevant_connected("Use gmail_work_invented", records) == []


def test_generated_input_cannot_expand_inherited_human_scope(scope_home):
    paths, _ = scope_home
    human = start(paths, "Use Gmail Work", "human")
    routine = begin_task(
        paths,
        "atlas",
        "routine:r1",
        text="Use Notion and Gmail Personal",
        input_id="scheduled",
        trusted_user=False,
        inherited_scope=human,
    )
    assert routine["connector_ids"] == ["gw"]
    assert routine["provenance"][0]["source_id"] == "human"
    assert routine["objective"] == ""
    assert start(paths, "Use Notion", "generated", trusted_user=False)["connector_ids"] == []


def test_inherited_ids_without_human_provenance_are_not_bindings(scope_home):
    paths, _ = scope_home
    scope = start(
        paths,
        "Use Notion",
        "generated",
        trusted_user=False,
        inherited_scope={"connector_ids": ["n"], "provenance": []},
    )
    assert scope["connector_ids"] == []


def test_stop_and_stale_finalization_do_not_change_successor(scope_home):
    paths, _ = scope_home
    first = start(paths, "Use Notion", "first")
    stopped = start(paths, "/stop", "stop", active_followup=True)
    assert stopped["status"] == "stopped"
    assert stopped["connector_ids"] == []
    assert not mark_task(
        paths, "atlas", "main", first["task_id"], first["revision"], "completed", "done"
    )
    assert read_task(paths, "atlas", "main") == stopped


def test_task_finalization_updates_only_matching_revision(scope_home):
    paths, _ = scope_home
    task = start(paths, "Use Notion", "first")
    assert mark_task(
        paths, "atlas", "main", task["task_id"], task["revision"], "completed", "Verified"
    )
    assert read_task(paths, "atlas", "main")["outcome"] == "Verified"


def test_conversations_keep_independent_scope(scope_home):
    paths, _ = scope_home
    direct = start(paths, "Use Notion", "first")
    room = begin_task(paths, "atlas", "room:r1", text="Use Gmail Work", input_id="room")
    assert room["task_id"] != direct["task_id"]
    assert read_task(paths, "atlas", "main")["connector_ids"] == ["n"]


def test_bound_routine_replays_human_scope_without_parsing_generated_prompt(scope_home):
    paths, _ = scope_home
    task = start(paths, "Use Gmail Work for the daily check", "human")
    routine = add_routine(paths, "atlas", prompt="Use Notion for the check", when="8am")
    bind_routine_scope(
        paths,
        "atlas",
        routine["id"],
        conversation="main",
        task_id=task["task_id"],
        revision=task["revision"],
    )
    update_routine(paths, "atlas", routine["id"], prompt="Use Gmail Personal and Notion")
    run_now(paths, "atlas", routine["id"])
    queued = messaging.pending(paths, "atlas")[0]
    replay = scope_for_input(paths, "atlas", queued.id)
    assert replay["connector_ids"] == ["gw"]
    assert replay["provenance"][0]["source_id"] == "human"
    assert read_task(paths, "atlas", "main")["task_id"] == task["task_id"]


def test_unbound_routine_has_no_implicit_scope(scope_home):
    paths, _ = scope_home
    start(paths, "Use Notion", "human")
    row = add_routine(paths, "atlas", prompt="Use Notion", when="8am")
    run_now(paths, "atlas", row["id"])
    queued = messaging.pending(paths, "atlas")[0]
    assert scope_for_input(paths, "atlas", queued.id) is None


def test_stale_task_cannot_bind_routine(scope_home):
    paths, _ = scope_home
    first = start(paths, "Use Notion", "first")
    start(paths, "Use Gmail Work", "second")
    row = add_routine(paths, "atlas", prompt="daily check", when="8am")
    with pytest.raises(RoutineError, match="no longer matches"):
        bind_routine_scope(
            paths,
            "atlas",
            row["id"],
            conversation="main",
            task_id=first["task_id"],
            revision=first["revision"],
        )
    assert "task_scope" not in list_routines(paths, "atlas")[0]


def test_registered_secret_never_reaches_durable_task_or_outcome(scope_home, monkeypatch):
    import json

    from harness import redaction

    monkeypatch.setattr(redaction, "_REGISTRY", redaction.SecretRedactionRegistry())
    secret = "test-scope-private-value-12345678"
    redaction.register_secret(secret, "TEST")
    paths, _ = scope_home
    task = start(paths, f"Use Notion; token {secret}", "human")
    assert secret not in json.dumps(scope_for_input(paths, "atlas", "human"))
    mark_task(paths, "atlas", "main", task["task_id"], task["revision"], "failed", secret)
    assert secret not in json.dumps(read_task(paths, "atlas", "main"))


def test_store_failure_propagates_without_fake_scope(scope_home, monkeypatch):
    import sqlite3

    from harness.statestore import StateStore

    paths, _ = scope_home

    def unavailable(self):
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(StateStore, "_tx", unavailable)
    with pytest.raises(sqlite3.OperationalError, match="unavailable"):
        start(paths, "Use Notion", "first")


def test_bound_routine_passes_scope_to_live_relay(scope_home):
    paths, _ = scope_home
    task = start(paths, "Use Gmail Work", "human")
    row = add_routine(paths, "atlas", prompt="Use Notion", when="8am")
    bind_routine_scope(
        paths,
        "atlas",
        row["id"],
        conversation="main",
        task_id=task["task_id"],
        revision=task["revision"],
    )
    calls = []
    run_now(paths, "atlas", row["id"], send=lambda bot, text, **kw: calls.append((bot, text, kw)))
    assert calls[0][2]["task_scope"]["connector_ids"] == ["gw"]
    assert calls[0][2]["task_scope"]["conversation"] == f"routine:{row['id']}"


def test_ambiguous_input_id_does_not_select_an_arbitrary_conversation(scope_home):
    paths, _ = scope_home
    start(paths, "Use Notion", "shared")
    begin_task(paths, "atlas", "room:other", text="Use Gmail Work", input_id="shared")
    with pytest.raises(ValueError, match="more than one"):
        scope_for_input(paths, "atlas", "shared")


@pytest.mark.parametrize(
    "text",
    [
        "Do not use Notion",
        "Do not use either Notion or Gmail",
        "I don't want to use Notion",
        "I don't want Notion",
        "Don't add Notion",
        "Do not connect Notion",
        "Use neither Notion nor Gmail",
    ],
)
def test_negative_or_quoted_catalog_name_does_not_offer_install(text):
    from harness.connectors import named_catalog_types

    assert named_catalog_types(text, []) == []
    assert named_catalog_types('Explain "Use Notion"', []) == []
    assert [r["type"] for r in named_catalog_types("Use Notion", [])] == ["notion"]


def test_explicit_not_name_is_excluded(scope_home):
    _, records = scope_home
    assert relevant_connected("Not Notion", records) == []


@pytest.mark.parametrize(
    "text",
    [
        "Use Gmail not Slack",
        "I'm not sure, use Gmail",
        "I have no time, use Gmail",
        "Do not use Slack, use Gmail",
        "Use @Gmail, not @Slack",
        "Don't use Slack or Notion, use Gmail",
        "I don't want Notion, use Gmail",
        "Don't add Notion, use Gmail",
        "Do not connect Notion, use Gmail",
    ],
)
def test_negation_targets_named_service_without_hiding_positive_name(text):
    from harness.connectors import named_catalog_types

    records = [
        {"id": name, "type": name, "name": name.title(), "secret_configured": True}
        for name in ("gmail", "slack", "notion")
    ]
    assert [r["id"] for r in relevant_connected(text, records)] == ["gmail"]
    assert [r["type"] for r in named_catalog_types(text, [])] == ["gmail"]


def test_negative_at_mention_does_not_hide_positive_at_mention():
    from harness.connectors import connector_scope_delta, mentioned_connected

    records = [
        {"id": name, "type": name, "name": name.title(), "secret_configured": True}
        for name in ("gmail", "slack")
    ]
    text = "Use @Gmail, not @Slack"
    assert [r["id"] for r in mentioned_connected(text, records)] == ["gmail"]
    assert connector_scope_delta(text, records)["excluded_ids"] == ["slack"]


def test_tool_scope_check_does_not_discover_schemas(scope_home, monkeypatch):
    from harness.taskscope import connector_bindings, tool_still_selected

    paths, records = scope_home
    task = start(paths, "Use Gmail Work", "first")
    bindings = connector_bindings(records, "atlas")
    monkeypatch.setattr(
        Connectors, "list", lambda self: pytest.fail("must use local stored records")
    )
    assert tool_still_selected(paths, "atlas", task, "gmail_work_send_message", bindings=bindings)
    assert not tool_still_selected(
        paths, "atlas", task, "gmail_personal_send_message", bindings=bindings
    )


@pytest.mark.parametrize("change", ["disabled", "deleted", "replaced", "renamed"])
def test_revalidation_tracks_exact_account_when_multiple_are_selected(scope_home, change):
    from harness.taskscope import connector_bindings, tool_still_selected

    paths, records = scope_home
    task = start(paths, "Use Gmail", "first")
    bindings = connector_bindings(records, "atlas")
    work = records[1]
    if change == "disabled":
        work["enabled_for"] = ["other-bot"]
    elif change == "deleted":
        records.remove(work)
    elif change == "replaced":
        work["id"] = "replacement"
    else:
        work["name"] = "Gmail Business"
    assert not tool_still_selected(
        paths, "atlas", task, "gmail_work_send_message", bindings=bindings
    )


def test_removing_sibling_account_cannot_reinterpret_a_loaded_namespace(scope_home):
    from harness.taskscope import connector_bindings, tool_still_selected

    paths, records = scope_home
    task = start(paths, "Use Gmail Work", "first")
    bindings = connector_bindings(records, "atlas")
    records.pop(2)
    # Registry now calls the remaining account gmail_*. The existing tool
    # instance is deliberately refused until a fresh loading establishes it.
    assert not tool_still_selected(
        paths, "atlas", task, "gmail_work_send_message", bindings=bindings
    )


def test_same_named_replacement_never_inherits_original_tool_binding(scope_home):
    from harness.taskscope import connector_bindings, tool_still_selected

    paths, records = scope_home
    task = start(paths, "Use Notion", "first")
    bindings = connector_bindings(records, "atlas")
    records[0]["id"] = "replacement"
    task["connector_ids"].append("replacement")
    assert not tool_still_selected(paths, "atlas", task, "notion_search", bindings=bindings)


def test_task_context_preserves_cumulative_corrections_with_their_sources(scope_home):
    paths, _ = scope_home
    first = start(paths, "Use Notion to draft the report", "first", source_id="message-1")
    start(
        paths,
        "Keep the response under 200 words",
        "second",
        source_id="message-2",
        active_followup=True,
    )
    third = start(
        paths, "Use British spelling", "third", source_id="message-3", active_followup=True
    )
    text = task_context(third)
    assert "Keep the response under 200 words" in text
    assert text.count("Use British spelling") == 1
    assert third["objective_source_id"] == "message-1"
    assert [item["source_id"] for item in third["instructions"]] == ["message-2", "message-3"]
    continued = start(paths, "continue", "fourth")
    assert continued["instructions"] == third["instructions"]
    assert continued["objective"] == first["objective"]


def test_bounded_corrections_explicitly_record_omitted_and_truncated_excerpts(scope_home):
    from harness.taskscope import MAX_TASK_INSTRUCTIONS, TASK_INSTRUCTION_CHARS

    paths, _ = scope_home
    first = start(paths, "Use Notion", "first")
    for i in range(MAX_TASK_INSTRUCTIONS + 2):
        task = start(
            paths,
            f"Correction {i}: " + "x" * TASK_INSTRUCTION_CHARS,
            f"correction-{i}",
            active_followup=True,
        )
    assert len(task["instructions"]) == MAX_TASK_INSTRUCTIONS
    assert task["omitted_instruction_count"] == 2
    assert all(
        len(item["text"]) <= TASK_INSTRUCTION_CHARS and item["truncated"]
        for item in task["instructions"]
    )
    assert task["objective"] == first["objective"]
    assert "omitted_instruction_count" in task_context(task)


def test_continue_with_a_correction_is_same_task_but_new_revision(scope_home):
    paths, _ = scope_home
    first = start(paths, "Use Notion", "first")
    second = start(paths, "Continue with the shorter version instead", "second")
    assert second["task_id"] == first["task_id"]
    assert second["revision"] > first["revision"]
    assert second["instructions"][0]["text"] == "Continue with the shorter version instead"


def test_replayed_correction_does_not_duplicate_instruction_or_clear_newer_constraints(scope_home):
    paths, _ = scope_home
    start(paths, "Use Notion", "first")
    second = start(paths, "Keep it short", "second", active_followup=True)
    third = start(paths, "Use British spelling", "third", active_followup=True)
    assert start(paths, "Keep it short", "second", active_followup=True) == second
    assert read_task(paths, "atlas", "main")["instructions"] == third["instructions"]


def test_unrelated_task_drops_old_correction_list(scope_home):
    paths, _ = scope_home
    start(paths, "Use Notion", "first")
    start(paths, "Keep it short", "second", active_followup=True)
    unrelated = start(paths, "Explain photosynthesis", "third")
    assert unrelated["instructions"] == []
    assert "Keep it short" not in task_context(unrelated)


def test_retention_keeps_current_active_waiting_and_unknown_tasks(scope_home):
    from harness.taskscope import active_task_ids, active_tasks

    paths, _ = scope_home
    retained = set()
    for status in ("active", "waiting", "unknown", "completed", "failed", "stopped"):
        task = begin_task(paths, "atlas", status, text="Use Notion", input_id=status)
        mark_task(paths, "atlas", status, task["task_id"], task["revision"], status)
        if status in {"active", "waiting", "unknown"}:
            retained.add(task["task_id"])
    assert active_task_ids(paths, "atlas") == retained
    assert active_task_ids(paths) == retained
    assert active_task_ids(paths, "another-bot") == set()
    other = begin_task(paths, "another-bot", "main", text="hello", input_id="another")
    assert active_tasks(paths) == {
        *(("atlas", task_id) for task_id in retained),
        ("another-bot", other["task_id"]),
    }


def test_action_surface_scope_requires_exact_live_source_task(scope_home):
    from harness.taskscope import matching_scope

    paths, _ = scope_home
    task = start(paths, "Use Notion", "first")
    assert matching_scope(paths, "atlas", "main", task["task_id"], task["revision"])
    assert matching_scope(paths, "atlas", "main", task["task_id"], task["revision"] + 1) is None
    mark_task(paths, "atlas", "main", task["task_id"], task["revision"], "unknown")
    assert matching_scope(paths, "atlas", "main", task["task_id"], task["revision"]) is None


def test_idle_generated_task_can_back_a_block_without_indefinite_receipt_retention(scope_home):
    from harness.taskscope import active_tasks, matching_scope

    paths, _ = scope_home
    human = start(paths, "Use Notion", "human")
    generated = begin_task(
        paths,
        "atlas",
        "routine:r1",
        text="",
        input_id="generated",
        trusted_user=False,
        inherited_scope=human,
    )
    assert mark_task(
        paths, "atlas", "routine:r1", generated["task_id"], generated["revision"], "idle"
    )
    assert ("atlas", generated["task_id"]) not in active_tasks(paths)
    assert matching_scope(paths, "atlas", "routine:r1", generated["task_id"], generated["revision"])


@pytest.mark.parametrize(
    "mutation,pause", [("created", "ok"), ("updated", "ok"), ("created", "failed")]
)
def test_routine_scope_failure_reports_existing_mutation_and_attempts_pause(
    scope_home, monkeypatch, mutation, pause
):
    from types import SimpleNamespace

    from agent import tools
    from harness import routines

    paths, _ = scope_home
    ctx = SimpleNamespace(
        paths=paths, bot="atlas", task_id="task", task_revision=1, task_conversation="main"
    )
    calls = []

    def api(paths, method, path, payload):
        calls.append((method, path, payload))
        if payload == {"enabled": False}:
            if pause == "failed":
                raise OSError("pause unavailable")
            return {"id": "existing-routine", "enabled": False}
        return {
            "id": "existing-routine",
            "title": "Daily check",
            "enabled": True,
            "once_at": 1,
            "schedule": "Once",
        }

    def bind(*args, **kwargs):
        raise OSError("scope store unavailable")

    monkeypatch.setattr(tools, "_api_json", api)
    monkeypatch.setattr(routines, "bind_routine_scope", bind)
    if mutation == "created":
        result = tools._create_routine(
            ctx, {"title": "Daily check", "prompt": "Check Gmail", "time": "in 1 hour"}
        )
    else:
        result = tools._update_routine(ctx, {"id": "existing-routine", "prompt": "Check Gmail"})
    assert result.startswith("error:")
    assert f"id=existing-routine was {mutation}" in result
    assert "Do not create another routine" in result
    assert calls[-1] == ("PATCH", "/api/bots/atlas/routines/existing-routine", {"enabled": False})
    assert len(calls) == 2
    if pause == "ok":
        assert "It is now disabled" in result
    else:
        assert "Pausing could not be confirmed; it may still be enabled" in result
        assert "It is now disabled" not in result
