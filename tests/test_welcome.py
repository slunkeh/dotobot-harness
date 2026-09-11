"""A new bot greets first (harness/welcome.py).

Creation queues one user-lane `origin="welcome"` turn; the prompt is the
bot's instruction (hidden from history and the user-bubble fan-out) and the
reply is an ordinary bot message. Recipe installs send the setup variant
after seeding; imports, duplicates and `welcome=False` send nothing.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest

from agent import messaging
from agent.history import user_thread
from agent.memory import Memory
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.recipes import get as get_recipe
from harness.recipes import install as install_recipe
from harness.server import make_server
from harness.welcome import (
    NO_JOB_OPTIONS,
    WELCOME_HEAD,
    is_welcome_prompt,
    queue_welcome,
    welcome_prompt,
)

ROSTER = """
[[bots]]
name = "atlas"
role = "a terse research assistant"
provider = "echo"
"""


# -- prompt composition ---------------------------------------------------
def test_manual_prompt_reads_the_job_from_name_and_description():
    text = welcome_prompt(
        title="Deal Hunting",
        name="deal-hunting",
        role="Finds tech deals",
        description="Compare landed cost across Argos, Amazon and Currys",
    )
    assert is_welcome_prompt(text)
    assert text.startswith(WELCOME_HEAD + " — you were just created]")
    assert '"Deal Hunting"' in text
    assert "Finds tech deals" in text
    assert "Compare landed cost across Argos, Amazon and Currys" in text
    # Drives the chat from that context, not a generic "what am I for".
    assert "propose the first concrete thing" in text
    assert "ask_user_choice" in text
    assert NO_JOB_OPTIONS[0] not in text
    # Nothing is set up before the job is confirmed.
    assert "Do not write memories or save routines until" in text


def test_manual_prompt_without_description_asks_what_the_bot_is_for():
    # Both the Mac app and add_bot fill an empty description with the name,
    # so a description equal to the title means "none".
    text = welcome_prompt(title="New Bot", name="new-bot", description="New Bot")
    assert "do not have a job yet" in text
    for option in NO_JOB_OPTIONS:
        assert f'"{option}"' in text
    assert "Their description of you" not in text
    assert "Role they gave you" not in text


def test_manual_prompt_points_a_drafted_soul_at_the_system_prompt():
    soul = "You are Atlas. " + "Be thorough. " * 80
    text = welcome_prompt(title="Atlas", name="atlas", description=soul)
    assert "already loaded as your soul" in text
    assert "Be thorough. Be thorough." not in text


def test_recipe_prompt_finishes_setup_then_asks_for_the_first_task():
    recipe = get_recipe("pr-reviewer")
    text = welcome_prompt(title="PR Reviewer", name="pr-reviewer", recipe=recipe)
    assert text.startswith(
        WELCOME_HEAD + ' — you were just installed from the "PR Reviewer" recipe]'
    )
    assert "routines, paused" in text
    assert "core memories" in text
    assert recipe["first_task"] in text
    assert "github plugin" in text
    for rule in recipe["never"]:
        assert rule in text
    assert "do not run the job itself yet" in text


def test_prompts_do_not_trip_the_echo_demo_flows():
    """Echo is the test provider: its canned flows (create bot, create routine,
    tips tour, stuck) key off phrases. A welcome that tripped one would leave
    an open choice card behind every bot a test creates."""
    from providers import echo

    prompts = [
        welcome_prompt(title="New Bot", name="new-bot", description="New Bot"),
        welcome_prompt(title="Deal Hunting", name="deal-hunting", description="Finds deals"),
    ]
    prompts += [
        welcome_prompt(title=r["name"], name=r["id"], recipe=r)
        for r in (get_recipe("pr-reviewer"), get_recipe("inbox-triage"))
    ]
    for text in prompts:
        assert not echo._CREATE_ROUTINE.search(text), text
        assert not echo._CREATE_BOT.search(text), text
        assert not echo._TIPS.search(text), text
        assert not echo._STUCK.search(text), text
        assert not echo._MENTION.match(text), text


# -- queueing at creation -------------------------------------------------
def _orch(tmp_path, monkeypatch):
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    orch.use_json_store()
    spawned: list[str] = []
    # No agent process: the inbox must still hold the welcome afterwards.
    monkeypatch.setattr(orch.backend, "spawn", lambda name, argv: spawned.append(name))
    return orch, spawned


def _welcomes(orch, bot):
    return [m for m in messaging.pending(orch.paths, bot) if m.origin == messaging.ORIGIN_WELCOME]


def test_add_bot_queues_one_user_lane_welcome(tmp_path, monkeypatch):
    orch, spawned = _orch(tmp_path, monkeypatch)
    bot = orch.add_bot(name="Deal Hunting", personality="Finds tech deals", provider="echo")
    assert spawned == [bot.name]
    queued = _welcomes(orch, bot.name)
    assert len(queued) == 1
    msg = queued[0]
    assert msg.frm == "user"
    assert messaging.lane_of(msg) == messaging.LANE_USER
    assert is_welcome_prompt(msg.text)
    assert '"Deal Hunting"' in msg.text
    assert "Finds tech deals" in msg.text


def test_add_bot_welcome_can_be_skipped(tmp_path, monkeypatch):
    orch, _ = _orch(tmp_path, monkeypatch)
    bot = orch.add_bot(name="quiet", provider="echo", welcome=False)
    assert messaging.pending(orch.paths, bot.name) == []


def test_unstarted_and_duplicated_bots_get_no_welcome(tmp_path, monkeypatch):
    orch, _ = _orch(tmp_path, monkeypatch)
    imported = orch.add_bot(name="imported", provider="echo", start=False)
    assert messaging.pending(orch.paths, imported.name) == []
    copy = orch.duplicate_bot("atlas")
    assert messaging.pending(orch.paths, copy.name) == []


def test_recipe_install_sends_the_recipe_welcome_after_seeding(tmp_path, monkeypatch):
    orch, _ = _orch(tmp_path, monkeypatch)
    row = install_recipe(orch, "pr-reviewer")
    queued = _welcomes(orch, row["name"])
    assert len(queued) == 1
    text = queued[0].text
    assert 'installed from the "PR Reviewer" recipe' in text
    assert get_recipe("pr-reviewer")["first_task"] in text
    # Seeded before the welcome could be drained: memories are on disk.
    facts = orch.memory_for(row["name"]).facts()
    assert any("green check" in f.get("text", "") for f in facts)
    # Welcome messages are timestamped after the seed, never before.
    assert queued[0].ts >= max(float(f.get("ts", 0.0)) for f in facts) - 1.0


# -- transcript + fan-out -------------------------------------------------
def test_history_hides_the_prompt_and_keeps_the_greeting(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    memory = Memory(paths=paths, bot="atlas")
    prompt = welcome_prompt(title="Atlas", name="atlas", description="Atlas")
    memory.log_turn("s1", "in:user", prompt, peer="user", origin=messaging.ORIGIN_WELCOME)
    memory.log_turn("s1", "out", "Hey. What do you want me around for?", peer="user")
    memory.log_turn("s1", "in:user", "[Welcome — leftover without origin]\nGreet.", peer="user")
    memory.log_turn("s1", "out", "Hello again.", peer="user")
    memory.log_turn("s1", "in:user", "a standing job", peer="user")
    rows = user_thread(memory, peer="user")
    assert [(r["frm"], r["text"]) for r in rows] == [
        ("atlas", "Hey. What do you want me around for?"),
        ("atlas", "Hello again."),
        ("user", "a standing job"),
    ]


@pytest.fixture
def server(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    orch.use_json_store()
    orch.up()
    deadline = time.time() + 10
    while time.time() < deadline and not all(h.status.value == "running" for h in orch.status()):
        time.sleep(0.2)
    httpd = make_server(orch, "127.0.0.1", 0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield ("127.0.0.1", port, orch)
    finally:
        httpd.shutdown()
        orch.down()


def _req(url, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def test_inbox_welcome_turn_does_not_fan_prompt_as_user(server):
    """The consult relay must not paint the welcome prompt as a user bubble."""
    from agent.streaming import StreamWriter
    from tests.test_ws import WSClient

    host, port, orch = server
    a = WSClient(host, port)
    b = WSClient(host, port)
    try:
        orch.control.set_busy(
            "atlas",
            "welcome-rid",
            frm="user",
            preview=welcome_prompt(title="Atlas", name="atlas"),
            origin=messaging.ORIGIN_WELCOME,
        )
        writer = StreamWriter(orch.paths, "welcome-rid")
        writer.status("thinking")
        writer.final("Hey. What do you want me around for?", "atlas")
        frames_b = b.recv_until("final")
        frames_a = a.recv_until("final")
        for frames in (frames_a, frames_b):
            assert not any(f.get("type") == "user" for f in frames)
            final = next(f for f in frames if f.get("type") == "final")
            assert final["text"] == "Hey. What do you want me around for?"
    finally:
        a.close()
        b.close()


def test_relay_bot_turn_keeps_welcome_text_off_the_routine_frame(server):
    from harness.server import relay_bot_turn
    from tests.test_ws import WSClient

    host, port, orch = server
    c = WSClient(host, port)
    try:
        prompt = welcome_prompt(title="Atlas", name="atlas")
        relay_bot_turn(orch, "atlas", prompt, origin=messaging.ORIGIN_WELCOME)
        frames = c.recv_until("final", limit=80)
        routine = next(f for f in frames if f["type"] == "routine")
        assert routine.get("origin") == messaging.ORIGIN_WELCOME
        assert "text" not in routine
        assert not any(f.get("type") == "user" for f in frames)
    finally:
        c.close()


def test_new_bot_greets_over_the_api_end_to_end(server):
    """POST /api/bots -> the echo bot answers its welcome; only the reply is
    on GET /history and the prompt never shows as a user line."""
    host, port, orch = server
    base = f"http://{host}:{port}"
    created = _req(
        f"{base}/api/bots",
        "POST",
        {"name": "scout", "personality": "Finds tech deals", "provider": "echo"},
    )
    assert created["name"] == "scout"
    deadline = time.time() + 20
    rows: list[dict] = []
    while time.time() < deadline:
        rows = _req(f"{base}/api/bots/scout/history")
        if any(r.get("frm") == "scout" for r in rows):
            break
        time.sleep(0.25)
    assert any(r.get("frm") == "scout" for r in rows), rows
    assert not any(is_welcome_prompt(r.get("text") or "") for r in rows), rows
    assert not any(r.get("frm") == "user" for r in rows), rows
    # The bot's own transcript still carries the instruction it answered.
    mem = Memory(paths=orch.paths, bot="scout")
    recorded = [r for r in mem._session_records() if is_welcome_prompt(str(r.get("text") or ""))]
    assert recorded and recorded[0].get("origin") == messaging.ORIGIN_WELCOME


def test_api_create_can_opt_out_of_the_welcome(server):
    host, port, orch = server
    base = f"http://{host}:{port}"
    _req(f"{base}/api/bots", "POST", {"name": "quiet", "provider": "echo", "welcome": False})
    time.sleep(1.0)
    assert _req(f"{base}/api/bots/quiet/history") == []
    q = _req(f"{base}/api/bots/quiet/queue")
    assert q["queued"] == 0


def test_queue_welcome_returns_the_inbox_message_id(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    mid = queue_welcome(paths, "atlas", welcome_prompt(title="Atlas", name="atlas"))
    pending = messaging.pending(paths, "atlas")
    assert [m.id for m in pending] == [mid]
    assert pending[0].origin == messaging.ORIGIN_WELCOME
