"""Default humanizer system block: always on, never a user-facing skill."""

from agent.humanizer import PROMPT as HUMANIZER_PROMPT
from agent.memory import Memory
from agent.runtime import Agent
from agent.skills import default_skill_names, skills_prompt
from harness.control import Control
from harness.paths import HarnessPaths
from harness.roster import Bot
from providers.echo import EchoProvider


def _agent(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    agent = Agent(
        paths=paths,
        bot=Bot(name="atlas", role="assistant", provider="echo"),
        provider=EchoProvider(),
        memory=Memory(paths=paths, bot="atlas"),
        control=Control(paths),
    )
    return agent, paths


def test_humanizer_is_on_every_turn(tmp_path):
    agent, _ = _agent(tmp_path)
    hello = agent.system_prompt("hello")
    computer = agent.system_prompt("open chrome")
    consult = agent.system_prompt("list env names", consult=True)
    assert HUMANIZER_PROMPT in hello
    assert HUMANIZER_PROMPT in computer
    assert HUMANIZER_PROMPT in consult


def test_humanizer_is_not_a_user_skill(tmp_path):
    agent, paths = _agent(tmp_path)
    assert "humanizer" not in default_skill_names()
    listing = skills_prompt(paths, "atlas")
    assert "humanizer" not in listing.lower()
    assert HUMANIZER_PROMPT not in (listing or "")


def test_humanizer_is_embedded_mode():
    assert "never mention them" in HUMANIZER_PROMPT
    assert "Return only the message" in HUMANIZER_PROMPT
    assert "draft" in HUMANIZER_PROMPT.lower()


def test_humanizer_keeps_chat_replies_short():
    assert "This is a chat" in HUMANIZER_PROMPT
    assert "short, concise, and succinct" in HUMANIZER_PROMPT
    assert "Quoted material" in HUMANIZER_PROMPT
