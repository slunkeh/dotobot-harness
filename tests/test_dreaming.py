"""Dreaming: opt-in, backed off, budgeted, and gated.

The feature is three small pieces that must agree: the scheduler
(`harness/dreaming.py`) decides *when* an unattended turn happens, the
message bus puts it in the background lane so it never starves a chat, and
the gate (`agent/govern.py`) holds side-effect intents back while nobody is
watching. It shipped one release earlier as "idle think", so every legacy
spelling (roster flag, env vars, wire origin) must keep working.
"""

from __future__ import annotations

import json

import agent.govern as govern
import agent.policy as policy
from agent import messaging
from harness import audit, dreaming
from harness.control import Control
from harness.paths import HarnessPaths
from harness.roster import Bot, Roster, load_roster
from harness.usage import record_usage, tokens_today


def _paths(tmp_path) -> HarnessPaths:
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    return paths


def _bot(**kw) -> Bot:
    kw.setdefault("name", "atlas")
    return Bot(**kw)


# -- the lane ---------------------------------------------------------------


def test_dreams_ride_the_background_lane():
    msg = messaging.Msg(to="atlas", frm="user", text="dream", origin=messaging.ORIGIN_DREAM)
    assert messaging.lane_of(msg) == messaging.LANE_BACKGROUND
    # the first release's wire origin still lands in the same lane
    legacy = messaging.Msg(to="atlas", frm="user", text="think", origin=messaging.ORIGIN_IDLE)
    assert messaging.lane_of(legacy) == messaging.LANE_BACKGROUND
    # a real user chat still outranks it
    chat = messaging.Msg(to="atlas", frm="user", text="hi")
    assert messaging.lane_of(chat) == messaging.LANE_USER


# -- the scheduler ----------------------------------------------------------


def test_opt_in_is_off_by_default(tmp_path):
    paths = _paths(tmp_path)
    assert dreaming.fire_due(paths, [_bot()], now=1_000_000.0) == []
    assert not messaging.pending(paths, "atlas")


def test_first_sighting_arms_without_firing(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DREAM_MIN_SECS", "600")
    paths = _paths(tmp_path)
    bot = _bot(dreaming=True)
    assert dreaming.fire_due(paths, [bot], now=1_000_000.0) == []  # armed, not fired
    assert dreaming.fire_due(paths, [bot], now=1_000_100.0) == []  # inside the gap
    assert dreaming.fire_due(paths, [bot], now=1_000_700.0) == ["atlas"]
    pending = messaging.pending(paths, "atlas")
    assert len(pending) == 1
    assert pending[0].origin == messaging.ORIGIN_DREAM
    assert "Dreaming" in pending[0].text
    for move in ("Consolidate", "Reflect", "Aspire"):
        assert move in pending[0].text


def test_legacy_env_names_still_tune_the_schedule(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_IDLE_MIN_SECS", "600")  # first release's name
    paths = _paths(tmp_path)
    bot = _bot(dreaming=True)
    dreaming.fire_due(paths, [bot], now=1_000_000.0)
    assert dreaming.load_state(paths, "atlas")["interval"] == 600.0


def test_backoff_doubles_and_activity_resets_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DREAM_MIN_SECS", "600")
    monkeypatch.setenv("HARNESS_DREAM_MAX_SECS", "2400")
    paths = _paths(tmp_path)
    bot = _bot(dreaming=True)
    now = 1_000_000.0
    dreaming.fire_due(paths, [bot], now=now)  # arm
    assert dreaming.fire_due(paths, [bot], now=now + 600) == ["atlas"]
    # doubled: the next dream waits 1200s, not 600
    assert dreaming.fire_due(paths, [bot], now=now + 1300) == []
    assert dreaming.fire_due(paths, [bot], now=now + 1900) == ["atlas"]
    # the dreams themselves are not "activity"; a real chat is
    messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="hello", ts=now + 2000))
    # backoff is back at the minimum, measured from the chat, not the dream
    assert dreaming.fire_due(paths, [bot], now=now + 2500) == []
    assert dreaming.fire_due(paths, [bot], now=now + 2601) == ["atlas"]


def test_backoff_caps_at_the_ceiling(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DREAM_MIN_SECS", "600")
    monkeypatch.setenv("HARNESS_DREAM_MAX_SECS", "900")
    paths = _paths(tmp_path)
    bot = _bot(dreaming=True)
    dreaming.fire_due(paths, [bot], now=1_000_000.0)
    dreaming.fire_due(paths, [bot], now=1_000_600.0)
    state = dreaming.load_state(paths, "atlas")
    assert state["interval"] == 900.0  # min(600*2, ceiling)


def test_state_from_the_idle_think_release_is_read(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DREAM_MIN_SECS", "600")
    paths = _paths(tmp_path)
    legacy = paths.home / "idle" / "atlas.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"last_tick": 1_000_000.0, "interval": 600.0}), encoding="utf-8")
    bot = _bot(dreaming=True)
    # armed by the legacy state: fires on its schedule instead of re-arming
    assert dreaming.fire_due(paths, [bot], now=1_000_700.0) == ["atlas"]
    # and the new state lands under dreams/
    assert (paths.home / "dreams" / "atlas.json").is_file()


def test_paused_or_busy_bot_does_not_dream(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DREAM_MIN_SECS", "600")
    paths = _paths(tmp_path)
    bot = _bot(dreaming=True)
    control = Control(paths)
    dreaming.fire_due(paths, [bot], now=1_000_000.0, control=control)
    control.take_over(bot.name, holder="human")
    assert dreaming.fire_due(paths, [bot], now=1_000_700.0, control=control) == []
    control.return_control(bot.name)
    assert dreaming.fire_due(paths, [bot], now=1_000_800.0, control=control) == ["atlas"]


def test_blocked_bot_does_not_dream(tmp_path, monkeypatch):
    """Guideline 1.2: Block bot means no turn of any origin. A blocked bot
    with dreaming on is skipped by the tick (not armed, not fired, no inbox
    write) and dreams again once the owner unblocks it."""
    monkeypatch.setenv("HARNESS_DREAM_MIN_SECS", "600")
    paths = _paths(tmp_path)
    bot = _bot(dreaming=True, blocked=True)
    assert dreaming.fire_due(paths, [bot], now=1_000_000.0) == []
    assert dreaming.fire_due(paths, [bot], now=1_000_700.0) == []
    assert not (paths.home / "dreams" / "atlas.json").exists()
    assert list(paths.inbox("atlas").glob("*.json")) == []
    bot.blocked = False
    assert dreaming.fire_due(paths, [bot], now=1_000_700.0) == []  # armed now
    assert dreaming.fire_due(paths, [bot], now=1_001_400.0) == ["atlas"]


def test_daily_budget_suspends_dreams(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DREAM_MIN_SECS", "600")
    monkeypatch.setenv("HARNESS_DREAM_TOKENS", "1000")
    paths = _paths(tmp_path)
    bot = _bot(dreaming=True)
    # split across the new and legacy origins: both count toward the allowance
    record_usage(
        paths,
        "claude",
        "m",
        tokens={"input_tokens": 600, "output_tokens": 100},
        bot="atlas",
        origin="dream",
    )
    record_usage(
        paths,
        "claude",
        "m",
        tokens={"input_tokens": 400},
        bot="atlas",
        origin="idle",
    )
    dreaming.fire_due(paths, [bot], now=None)  # arm from the real clock
    state = dreaming.load_state(paths, "atlas")
    later = state["last_tick"] + 700
    assert dreaming.fire_due(paths, [bot], now=later) == []  # over allowance
    assert not messaging.pending(paths, "atlas")
    # but the tick was stamped, so the ledger is not re-read every pass
    assert dreaming.load_state(paths, "atlas")["last_tick"] == later


def test_tokens_today_counts_only_the_named_bot_and_origin(tmp_path):
    paths = _paths(tmp_path)
    record_usage(paths, "claude", "m", tokens={"input_tokens": 10}, bot="atlas", origin="dream")
    record_usage(paths, "claude", "m", tokens={"input_tokens": 20}, bot="atlas")  # a chat turn
    record_usage(paths, "claude", "m", tokens={"input_tokens": 40}, bot="nova", origin="dream")
    record_usage(paths, "claude", "m", tokens={"input_tokens": 80})  # pre-attribution record
    assert tokens_today(paths, "atlas", origin="dream") == 10
    assert tokens_today(paths, "atlas") == 30
    ledger = (paths.usage / "usage.jsonl").read_text(encoding="utf-8").splitlines()
    assert "bot" not in json.loads(ledger[-1])  # unattributed records stay clean


# -- the gate ---------------------------------------------------------------


class _Ctx:
    def __init__(self, origin=None):
        self.tool_call_id = "call-1"
        self.origin = origin


def test_dream_turn_holds_side_effects_back(tmp_path):
    paths = _paths(tmp_path)
    refusal = govern.govern(
        _Ctx(origin="dream"),
        "linear_create_issue",
        {},
        paths=paths,
        bot="atlas",
        connector_tools={"linear_create_issue"},
    )
    assert refusal is not None and "held back while dreaming" in refusal
    row = audit.read(paths, "atlas")[0]
    assert row["decision"] == "refuse"
    assert row["source"] == "dream-default"
    # the first release's wire origin gets the same default
    assert (
        govern.govern(
            _Ctx(origin="idle"),
            "linear_create_issue",
            {},
            paths=paths,
            bot="atlas",
            connector_tools={"linear_create_issue"},
        )
        is not None
    )


def test_dream_turn_keeps_read_and_reflect_tools(tmp_path):
    paths = _paths(tmp_path)
    for name, args in (
        ("recall", {"query": "deploys"}),
        ("remember", {"text": "follow up on the deploy"}),
        ("write_soul", {"section": "voice"}),
        ("propose_skill", {"name": "deploy-runbook"}),
        ("run_command", {"command": "ls"}),
        ("linear_get_issue", {}),
    ):
        assert (
            govern.govern(
                _Ctx(origin="dream"),
                name,
                args,
                paths=paths,
                bot="atlas",
                connector_tools={"linear_get_issue"},
            )
            is None
        ), name


def test_chat_turns_are_untouched_by_the_dream_default(tmp_path):
    paths = _paths(tmp_path)
    assert (
        govern.govern(
            _Ctx(origin=None),
            "linear_create_issue",
            {},
            paths=paths,
            bot="atlas",
            connector_tools={"linear_create_issue"},
        )
        is None
    )


def test_an_operator_allow_rule_widens_a_dream_turn(tmp_path):
    paths = _paths(tmp_path)
    pol = policy.parse({"allow": [{"origin": "dream", "intent": "write_tool"}, {"bot": "*"}]})
    assert (
        govern.govern(
            _Ctx(origin="dream"),
            "linear_create_issue",
            {},
            paths=paths,
            bot="atlas",
            policy=pol,
            connector_tools={"linear_create_issue"},
        )
        is None
    )


def test_an_operator_deny_rule_narrows_a_dream_turn(tmp_path):
    paths = _paths(tmp_path)
    pol = policy.parse({"deny": [{"origin": "dream", "intent": "run_command"}]})
    refusal = govern.govern(
        _Ctx(origin="dream"),
        "run_command",
        {"command": "ls"},
        paths=paths,
        bot="atlas",
        policy=pol,
    )
    assert refusal is not None and "action policy" in refusal
    # and the same deny does not touch a chat turn
    assert (
        govern.govern(
            _Ctx(origin=None),
            "run_command",
            {"command": "ls"},
            paths=paths,
            bot="atlas",
            policy=pol,
        )
        is None
    )


# -- the roster flag --------------------------------------------------------


def test_dreaming_round_trips_the_roster(tmp_path):
    path = tmp_path / "roster.toml"
    path.write_text(
        '[[bots]]\nname = "atlas"\ndreaming = true\n\n'
        '[[bots]]\nname = "iris"\nidle_think = true\n\n'  # first release's key
        '[[bots]]\nname = "nova"\n',
        encoding="utf-8",
    )
    roster = load_roster(path)
    assert roster.get("atlas").dreaming is True
    assert roster.get("iris").dreaming is True
    assert roster.get("nova").dreaming is False
    assert roster.get("atlas").to_dict()["dreaming"] is True
    assert Roster(bots=[Bot(name="x")]).get("x").dreaming is False
