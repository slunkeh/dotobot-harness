"""Resolution state on prompt entries + skipped-prompt sweep.

A blocking prompt's outcome is persisted on its `prompts/` record and
re-emitted as an `updated` card carrying `resolution`, so live clients settle
the box in place and the reseed renders settled prompts as settled. Choice /
confirm / control_return prompts whose wait ends unanswered are swept: marked
skipped, re-emitted, expired from `prompts/`, and a one-line note is queued
for the bot's next turn. Secret requests are never swept; the waiting tool
persists a skipped resolution to the session log when its own wait ends.
"""

from __future__ import annotations

import threading
import time

from agent.history import user_thread
from agent.memory import Memory
from agent.streaming import (
    StreamEvent,
    StreamReader,
    StreamWriter,
    is_blocking,
    list_prompts,
    pop_turn_notes,
    resolve_prompt,
    settles,
    sweep_skipped_prompts,
    write_answer,
    write_prompt,
)
from agent.tools import ToolContext, default_tools
from harness.paths import HarnessPaths


def _paths(tmp_path) -> HarnessPaths:
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas"])
    return p


def _ctx(paths, writer=None, timeout=1.0) -> ToolContext:
    return ToolContext(
        paths=paths,
        bot="atlas",
        memory=Memory(paths=paths, bot="atlas"),
        writer=writer,
        user_input_timeout=timeout,
    )


def _events(paths, request_id):
    return StreamReader(paths, request_id)._read_new()


def _answer_when_prompted(paths, value):
    def run():
        deadline = time.time() + 4
        while time.time() < deadline:
            rows = list_prompts(paths)
            if rows:
                write_answer(paths, rows[0]["id"], value)
                return
            time.sleep(0.05)

    t = threading.Thread(target=run)
    t.start()
    return t


# -- resolution persisted + re-emitted on settle ---------------------------
def test_confirm_answer_persists_resolution_and_reemits(tmp_path):
    paths = _paths(tmp_path)
    writer = StreamWriter(paths, "r-res-confirm")
    ctx = _ctx(paths, writer=writer, timeout=5.0)
    t = _answer_when_prompted(paths, "confirm")
    out = default_tools()["confirm"].handler(ctx, {"question": "Merge it?"})
    t.join()
    assert out == "user confirmed"

    cards = [e for e in _events(paths, "r-res-confirm") if e.type == "card"]
    assert [c.mutation for c in cards] == ["appended", "updated"]
    assert cards[0].id == cards[1].id
    res = cards[1].resolution
    assert res["state"] == "answered"
    assert res["responded_value"] == "confirm"
    assert res["approved"] is True
    assert res["skipped"] is False
    # a settled card never parks a resuming stream again
    assert not is_blocking(cards[1]) and not settles(cards[1])
    assert is_blocking(cards[0])

    # the record persists WITH its resolution (for the reseed), but is
    # invisible to default open-prompt listings
    assert list_prompts(paths) == []
    rows = list_prompts(paths, include_resolved=True)
    assert len(rows) == 1
    assert rows[0]["id"] == cards[1].id
    assert rows[0]["resolution"]["responded_value"] == "confirm"


def test_choice_answer_persists_resolution_and_reemits(tmp_path):
    paths = _paths(tmp_path)
    writer = StreamWriter(paths, "r-res-choice")
    ctx = _ctx(paths, writer=writer, timeout=5.0)
    t = _answer_when_prompted(paths, "prod")
    out = default_tools()["ask_user_choice"].handler(
        ctx, {"question": "Deploy where?", "options": ["staging", "prod"]}
    )
    t.join()
    assert out == "user chose: prod"

    cards = [e for e in _events(paths, "r-res-choice") if e.type == "card"]
    assert len(cards) == 1  # the choice box itself is a `choice` frame
    assert cards[0].card_type == "choice"
    assert cards[0].mutation == "updated"
    assert cards[0].resolution["responded_value"] == "prod"
    assert cards[0].resolution["skipped"] is False
    rows = list_prompts(paths, include_resolved=True)
    assert rows and rows[0]["resolution"]["state"] == "answered"
    assert list_prompts(paths) == []


def test_room_choice_prompt_carries_room_and_stays_off_1to1(tmp_path):
    """A room-turn choice stamps room on the prompt and never logs the 1:1 card."""
    paths = _paths(tmp_path)
    writer = StreamWriter(paths, "r-res-room")
    ctx = _ctx(paths, writer=writer, timeout=5.0)
    ctx.room = "standup"
    ctx.session_id = "s1"
    t = _answer_when_prompted(paths, "prod")
    out = default_tools()["ask_user_choice"].handler(
        ctx, {"question": "Deploy where?", "options": ["staging", "prod"]}
    )
    t.join()
    assert out == "user chose: prod"
    rows = list_prompts(paths, include_resolved=True)
    assert rows and rows[0]["room"] == "standup"
    hist = user_thread(Memory(paths=paths, bot="atlas"), peer="user")
    assert not any(r.get("type") == "card" for r in hist)


def test_secret_provided_resolution_never_carries_the_value(tmp_path):
    from harness.secrets import set_secret

    paths = _paths(tmp_path)
    writer = StreamWriter(paths, "r-res-secret")
    ctx = _ctx(paths, writer=writer, timeout=5.0)

    def provide():
        deadline = time.time() + 4
        while time.time() < deadline:
            if list_prompts(paths):
                set_secret("API_KEY", "sup3r-s3cret", paths)
                return
            time.sleep(0.05)

    t = threading.Thread(target=provide)
    t.start()
    out = default_tools()["request_secret"].handler(ctx, {"name": "API_KEY"})
    t.join()
    assert out.startswith("ok:")

    rows = list_prompts(paths, include_resolved=True)
    assert len(rows) == 1
    res = rows[0]["resolution"]
    assert res["secret_provided"] is True
    assert "sup3r-s3cret" not in str(rows[0])
    assert list_prompts(paths) == []  # still hidden from open listings


# -- skipped-prompt sweep ---------------------------------------------------
def test_choice_timeout_sweeps_marks_skipped_and_queues_note(tmp_path):
    paths = _paths(tmp_path)
    writer = StreamWriter(paths, "r-skip")
    ctx = _ctx(paths, writer=writer, timeout=0.3)
    out = default_tools()["ask_user_choice"].handler(
        ctx, {"question": "Deploy where?", "options": ["staging", "prod"]}
    )
    assert out.startswith("error:")

    # the box is expired outright — it must not reappear on any reseed
    assert list_prompts(paths, include_resolved=True) == []
    cards = [e for e in _events(paths, "r-skip") if e.type == "card"]
    assert len(cards) == 1
    assert cards[0].mutation == "updated"
    assert cards[0].resolution["state"] == "skipped"
    assert cards[0].resolution["skipped"] is True
    # the model hears about it on its next turn — exactly once
    notes = pop_turn_notes(paths, "atlas")
    assert len(notes) == 1
    assert "Deploy where?" in notes[0]
    assert "not answered" in notes[0]
    assert pop_turn_notes(paths, "atlas") == []


def test_confirm_timeout_sweeps_and_queues_note(tmp_path):
    paths = _paths(tmp_path)
    writer = StreamWriter(paths, "r-skip-c")
    ctx = _ctx(paths, writer=writer, timeout=0.3)
    out = default_tools()["confirm"].handler(ctx, {"question": "Delete the db?"})
    assert out.startswith("error:")
    assert list_prompts(paths, include_resolved=True) == []
    cards = [e for e in _events(paths, "r-skip-c") if e.type == "card"]
    assert cards[-1].resolution["skipped"] is True
    assert any("Delete the db?" in n for n in pop_turn_notes(paths, "atlas"))


def test_sweep_never_touches_secret_requests_or_resolved_rows(tmp_path):
    paths = _paths(tmp_path)
    write_prompt(
        paths,
        {"id": "sec1", "type": "secret_request", "bot": "atlas", "name": "TOKEN"},
    )
    write_prompt(
        paths,
        {
            "id": "cho1",
            "type": "choice",
            "bot": "atlas",
            "question": "Answered already?",
            "options": ["a", "b"],
        },
    )
    resolve_prompt(paths, "cho1", {"state": "answered", "responded_value": "a"})
    write_prompt(
        paths,
        {
            "id": "conf1",
            "type": "card",
            "card_type": "confirm",
            "bot": "atlas",
            "payload": {"question": "Stale?"},
        },
    )
    write_prompt(
        paths,
        {
            "id": "ret1",
            "type": "card",
            "card_type": "control_return",
            "bot": "atlas",
            "payload": {"question": "Can I have the computer back?"},
        },
    )
    write_prompt(  # another bot's prompt: out of scope for atlas's sweep
        paths,
        {"id": "cho2", "type": "choice", "bot": "nova", "question": "q", "options": ["a", "b"]},
    )

    swept = sweep_skipped_prompts(paths, "atlas")
    assert {r["id"] for r in swept} == {"conf1", "ret1"}
    open_ids = {r["id"] for r in list_prompts(paths)}
    assert open_ids == {"sec1", "cho2"}  # secret box + other bot untouched
    resolved = [r for r in list_prompts(paths, include_resolved=True) if r["id"] == "cho1"]
    assert resolved and resolved[0]["resolution"]["state"] == "answered"
    assert any("Stale?" in n for n in pop_turn_notes(paths, "atlas"))
    assert pop_turn_notes(paths, "nova") == []


def test_resolved_records_expire_after_ttl(tmp_path):
    paths = _paths(tmp_path)
    write_prompt(
        paths,
        {"id": "old1", "type": "choice", "bot": "atlas", "question": "q", "options": ["a", "b"]},
    )
    resolve_prompt(paths, "old1", {"state": "answered", "responded_value": "a", "ts": 1.0})
    assert list_prompts(paths, include_resolved=True) == []  # pruned as expired
    assert not (paths.prompts / "old1.json").is_file()


# -- turn-end sweep + next-turn note through the runtime -------------------
def test_runtime_sweeps_stale_prompts_at_turn_end_and_notes_next_turn(tmp_path):
    from agent.runtime import Agent
    from harness.control import Control
    from harness.roster import Bot
    from providers.base import Completion, Provider

    class RecordingProvider(Provider):
        id = "recording"

        def __init__(self, model="rec-1", auth=None, **options):
            super().__init__(model, auth, **options)
            self.systems: list[str] = []

        def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
            self.systems.append(system or "")
            return Completion(text="ok", finish_reason="stop")

    paths = _paths(tmp_path)
    provider = RecordingProvider()
    agent = Agent(
        paths=paths,
        bot=Bot(name="atlas", role="an assistant", provider="echo"),
        provider=provider,
        memory=Memory(paths=paths, bot="atlas"),
        control=Control(paths),
        stream_delay=0.0,
    )
    # a stale unanswered choice box left over from a crashed earlier turn
    write_prompt(
        paths,
        {
            "id": "stale1",
            "type": "choice",
            "bot": "atlas",
            "question": "Which branch?",
            "options": ["main", "dev"],
        },
    )
    writer = StreamWriter(paths, "r-turn1")
    agent._produce("user", "hello", writer=writer, turn_id="r-turn1")

    # swept at turn end: expired + skipped resolution on this turn's stream
    assert list_prompts(paths, include_resolved=True) == []
    cards = [e for e in _events(paths, "r-turn1") if e.type == "card"]
    assert cards and cards[-1].resolution["skipped"] is True
    # the resolution card precedes `final` so it rides the live stream
    kinds = [e.type for e in _events(paths, "r-turn1")]
    assert kinds.index("card") < kinds.index("final")

    # the NEXT turn's system prompt carries the one-line note, then it drains
    agent._produce("user", "and now?", turn_id="r-turn2")
    assert "Which branch?" in provider.systems[-1]
    assert "not answered" in provider.systems[-1]
    assert pop_turn_notes(paths, "atlas") == []
    agent._produce("user", "third", turn_id="r-turn3")
    assert "Which branch?" not in provider.systems[-1]


# -- wire shape ------------------------------------------------------------
def test_resolution_survives_the_sse_relay_allowlist():
    from harness.server import asdict_event

    ev = StreamEvent(
        type="card",
        id="c1",
        card_type="confirm",
        mutation="updated",
        resolution={"state": "skipped", "skipped": True},
    )
    frame = {k: v for k, v in asdict_event(ev).items() if v is not None}
    assert frame["resolution"] == {"state": "skipped", "skipped": True}
    assert frame["mutation"] == "updated"
