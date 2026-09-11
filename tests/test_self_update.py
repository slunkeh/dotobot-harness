"""Bots update themselves and each other — memory, skills, instructions —
and the change lands where the user looks: that bot's Settings.

`update_bot`, `teach_bot`, and `share_skill` reach the harness over the same
loopback API the app uses (like `create_bot`), so the server persists the
change, fans a frame to every live client, and a running bot adopts a new
profile on its next turn without a restart.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

import agent.govern as govern
from agent.memory import Memory
from agent.runtime import _SELF_UPDATE_PROMPT, Agent, _intent_prompts
from agent.skills import load_skills
from agent.tools import ToolContext, default_tools
from harness.control import Control
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.roster import Bot, Roster, save_roster
from harness.server import make_server
from providers.echo import EchoProvider

ROSTER = """
[[bots]]
name = "atlas"
role = "a terse research assistant"
personality = "Answer briefly."
provider = "echo"

[[bots]]
name = "cloud-engineer"
title = "Cloud Engineer"
role = "runs the servers"
provider = "echo"
"""

NEW_TOOLS = ("update_bot", "teach_bot", "share_skill")


@pytest.fixture
def server(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    orch.use_json_store()
    httpd = make_server(orch, "127.0.0.1", 0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    frames: list[dict] = []
    orig = orch.ws_hub.broadcast

    def capture(frame):
        frames.append(frame)
        orig(frame)

    orch.ws_hub.broadcast = capture  # type: ignore[method-assign]
    try:
        yield f"http://127.0.0.1:{port}", orch, frames
    finally:
        orch.ws_hub.broadcast = orig  # type: ignore[method-assign]
        httpd.shutdown()
        orch.down()


def _req(url, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _ctx(paths, bot="atlas") -> ToolContext:
    return ToolContext(paths=paths, bot=bot, memory=Memory(paths=paths, bot=bot))


def _paths(tmp_path) -> HarnessPaths:
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas", "nova"])
    return p


# -- catalogue + gate --------------------------------------------------------


def test_self_update_tools_are_default_and_governed_as_manage():
    tools = default_tools()
    for name in NEW_TOOLS:
        assert name in tools
        intent, target, askable = govern.classify(name, {"bot": "nova"})
        assert intent == govern.INTENT_MANAGE
        assert target == "nova"
        assert askable is True
        # An omitted bot is the caller: the audit row names *something*.
        assert govern.classify(name, {})[1] == "self"


def test_self_update_tools_hold_on_dream_and_after_web(tmp_path):
    """Reshaping a deployment is not a thing an unattended or web-exposed
    turn does on its own — the same default hold create_bot carries."""
    paths = HarnessPaths.resolve(tmp_path)
    for name in NEW_TOOLS:
        assert govern.classify(name, {})[0] in govern.DREAM_HELD_INTENTS
        assert govern.classify(name, {})[0] in govern.EXPOSURE_HELD_INTENTS
    dreaming = _Ctx()
    dreaming.origin = "dream"
    refusal = govern.govern(
        dreaming, "update_bot", {"instructions": "be louder"}, paths=paths, bot="atlas"
    )
    assert refusal
    # An attended chat turn with no policy: permitted, like create_bot.
    assert (
        govern.govern(_Ctx(), "update_bot", {"instructions": "x"}, paths=paths, bot="atlas") is None
    )


class _Ctx:
    tool_call_id = "call-1"
    origin = ""
    web_exposed = False
    delivery_uncertain = False


# -- argument handling (no server) ------------------------------------------


def test_update_bot_needs_a_field_and_a_server(tmp_path):
    paths = _paths(tmp_path)
    tools = default_tools()
    out = tools["update_bot"].handler(_ctx(paths), {})
    assert out.startswith("error:")
    assert "instructions" in out
    out = tools["update_bot"].handler(_ctx(paths), {"instructions": "Be kind."})
    assert "serve.json" in out


def test_teach_bot_self_writes_own_memory_without_a_server(tmp_path):
    paths = _paths(tmp_path)
    tools = default_tools()
    assert tools["teach_bot"].handler(_ctx(paths), {"bot": "nova"}).startswith("error:")
    out = tools["teach_bot"].handler(_ctx(paths), {"text": "the wifi is flaky"})
    assert out.startswith("ok:")
    assert Memory(paths=paths, bot="atlas").recall("wifi")
    # A peer needs the harness API.
    out = tools["teach_bot"].handler(_ctx(paths), {"bot": "nova", "text": "the wifi is flaky"})
    assert "serve.json" in out
    assert Memory(paths=paths, bot="nova").recall("wifi") == []


def test_share_skill_self_writes_a_private_skill(tmp_path):
    paths = _paths(tmp_path)
    tools = default_tools()
    assert tools["share_skill"].handler(_ctx(paths), {"name": "x"}).startswith("error:")
    out = tools["share_skill"].handler(
        _ctx(paths),
        {"name": "Tidy Desk", "description": "how to tidy", "body": "1. clear\n2. wipe"},
    )
    assert out.startswith("ok:")
    assert "/tidy-desk" in out
    skill = next(s for s in load_skills(paths, "atlas") if s.skill_id == "tidy-desk")
    assert "wipe" in skill.body
    assert skill.source == "private"


def test_peer_names_are_path_safe(tmp_path):
    """A bot name becomes an API path segment; anything else is refused
    before any request is built (no roster to resolve against here)."""
    paths = _paths(tmp_path)
    tools = default_tools()
    for bad in ("../atlas", "nova/restart", "a b"):
        out = tools["teach_bot"].handler(_ctx(paths), {"bot": bad, "text": "x"})
        assert out.startswith("error:"), out
        assert "serve.json" not in out


# -- end to end over the loopback API ----------------------------------------


def test_update_bot_rewrites_self_and_peer_and_pushes_roster(server):
    base, orch, frames = server
    tools = default_tools()
    ctx = _ctx(orch.paths)

    # Seed the soul from the current personality, as a first turn does.
    assert "Answer briefly." in _req(f"{base}/api/bots/atlas/soul")["soul"]
    out = tools["update_bot"].handler(ctx, {"instructions": "Always answer in haiku."})
    assert out.startswith("ok:"), out
    # A soul that was still the verbatim seed follows the new instructions;
    # the old text does not linger in every prompt as the bot's "soul".
    assert _req(f"{base}/api/bots/atlas/soul")["soul"].strip() == "Always answer in haiku."
    assert "yourself" in out and "instructions" in out
    assert orch.roster.get("atlas").personality == "Always answer in haiku."
    store = json.loads((orch.paths.home / "roster.json").read_text(encoding="utf-8"))
    row = next(b for b in store["bots"] if b["name"] == "atlas")
    assert row["personality"] == "Always answer in haiku."
    # The app's roster snapshot rides the same push a Settings edit does.
    assert any(f.get("type") == "bots" for f in frames)
    listed = {b["name"]: b for b in _req(f"{base}/api/bots")}
    assert listed["atlas"]["personality"] == "Always answer in haiku."

    # An edited soul is the bot's own: a later instructions rewrite leaves it.
    _req(f"{base}/api/bots/atlas/soul", "PUT", {"soul": "I am atlas, and I like maps."})
    out = tools["update_bot"].handler(ctx, {"instructions": "Answer in limericks."})
    assert out.startswith("ok:"), out
    assert "like maps" in _req(f"{base}/api/bots/atlas/soul")["soul"]

    # A peer by display name, several fields at once, plus its soul.
    out = tools["update_bot"].handler(
        ctx,
        {
            "bot": "Cloud Engineer",
            "role": "keeps Example Host green",
            "title": "SRE",
            "instructions": "Prefer terraform over clicking.",
            "soul": "I am the calm one on call.",
        },
    )
    assert out.startswith("ok:"), out
    assert "'cloud-engineer'" in out
    peer = orch.roster.get("cloud-engineer")
    assert peer.role == "keeps Example Host green"
    assert peer.title == "SRE"
    assert peer.personality == "Prefer terraform over clicking."
    soul = _req(f"{base}/api/bots/cloud-engineer/soul")
    assert "calm one on call" in soul["soul"]
    soul_frames = [f for f in frames if f.get("type") == "soul"]
    assert soul_frames and soul_frames[-1]["bot"] == "cloud-engineer"
    assert "calm one on call" in soul_frames[-1]["soul"]

    # Provider / model are the user's: they never ride this tool.
    before = orch.roster.get("atlas").provider
    out = tools["update_bot"].handler(ctx, {"provider": "claude", "model": "x"})
    assert out.startswith("error:")
    assert orch.roster.get("atlas").provider == before


def test_update_bot_unknown_or_ambiguous_peer_asks(server):
    base, orch, _frames = server
    tools = default_tools()
    ctx = _ctx(orch.paths)
    out = tools["update_bot"].handler(ctx, {"bot": "nobody", "role": "x"})
    assert out.startswith("error:")
    assert "ask_user_choice" in out
    assert orch.roster.get("atlas").role == "a terse research assistant"


def test_teach_bot_lands_in_peer_memory_and_settings(server):
    base, orch, frames = server
    tools = default_tools()
    out = tools["teach_bot"].handler(
        _ctx(orch.paths), {"bot": "cloud-engineer", "text": "prod deploys happen after 6pm"}
    )
    assert out.startswith("ok:"), out
    assert "Memory" in out
    facts = _req(f"{base}/api/bots/cloud-engineer/memory")["facts"]
    assert any("6pm" in f["text"] for f in facts)
    assert Memory(paths=orch.paths, bot="cloud-engineer").recall("deploys")
    # The teacher's own memory is untouched.
    assert Memory(paths=orch.paths, bot="atlas").recall("deploys") == []
    mem_frames = [f for f in frames if f.get("type") == "memory"]
    assert mem_frames and mem_frames[-1]["bot"] == "cloud-engineer"
    assert any("6pm" in f["text"] for f in mem_frames[-1]["facts"])


def test_share_skill_lands_in_peer_skills_and_settings(server):
    base, orch, frames = server
    tools = default_tools()
    out = tools["share_skill"].handler(
        _ctx(orch.paths),
        {
            "bot": "Cloud Engineer",
            "name": "Rotate Keys",
            "description": "rotate the deploy key",
            "body": "1. mint a key\n2. swap it in\n3. revoke the old one",
            "when_to_use": "quarterly",
        },
    )
    assert out.startswith("ok:"), out
    assert "/rotate-keys" in out
    skills = {s["name"]: s for s in _req(f"{base}/api/skills?bot=cloud-engineer")}
    assert "rotate-keys" in skills
    assert skills["rotate-keys"]["source"] == "private"
    private = next(
        s for s in load_skills(orch.paths, "cloud-engineer") if s.skill_id == "rotate-keys"
    )
    assert "revoke the old one" in private.body
    assert private.when_to_use == "quarterly"
    # Not the sharer's skill.
    assert all(s.skill_id != "rotate-keys" for s in load_skills(orch.paths, "atlas"))
    saved = [f for f in frames if f.get("type") == "skill_saved"]
    assert saved and saved[-1]["bot"] == "cloud-engineer"
    assert saved[-1]["name"] == "rotate-keys"
    assert any(s.get("name") == "rotate-keys" for s in saved[-1]["skills"])


def test_skill_route_validates(server):
    base, _orch, _frames = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _req(f"{base}/api/bots/atlas/skills", "POST", {"name": "x"})
    assert exc.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as exc:
        _req(f"{base}/api/bots/nobody/skills", "POST", {"name": "x", "body": "y"})
    assert exc.value.code == 404


# -- a running bot adopts the change without a restart -----------------------


def _agent(paths, personality="Answer briefly.") -> Agent:
    return Agent(
        paths=paths,
        bot=Bot(name="atlas", role="assistant", provider="echo", personality=personality),
        provider=EchoProvider(),
        memory=Memory(paths=paths, bot="atlas"),
        control=Control(paths),
    )


def test_running_bot_adopts_roster_edits_next_turn(tmp_path):
    paths = _paths(tmp_path)
    agent = _agent(paths)
    assert "Answer briefly." in agent.system_prompt("hi")
    # Nothing to adopt: no roster file, and later an entry that is not us.
    assert agent.refresh_profile() is False
    save_roster(paths.home / "roster.json", Roster([Bot(name="nova", provider="echo")]))
    assert agent.refresh_profile() is False
    assert agent.bot.personality == "Answer briefly."

    save_roster(
        paths.home / "roster.json",
        Roster(
            [
                Bot(
                    name="atlas",
                    role="poet",
                    title="Atlas the Poet",
                    provider="claude",
                    model="opus",
                    personality="Always answer in haiku.",
                )
            ]
        ),
    )
    assert agent.refresh_profile() is True
    assert agent.refresh_profile() is False
    prompt = agent.system_prompt("hi")
    assert "Always answer in haiku." in prompt
    assert "Atlas the Poet" in prompt and "poet" in prompt
    assert "Answer briefly." not in agent.bot.system_prompt()
    # Identity stays what the process was spawned with: the orchestrator
    # restarts a bot for those, this refresh must never swap them live.
    assert agent.bot.provider == "echo"
    assert agent.bot.model is None


def test_turn_refreshes_profile_before_prompting(tmp_path):
    paths = _paths(tmp_path)
    agent = _agent(paths)
    save_roster(
        paths.home / "roster.json",
        Roster([Bot(name="atlas", provider="echo", personality="Always answer in haiku.")]),
    )
    agent._produce("user", "hello")
    assert agent.bot.personality == "Always answer in haiku."


def test_malformed_roster_keeps_current_profile(tmp_path):
    paths = _paths(tmp_path)
    agent = _agent(paths)
    (paths.home / "roster.json").write_text("{not json", encoding="utf-8")
    assert agent.refresh_profile() is False
    assert agent.bot.personality == "Answer briefly."


# -- the tutorial rides the turns that need it ------------------------------


def test_self_update_prompt_only_on_relevant_turns():
    assert _SELF_UPDATE_PROMPT in "\n".join(_intent_prompts("update your instructions"))
    assert _SELF_UPDATE_PROMPT in "\n".join(_intent_prompts("teach nova about the deploy"))
    assert _SELF_UPDATE_PROMPT in "\n".join(_intent_prompts("remember that I like tea"))
    assert _SELF_UPDATE_PROMPT not in "\n".join(_intent_prompts("what is the weather"))
    for tool in NEW_TOOLS:
        assert tool in _SELF_UPDATE_PROMPT
    assert "Settings" in _SELF_UPDATE_PROMPT
