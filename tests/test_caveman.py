"""Caveman mode: an account default and a per-bot override, each able to
override the other, both re-read every turn (`agent/caveman.py`)."""

from agent.caveman import PROMPT, effective, enabled_for
from agent.memory import Memory
from agent.runtime import Agent
from harness import prefs
from harness.control import Control
from harness.paths import HarnessPaths
from harness.roster import Bot, Roster, save_roster
from providers.echo import EchoProvider


def _agent(tmp_path, **bot):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    agent = Agent(
        paths=paths,
        bot=Bot(name="atlas", role="assistant", provider="echo", **bot),
        provider=EchoProvider(),
        memory=Memory(paths=paths, bot="atlas"),
        control=Control(paths),
    )
    return agent, paths


def test_effective_truth_table():
    # bot unset → the account decides
    assert effective(None, False) is False
    assert effective(None, True) is True
    # account on, bot off → off
    assert effective(False, True) is False
    # account off, bot on → on
    assert effective(True, False) is True
    assert effective(True, True) is True
    assert effective(False, False) is False


def test_off_everywhere_by_default(tmp_path):
    agent, paths = _agent(tmp_path)
    assert prefs.caveman_default(paths) is False
    assert prefs.account_prefs(paths)["caveman"] is False
    assert PROMPT not in agent.system_prompt("hello")


def test_account_default_reaches_a_bot_that_follows_it(tmp_path):
    agent, paths = _agent(tmp_path)
    prefs.set_caveman(paths, True)
    assert enabled_for(agent.bot, paths) is True
    assert PROMPT in agent.system_prompt("hello")
    assert PROMPT in agent.system_prompt("plan the sprint", consult=True)


def test_bot_off_overrides_account_on(tmp_path):
    agent, paths = _agent(tmp_path, caveman=False)
    prefs.set_caveman(paths, True)
    assert enabled_for(agent.bot, paths) is False
    assert PROMPT not in agent.system_prompt("hello")


def test_bot_on_overrides_account_off(tmp_path):
    agent, paths = _agent(tmp_path, caveman=True)
    assert prefs.caveman_default(paths) is False
    assert enabled_for(agent.bot, paths) is True
    assert PROMPT in agent.system_prompt("hello")


def test_account_toggle_applies_next_turn_without_a_restart(tmp_path):
    agent, paths = _agent(tmp_path)
    assert PROMPT not in agent.system_prompt("one")
    prefs.set_caveman(paths, True)
    assert PROMPT in agent.system_prompt("two")
    prefs.set_caveman(paths, False)
    assert PROMPT not in agent.system_prompt("three")


def test_bot_toggle_is_a_live_profile_field(tmp_path):
    """Settings PATCHes the roster; the running bot adopts it on its next
    turn through refresh_profile, like `dreaming` and the persona labels."""
    agent, paths = _agent(tmp_path)
    save_roster(
        paths.home / "roster.json",
        Roster([Bot(name="atlas", role="assistant", provider="echo", caveman=True)]),
    )
    assert agent.refresh_profile() is True
    assert agent.bot.caveman is True
    assert PROMPT in agent.system_prompt("hi")
    save_roster(
        paths.home / "roster.json",
        Roster([Bot(name="atlas", role="assistant", provider="echo", caveman=None)]),
    )
    assert agent.refresh_profile() is True
    assert agent.bot.caveman is None
    assert PROMPT not in agent.system_prompt("hi")


def test_a_broken_settings_file_means_off(tmp_path):
    agent, paths = _agent(tmp_path)
    prefs.path(paths).write_text("{not json", encoding="utf-8")
    assert prefs.caveman_default(paths) is False
    assert PROMPT not in agent.system_prompt("hello")


def test_prompt_keeps_the_guardrails():
    """Compression must never flip meaning, leak into persisted text, or
    apply to a warning — the parts of the skill that protect the user."""
    assert "not / never / no / only / except" in PROMPT
    assert "security warnings" in PROMPT
    assert "irreversible" in PROMPT
    assert "outside this chat" in PROMPT
    assert "user's language" in PROMPT
    assert "Caveman:" in PROMPT  # the no-prefix rule
