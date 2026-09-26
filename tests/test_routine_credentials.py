"""Routine consent narrows future credential asks without granting script actions."""

import json
from unittest.mock import Mock

import pytest

from agent import govern, policy, tools
from agent.memory import Memory
from agent.streaming import write_prompt
from agent.tools import ToolContext
from harness import machine_secrets, routines
from harness.approvals import VERDICT_ALLOW, VERDICT_ASK, VERDICT_REFUSED, ApprovalStore
from harness.paths import HarnessPaths
from harness.secrets import delete_secret, set_secret
from harness.taskscope import begin_task


@pytest.fixture
def setup(tmp_path, monkeypatch):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas", "other"])
    monkeypatch.delenv("SERVICE_KEY", raising=False)
    paths.run_file("atlas").write_text(json.dumps({"backend": "machines", "machine": "machine-0"}))
    monkeypatch.setattr(machine_secrets, "mounted_for_bot", lambda *args: True)
    set_secret("SERVICE_KEY", "fictional-original-value", paths)
    row = routines.add_routine(paths, "atlas", title="Inventory report", prompt="Read inventory",
                               when="0 9 * * *", enabled=True)
    task = begin_task(paths, "atlas", "peer:user", text="Save credential permission for inventory",
                      input_id="human-request")
    approvals = ApprovalStore(paths, "atlas")
    ctx = ToolContext(paths, "atlas", Memory(paths, "atlas"), approvals=approvals,
                      task_conversation="peer:user", task_id=task["task_id"],
                      task_revision=task["revision"])
    return paths, ctx, row


def save_permission(setup, monkeypatch):
    paths, ctx, row = setup
    confirm = Mock(return_value="user confirmed")
    monkeypatch.setattr(tools, "_confirm", confirm)
    out = tools._request_routine_credential_permission(ctx, {"name": "SERVICE_KEY", "routine_id": row["id"]})
    assert out.startswith("Saved permission")
    assert confirm.call_args.kwargs["subject"]["routine_id"] == row["id"]
    return ctx.approvals.standing_permissions("peer:user")[0]


def routine_context(setup, *, metadata=True, bot="atlas"):
    paths, _, row = setup
    current = next(r for r in routines.list_routines(paths, "atlas") if r["id"] == row["id"])
    inherited = {"routine_id": row["id"], "routine_revision": routines.routine_revision(current)} if metadata else {}
    conversation = f"routine:{row['id']}:occurrence"
    task = begin_task(paths, bot, conversation, text="Read inventory", input_id="scheduled",
                      trusted_user=False, inherited_scope=inherited)
    return ToolContext(paths, bot, Memory(paths, bot), origin="routine", approvals=ApprovalStore(paths, bot),
                       task_conversation=conversation, task_id=task["task_id"], task_revision=task["revision"])


def check(ctx, *, name="SERVICE_KEY", rules=None):
    return govern.govern(ctx, "use_secret_file", {"name": name}, paths=ctx.paths, bot=ctx.bot,
                         policy=policy.parse(rules or {"ask": [{"intent": "type_secret"}]}),
                         approver=tools.require_approval)


def test_explicit_consent_survives_restart_for_one_routine_only(setup, monkeypatch):
    save_permission(setup, monkeypatch)
    ctx = routine_context(setup)
    ctx.approvals.begin_turn()
    monkeypatch.setattr(tools, "_confirm", Mock(side_effect=AssertionError("no repeat ask")))
    assert check(ctx) is None
    result = tools._use_secret_file(ctx, {"name": "SERVICE_KEY"})
    assert "/run/harness/SERVICE_KEY" in result
    assert "fictional-original-value" not in result


@pytest.mark.parametrize("change", ["routine", "disabled", "removed", "legacy", "bot", "credential", "stale_task"])
def test_changed_or_unverifiable_scope_needs_new_consent(setup, monkeypatch, change):
    paths, _, row = setup
    save_permission(setup, monkeypatch)
    ctx = routine_context(setup, metadata=change != "legacy", bot="other" if change == "bot" else "atlas")
    if change == "routine":
        routines.update_routine(paths, "atlas", row["id"], prompt="Write changed inventory")
    elif change == "disabled":
        routines.update_routine(paths, "atlas", row["id"], enabled=False)
    elif change == "removed":
        routines.remove_routine(paths, "atlas", row["id"])
    elif change == "stale_task":
        begin_task(paths, "atlas", ctx.task_conversation, text="New task", input_id="replacement")
    monkeypatch.setattr(tools, "_confirm", Mock(return_value="user cancelled"))
    assert check(ctx, name="OTHER_KEY" if change == "credential" else "SERVICE_KEY").startswith("error:")


def test_routine_identity_not_inferred_from_external_prompt_or_human_task(setup, monkeypatch):
    _, human, _ = setup
    save_permission(setup, monkeypatch)
    monkeypatch.setattr(tools, "_confirm", Mock(return_value="user cancelled"))
    assert check(human).startswith("error:")
    ctx = routine_context(setup, metadata=False)
    assert check(ctx).startswith("error:")
    assert tools._request_routine_credential_permission(ctx, {"name": "SERVICE_KEY", "routine_id": "anything"}).startswith("error:")


def test_policy_denial_and_refusal_win_over_standing_consent(setup, monkeypatch):
    paths, _, row = setup
    save_permission(setup, monkeypatch)
    ctx = routine_context(setup)
    no_ask = Mock(side_effect=AssertionError("policy deny must not ask"))
    monkeypatch.setattr(tools, "_confirm", no_ask)
    assert "policy" in check(ctx, rules={"deny": [{"intent": "type_secret"}]})
    version = routines.routine_revision(routines.list_routines(paths, "atlas")[0])
    ctx.approvals.record_refusal("use-secret-file", "exact-target")
    assert ctx.approvals.check("use-secret-file", "exact-target", tool_name="use_secret_file",
                               routine_scope=(row["id"], version), credential="SERVICE_KEY")[0] == VERDICT_REFUSED


def test_default_allow_does_not_introduce_new_secret_asks(setup):
    ctx = routine_context(setup)
    approver = Mock(side_effect=AssertionError("no implicit ask"))
    assert govern.govern(ctx, "use_secret_file", {"name": "SERVICE_KEY"}, paths=ctx.paths,
                         bot=ctx.bot, approver=approver) is None


@pytest.mark.parametrize("source", ["store", "environment", "delete_and_restore"])
def test_credential_rotation_invalidates_standing_consent(setup, monkeypatch, source):
    paths, _, row = setup
    permission = save_permission(setup, monkeypatch)
    ctx = routine_context(setup)
    version = routines.routine_revision(routines.list_routines(paths, "atlas")[0])
    kwargs = {"tool_name": "use_secret_file", "routine_scope": (row["id"], version), "credential": "SERVICE_KEY"}
    assert ctx.approvals.check("use-secret-file", "target", **kwargs)[0] == VERDICT_ALLOW
    if source == "environment":
        monkeypatch.setenv("SERVICE_KEY", "fictional-replacement-value")
    elif source == "delete_and_restore":
        delete_secret("SERVICE_KEY", paths)
        set_secret("SERVICE_KEY", "fictional-original-value", paths)
    else:
        set_secret("SERVICE_KEY", "fictional-replacement-value", paths)
    assert ctx.approvals.check("use-secret-file", "target", **kwargs)[0] == VERDICT_ASK
    public = tools._list_chat_permissions(setup[1], {}) + tools._credential_status(ctx, {"name": "SERVICE_KEY"})
    assert permission["credential_version"] not in public
    assert "fictional-original-value" not in public and "fictional-replacement-value" not in public


def test_revocation_stops_automatic_rematerialization_preserves_separate_bot_grant(setup, monkeypatch):
    paths, human, _ = setup
    permission = save_permission(setup, monkeypatch)
    machine_secrets.grant(paths, "atlas", "SERVICE_KEY")
    assert not human.approvals.revoke_standing("thread:unrelated", permission["id"])
    assert human.approvals.revoke_standing("peer:user", permission["id"])
    ctx = routine_context(setup)
    monkeypatch.setattr(tools, "_confirm", Mock(return_value="user cancelled"))
    assert check(ctx).startswith("error:")
    assert machine_secrets.credential_status(paths, "atlas", "SERVICE_KEY")["path"] == "/run/harness/SERVICE_KEY"


def test_changed_config_or_credential_during_confirmation_saves_nothing(setup, monkeypatch):
    paths, ctx, row = setup
    def confirm(*args, **kwargs):
        routines.update_routine(paths, "atlas", row["id"], prompt="Different action")
        return "user confirmed"
    monkeypatch.setattr(tools, "_confirm", confirm)
    assert "changed" in tools._request_routine_credential_permission(ctx, {"name": "SERVICE_KEY", "routine_id": row["id"]})
    assert ctx.approvals.standing_permissions("peer:user") == []


def test_cancelled_permission_card_has_no_side_effect(setup, monkeypatch):
    _, ctx, row = setup
    monkeypatch.setattr(tools, "_confirm", Mock(return_value="user cancelled"))
    assert tools._request_routine_credential_permission(ctx, {"name": "SERVICE_KEY", "routine_id": row["id"]}) == "user cancelled"
    assert ctx.approvals.standing_permissions("peer:user") == []


def test_safe_inventory_scoped_to_bot_grants_and_conversation_cards(setup):
    paths, ctx, _ = setup
    set_secret("UNRELATED_KEY", "fictional-unrelated-value", paths)
    write_prompt(paths, {"id": "own", "bot": "atlas", "type": "secret_request", "name": "SERVICE_KEY", "task_conversation": "peer:user"})
    write_prompt(paths, {"id": "private", "bot": "atlas", "type": "secret_request", "name": "UNRELATED_KEY", "task_conversation": "thread:private"})
    before = tools._credential_status(ctx, {})
    assert "SERVICE_KEY" in before and "UNRELATED_KEY" not in before
    assert json.loads(before)["credentials"][0]["path"] is None
    machine_secrets.grant(paths, "atlas", "SERVICE_KEY")
    current = json.loads(tools._credential_status(ctx, {}))["credentials"][0]
    assert current["current"] and current["mounted"] and current["path"] == "/run/harness/SERVICE_KEY"
    assert "fictional-" not in json.dumps(current)
    other = ToolContext(paths, "other", Memory(paths, "other"))
    assert json.loads(tools._credential_status(other, {}))["credentials"] == []


def test_environment_rotation_refreshes_only_existing_grant_and_hides_stale_path(setup, monkeypatch):
    paths, ctx, _ = setup
    machine_secrets.grant(paths, "atlas", "SERVICE_KEY")
    monkeypatch.setenv("SERVICE_KEY", "fictional-environment-replacement")
    monkeypatch.setenv("UNGRANTED_KEY", "fictional-ungranted-value")
    stale = json.loads(tools._credential_status(ctx, {"name": "SERVICE_KEY"}))["credentials"][0]
    assert not stale["current"] and stale["path"] is None
    with machine_secrets.script_secret_scope(paths, "atlas"):
        directory = machine_secrets.directory_for_bot(paths, "atlas")
        assert (directory / "SERVICE_KEY").read_text() == "fictional-environment-replacement"
        assert not (directory / "UNGRANTED_KEY").exists()
    assert machine_secrets.credential_status(paths, "atlas", "SERVICE_KEY")["current"]
    assert not machine_secrets.directory_for_bot(paths, "other").exists()


def test_allow_all_names_its_temporary_duration(setup, monkeypatch):
    ctx = routine_context(setup)
    confirm = Mock(return_value="user confirmed all")
    monkeypatch.setattr(tools, "_confirm", confirm)
    assert check(ctx) is None
    card = confirm.call_args.args[1]
    assert card["allow_all_label"] == "Allow all this turn"
    assert "does not grant future runs" in card["detail"]
    ctx.approvals.begin_turn()
    assert check(ctx) is None
    assert confirm.call_count == 2


def test_routine_binding_records_human_authority_not_generated_claims(setup):
    paths, ctx, row = setup
    routines.bind_routine_scope(paths, ctx.bot, row["id"], conversation=ctx.task_conversation,
                               task_id=ctx.task_id, revision=ctx.task_revision)
    bound = routines.list_routines(paths, ctx.bot)[0]["task_scope"]
    assert bound["source"]["input_id"] == "human-request"
    generated = routine_context(setup)
    with pytest.raises(routines.RoutineError):
        routines.bind_routine_scope(paths, ctx.bot, row["id"], conversation=generated.task_conversation,
                                   task_id=generated.task_id, revision=generated.task_revision)
    assert tools._bind_routine_after_mutation(generated, row["id"], "update") is None
    assert routines.list_routines(paths, ctx.bot)[0]["task_scope"] == bound


@pytest.mark.parametrize("change", ["store", "environment", "delete", "revoke"])
def test_old_accepted_card_cannot_reapprove_changed_or_revoked_credential(setup, monkeypatch, change):
    from agent.streaming import answer_prompt, list_prompts
    from harness.secrets import credential_fingerprint

    paths, ctx, row = setup
    cards = []
    def answer_current(_ctx, ready):
        prompt = list_prompts(paths, bot="atlas")[0]
        cards.append(prompt["id"])
        answer_prompt(paths, prompt["id"], "confirm", bot="atlas")
        return ready()
    monkeypatch.setattr(tools, "_wait_for_human", answer_current)
    args = {"name": "SERVICE_KEY", "routine_id": row["id"]}
    assert tools._request_routine_credential_permission(ctx, args).startswith("Saved")
    original_version = credential_fingerprint("SERVICE_KEY", paths)
    if change == "store":
        set_secret("SERVICE_KEY", "fictional-new-version", paths)
    elif change == "environment":
        monkeypatch.setenv("SERVICE_KEY", "fictional-new-version")
    elif change == "delete":
        delete_secret("SERVICE_KEY", paths)
        set_secret("SERVICE_KEY", "fictional-original-value", paths)
    else:
        permission = ctx.approvals.standing_permissions("peer:user")[0]
        ctx.approvals.revoke_standing("peer:user", permission["id"])
    # Same task revision, same routine and name: only the private consent
    # version changed. Replaying the accepted first card would fail this.
    assert tools._request_routine_credential_permission(ctx, args).startswith("Saved")
    assert len(set(cards)) == 2
    shown = json.dumps(list_prompts(paths, bot="atlas", include_resolved=True))
    assert original_version not in shown
    assert credential_fingerprint("SERVICE_KEY", paths) not in shown


def test_pending_consent_card_identity_survives_store_reopen(setup):
    paths, ctx, _ = setup
    from harness.secrets import credential_fingerprint

    version = credential_fingerprint("SERVICE_KEY", paths)
    token = ctx.approvals.credential_proposal_token("SERVICE_KEY", version)
    assert ApprovalStore(paths, "atlas").credential_proposal_token("SERVICE_KEY", version) == token
    assert version not in token


@pytest.mark.parametrize("source", ["store", "environment"])
def test_restoring_old_credential_does_not_revive_invalidated_consent(setup, monkeypatch, source):
    paths, _, _ = setup
    save_permission(setup, monkeypatch)
    ctx = routine_context(setup)
    if source == "store":
        set_secret("SERVICE_KEY", "fictional-rotated-value", paths)
        set_secret("SERVICE_KEY", "fictional-original-value", paths)
    else:
        monkeypatch.setenv("SERVICE_KEY", "fictional-rotated-value")
        scope = routines.routine_scope_for_task(paths, "atlas", ctx.task_conversation, ctx.task_id, ctx.task_revision)
        assert ctx.approvals.check("use-secret-file", "target", tool_name="use_secret_file",
                                   routine_scope=scope, credential="SERVICE_KEY")[0] == VERDICT_ASK
        monkeypatch.setenv("SERVICE_KEY", "fictional-original-value")
    monkeypatch.setattr(tools, "_confirm", Mock(return_value="user cancelled"))
    assert check(ctx).startswith("error:")


@pytest.mark.parametrize("change", ["delete_and_restore", "revoke_existing"])
def test_revocation_while_confirmation_waits_cannot_restore_consent(setup, monkeypatch, change):
    paths, ctx, row = setup
    permission = save_permission(setup, monkeypatch)
    # A configuration edit requires another confirmation for this same key.
    routines.update_routine(paths, "atlas", row["id"], prompt="Read updated inventory")
    def confirm(*args, **kwargs):
        if change == "delete_and_restore":
            delete_secret("SERVICE_KEY", paths)
            set_secret("SERVICE_KEY", "fictional-original-value", paths)
        else:
            assert ctx.approvals.revoke_standing("peer:user", permission["id"])
        return "user confirmed"
    monkeypatch.setattr(tools, "_confirm", confirm)
    out = tools._request_routine_credential_permission(ctx, {"name": "SERVICE_KEY", "routine_id": row["id"]})
    assert "changed" in out
    assert ctx.approvals.standing_permissions("peer:user") == []


def test_mount_observation_of_environment_rotation_invalidates_standing_consent(setup, monkeypatch):
    paths, ctx, _ = setup
    save_permission(setup, monkeypatch)
    machine_secrets.grant(paths, "atlas", "SERVICE_KEY")
    monkeypatch.setenv("SERVICE_KEY", "fictional-environment-rotation")
    with machine_secrets.script_secret_scope(paths, "atlas"):
        assert machine_secrets.credential_status(paths, "atlas", "SERVICE_KEY")["current"]
    assert ctx.approvals.standing_permissions("peer:user") == []
    monkeypatch.setenv("SERVICE_KEY", "fictional-original-value")
    with machine_secrets.script_secret_scope(paths, "atlas"):
        assert machine_secrets.credential_status(paths, "atlas", "SERVICE_KEY")["current"]
    scheduled = routine_context(setup)
    monkeypatch.setattr(tools, "_confirm", Mock(return_value="user cancelled"))
    assert check(scheduled).startswith("error:")
