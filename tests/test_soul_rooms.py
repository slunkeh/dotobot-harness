import json

import pytest

from agent.memory import Memory
from agent.soul import load_soul, save_soul, soul_block
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.rooms import RoomError, create_room, get_room, recent_messages, room_file, save_room
from harness.roster import RosterError


def _paths(tmp_path, bots=("atlas", "nova")):
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(list(bots))
    return p


def test_soul_is_private_per_bot(tmp_path):
    paths = _paths(tmp_path)
    save_soul(paths, "atlas", "I am precise and cite sources.")
    save_soul(paths, "nova", "I am warm and write clean prose.")
    assert "precise" in load_soul(paths, "atlas")
    assert "warm" in load_soul(paths, "nova")
    assert "warm" not in load_soul(paths, "atlas")
    assert "Your soul" in soul_block(paths, "atlas")


def test_soul_seeds_from_personality(tmp_path):
    paths = _paths(tmp_path)
    text = load_soul(paths, "atlas", personality="Stay terse.")
    assert "Stay terse." in text
    # second read does not overwrite a later edit
    save_soul(paths, "atlas", "Updated soul.")
    assert load_soul(paths, "atlas", personality="Stay terse.") == "Updated soul.\n"


def test_memory_stays_private_in_group_home(tmp_path):
    paths = _paths(tmp_path)
    Memory(paths=paths, bot="atlas").remember("atlas only: red balloon")
    Memory(paths=paths, bot="nova").remember("nova only: blue kite")
    assert Memory(paths=paths, bot="atlas").recall("balloon")
    assert Memory(paths=paths, bot="nova").recall("balloon") == []
    assert Memory(paths=paths, bot="nova").recall("kite")


def test_create_and_transcript_room(tmp_path):
    paths = _paths(tmp_path)
    room = create_room(paths, "Research", ["atlas", "nova"])
    got = get_room(paths, room.id)
    assert got.title == "Research"
    assert got.members == ["atlas", "nova"]
    assert got.owner == "user"
    from harness.rooms import append_message

    append_message(paths, room.id, frm="user", text="@atlas remember the launch")
    append_message(paths, room.id, frm="atlas", text="noted")
    rows = recent_messages(paths, room.id)
    assert rows[-1]["frm"] == "atlas"
    assert rows[0]["frm"] == "user"


def test_room_needs_two_bots(tmp_path):
    paths = _paths(tmp_path)
    try:
        create_room(paths, "solo", ["atlas"])
        raise AssertionError("expected RoomError")
    except RoomError:
        pass


def test_room_allows_at_most_six_bots(tmp_path):
    paths = _paths(tmp_path, bots=("atlas", "nova", "kai", "mira", "sol", "tess", "uma"))
    room = create_room(paths, "Six", ["atlas", "nova", "kai", "mira", "sol", "tess"])
    assert room.members == ["atlas", "nova", "kai", "mira", "sol", "tess"]
    try:
        create_room(paths, "Seven", ["atlas", "nova", "kai", "mira", "sol", "tess", "uma"])
        raise AssertionError("expected RoomError")
    except RoomError as exc:
        assert "at most six bots" in str(exc)


def _orch(tmp_path):
    roster = tmp_path / "roster.toml"
    roster.write_text(
        """
[[bots]]
name = "atlas"
role = "researcher"
provider = "echo"
personality = "precise"

[[bots]]
name = "nova"
role = "writer"
provider = "echo"
personality = "warm"
""",
        encoding="utf-8",
    )
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=roster, backend="process")
    orch.init()
    return orch


def test_recipients_for_one_to_one_and_group(tmp_path):
    orch = _orch(tmp_path)
    assert orch.recipients_for("hello", bot="atlas") == ["atlas"]
    assert orch.recipients_for("@nova draft this", bot="atlas") == ["nova"]
    assert orch.recipients_for("what do you think @nova?", bot="atlas") == ["atlas", "nova"]
    room = orch.create_room("Pair", ["atlas", "nova"])
    assert room.owner == "user"
    # All members hear an un-addressed group message; mentions select speakers.
    assert orch.recipients_for("hey all", room=room) == ["atlas", "nova"]
    assert orch.recipients_for("@everyone check in", room=room) == ["atlas", "nova"]
    assert orch.recipients_for("@nova draft this", room=room) == ["nova"]
    assert orch.recipients_for("@atlas use your memory", room=room) == ["atlas"]
    assert orch.recipients_for("@ghost hi", room=room) == ["atlas", "nova"]
    from harness.orchestrator import _deliver_text

    assert _deliver_text("nova", "@nova draft an intro") == "draft an intro"
    assert _deliver_text("atlas", "@nova draft an intro") == "@nova draft an intro"


@pytest.mark.parametrize("legacy_owner", [None, "", "atlas", "nova", "deleted-bot", "user"])
def test_legacy_room_is_user_owned_without_losing_data(tmp_path, legacy_owner):
    orch = _orch(tmp_path)
    room = orch.create_room("Pair", ["nova", "atlas"], description="Launch crew")
    path = room_file(orch.paths, room.id)
    old = json.loads(path.read_text())
    if legacy_owner is None:
        old.pop("owner", None)
    else:
        old["owner"] = legacy_owner
    path.write_text(json.dumps(old))
    original = path.read_bytes()
    from harness.rooms import append_message, transcript_file

    append_message(orch.paths, room.id, frm="user", text="Earlier message")
    transcript = transcript_file(orch.paths, room.id).read_bytes()

    loaded = get_room(orch.paths, room.id)
    assert loaded.owner == "user"
    assert loaded.to_dict() == {**old, "owner": "user"}
    assert orch.recipients_for("Update please", room=loaded) == ["nova", "atlas"]
    assert path.read_bytes() == original  # Reads leave rollback data intact.
    save_room(orch.paths, loaded)
    assert json.loads(path.read_text()) == {**old, "owner": "user"}
    assert transcript_file(orch.paths, room.id).read_bytes() == transcript


def test_group_update_reaches_all_six_members(tmp_path):
    names = [
        "atlas",
        "nova",
        "Orion",
        "lyra",
        "vega",
        "sirius",
    ]
    roster = tmp_path / "roster.toml"
    roster.write_text(
        "\n".join(
            f'[[bots]]\nname = "{name}"\nrole = "assistant"\nprovider = "echo"' for name in names
        )
    )
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=roster, backend="process")
    orch.init()
    room = orch.create_room("Updates", names, owner="atlas")
    text = "I'd like everyone to give me an update on what was achieved over the past 2 days"
    turns = orch.dispatch_chat(text, room_id=room.id)
    assert [name for name, _, _ in turns] == names
    assert len({rid for _, rid, _ in turns}) == 6
    assert room.owner == "user"
    assert len(recent_messages(orch.paths, room.id)) == 1


def test_group_member_can_finish_silently_without_a_blank_reply(tmp_path):
    from agent.streaming import StreamReader, StreamWriter
    from providers.base import Completion, ToolCall
    from tests.test_agent_repairs import ScriptedProvider, _agent

    agent = _agent(tmp_path)
    room = create_room(agent.paths, "Pair", ["atlas", "nova"])
    agent.provider = ScriptedProvider(
        [
            Completion(tool_calls=[ToolCall("quiet", "stay_silent", {})]),
            Completion(text="I should not be asked to acknowledge silence."),
        ]
    )
    writer = StreamWriter(agent.paths, "quiet-turn", room=room.id)
    assert (
        agent._produce(
            "user",
            "Only nova should reply. Atlas, stay silent.",
            writer=writer,
            room=room.id,
            turn_id="quiet-turn",
        )
        == ""
    )
    assert len(agent.provider.calls) == 1
    assert recent_messages(agent.paths, room.id) == []
    events = list(StreamReader(agent.paths, "quiet-turn").events())
    assert any(e.type == "final" and e.text == "" for e in events)
    assert not any(e.type in {"delta", "message"} and (e.text or "").strip() for e in events)
    assert not any(e.type in {"tool", "card", "block"} for e in events)


def test_group_silence_does_not_disable_empty_provider_repairs(tmp_path):
    from agent import repairs
    from providers.base import Completion
    from tests.test_agent_repairs import ScriptedProvider, _agent

    agent = _agent(tmp_path)
    room = create_room(agent.paths, "Pair", ["atlas", "nova"])
    agent.provider = ScriptedProvider([Completion(text=""), Completion(text="My update")])
    assert agent._produce("user", "Everyone give an update", room=room.id) == "My update"
    assert len(agent.provider.calls) == 2
    assert agent.provider.calls[-1][-1].content == repairs.EMPTY_RESPONSE_NUDGE


def test_silence_is_not_available_in_one_to_one_chat(tmp_path):
    from providers.base import Completion, ToolCall
    from tests.test_agent_repairs import ScriptedProvider, _agent

    agent = _agent(tmp_path)
    agent.provider = ScriptedProvider(
        [
            Completion(tool_calls=[ToolCall("quiet", "stay_silent", {})]),
            Completion(text="Here is your answer."),
        ]
    )
    assert agent._produce("user", "Please answer") == "Here is your answer."
    assert len(agent.provider.calls) == 2


def test_group_silence_is_governed(tmp_path):
    from agent import policy
    from providers.base import Completion, ToolCall
    from tests.test_agent_repairs import ScriptedProvider, _agent

    agent = _agent(tmp_path)
    room = create_room(agent.paths, "Pair", ["atlas", "nova"])
    policy.policy_path(agent.paths).write_text('deny = ["stay_silent"]\n')
    agent.provider = ScriptedProvider(
        [
            Completion(tool_calls=[ToolCall("quiet", "stay_silent", {})]),
            Completion(text="I cannot suppress this reply under the current policy."),
        ]
    )
    assert agent._produce("user", "Stay silent", room=room.id).startswith("I cannot")
    assert len(recent_messages(agent.paths, room.id)) == 1


@pytest.mark.parametrize("trailing_tool", [False, True])
def test_new_user_followup_overrides_a_group_silence_decision(tmp_path, monkeypatch, trailing_tool):
    from providers.base import Completion, Message, ToolCall
    from tests.test_agent_repairs import ScriptedProvider, _agent

    agent = _agent(tmp_path)
    room = create_room(agent.paths, "Pair", ["atlas", "nova"])
    calls = [ToolCall("quiet", "stay_silent", {})]
    if trailing_tool:
        calls.append(ToolCall("write", "write_file", {"path": "must-not-exist", "content": "no"}))
    agent.provider = ScriptedProvider(
        [
            Completion(tool_calls=calls),
            Completion(text="My update"),
        ]
    )
    original = agent._inject_followups
    steered = False

    def followup(messages, **kwargs):
        nonlocal steered
        ctx = kwargs.get("context")
        if ctx and ctx.silent_room_turn and not steered:
            steered = True
            messages.append(Message(role="user", content="Actually, atlas, give your update."))
            return True
        return original(messages, **kwargs)

    monkeypatch.setattr(agent, "_inject_followups", followup)
    assert agent._produce("user", "Only nova should reply", room=room.id) == "My update"
    assert recent_messages(agent.paths, room.id)[-1]["text"] == "My update"
    resumed = agent.provider.calls[-1]
    results = {m.tool_call_id: m.content for m in resumed if m.role == "tool"}
    assert set(results) == {call.id for call in calls}
    if trailing_tool:
        assert results["write"].startswith("not run:")
        assert not (agent.paths.workspace / "must-not-exist").exists()


def test_group_handoff_can_reach_a_third_member(tmp_path):
    from agent import messaging
    from agent.runtime import build_agent
    from harness.roster import Bot
    from providers.base import Completion
    from tests.test_agent_repairs import ScriptedProvider

    paths = _paths(tmp_path, bots=("atlas", "nova", "kai"))
    room = create_room(paths, "Chain", ["atlas", "nova", "kai"])
    scripts = {"atlas": "@nova P6 START", "nova": "@kai P6 MIDDLE", "kai": "P6 DONE"}
    message = messaging.Msg(to="atlas", frm="user", text="Start the chain", room=room.id)
    for name, reply in scripts.items():
        agent = build_agent(paths, Bot(name=name, role="test", provider="echo"), stream_delay=0)
        agent.provider = ScriptedProvider([Completion(text=reply)])
        assert agent.handle_message_streamed(message).text == reply
        if name != "kai":
            target = "nova" if name == "atlas" else "kai"
            inbox = messaging.read_inbox(paths, target)
            assert len(inbox) == 1
            path, message = inbox[0]
            assert message.room == room.id and message.origin == "room_handoff"
            assert message.room_handoff_depth == (1 if name == "atlas" else 2)
            messaging.mark_processed(paths, target, path)
    assert [r["text"] for r in recent_messages(paths, room.id)] == list(scripts.values())


@pytest.mark.parametrize("via_tool", [False, True])
def test_group_handoff_chain_is_bounded(tmp_path, via_tool):
    from agent import messaging
    from providers.base import Completion, ToolCall
    from tests.test_agent_repairs import ScriptedProvider, _agent

    agent = _agent(tmp_path)
    room = create_room(agent.paths, "Pair", ["atlas", "nova"])
    script = [Completion(text="@nova please continue")]
    if via_tool:
        script.insert(
            0,
            Completion(
                tool_calls=[
                    ToolCall(
                        "handoff",
                        "message_agent",
                        {
                            "to": "nova",
                            "text": "Please continue",
                            "wait": False,
                        },
                    )
                ]
            ),
        )
    agent.provider = ScriptedProvider(script)
    msg = messaging.Msg(
        to="atlas",
        frm="nova",
        room=room.id,
        text="Continue",
        origin="room_handoff",
        room_handoff_depth=messaging.MAX_ROOM_HANDOFF_DEPTH,
    )
    agent.handle_message_streamed(msg)
    assert messaging.read_inbox(agent.paths, "nova") == []
    assert len(recent_messages(agent.paths, room.id)) == 1


def test_room_system_prompt_skips_one_to_one_relay(tmp_path):
    from agent.runtime import _GROUP_PROMPT, _RELAY_PROMPT, Agent
    from harness.control import Control
    from harness.roster import Bot
    from providers.echo import EchoProvider

    orch = _orch(tmp_path)
    agent = Agent(
        paths=orch.paths,
        bot=Bot(name="atlas", role="researcher", provider="echo"),
        provider=EchoProvider(),
        memory=Memory(paths=orch.paths, bot="atlas"),
        control=Control(orch.paths),
    )
    one = agent.system_prompt("hello")
    group = agent.system_prompt("hello", room=True)
    assert _RELAY_PROMPT in one
    assert _RELAY_PROMPT not in group
    assert _GROUP_PROMPT not in group  # appended at produce-time, not system_prompt
    assert "That bot's own chat" in one


def test_create_room_rejects_unknown_bot(tmp_path):
    orch = _orch(tmp_path)
    try:
        orch.create_room("Nope", ["atlas", "ghost"])
        raise AssertionError("expected RosterError")
    except RosterError:
        pass
