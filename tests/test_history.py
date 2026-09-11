from agent.history import (
    _fair_truncate_turns,
    build_history,
    estimate_tokens,
    fair_char_allocations,
    history_budget,
    transcript_pointer,
    user_thread,
)
from agent.memory import Memory
from agent.runtime import Agent
from harness.control import Control
from harness.paths import HarnessPaths
from harness.roster import Bot
from providers.base import Completion, Message, Provider


def _memory(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    return Memory(paths=paths, bot="atlas"), paths


class RecordingProvider(Provider):
    """Captures the messages of every complete() call; replies with canned text."""

    id = "recording"

    def __init__(self, model="rec-1", auth=None, **options):
        super().__init__(model, auth, **options)
        self.calls: list[list[Message]] = []

    def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
        self.calls.append(list(messages))
        return Completion(text="ok", finish_reason="stop")


def test_log_turn_stores_extra_fields(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "hello", peer="user", room=None)
    memory.log_turn("s1", "out", "hi", peer="user", room="standup")
    records = memory._session_records()
    assert records[0]["peer"] == "user"
    assert "room" not in records[0]  # None values are not stored
    assert records[1]["room"] == "standup"


def test_build_history_maps_turns_and_reports_cutoff(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "first question", peer="user")
    memory.log_turn("s1", "out", "first answer", peer="user")
    messages, cutoff = build_history(memory, peer="user", provider=Provider(model="m"))
    assert [(m.role, m.content) for m in messages] == [
        ("user", "first question"),
        ("assistant", "first answer"),
    ]
    assert messages[0].name == "user"
    assert cutoff == memory._session_records()[0]["ts"]


def test_user_thread_hides_dream_prompt(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "hello", peer="user")
    memory.log_turn("s1", "out", "hi", peer="user")
    memory.log_turn(
        "s1",
        "in:user",
        "[Dreaming — nobody has messaged you for a while]\nConsolidate.",
        peer="user",
        origin="dream",
    )
    memory.log_turn("s1", "out", "Nothing that needs you.", peer="user")
    memory.log_turn(
        "s1",
        "in:user",
        "[Idle reflection — leftover without origin]\nThink.",
        peer="user",
    )
    memory.log_turn("s1", "out", "Checked.", peer="user")
    rows = user_thread(memory, peer="user")
    assert [(r["frm"], r["text"]) for r in rows] == [
        ("user", "hello"),
        ("atlas", "hi"),
        ("atlas", "Nothing that needs you."),
        ("atlas", "Checked."),
    ]


def test_user_thread_collapses_routine_prompt_to_card(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "hello", peer="user")
    memory.log_turn(
        "s1",
        "in:user",
        "[Routine: Daily competitor watch]\nNamed competitors only.\nSequence: 1. Recall.",
        peer="user",
        origin="routine",
        message_id="rout1",
    )
    memory.log_turn("s1", "out", "Nothing material moved.", peer="user")
    rows = user_thread(memory, peer="user")
    assert rows[0]["frm"] == "user" and rows[0]["text"] == "hello"
    card = rows[1]
    assert card["type"] == "card"
    assert card["card_type"] == "routine"
    assert card["card_id"] == "routine:rout1"
    assert card["payload"]["title"] == "Daily competitor watch"
    assert "Named competitors only" in card["payload"]["detail"]
    assert rows[2]["frm"] == "atlas"
    assert "[Routine:" not in (rows[1].get("text") or "")
    messages, _cutoff = build_history(memory, peer="user", provider=Provider(model="m"))
    assert any("[Routine: Daily competitor watch]" in (m.content or "") for m in messages)


def test_user_thread_keeps_plain_origin_routine_as_chat(tmp_path):
    """Late prompt answers reuse origin=routine; they are a person talking."""
    memory, _ = _memory(tmp_path)
    memory.log_turn(
        "s1",
        "in:user",
        "I mean the project name",
        peer="user",
        origin="routine",
        message_id="ans1",
    )
    memory.log_turn("s1", "out", "Got it — ALT it is.", peer="user")
    rows = user_thread(memory, peer="user")
    assert [(r["frm"], r.get("text"), r.get("type"), r.get("card_type")) for r in rows] == [
        ("user", "I mean the project name", None, None),
        ("atlas", "Got it — ALT it is.", None, None),
    ]


def test_user_thread_hides_fast_ack_receipts(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "hey", peer="user")
    memory.log_turn("s1", "out", "Got it — posting all of them.", peer="user", origin="ack")
    memory.log_turn("s1", "out", "Hey. What's up?", peer="user")
    rows = user_thread(memory, peer="user")
    assert [(r["frm"], r["text"]) for r in rows] == [
        ("user", "hey"),
        ("atlas", "Hey. What's up?"),
    ]
    messages, _cutoff = build_history(memory, peer="user", provider=Provider(model="m"))
    assert [(m.role, m.content) for m in messages] == [
        ("user", "hey"),
        ("assistant", "Hey. What's up?"),
    ]


def test_user_thread_is_one_bubble_per_turn(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "hello", peer="user")
    memory.log_turn("s1", "out", "hi", peer="user")
    memory.log_turn("s1", "in:user", "again", peer="user")
    memory.log_turn("s1", "in:nova", "bot chat", peer="nova")
    memory.log_turn("s1", "in:user", "room", peer="user", room="standup")
    rows = user_thread(memory, peer="user")
    assert [(r["frm"], r["text"]) for r in rows] == [
        ("user", "hello"),
        ("atlas", "hi"),
        ("user", "again"),
    ]


def test_user_thread_uses_explicit_frm_for_handoffs(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["nova"])
    memory = Memory(paths=paths, bot="nova")
    memory.log_turn("s1", "in:atlas", "create three tickets", peer="user", frm="atlas")
    memory.log_turn("s1", "out", "Done: filed three tickets", peer="user")
    rows = user_thread(memory, peer="user")
    assert [(r["frm"], r["text"]) for r in rows] == [
        ("atlas", "create three tickets"),
        ("nova", "Done: filed three tickets"),
    ]


def test_user_thread_pages_older_than_before(tmp_path):
    memory, _ = _memory(tmp_path)
    for i in range(6):
        memory.log_turn("s1", "in:user", f"u{i}", peer="user")
        memory.log_turn("s1", "out", f"a{i}", peer="user")
    newest = user_thread(memory, peer="user", limit=4)
    assert [r["text"] for r in newest] == ["u4", "a4", "u5", "a5"]
    older = user_thread(memory, peer="user", limit=4, before=newest[0]["ts"])
    assert [r["text"] for r in older] == ["u2", "a2", "u3", "a3"]
    assert older[-1]["ts"] < newest[0]["ts"]


def test_user_thread_includes_progress_and_secret_cards(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "sign in", peer="user")
    memory.log_turn(
        "s1",
        "card",
        "",
        peer="user",
        card_id="p1",
        card_type="progress",
        payload={"title": "EE staging dry run", "state": "running"},
        frm="atlas",
    )
    memory.log_turn(
        "s1",
        "card",
        "",
        peer="user",
        card_id="sec1",
        card_type="secret_request",
        payload={"name": "WEBFLOW_PASSWORD", "title": "Webflow password", "detail": "login"},
        frm="atlas",
    )
    rows = user_thread(memory, peer="user")
    kinds = [r.get("card_type") or r["frm"] for r in rows]
    assert kinds == ["user", "progress", "secret_request"]
    assert rows[1]["payload"]["title"] == "EE staging dry run"
    assert rows[2]["payload"]["name"] == "WEBFLOW_PASSWORD"


def test_user_thread_includes_durable_cards(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "show a chart", peer="user")
    memory.log_turn(
        "s1",
        "card",
        "",
        peer="user",
        card_id="c1",
        card_type="chart",
        payload={"title": "Signups", "chart": {"kind": "line", "series": []}},
        frm="atlas",
    )
    memory.log_turn("s1", "out", "plotted", peer="user")
    rows = user_thread(memory, peer="user")
    assert [r.get("type") or r["frm"] for r in rows] == ["user", "card", "atlas"]
    assert rows[1]["card_id"] == "c1"
    assert rows[1]["card_type"] == "chart"
    assert rows[1]["payload"]["title"] == "Signups"
    assert rows[1]["frm"] == "atlas"
    assert rows[1]["text"] == ""


def test_user_thread_keeps_answered_confirm(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "delete it?", peer="user")
    memory.log_turn(
        "s1",
        "card",
        "",
        peer="user",
        card_id="c-ok",
        card_type="confirm",
        payload={"question": "Delete the file?", "confirm_label": "Accept"},
        frm="atlas",
    )
    memory.log_turn(
        "s1",
        "card",
        "",
        peer="user",
        card_id="c-ok",
        card_type="confirm",
        payload={"question": "Delete the file?", "confirm_label": "Accept"},
        resolution={"state": "answered", "responded_value": "confirm", "skipped": False},
        frm="atlas",
    )
    rows = user_thread(memory, peer="user")
    cards = [r for r in rows if r.get("type") == "card"]
    assert len(cards) == 1
    assert cards[0]["card_type"] == "confirm"
    assert cards[0]["resolution"]["responded_value"] == "confirm"


def test_user_thread_keeps_skipped_secret_and_return(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "sign in", peer="user")
    memory.log_turn(
        "s1",
        "card",
        "",
        peer="user",
        card_id="sec1",
        card_type="secret_request",
        payload={"name": "TOKEN", "title": "Token"},
        frm="atlas",
    )
    memory.log_turn(
        "s1",
        "card",
        "",
        peer="user",
        card_id="sec1",
        card_type="secret_request",
        payload={"name": "TOKEN", "title": "Token"},
        resolution={"state": "skipped", "skipped": True},
        frm="atlas",
    )
    memory.log_turn(
        "s1",
        "card",
        "",
        peer="user",
        card_id="ret1",
        card_type="control_return",
        payload={"question": "Can I have the computer back?"},
        frm="atlas",
    )
    memory.log_turn(
        "s1",
        "card",
        "",
        peer="user",
        card_id="ret1",
        card_type="control_return",
        payload={"question": "Can I have the computer back?"},
        resolution={"state": "skipped", "skipped": True},
        frm="atlas",
    )
    rows = user_thread(memory, peer="user")
    cards = [r for r in rows if r.get("type") == "card"]
    assert [c["card_type"] for c in cards] == ["secret_request", "control_return"]
    assert cards[0]["resolution"]["skipped"] is True
    assert cards[1]["resolution"]["skipped"] is True


def test_build_history_skips_card_records(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "show a chart", peer="user")
    memory.log_turn(
        "s1",
        "card",
        "",
        peer="user",
        card_id="c1",
        card_type="chart",
        payload={"title": "Signups"},
        frm="atlas",
    )
    memory.log_turn("s1", "out", "plotted", peer="user")
    messages, _ = build_history(memory, peer="user", provider=Provider(model="m"))
    assert [(m.role, m.content) for m in messages] == [
        ("user", "show a chart"),
        ("assistant", "plotted"),
    ]


def test_user_thread_keeps_image_attachments(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn(
        "s1",
        "in:user",
        "",
        peer="user",
        attachments=[{"name": "shot.png", "path": "/data/uploads/a-shot.png"}],
    )
    memory.log_turn("s1", "out", "nice pic", peer="user")
    rows = user_thread(memory, peer="user")
    assert rows[0]["frm"] == "user"
    assert rows[0]["text"] == ""
    assert rows[0]["attachments"][0]["name"] == "shot.png"
    assert rows[1]["text"] == "nice pic"


def test_build_history_keeps_attachment_on_its_own_turn(tmp_path):
    """A chat photo stays on the turn that sent it, not later questions."""
    memory, _ = _memory(tmp_path)
    memory.log_turn(
        "s1",
        "in:user",
        "here are mockups",
        peer="user",
        attachments=[{"name": "earlier.png", "path": "/data/uploads/earlier.png"}],
    )
    memory.log_turn("s1", "out", "got it", peer="user")
    memory.log_turn("s1", "in:user", "what did you mean about git", peer="user")
    memory.log_turn("s1", "out", "the clone is in /tmp", peer="user")
    messages, _ = build_history(memory, peer="user", provider=Provider(model="m"))
    contents = [m.content for m in messages]
    assert contents[0].startswith("here are mockups")
    assert "[attached: earlier.png path=/data/uploads/earlier.png]" in contents[0]
    assert contents[2] == "what did you mean about git"
    assert "earlier.png" not in contents[2]


def test_build_history_empty_text_attachment_includes_path(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn(
        "s1",
        "in:user",
        "",
        peer="user",
        attachments=[{"name": "shot.png", "path": "/data/uploads/a-shot.png"}],
    )
    memory.log_turn("s1", "out", "nice pic", peer="user")
    messages, _ = build_history(memory, peer="user", provider=Provider(model="m"))
    assert messages[0].content == "[attached: shot.png path=/data/uploads/a-shot.png]"
    assert messages[1].content == "nice pic"


def test_build_history_filters_by_peer_and_room(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "for me", peer="user")
    memory.log_turn("s1", "out", "reply to user", peer="user")
    memory.log_turn("s1", "in:nova", "bot to bot", peer="nova")
    memory.log_turn("s1", "in:user", "room chatter", peer="user", room="standup")
    messages, _ = build_history(memory, peer="user", provider=Provider(model="m"))
    contents = [m.content for m in messages]
    assert "bot to bot" not in contents
    assert "room chatter" not in contents
    assert contents == ["for me", "reply to user"]


def test_build_history_reads_legacy_records(tmp_path):
    memory, _ = _memory(tmp_path)
    # records predating the peer field: sender only in the role, out untagged
    memory.log_turn("s1", "in:user", "old question")
    memory.log_turn("s1", "out", "old answer")
    messages, _ = build_history(memory, peer="user", provider=Provider(model="m"))
    assert [(m.role, m.content) for m in messages] == [
        ("user", "old question"),
        ("assistant", "old answer"),
    ]


def test_build_history_spans_session_files_and_coalesces(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("20240101-000000", "in:user", "from the first boot", peer="user")
    # no reply logged (e.g. the bot crashed) -> consecutive user turns coalesce
    memory.log_turn("20240102-000000", "in:user", "after a restart", peer="user")
    memory.log_turn("20240102-000000", "out", "reply", peer="user")
    messages, _ = build_history(memory, peer="user", provider=Provider(model="m"))
    assert messages[0].role == "user"
    assert messages[0].content == "from the first boot\n\nafter a restart"
    assert messages[1].role == "assistant"


def test_build_history_drops_oldest_and_never_leads_with_assistant(tmp_path, monkeypatch):
    memory, _ = _memory(tmp_path)
    for i in range(6):
        memory.log_turn("s1", "in:user", f"question {i} " + "x" * 200, peer="user")
        memory.log_turn("s1", "out", f"answer {i} " + "y" * 200, peer="user")
    monkeypatch.setenv("HARNESS_HISTORY_TOKENS", "160")
    messages, _ = build_history(memory, peer="user", provider=Provider(model="m"))
    assert 0 < len(messages) < 12
    assert messages[0].role == "user"  # a leading assistant turn is dropped
    assert messages[-1].content.startswith("answer 5")


def test_fair_char_allocations_splits_leftover_between_giants():
    # 100 fits whole; the two giants split the remaining 2900 chars evenly
    assert fair_char_allocations([100, 10000, 10000], 3000) == [100, 1450, 1450]


def test_fair_char_allocations_returns_small_message_surplus_to_pool():
    # 10 and 100 take only what they need; the giant gets everything left
    assert fair_char_allocations([10, 100, 1000], 300) == [10, 100, 190]


def test_fair_truncate_omits_when_share_is_below_useful_minimum():
    turns = [("user", "u" * 100, 1.0), ("assistant", "a" * 120, 2.0), ("user", "x" * 5000, 3.0)]
    out, survived = _fair_truncate_turns(turns, 100)  # 400 chars of budget
    assert survived
    assert out[0][1] == "u" * 100  # small turns survive whole
    assert out[1][1] == "a" * 120
    assert out[2][1] == "[omitted message, 5000 chars]"  # its 180-char share is useless


def test_build_history_truncates_giant_dump_and_keeps_oldest_instruction(tmp_path, monkeypatch):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "deploy the frontend with the new flag", peer="user")
    memory.log_turn("s1", "out", "x" * 20_000, peer="user")  # giant tool-ish dump
    memory.log_turn("s1", "in:user", "did the deploy finish?", peer="user")
    memory.log_turn("s1", "out", "yes, all green", peer="user")
    monkeypatch.setenv("HARNESS_HISTORY_TOKENS", "1000")
    messages, cutoff = build_history(memory, peer="user", provider=Provider(model="m"))
    assert len(messages) == 4  # no turn is dropped whole
    assert "deploy the frontend with the new flag" in messages[0].content
    assert messages[1].content.endswith("[... truncated, 20000 chars]")
    assert len(messages[1].content) < 20_000  # the giant paid for the overrun
    assert messages[3].content == "yes, all green"
    assert cutoff == memory._session_records()[0]["ts"]


def test_build_history_trim_appends_transcript_pointer(tmp_path, monkeypatch):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "remember this " + "x" * 20_000, peer="user")
    memory.log_turn("s1", "out", "noted", peer="user")
    monkeypatch.setenv("HARNESS_HISTORY_TOKENS", "1000")
    messages, _ = build_history(memory, peer="user", provider=Provider(model="m"))
    note = transcript_pointer(memory)
    assert messages[0].content.startswith(note)  # rides on the oldest user turn
    assert str(memory.sessions_dir) in note  # memory/<bot>/sessions/... path
    assert "grep" in note and "never read them linearly" in note


def test_build_history_untrimmed_has_no_transcript_pointer(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "hello", peer="user")
    memory.log_turn("s1", "out", "hi", peer="user")
    messages, _ = build_history(memory, peer="user", provider=Provider(model="m"))
    assert [m.content for m in messages] == ["hello", "hi"]


def test_build_history_empty_when_budget_exhausted(tmp_path, monkeypatch):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "hello", peer="user")
    monkeypatch.setenv("HARNESS_HISTORY_TOKENS", "0")
    messages, cutoff = build_history(memory, peer="user", provider=Provider(model="m"))
    assert messages == []
    assert cutoff is None


def test_history_budget_respects_context_window():
    small = Provider(model="m", context_window=64)
    assert history_budget(small, system="s" * 400, current="c" * 400) == 0
    assert estimate_tokens("abcdefgh") == 2


def test_history_budget_prefers_last_usage_over_char_estimate():
    provider = Provider(model="m", context_window=10_000)
    via_usage = history_budget(
        provider,
        system="ignored" * 50,
        usage={"input_tokens": 2_500},
        last_history_tokens=500,
    )
    assert via_usage == 10_000 - 2_000 - 1_024 - 1_024


def _agent(tmp_path, provider):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    bot = Bot(name="atlas", role="an assistant", provider="echo")
    agent = Agent(
        paths=paths,
        bot=bot,
        provider=provider,
        memory=Memory(paths=paths, bot="atlas"),
        control=Control(paths),
        stream_delay=0.0,
    )
    return agent, paths


def test_agent_sends_prior_turns_to_the_model(tmp_path):
    provider = RecordingProvider()
    agent, _ = _agent(tmp_path, provider)
    agent._produce("user", "my name is Alice")
    agent._produce("user", "what is my name?")
    assert [m.content for m in provider.calls[0]] == ["my name is Alice"]
    second = provider.calls[1]
    assert [(m.role, m.content) for m in second] == [
        ("user", "my name is Alice"),
        ("assistant", "ok"),
        ("user", "what is my name?"),
    ]


def test_agent_room_turns_use_transcript_not_history(tmp_path):
    provider = RecordingProvider()
    agent, _ = _agent(tmp_path, provider)
    agent._produce("user", "a private 1:1 turn")
    agent._produce("user", "hello room", room="standup")
    room_call = provider.calls[1]
    assert len(room_call) == 1  # no 1:1 history in a room turn
    assert "group chat" in room_call[0].content.lower()
    assert "reply only as yourself" in room_call[0].content.lower()
    assert "a private 1:1 turn" not in room_call[0].content
    assert "you may @mention another member to ask them" not in room_call[0].content.lower()


def test_agent_sends_user_images_to_the_model(tmp_path):
    provider = RecordingProvider()
    agent, paths = _agent(tmp_path, provider)
    paths.uploads.mkdir(parents=True, exist_ok=True)
    pic = paths.uploads / "shot.png"
    pic.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    agent._produce(
        "user",
        "what is this",
        attachments=[{"name": "shot.png", "path": str(pic), "size": pic.stat().st_size}],
    )
    user = provider.calls[0][-1]
    assert user.role == "user"
    assert user.images == [("image/png", pic.read_bytes())]
    rows = user_thread(agent.memory, peer="user")
    assert rows[0]["attachments"][0]["name"] == "shot.png"
