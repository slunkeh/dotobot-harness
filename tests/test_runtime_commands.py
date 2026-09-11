from agent.memory import Memory
from agent.runtime import build_agent
from agent.skills import propose_skill
from agent.soul import save_soul
from harness.paths import HarnessPaths
from harness.roster import Bot


def _agent(tmp_path, name="atlas", personality="precise"):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas", "nova"])
    bot = Bot(name=name, role=f"{name} bot", personality=personality, provider="echo")
    return build_agent(paths, bot, stream_delay=0), paths


def test_memory_command_is_private_to_that_bot(tmp_path):
    atlas, paths = _agent(tmp_path, "atlas")
    Memory(paths=paths, bot="atlas").remember("the sky demo is Tuesday")
    Memory(paths=paths, bot="nova").remember("nova likes short sentences")

    out = atlas._produce("user", "/memory demo")
    assert "Tuesday" in out
    assert "short sentences" not in out

    nova, _ = _agent(tmp_path, "nova", personality="warm")
    # same home — recreate agent against existing paths
    nova = build_agent(
        paths, Bot(name="nova", role="writer", personality="warm", provider="echo"), stream_delay=0
    )
    out2 = nova._produce("user", "/memory demo")
    assert "Tuesday" not in out2
    out3 = nova._produce("user", "/memory sentences")
    assert "short sentences" in out3


def test_remember_and_soul_commands(tmp_path):
    atlas, paths = _agent(tmp_path)
    assert "remembered" in atlas._produce("user", "/remember tea at 4")
    hits = Memory(paths=paths, bot="atlas").recall("tea")
    assert hits
    save_soul(paths, "atlas", "I never guess.")
    soul = atlas._produce("user", "/soul")
    assert "never guess" in soul
    listing = atlas._produce("user", "/skills")
    assert "cite-sources" in listing or "Available skills" in listing


def test_slash_skill_injects_body(tmp_path):
    atlas, paths = _agent(tmp_path)
    propose_skill(
        paths,
        "atlas",
        name="brief-me",
        description="short brief",
        body="Always answer in exactly one word: done",
        when_to_use="user wants a brief",
    )
    out = atlas._produce("user", "/brief-me the report")
    assert "the report" in out
    assert "brief-me" in out or "one word" in out


def test_natural_language_use_skill_injects_body(tmp_path):
    """'use the skill' without a slash still gets the SKILL.md body."""
    atlas, paths = _agent(tmp_path)
    propose_skill(
        paths,
        "atlas",
        name="brief-me",
        description="short brief",
        body="Always answer in exactly one word: done",
        when_to_use="user wants a brief",
    )
    out = atlas._produce("user", "use brief-me on the report")
    assert "one word: done" in out
    assert "the report" in out


def test_skills_prompt_does_not_invite_read_file(tmp_path):
    _, paths = _agent(tmp_path)
    from agent.skills import skills_prompt

    prompt = skills_prompt(paths, "atlas")
    assert "read_file" in prompt  # named as something that does not exist
    assert "load_skill" in prompt
