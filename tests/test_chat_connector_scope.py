"""Chat account selection outlives individual task identities and model context."""

import json

import pytest

from harness.connectors import Connectors
from harness.paths import HarnessPaths
from harness.statestore import store_for
from harness.taskscope import begin_task, mark_task


@pytest.fixture
def chat(tmp_path, monkeypatch):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    records = [{"id": "work", "type": "gmail", "name": "Gmail Work"}]
    monkeypatch.setattr(Connectors, "list", lambda self: records)
    return paths, records


def turn(paths, text, input_id, **kwargs):
    return begin_task(paths, "atlas", "peer:user", text=text, input_id=input_id, **kwargs)


def test_new_task_keeps_chat_account_after_restart_and_stop(chat):
    paths, _ = chat
    first = turn(paths, "Use Gmail Work", "one")
    assert mark_task(paths, "atlas", "peer:user", first["task_id"], 1, "stopped")
    later = turn(HarnessPaths.resolve(paths.home), "Check my inbox for new replies", "two")
    assert later["task_id"] != first["task_id"]
    assert later["connector_ids"] == ["work"]
    assert later["provenance"][0]["source_id"] == "one"


def test_revocation_is_durable_while_account_disabled(chat):
    paths, records = chat
    turn(paths, "Use Gmail Work", "one")
    records[0]["enabled_for"] = ["nova"]
    turn(paths, "Stop using Gmail Work", "two")
    records[0].pop("enabled_for")
    assert turn(paths, "Check my inbox", "three")["connector_ids"] == []


def test_generated_overwrite_preserves_but_cannot_use_chat_selection(chat):
    paths, _ = chat
    first = turn(paths, "Use Gmail Work", "one")
    generated = turn(paths, "Check my inbox", "generated", trusted_user=False)
    assert generated["connector_ids"] == []
    assert generated["chat_connector_state"] == first["chat_connector_state"]
    assert turn(paths, "Check my inbox", "two")["connector_ids"] == ["work"]


def test_migration_recovers_selection_lost_by_older_task_boundary(chat):
    paths, _ = chat
    first = turn(paths, "Use Gmail Work", "one")
    later = turn(paths, "Explain a rainbow", "two")
    with store_for(paths)._tx() as conn:
        for table in ("agent_tasks", "agent_task_inputs"):
            rows = conn.execute(f"SELECT rowid,data FROM {table}").fetchall()
            for row in rows:
                data = json.loads(row["data"])
                data.pop("chat_connector_state", None)
                if data["input_id"] == later["input_id"]:
                    data.update(connector_ids=[], provenance=[])
                conn.execute(f"UPDATE {table} SET data=? WHERE rowid=?", (json.dumps(data), row["rowid"]))
    resumed = turn(paths, "Check my inbox", "three")
    assert resumed["connector_ids"] == first["connector_ids"]


def test_old_transcript_recovers_only_exact_human_account_id(chat):
    paths, _ = chat
    store_for(paths).append_transcript("atlas", "old", {
        "role": "in:user", "peer": "user", "text": "Use @connector:work", "ts": 1,
        "message_id": "original",
    })
    result = turn(paths, "Check my inbox", "now")
    assert result["connector_ids"] == ["work"]
    assert result["provenance"][0]["source_id"] == "original"


@pytest.mark.parametrize("extra,text", [
    ({}, "Use Gmail Work"),
    ({}, '> Use @connector:work'),
    ({"origin": "routine"}, "Use @connector:work"),
    ({"role": "in:nova"}, "Use @connector:work"),
    ({"thread_id": "other"}, "Use @connector:work"),
    ({"room": "other"}, "Use @connector:work"),
    ({"is_summary": True}, "Use @connector:work"),
])
def test_transcript_fallback_cannot_expand_from_other_sources(chat, extra, text):
    paths, _ = chat
    store_for(paths).append_transcript("atlas", "old", {
        "role": "in:user", "peer": "user", "text": text, "ts": 1,
        "message_id": "old", **extra,
    })
    assert turn(paths, "Check my inbox", "now")["connector_ids"] == []


def test_account_disable_is_dormant_and_replacement_never_inherits(chat):
    paths, records = chat
    turn(paths, "Use Gmail Work", "one")
    records[0]["enabled_for"] = ["nova"]
    assert turn(paths, "Check my inbox", "two")["connector_ids"] == []
    records[0].pop("enabled_for")
    assert turn(paths, "Check my inbox", "three")["connector_ids"] == ["work"]
    records[0]["id"] = "replacement"
    assert turn(paths, "Check my inbox", "four")["connector_ids"] == []


def test_canonical_chats_and_bots_are_isolated(chat):
    paths, _ = chat
    turn(paths, "Use Gmail Work", "one")
    for bot, conversation in [
        ("nova", "peer:user"), ("atlas", "room:team"), ("atlas", "thread:one"),
        ("atlas", "generated:routine:one"), ("atlas", "generated:dream:one"),
        ("atlas", "peer:nova"),
    ]:
        result = begin_task(paths, bot, conversation, text="Check my inbox", input_id=conversation)
        assert result["connector_ids"] == []


def test_explicit_bound_background_scope_does_not_expand_from_chat(chat):
    paths, _ = chat
    turn(paths, "Use Gmail Work", "one")
    result = turn(paths, "Check mail", "background", trusted_user=False,
                  inherited_scope={"connector_ids": [], "provenance": []})
    assert result["connector_ids"] == []
    assert result["chat_connector_state"]["connector_ids"] == ["work"]


def test_empty_initialized_state_never_reimports_older_selection(chat):
    paths, _ = chat
    turn(paths, "Use Gmail Work", "one")
    with store_for(paths)._tx() as conn:
        row = conn.execute("SELECT data FROM agent_tasks").fetchone()
        value = json.loads(row[0])
        value["chat_connector_state"] = {}
        conn.execute("UPDATE agent_tasks SET data=?", (json.dumps(value),))
    assert turn(paths, "Check my inbox", "two")["connector_ids"] == []


def test_lost_field_recovers_latest_initialized_tombstone(chat):
    paths, _ = chat
    turn(paths, "Use Gmail Work", "one")
    turn(paths, "Stop using Gmail Work", "two")
    with store_for(paths)._tx() as conn:
        row = conn.execute("SELECT data FROM agent_tasks").fetchone()
        value = json.loads(row[0])
        value.pop("chat_connector_state")
        conn.execute("UPDATE agent_tasks SET data=?", (json.dumps(value),))
    assert turn(paths, "Check my inbox", "three")["connector_ids"] == []


def test_malformed_state_fails_closed_instead_of_reimporting(chat):
    paths, _ = chat
    turn(paths, "Use Gmail Work", "one")
    with store_for(paths)._tx() as conn:
        row = conn.execute("SELECT data FROM agent_tasks").fetchone()
        value = json.loads(row[0])
        value["chat_connector_state"] = None
        conn.execute("UPDATE agent_tasks SET data=?", (json.dumps(value),))
    with pytest.raises(ValueError, match="saved chat connector"):
        turn(paths, "Check my inbox", "two")


def test_old_transcript_negative_account_name_revokes_exact_id(chat):
    paths, _ = chat
    for index, text in enumerate(["Use @connector:work", "Stop using Gmail Work"]):
        store_for(paths).append_transcript("atlas", "old", {
            "role": "in:user", "peer": "user", "text": text, "ts": index,
            "message_id": str(index),
        })
    assert turn(paths, "Check my inbox", "now")["connector_ids"] == []


def test_chat_selection_survives_removed_transcript_context(chat):
    paths, _ = chat
    turn(paths, "Use Gmail Work", "one")
    with store_for(paths)._tx() as conn:
        conn.execute("DELETE FROM transcripts")
    assert turn(paths, "New task: check my inbox", "two")["connector_ids"] == ["work"]


def _remove_initialized_fields(paths):
    with store_for(paths)._tx() as conn:
        for table in ("agent_tasks", "agent_task_inputs"):
            for raw in conn.execute(f"SELECT rowid,data FROM {table}").fetchall():
                row = json.loads(raw["data"])
                row.pop("chat_connector_state", None)
                conn.execute(f"UPDATE {table} SET data=? WHERE rowid=?", (json.dumps(row), raw["rowid"]))


@pytest.mark.parametrize("grant_source", ["one", "unmatched-old-source"])
def test_transcript_revocation_cannot_be_undone_by_older_admission(chat, grant_source):
    paths, _ = chat
    turn(paths, "Use @connector:work", "one", source_id=grant_source)
    for index, text in enumerate(["Use @connector:work", "Stop using Gmail Work"]):
        store_for(paths).append_transcript("atlas", "old", {
            "role": "in:user", "peer": "user", "text": text, "ts": index,
            "message_id": ["one", "two"][index],
        })
    _remove_initialized_fields(paths)
    assert turn(paths, "Check my inbox", "now")["connector_ids"] == []


def test_later_exact_transcript_reselection_follows_older_admission_revoke(chat):
    paths, _ = chat
    turn(paths, "Use @connector:work", "one")
    turn(paths, "Stop using Gmail Work", "two")
    for index, text in enumerate(["Use @connector:work", "Stop using Gmail Work", "Use @connector:work"]):
        store_for(paths).append_transcript("atlas", "old", {
            "role": "in:user", "peer": "user", "text": text, "ts": index,
            "message_id": ["one", "two", "three"][index],
        })
    _remove_initialized_fields(paths)
    assert turn(paths, "Check my inbox", "now")["connector_ids"] == ["work"]


def test_disabled_named_account_cannot_create_provisional_replacement_grant(chat):
    paths, records = chat
    turn(paths, "Use @connector:work", "one")
    records[0]["enabled_for"] = ["nova"]
    disabled = turn(paths, "Use Gmail Work", "two")
    assert disabled["chat_connector_state"]["pending_catalog"] == []
    records[:] = [{"id": "personal", "type": "gmail", "name": "Gmail Personal"}]
    assert turn(paths, "Check my inbox", "three")["connector_ids"] == []


@pytest.mark.parametrize("conversation", ["routine:one", "dream:one", "chat", "generated:routine:one"])
def test_noncanonical_namespaces_do_not_preserve_chat_grants(chat, conversation):
    paths, _ = chat
    first = begin_task(paths, "atlas", conversation, text="Use Gmail Work", input_id="one")
    assert first["chat_connector_state"]["connector_ids"] == []
    later = begin_task(paths, "atlas", conversation, text="Check my inbox", input_id="two")
    assert later["connector_ids"] == []


def test_historical_consumed_catalog_choice_never_transfers_to_replacement(chat):
    paths, records = chat
    records.clear()
    first = turn(paths, "Use Gmail", "one")
    records.append({"id": "work", "type": "gmail", "name": "Gmail Work"})
    consumed = turn(paths, "continue", "two")
    assert consumed["connector_ids"] == ["work"]
    assert consumed["provenance"][0]["input_id"] == first["input_id"]
    dropped = turn(paths, "Explain a rainbow", "three")
    _remove_initialized_fields(paths)
    with store_for(paths)._tx() as conn:
        for table in ("agent_tasks", "agent_task_inputs"):
            for raw in conn.execute(f"SELECT rowid,data FROM {table}").fetchall():
                value = json.loads(raw["data"])
                if value["input_id"] == dropped["input_id"]:
                    value.update(connector_ids=[], provenance=[], pending_catalog=[])
                    conn.execute(f"UPDATE {table} SET data=? WHERE rowid=?", (json.dumps(value), raw["rowid"]))
    records[:] = [{"id": "personal", "type": "gmail", "name": "Gmail Personal"}]
    current = turn(paths, "Check my inbox", "four")
    assert current["connector_ids"] == []
    assert current["chat_connector_state"]["connector_ids"] == ["work"]
    assert current["chat_connector_state"]["pending_catalog"] == []


@pytest.mark.parametrize("conversation,routing", [
    ("thread:root", {"thread_id": "root"}),
    ("room:team", {"room": "team"}),
])
def test_old_transcript_exact_selection_stays_in_its_canonical_chat(chat, conversation, routing):
    paths, _ = chat
    store_for(paths).append_transcript("atlas", "old", {
        "role": "in:user", "peer": "user", "text": "Use @connector:work", "ts": 1,
        "message_id": "original", **routing,
    })
    result = begin_task(paths, "atlas", conversation, text="Check my inbox", input_id="now")
    assert result["connector_ids"] == ["work"]
    assert turn(paths, "Check my inbox", "main")["connector_ids"] == []


def test_replayed_old_transcript_grant_cannot_undo_later_revocation(chat):
    paths, _ = chat
    for index, (source, text) in enumerate([
        ("one", "Use @connector:work"),
        ("two", "Stop using Gmail Work"),
        ("one", "Use @connector:work"),
    ]):
        store_for(paths).append_transcript("atlas", "old", {
            "role": "in:user", "peer": "user", "text": text, "ts": index,
            "message_id": source,
        })
    assert turn(paths, "Check my inbox", "now")["connector_ids"] == []
