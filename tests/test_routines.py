"""Scheduled routines: parse time, persist, cron tick, create via chat tool."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime

from agent.memory import Memory
from agent.runtime import _ROUTINE_PROMPT, Agent
from agent.tools import ToolContext, default_tools
from harness.control import Control
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.roster import Bot
from harness.routines import (
    RoutineError,
    add_routine,
    cron_match,
    describe,
    fire_due,
    list_routines,
    parse_schedule,
    parse_when,
    run_now,
    update_routine,
)
from harness.server import make_server
from providers.base import Message, ToolSpec
from providers.echo import EchoProvider


def _paths(tmp_path) -> HarnessPaths:
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas"])
    return p


def test_parse_schedule_times():
    assert parse_schedule("8am") == "0 8 * * *"
    assert parse_schedule("8:00") == "0 8 * * *"
    assert parse_schedule("9:30am") == "30 9 * * *"
    assert parse_schedule("10pm") == "0 22 * * *"
    assert parse_schedule("0 7 * * *") == "0 7 * * *"
    assert describe("0 8 * * *") == "Every day at 08:00"


def test_parse_when_delay_and_once():
    now = datetime(2026, 8, 31, 15, 0, 0)
    hour = parse_when("in an hour", now=now)
    assert abs(hour["once_at"] - now.timestamp() - 3600) < 1
    mins = parse_when("in 20 minutes", now=now)
    assert abs(mins["once_at"] - now.timestamp() - 1200) < 1
    numbered = parse_when("in 1 hour", now=now)
    assert abs(numbered["once_at"] - now.timestamp() - 3600) < 1
    iso = parse_when("2026-08-31 17:30", now=now)
    assert datetime.fromtimestamp(iso["once_at"]).strftime("%Y-%m-%d %H:%M") == "2026-08-31 17:30"
    once = parse_when("once at 5pm", now=now)
    stamp = datetime.fromtimestamp(once["once_at"])
    assert stamp.hour == 17 and stamp.minute == 0
    assert parse_when("8am", now=now) == {"cron": "0 8 * * *"}
    try:
        parse_when("in 0 minutes", now=now)
        raise AssertionError("zero delay should fail")
    except RoutineError as exc:
        assert "minute" in str(exc)


def test_cron_match_daily_hour():
    now = datetime(2026, 8, 23, 8, 0)
    assert cron_match("0 8 * * *", now)
    assert not cron_match("0 9 * * *", now)
    assert not cron_match("0 8 * * *", datetime(2026, 8, 23, 8, 1))


def test_add_and_fire_writes_inbox(tmp_path):
    paths = _paths(tmp_path)
    row = add_routine(
        paths,
        "atlas",
        title="Ahrefs crawl check",
        prompt="Go to Ahrefs and check for crawl errors",
        when="8am",
        enabled=True,
    )
    assert row["schedule"] == "Every day at 08:00"
    listed = list_routines(paths, "atlas")
    assert listed[0]["title"] == "Ahrefs crawl check"

    now = datetime(2026, 8, 23, 8, 0)
    fired = fire_due(paths, ["atlas"], now=now)
    assert len(fired) == 1
    inbox = list((paths.inbox("atlas")).glob("*.json"))
    assert len(inbox) == 1
    body = json.loads(inbox[0].read_text(encoding="utf-8"))
    assert "Ahrefs crawl check" in body["text"]
    assert body["frm"] == "user"

    again = fire_due(paths, ["atlas"], now=now)
    assert again == []
    assert len(list(paths.inbox("atlas").glob("*.json"))) == 1


def test_fire_due_uses_send_callback(tmp_path):
    paths = _paths(tmp_path)
    add_routine(
        paths,
        "atlas",
        title="Ping",
        prompt="say hi",
        when="8am",
        enabled=True,
    )
    sent: list[tuple[str, str]] = []
    fired = fire_due(
        paths,
        ["atlas"],
        now=datetime(2026, 8, 23, 8, 0),
        send=lambda bot, text: sent.append((bot, text)),
    )
    assert len(fired) == 1
    assert sent == [("atlas", "[Routine: Ping]\nsay hi")]
    assert list(paths.inbox("atlas").glob("*.json")) == []


def test_draft_routine_and_test_run(tmp_path):
    from harness.routines import run_now

    paths = _paths(tmp_path)
    row = add_routine(paths, "atlas")
    assert row["title"] == ""
    assert row["triggers"] == []
    try:
        run_now(paths, "atlas", row["id"])
        raise AssertionError("empty instruction should not run")
    except Exception as exc:
        assert "instruction" in str(exc)
    from harness.routines import update_routine

    update_routine(
        paths,
        "atlas",
        row["id"],
        title="Ping",
        prompt="Say hello",
        triggers=[{"type": "schedule", "time": "9am"}],
    )
    ran = run_now(paths, "atlas", row["id"])
    assert ran["history"]
    assert ran["history"][-1]["kind"] == "test"
    inbox = list(paths.inbox("atlas").glob("*.json"))
    assert inbox


def test_disabled_routine_does_not_fire(tmp_path):
    paths = _paths(tmp_path)
    row = add_routine(paths, "atlas", title="Skip me", prompt="do not run", when="8am")
    from harness.routines import update_routine

    update_routine(paths, "atlas", row["id"], enabled=False)
    fired = fire_due(paths, ["atlas"], now=datetime(2026, 8, 23, 8, 0))
    assert fired == []


def test_system_prompt_mentions_routines(tmp_path):
    paths = _paths(tmp_path)
    agent = Agent(
        paths=paths,
        bot=Bot(name="atlas", role="assistant", provider="echo"),
        provider=EchoProvider(),
        memory=Memory(paths=paths, bot="atlas"),
        control=Control(paths),
    )
    prompt = agent.system_prompt("every morning check ahrefs")
    assert _ROUTINE_PROMPT in prompt
    assert "create_routine" in prompt
    assert "8am" in prompt


def _echo_tools():
    return [
        ToolSpec(name="ask_user_choice", description="", parameters={}),
        ToolSpec(name="create_routine", description="", parameters={}),
        ToolSpec(name="create_bot", description="", parameters={}),
    ]


def test_echo_asks_when_for_a_routine():
    p = EchoProvider()
    out = p.complete(
        [Message(role="user", content="every morning go to ahrefs and check crawl errors")],
        tools=_echo_tools(),
    )
    assert out.tool_calls
    call = out.tool_calls[0]
    assert call.name == "ask_user_choice"
    assert "8am" in call.arguments["options"]
    assert "Other" in call.arguments["options"]


def test_echo_creates_routine_after_time_choice():
    p = EchoProvider()
    out = p.complete(
        [
            Message(role="user", content="set up a daily routine to check Ahrefs crawl errors"),
            Message(role="tool", content="user chose: 8am", name="ask_user_choice"),
        ],
        tools=_echo_tools(),
    )
    assert out.tool_calls
    call = out.tool_calls[0]
    assert call.name == "create_routine"
    assert call.arguments["time"] == "8am"
    assert "ahrefs" in call.arguments["prompt"].lower()


def test_echo_other_asks_for_a_typed_time():
    p = EchoProvider()
    out = p.complete(
        [
            Message(role="user", content="create a routine to check crawl errors every morning"),
            Message(role="tool", content="user chose: Other", name="ask_user_choice"),
        ],
        tools=_echo_tools(),
    )
    assert not out.tool_calls
    assert "time" in out.text.lower()


def test_create_routine_tool_posts(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(
        '[[bots]]\nname = "atlas"\nrole = "helper"\nprovider = "echo"\n', encoding="utf-8"
    )
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    httpd = make_server(orch, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        ctx = ToolContext(
            paths=orch.paths, bot="atlas", memory=Memory(paths=orch.paths, bot="atlas")
        )
        out = default_tools()["create_routine"].handler(
            ctx,
            {
                "title": "Ahrefs crawl check",
                "prompt": "Go to Ahrefs and check for crawl errors",
                "time": "8am",
            },
        )
        assert out.startswith("ok:"), out
        rows = list_routines(orch.paths, "atlas")
        assert rows[0]["title"] == "Ahrefs crawl check"
        assert rows[0]["cron"] == "0 8 * * *"
        assert rows[0]["enabled"] is False
        assert "DISABLED" in out or "disabled" in out.lower()
    finally:
        httpd.shutdown()
        orch.down()


def test_draft_routine_does_not_fire_until_enabled(tmp_path):
    paths = _paths(tmp_path)
    row = add_routine(
        paths,
        "atlas",
        title="Stay off",
        prompt="do not run yet",
        when="8am",
    )
    assert row["enabled"] is False
    fired = fire_due(paths, ["atlas"], now=datetime(2026, 8, 23, 8, 0))
    assert fired == []


def test_one_shot_starts_enabled_and_fires_once(tmp_path):
    paths = _paths(tmp_path)
    due = datetime(2026, 8, 31, 16, 0, 0).timestamp()
    row = add_routine(
        paths,
        "atlas",
        title="Staging check",
        prompt="Check staging only, not live",
        once_at=due,
    )
    assert row["enabled"] is True
    assert row["once_at"] == due
    assert row["cron"] == ""
    assert "Once" in row["schedule"]
    assert fire_due(paths, ["atlas"], now=datetime(2026, 8, 31, 15, 59, 0)) == []
    fired = fire_due(paths, ["atlas"], now=datetime(2026, 8, 31, 16, 0, 1))
    assert len(fired) == 1
    assert fired[0]["enabled"] is False
    assert fired[0]["once_fired"] is True
    inbox = list(paths.inbox("atlas").glob("*.json"))
    assert inbox
    body = json.loads(inbox[0].read_text(encoding="utf-8"))
    assert "Staging check" in body["text"]
    assert fire_due(paths, ["atlas"], now=datetime(2026, 8, 31, 16, 5, 0)) == []
    assert len(list(paths.inbox("atlas").glob("*.json"))) == 1


def test_one_shot_delay_autosave_keeps_deadline(tmp_path):
    paths = _paths(tmp_path)
    due = datetime.now().timestamp() + 3600
    row = add_routine(
        paths,
        "atlas",
        title="Later",
        prompt="do the thing",
        once_at=due,
    )
    updated = update_routine(
        paths,
        "atlas",
        row["id"],
        triggers=[{"type": "once", "time": "in 1 hour"}],
    )
    assert abs(updated["once_at"] - due) < 1


def test_one_shot_test_run_does_not_consume(tmp_path):
    paths = _paths(tmp_path)
    due = datetime(2026, 8, 31, 16, 0, 0).timestamp()
    row = add_routine(
        paths,
        "atlas",
        title="Later",
        prompt="do the thing",
        once_at=due,
    )
    ran = run_now(paths, "atlas", row["id"])
    assert ran["history"][-1]["kind"] == "test"
    stored = list_routines(paths, "atlas")[0]
    assert not stored.get("once_fired")
    assert stored["enabled"] is True


def test_echo_creates_one_shot_from_delay():
    p = EchoProvider()
    out = p.complete(
        [Message(role="user", content="in an hour check staging only, not live")],
        tools=_echo_tools(),
    )
    assert out.tool_calls
    call = out.tool_calls[0]
    assert call.name == "create_routine"
    assert "in an hour" in call.arguments["time"].lower()
    assert "staging" in call.arguments["prompt"].lower()


def test_create_routine_tool_posts_one_shot(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(
        '[[bots]]\nname = "atlas"\nrole = "helper"\nprovider = "echo"\n', encoding="utf-8"
    )
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    httpd = make_server(orch, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        ctx = ToolContext(
            paths=orch.paths, bot="atlas", memory=Memory(paths=orch.paths, bot="atlas")
        )
        out = default_tools()["create_routine"].handler(
            ctx,
            {
                "title": "Staging check",
                "prompt": "Check staging only, not live",
                "time": "in 1 hour",
            },
        )
        assert out.startswith("ok:"), out
        assert "ACTIVE" in out or "active" in out.lower()
        assert "DISABLED" not in out
        rows = list_routines(orch.paths, "atlas")
        assert rows[0]["enabled"] is True
        assert rows[0]["once_at"]
        assert rows[0]["cron"] == ""
        assert rows[0]["once_at"] > time.time()
    finally:
        httpd.shutdown()
        orch.down()


def test_system_prompt_mentions_one_shot(tmp_path):
    paths = _paths(tmp_path)
    agent = Agent(
        paths=paths,
        bot=Bot(name="atlas", role="assistant", provider="echo"),
        provider=EchoProvider(),
        memory=Memory(paths=paths, bot="atlas"),
        control=Control(paths),
    )
    prompt = agent.system_prompt("in an hour check staging only")
    assert _ROUTINE_PROMPT in prompt
    assert "ping" in prompt.lower()


def test_split_routine_prompt():
    from agent.messaging import is_routine_prompt, split_routine_prompt

    title, body = split_routine_prompt("[Routine: Ping]\nsay hi")
    assert title == "Ping"
    assert body == "say hi"
    assert is_routine_prompt("[Routine: Ping]\nsay hi")
    assert is_routine_prompt("[Routine: Ping]\nsay hi", origin="routine")
    # origin=routine is not enough: late prompt answers reuse that stamp.
    assert not is_routine_prompt("plain", origin="routine")
    assert not is_routine_prompt("plain hello")


def _loopback(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(
        '[[bots]]\nname = "atlas"\nrole = "helper"\nprovider = "echo"\n', encoding="utf-8"
    )
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    httpd = make_server(orch, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    ctx = ToolContext(paths=orch.paths, bot="atlas", memory=Memory(paths=orch.paths, bot="atlas"))
    return orch, httpd, ctx


def test_routine_tools_list_enable_and_delete_from_chat(tmp_path):
    """A bot asked to "turn the new one on and delete the old one" must be
    able to do both from chat (ALT: it used to answer "no tool for that" and
    send the user to Settings). list_routines shows the ids, update_routine
    flips `enabled`, delete_routine removes the row — all over the same
    loopback API the inspector uses."""
    orch, httpd, ctx = _loopback(tmp_path)
    tools = default_tools()
    try:
        old = add_routine(
            orch.paths, "atlas", title="Monday bin reminder", prompt="old path", when="0 20 * * 1"
        )
        new = add_routine(
            orch.paths, "atlas", title="Monday bin reminder", prompt="new path", when="0 20 * * 1"
        )
        assert old["enabled"] is False and new["enabled"] is False

        listed = tools["list_routines"].handler(ctx, {})
        assert listed.startswith("ok:"), listed
        assert old["id"] in listed and new["id"] in listed
        assert "disabled" in listed

        out = tools["update_routine"].handler(ctx, {"id": new["id"], "enabled": True})
        assert out.startswith("ok:"), out
        assert "ENABLED" in out
        rows = {r["id"]: r for r in list_routines(orch.paths, "atlas")}
        assert rows[new["id"]]["enabled"] is True
        assert rows[old["id"]]["enabled"] is False  # only the named one changed

        out = tools["delete_routine"].handler(ctx, {"id": old["id"]})
        assert out.startswith("ok:"), out
        ids = [r["id"] for r in list_routines(orch.paths, "atlas")]
        assert ids == [new["id"]]

        # A second delete of the same id is a clean error, not a crash.
        assert tools["delete_routine"].handler(ctx, {"id": old["id"]}).startswith("error:")
    finally:
        httpd.shutdown()
        orch.down()


def test_update_routine_tool_rewrites_prompt_title_and_time(tmp_path):
    orch, httpd, ctx = _loopback(tmp_path)
    tools = default_tools()
    try:
        row = add_routine(orch.paths, "atlas", title="Bins", prompt="curl it", when="0 20 * * 1")
        out = tools["update_routine"].handler(
            ctx,
            {"id": row["id"], "title": "Bin day", "prompt": "use PHPSESSID", "time": "9am"},
        )
        assert out.startswith("ok:"), out
        saved = list_routines(orch.paths, "atlas")[0]
        assert saved["title"] == "Bin day"
        assert saved["prompt"] == "use PHPSESSID"
        assert saved["cron"] == "0 9 * * *"
        assert saved["enabled"] is False  # a rewrite does not silently enable

        # Disable by name works too, and nothing-to-change is an error.
        tools["update_routine"].handler(ctx, {"id": row["id"], "enabled": True})
        out = tools["update_routine"].handler(ctx, {"id": row["id"], "enabled": False})
        assert "disabled" in out
        assert list_routines(orch.paths, "atlas")[0]["enabled"] is False
        assert tools["update_routine"].handler(ctx, {"id": row["id"]}).startswith("error:")
        assert tools["update_routine"].handler(ctx, {}).startswith("error:")
        missing = tools["update_routine"].handler(ctx, {"id": "nope", "enabled": True})
        assert missing.startswith("error:")
    finally:
        httpd.shutdown()
        orch.down()


def test_routine_tools_are_governed_and_askable():
    """The new tools ride the gate like create_routine: reads are reads,
    enabling or deleting a schedule is `manage` and worth a policy rule."""
    from agent import govern

    assert govern.EFFECTS["list_routines"].intent == govern.INTENT_READ
    for name in ("update_routine", "delete_routine"):
        eff = govern.EFFECTS[name]
        assert eff.intent == govern.INTENT_MANAGE
        assert eff.ask is True
        assert eff.target({"id": "abc123"}) == "abc123"


def test_london_routine_tracks_bst_and_gmt(tmp_path):

    p = _paths(tmp_path)
    add_routine(
        p,
        "atlas",
        title="UK orders",
        prompt="report",
        when="0 8 * * *",
        enabled=True,
        timezone="Europe/London",
    )
    sent = []
    for month, utc_hour in [(9, 7), (11, 8)]:
        wrong = datetime(2026, month, 5, utc_hour - 1, 0, tzinfo=UTC)
        due = datetime(2026, month, 5, utc_hour, 0, tzinfo=UTC)
        assert fire_due(p, ["atlas"], now=wrong, send=lambda *a: sent.append(a)) == []
        assert len(fire_due(p, ["atlas"], now=due, send=lambda *a: sent.append(a))) == 1
        assert fire_due(p, ["atlas"], now=due, send=lambda *a: sent.append(a)) == []
    assert len(sent) == 2
    row = list_routines(p, "atlas")[0]
    assert row["timezone"] == "Europe/London"


def test_invalid_routine_timezone_does_not_change_saved_schedule(tmp_path):
    import pytest

    p = _paths(tmp_path)
    row = add_routine(p, "atlas", title="orders", prompt="report", when="8am")
    before = list_routines(p, "atlas")
    with pytest.raises(RoutineError):
        update_routine(p, "atlas", row["id"], timezone="not/a/timezone", enabled=True)
    assert list_routines(p, "atlas") == before
    updated = update_routine(p, "atlas", row["id"], timezone="Europe/London")
    assert updated["timezone"] == "Europe/London"
    assert "Europe/London" in updated["schedule"]


def test_timezone_change_allows_same_day_slot(tmp_path):
    """A zone-less last_run must not skip the same clock time in a new zone."""
    p = _paths(tmp_path)
    row = add_routine(
        p,
        "atlas",
        title="orders",
        prompt="report",
        when="0 8 * * *",
        enabled=True,
        timezone="Europe/London",
    )
    london_due = datetime(2026, 9, 5, 7, 0, tzinfo=UTC)
    assert len(fire_due(p, ["atlas"], now=london_due, send=lambda *a: None)) == 1
    assert fire_due(p, ["atlas"], now=london_due, send=lambda *a: None) == []
    update_routine(p, "atlas", row["id"], timezone="Europe/London")
    assert fire_due(p, ["atlas"], now=london_due, send=lambda *a: None) == []
    update_routine(p, "atlas", row["id"], timezone="America/New_York")
    ny_due = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    assert len(fire_due(p, ["atlas"], now=ny_due, send=lambda *a: None)) == 1
    assert fire_due(p, ["atlas"], now=ny_due, send=lambda *a: None) == []


def test_adding_timezone_allows_same_day_slot(tmp_path):
    p = _paths(tmp_path)
    row = add_routine(p, "atlas", title="orders", prompt="report", when="0 8 * * *", enabled=True)
    local_due = datetime(2026, 9, 5, 8, 0)
    assert len(fire_due(p, ["atlas"], now=local_due, send=lambda *a: None)) == 1
    update_routine(p, "atlas", row["id"], timezone="America/New_York")
    ny_due = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    assert len(fire_due(p, ["atlas"], now=ny_due, send=lambda *a: None)) == 1


def test_london_repeated_autumn_minute_fires_once(tmp_path):

    p = _paths(tmp_path)
    add_routine(
        p,
        "atlas",
        title="report",
        prompt="report",
        when="30 1 * * *",
        enabled=True,
        timezone="Europe/London",
    )
    first = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    second = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    assert len(fire_due(p, ["atlas"], now=first, send=lambda *a: None)) == 1
    assert fire_due(p, ["atlas"], now=second, send=lambda *a: None) == []


def test_routine_timezone_round_trips_through_chat_api(tmp_path):
    orch, httpd, ctx = _loopback(tmp_path)
    tools = default_tools()
    try:
        out = tools["create_routine"].handler(
            ctx,
            {
                "title": "UK orders",
                "prompt": "read orders",
                "time": "8am",
                "timezone": "Europe/London",
            },
        )
        assert out.startswith("ok:"), out
        row = list_routines(orch.paths, "atlas")[0]
        assert row["timezone"] == "Europe/London"
        out = tools["update_routine"].handler(ctx, {"id": row["id"], "timezone": "UTC"})
        assert out.startswith("ok:"), out
        assert list_routines(orch.paths, "atlas")[0]["timezone"] == "UTC"
        assert "timezone" in tools["create_routine"].spec.parameters["properties"]
        assert "timezone" in tools["update_routine"].spec.parameters["properties"]
    finally:
        httpd.shutdown()
        orch.down()


def test_zoned_routine_can_be_converted_to_one_shot(tmp_path):
    p = _paths(tmp_path)
    for fields in (
        {"time": "in 1 hour"},
        {"once_at": 1893456000},
        {"triggers": [{"id": "once", "type": "once", "at": 1893456000}]},
    ):
        row = add_routine(
            p, "atlas", title="orders", prompt="report", when="8am", timezone="Europe/London"
        )
        updated = update_routine(p, "atlas", row["id"], **fields)
        assert updated["once_at"] is not None
        assert not updated.get("timezone")


def test_explicit_one_shot_timezone_is_rejected_without_saving(tmp_path):
    import pytest

    p = _paths(tmp_path)
    row = add_routine(p, "atlas", title="orders", prompt="report", when="8am")
    before = list_routines(p, "atlas")
    with pytest.raises(RoutineError, match="recurring"):
        update_routine(p, "atlas", row["id"], time="in 1 hour", timezone="Europe/London")
    assert list_routines(p, "atlas") == before
