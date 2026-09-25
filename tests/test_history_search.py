import json

import pytest

from agent.memory import Memory
from agent.records import search
from agent.streaming import answer_prompt, write_prompt
from agent.tools import ToolContext, default_tools
from harness.paths import HarnessPaths
from harness.taskscope import begin_task


@pytest.fixture
def ctx(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas", "other"])
    return ToolContext(paths, "atlas", Memory(paths, "atlas"), sender="user")


def test_original_cards_retrievable_after_task_changed_and_summary(ctx):
    first = begin_task(ctx.paths, ctx.bot, "peer:user", text="Prepare a forum reply", input_id="first")
    write_prompt(ctx.paths, {"id": "accepted", "bot": ctx.bot, "type": "card",
                            "card_type": "confirm", "task_id": first["task_id"],
                            "task_revision": 1, "task_conversation": "peer:user",
                            "payload": {"question": "Publish draft?", "detail": "Exact draft"}})
    answer_prompt(ctx.paths, "accepted", "confirm", bot=ctx.bot)
    begin_task(ctx.paths, ctx.bot, "peer:user", text="New subject", input_id="second")
    ctx.memory.log_turn("s", "summary", "Lossy summary", is_summary=True)
    result = search(ctx, query="draft", source="decisions")
    assert len(result["records"]) == 1
    record = result["records"][0]
    assert record["protected_context"] is True
    assert json.loads(record["text"])["resolution"]["approved"] is True
    assert "not current authorization" in result["notice"]


def test_scope_never_crosses_bot_room_thread_or_colleague(ctx):
    for i, extra in enumerate(({}, {"room": "room-a"}, {"thread_id": "thread-a"}, {"peer": "helper"})):
        ctx.memory.log_turn("s", "in:user", f"record-{i}", **extra)
    Memory(ctx.paths, "other").log_turn("s", "in:user", "other-bot")
    assert [r["text"] for r in search(ctx)["records"]] == ["record-0"]
    ctx.thread_id = "thread-a"
    assert [r["text"] for r in search(ctx)["records"]] == ["record-2"]
    ctx.room = "room-a"
    assert [r["text"] for r in search(ctx)["records"]] == ["record-1"]


def test_authoritative_prompt_routes_legacy_card_and_hides_secret_values(ctx):
    ctx.memory.log_card("s", card_id="secret", card_type="secret_request",
                        payload={"name": "SERVICE_KEY", "value": "must-not-leak"}, frm=ctx.bot)
    write_prompt(ctx.paths, {"id": "secret", "bot": ctx.bot, "type": "secret_request",
                            "task_conversation": "thread:private",
                            "name": "SERVICE_KEY", "value": "must-not-leak",
                            "resolution": {"state": "answered", "responded_value": "must-not-leak"}})
    assert search(ctx)["records"] == []
    ctx.thread_id = "private"
    result = search(ctx)
    assert "SERVICE_KEY" in json.dumps(result)
    assert "must-not-leak" not in json.dumps(result)


def test_original_text_pages_do_not_drop_long_tail(ctx):
    original = "Beginning " + "x" * 8000 + " decision at the end"
    ctx.memory.log_turn("s", "in:user", original)
    first = search(ctx, text_limit=2000)["records"][0]
    assert first["next_text_offset"] == 2000
    reconstructed = first["text"]
    while first["next_text_offset"] is not None:
        first = search(ctx, record_id=first["id"], text_offset=first["next_text_offset"])["records"][0]
        reconstructed += first["text"]
    assert reconstructed == original


def test_pages_and_boundaries_are_explicit(ctx):
    for i in range(4):
        ctx.memory.log_turn("s", "out", str(i), ts=float(i + 1))
    first = search(ctx, limit=2)
    second = search(ctx, limit=2, offset=first["next_offset"])
    assert [r["text"] for r in first["records"] + second["records"]] == ["3", "2", "1", "0"]
    assert second["next_offset"] is None
    assert [r["text"] for r in search(ctx, since=2, before=4)["records"]] == ["2", "1"]
    with pytest.raises(ValueError):
        search(ctx, limit=21)


def test_tool_is_host_owned_read_only_and_scope_not_a_model_argument(ctx):
    tool = default_tools()["search_history"]
    assert "bot" not in tool.spec.parameters["properties"]
    assert "conversation" not in tool.spec.parameters["properties"]
    ctx.memory.log_turn("s", "in:user", "Saved original")
    result = json.loads(tool.handler(ctx, {"query": "original"}))
    assert result["records"][0]["text"] == "Saved original"


def test_old_compacted_pointer_is_upgraded_without_rewriting_original():
    from agent.history import summaries_block

    old = {"text": "Original summary", "durable":
           "[Durable context]\n- transcript_pointer: use host database\n- skills: unchanged"}
    shown = summaries_block([old])
    assert "Use search_history" in shown and "use host database" not in shown
    assert "- skills: unchanged" in shown
    assert "use host database" in old["durable"]


def test_legacy_record_identity_survives_new_sessions_and_appends(ctx):
    ctx.memory.log_turn("session-z", "in:user", "original " + "x" * 7000)
    first = search(ctx)["records"][0]
    ctx.memory.log_turn("session-a", "in:user", "earlier sorting new session")
    ctx.memory.log_turn("session-z", "out", "new append")
    resumed = search(ctx, record_id=first["id"], text_offset=first["next_text_offset"])["records"][0]
    assert resumed["id"] == first["id"]
    assert resumed["text"] == "x" * 2000


def test_thread_search_includes_its_original_root_but_no_other_main_messages(ctx):
    ctx.memory.log_turn("s", "in:user", "root instruction", message_id="root")
    ctx.memory.log_turn("s", "in:user", "other instruction", message_id="other")
    ctx.thread_id = "root"
    assert [r["text"] for r in search(ctx)["records"]] == ["root instruction"]


def test_receipts_preserve_unknown_outcome_and_scope_to_original_task(ctx):
    from harness.delivery import Ledger

    own = begin_task(ctx.paths, ctx.bot, "peer:user", text="Publish draft", input_id="own-input")
    private = begin_task(ctx.paths, ctx.bot, "thread:private", text="Private draft", input_id="private-input")
    ledger = Ledger(ctx.paths)
    ledger.uncertain(ctx.bot, own["task_id"], "tool:publish", detail="outcome unknown")
    ledger.receipt(ctx.bot, own["input_id"], "chat:user", detail="chat reply delivered")
    ledger.receipt(ctx.bot, private["task_id"], "tool:publish", detail="private receipt")
    ledger.receipt("other", own["task_id"], "tool:publish", detail="other bot receipt")
    begin_task(ctx.paths, ctx.bot, "peer:user", text="New task", input_id="new")
    records = search(ctx, source="receipts")["records"]
    assert len(records) == 2 and all(r["protected_context"] for r in records)
    assert {json.loads(r["text"])["status"] for r in records} == {"sent", "uncertain"}
    assert "private receipt" not in json.dumps(records)
    assert "other bot receipt" not in json.dumps(records)
