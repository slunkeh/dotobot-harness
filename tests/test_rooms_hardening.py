"""Group chat hardening: transcript rows, prompt assembly, routing, relays.

Every test here pins a behaviour that the room review found missing or
wrong: text-only transcripts that lost cards and attachments, the user line
replayed twice per turn, handoff turns that never reached a live client,
`/stop` fanning out as a model turn, and group messages reaching only one bot.
"""

from __future__ import annotations

import io
import json
import select
import socket
import threading
import time

import pytest

from agent import messaging
from agent.memory import Memory
from agent.streaming import StreamWriter, write_prompt
from harness import ws as wsproto
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.rooms import (
    Room,
    RoomError,
    append_card,
    append_message,
    archive_dir,
    create_room,
    delete_room,
    get_room,
    recent_messages,
    save_room,
    transcript_block,
    transcript_file,
)
from harness.server import make_server
from tests.test_ws import WSClient, _http, _recv_type

ROSTER = """
[[bots]]
name = "atlas"
role = "a terse research assistant"
provider = "echo"

[[bots]]
name = "nova"
role = "a careful editor"
provider = "echo"
"""


def _paths(tmp_path) -> HarnessPaths:
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas", "nova"])
    return paths


@pytest.fixture
def server(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
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


def _collect(client: WSClient, secs: float) -> list[dict]:
    """Every frame that lands within `secs`.

    Waits with `select` rather than a socket timeout: a timeout that fires
    mid-frame leaves the buffered reader holding half a frame, and every
    later read on that socket is garbage.
    """
    out: list[dict] = []
    end = time.time() + secs
    client.sock.settimeout(None)
    while time.time() < end:
        ready, _, _ = select.select([client.sock], [], [], min(0.2, max(0.0, end - time.time())))
        if not ready:
            continue
        frame = client.recv()
        if frame is None:
            break
        out.append(frame)
    return out


# -- transcript store -----------------------------------------------------


def test_transcript_rows_carry_attachments_ids_and_quote(tmp_path):
    paths = _paths(tmp_path)
    room = create_room(paths, "Pair", ["atlas", "nova"])
    row = append_message(
        paths,
        room.id,
        frm="user",
        text="see the brief",
        attachments=[{"name": "brief.txt", "path": "/x/brief.txt", "size": 12, "extra": 1}],
        message_id="m-1",
        quote={"id": "q1", "author": "atlas", "text": "earlier"},
    )
    assert row["message_id"] == "m-1"
    assert row["attachments"] == [{"name": "brief.txt", "path": "/x/brief.txt", "size": 12}]
    assert row["quote"] == {"id": "q1", "author": "atlas", "text": "earlier"}
    reply = append_message(paths, room.id, frm="atlas", text="read it", request_id="rid-9")
    assert reply["request_id"] == "rid-9"
    rows = recent_messages(paths, room.id)
    assert [r.get("message_id") for r in rows] == ["m-1", None]
    assert rows[0]["attachments"][0]["name"] == "brief.txt"
    # the attachment is part of what the members see
    assert "[attached: brief.txt]" in transcript_block(paths, room.id)


def test_transcript_block_leaves_out_the_line_being_answered(tmp_path):
    paths = _paths(tmp_path)
    room = create_room(paths, "Pair", ["atlas", "nova"])
    append_message(paths, room.id, frm="user", text="first question", message_id="m-1")
    append_message(paths, room.id, frm="atlas", text="first answer")
    append_message(paths, room.id, frm="user", text="second question", message_id="m-2")
    block = transcript_block(paths, room.id, exclude_message_id="m-2")
    assert "first question" in block
    assert "first answer" in block
    assert "second question" not in block
    assert "second question" in transcript_block(paths, room.id)


def test_transcript_block_is_budgeted_not_line_counted(tmp_path):
    paths = _paths(tmp_path)
    room = create_room(paths, "Pair", ["atlas", "nova"])
    for i in range(30):
        append_message(paths, room.id, frm="user", text=f"line {i:02d} " + "x" * 200)
    block = transcript_block(paths, room.id, budget_tokens=300)
    assert "line 29" in block
    assert "line 00" not in block
    assert "earlier message(s) omitted" in block
    # a generous budget keeps everything (the old 16-line cap is gone)
    full = transcript_block(paths, room.id, budget_tokens=100_000)
    assert "line 00" in full and "omitted" not in full


def test_card_rows_coalesce_by_card_id_and_read_back_settled(tmp_path):
    paths = _paths(tmp_path)
    room = create_room(paths, "Pair", ["atlas", "nova"])
    append_message(paths, room.id, frm="user", text="deploy?")
    append_card(
        paths,
        room.id,
        frm="atlas",
        card_id="c1",
        card_type="confirm",
        payload={"question": "Deploy to prod?"},
    )
    append_message(paths, room.id, frm="nova", text="I'd wait")
    append_card(
        paths,
        room.id,
        frm="atlas",
        card_id="c1",
        card_type="confirm",
        payload={"question": "Deploy to prod?"},
        resolution={"state": "answered", "responded_value": "yes", "approved": True},
    )
    rows = recent_messages(paths, room.id)
    cards = [r for r in rows if r.get("type") == "card"]
    assert len(cards) == 1
    assert cards[0]["card_id"] == "c1"
    assert cards[0]["resolution"]["responded_value"] == "yes"
    # the settled card keeps its original position, before nova's line
    assert [r["frm"] for r in rows] == ["user", "atlas", "nova"]
    assert "[confirm card — answered: yes]" in transcript_block(paths, room.id)


def test_delete_room_archives_instead_of_unlinking(tmp_path):
    paths = _paths(tmp_path)
    room = create_room(paths, "Pair", ["atlas", "nova"])
    append_message(paths, room.id, frm="user", text="keep me")
    delete_room(paths, room.id)
    assert not transcript_file(paths, room.id).exists()
    with pytest.raises(RoomError):
        get_room(paths, room.id)
    archived = sorted(archive_dir(paths).glob(f"{room.id}.*.jsonl"))
    assert archived and "keep me" in archived[0].read_text(encoding="utf-8")


def test_room_description_round_trips(tmp_path):
    paths = _paths(tmp_path)
    room = create_room(paths, "Pair", ["atlas", "nova"], description="  Ship it  ")
    assert get_room(paths, room.id).description == "Ship it"
    room.description = "Changed"
    save_room(paths, room)
    assert Room.from_dict(json.loads((paths.rooms / f"{room.id}.json").read_text()))
    assert get_room(paths, room.id).to_dict()["description"] == "Changed"


# -- stream contract --------------------------------------------------------


def test_stream_writer_stamps_room_on_every_event(tmp_path):
    paths = _paths(tmp_path)
    writer = StreamWriter(paths, "rid-room", room="standup")
    writer.status("typing")
    writer.delta("hi")
    writer.card("atlas", "c1", "table", {"rows": []})
    writer.final("hi", "atlas")
    events = [
        json.loads(line)
        for line in paths.stream_file("rid-room").read_text(encoding="utf-8").splitlines()
    ]
    assert events and all(ev.get("room") == "standup" for ev in events)
    plain = StreamWriter(paths, "rid-plain")
    plain.final("hi", "atlas")
    for line in paths.stream_file("rid-plain").read_text(encoding="utf-8").splitlines():
        assert "room" not in json.loads(line)


# -- agent turn assembly ----------------------------------------------------


def test_room_turn_carries_the_user_line_once(tmp_path):
    from agent.runtime import Agent
    from harness.control import Control
    from harness.roster import Bot
    from tests.test_history import RecordingProvider

    paths = _paths(tmp_path)
    room = create_room(paths, "Pair", ["atlas", "nova"])
    append_message(paths, room.id, frm="user", text="earlier chatter")
    append_message(paths, room.id, frm="nova", text="noted")
    append_message(paths, room.id, frm="user", text="what is the plan", message_id="m-7")
    provider = RecordingProvider()
    agent = Agent(
        paths=paths,
        bot=Bot(name="atlas", role="an assistant", provider="echo"),
        provider=provider,
        memory=Memory(paths=paths, bot="atlas"),
        control=Control(paths),
        stream_delay=0.0,
    )
    agent._produce("user", "what is the plan", room=room.id, message_id="m-7", turn_id="t-1")
    prompt = provider.calls[-1][0].content
    assert prompt.count("what is the plan") == 1
    assert "earlier chatter" in prompt and "noted" in prompt
    # the reply lands on the transcript tagged with the turn it closed
    last = recent_messages(paths, room.id)[-1]
    assert last["frm"] == "atlas" and last["request_id"] == "t-1"


def test_room_handoff_is_a_pointer_not_a_copy_of_the_reply(tmp_path):
    from agent.runtime import Agent
    from harness.control import Control
    from harness.roster import Bot
    from tests.test_history import RecordingProvider

    paths = _paths(tmp_path)
    room = create_room(paths, "Pair", ["atlas", "nova"])
    agent = Agent(
        paths=paths,
        bot=Bot(name="atlas", role="an assistant", provider="echo"),
        provider=RecordingProvider(),
        memory=Memory(paths=paths, bot="atlas"),
        control=Control(paths),
        stream_delay=0.0,
    )
    reply = "Here is a very long reply with lots of detail. @nova can you check it?"
    agent._enqueue_room_mentions(room.id, reply)
    pending = messaging.pending(paths, "nova")
    assert len(pending) == 1
    msg = pending[0]
    assert msg.room == room.id and msg.origin == "room_handoff"
    assert "very long reply" not in msg.text
    assert "atlas mentioned you" in msg.text


def test_room_turns_stay_searchable_in_the_bots_own_recall(tmp_path):
    """Deliberate: a bot remembers what it said in a group (its own log),
    while other members' private memory never leaks in (test_soul_rooms)."""
    paths = _paths(tmp_path)
    mem = Memory(paths=paths, bot="atlas")
    mem.log_turn("s1", "out", "the launch codename is Bluebird", peer="user", room="standup")
    assert any("Bluebird" in str(hit) for hit in mem.recall("Bluebird"))


# -- orchestrator -----------------------------------------------------------


def test_room_card_persists_to_the_room_transcript(tmp_path):
    from agent.tools import ToolContext, _persist_card

    paths = _paths(tmp_path)
    room = create_room(paths, "Pair", ["atlas", "nova"])
    ctx = ToolContext(
        paths=paths,
        bot="atlas",
        memory=Memory(paths=paths, bot="atlas"),
        session_id="s1",
        room=room.id,
    )
    _persist_card(ctx, "c9", "table", {"rows": [[1, 2]]})
    rows = recent_messages(paths, room.id)
    assert rows[-1]["type"] == "card" and rows[-1]["card_id"] == "c9"
    # and nothing landed on the 1:1 log
    assert all(r.get("card_id") != "c9" for r in ctx.memory._session_records())


# -- live server ------------------------------------------------------------


def test_collect_receives_frames_coalesced_with_the_websocket_handshake(monkeypatch):
    """Reading the reseed must not hide later frames from socket readiness."""
    local, remote = socket.socketpair()
    frames = [{"type": "message", "text": "handoff"}, {"type": "final"}]
    wire = io.BytesIO()
    wire.write(b"HTTP/1.1 101 Switching Protocols\r\n\r\n")
    for frame in [{"type": "hello"}, {"type": "ready"}, *frames]:
        wsproto.send_json(wire, frame)
    remote.sendall(wire.getvalue())
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: local)
    client = None
    try:
        client = WSClient("unused", 0)
        assert _collect(client, 0.1) == frames
    finally:
        if client is not None:
            client.rfile.close()
        local.close()
        remote.close()


def test_stop_in_a_room_acks_every_member_without_a_model_turn(server):
    host, port, orch = server
    room = _http(host, port, "/api/rooms", "POST", {"title": "Pair", "members": ["atlas", "nova"]})
    a = WSClient(host, port)
    b = WSClient(host, port)
    try:
        a.send({"type": "chat", "room": room["id"], "text": "/stop"})
        frames = _collect(b, 4)
    finally:
        a.close()
        b.close()
    finals = [f for f in frames if f.get("type") == "final"]
    assert {f.get("bot") for f in finals} == {"atlas", "nova"}
    assert all(f.get("room") == room["id"] and f.get("text") == "Stopped." for f in finals)
    rows = recent_messages(orch.paths, room["id"])
    assert [r["frm"] for r in rows] == ["user"]
    assert rows[0]["text"] == "/stop"


def test_room_handoff_reply_reaches_a_live_client_in_the_room(server):
    """A room mention either joins an active turn or starts its own;
    its settlement must still reach every app in the room."""
    host, port, orch = server
    room = _http(host, port, "/api/rooms", "POST", {"title": "Pair", "members": ["atlas", "nova"]})
    a = WSClient(host, port)
    b = WSClient(host, port)
    try:
        # both answer directly; echo turns atlas's "@nova …" into a
        # message_agent handoff, which joins nova or gets its own relayed turn.
        a.send({"type": "chat", "room": room["id"], "text": "@atlas @nova please review the plan"})
        frames = _collect(b, 10)
    finally:
        a.close()
        b.close()
    # the two direct turns are announced first (by the WS handler); every
    # `accepted` after that is a relayed handoff turn
    accepted = [f["request_id"] for f in frames if f.get("type") == "accepted"]
    direct = set(accepted[:2])
    assert len(direct) == 2
    handoff = [
        f
        for f in frames
        if f.get("type") in {"final", "steered"}
        and f.get("request_id") not in direct
        and f.get("bot")
    ]
    assert handoff, f"handoff settlement never reached the socket: {frames}"
    assert all(f.get("room") == room["id"] for f in handoff)
    assert {f["bot"] for f in handoff} <= {"atlas", "nova"}
    # and every frame of every turn is routed to the room, never a 1:1
    assert all(f.get("room") == room["id"] for f in frames if f.get("request_id"))


def test_room_prompt_resolution_lands_on_the_room_transcript(server):
    host, port, orch = server
    room = _http(host, port, "/api/rooms", "POST", {"title": "Pair", "members": ["atlas", "nova"]})
    write_prompt(
        orch.paths,
        {
            "id": "cho-room-2",
            "type": "choice",
            "bot": "atlas",
            "room": room["id"],
            "question": "Deploy where?",
            "options": ["staging", "prod"],
        },
    )
    c = WSClient(host, port)
    try:
        out = _http(
            host,
            port,
            "/api/answers",
            "POST",
            {"id": "cho-room-2", "value": "prod", "bot": "atlas"},
        )
        assert out.get("ok") is True
        card = _recv_type(c, "card")
        assert card["room"] == room["id"]
    finally:
        c.close()
    rows = _http(host, port, f"/api/rooms/{room['id']}/messages")
    cards = [r for r in rows if r.get("type") == "card" and r.get("card_id") == "cho-room-2"]
    assert len(cards) == 1
    assert cards[0]["resolution"]["responded_value"] == "prod"
    hist = _http(host, port, "/api/bots/atlas/history")
    assert all(r.get("card_id") != "cho-room-2" for r in hist)


def test_room_description_and_owner_patch_via_api(server):
    host, port, _orch = server
    room = _http(
        host,
        port,
        "/api/rooms",
        "POST",
        {"title": "Pair", "members": ["atlas", "nova"], "description": "Launch crew"},
    )
    assert room["description"] == "Launch crew"
    patched = _http(
        host,
        port,
        f"/api/rooms/{room['id']}",
        "PATCH",
        {"description": " Ship week ", "owner": "nova"},
    )
    assert patched["description"] == "Ship week" and patched["owner"] == "user"
    got = _http(host, port, f"/api/rooms/{room['id']}")
    assert got["description"] == "Ship week"
    listed = {r["id"]: r for r in _http(host, port, "/api/rooms")}
    assert listed[room["id"]]["description"] == "Ship week"


@pytest.mark.parametrize("owner", [None, "user", "nova", "deleted-bot"])
def test_api_room_owner_stays_user_for_legacy_clients(server, owner):
    host, port, orch = server
    body = {"title": "Pair", "members": ["atlas", "nova"]}
    if owner is not None:
        body["owner"] = owner
    room = _http(host, port, "/api/rooms", "POST", body)
    assert room["owner"] == "user"
    patched = _http(
        host,
        port,
        f"/api/rooms/{room['id']}",
        "PATCH",
        {"owner": owner, "members": ["nova", "atlas"]},
    )
    assert patched["owner"] == "user"
    assert patched["members"] == ["nova", "atlas"]
    assert get_room(orch.paths, room["id"]).owner == "user"
    assert _http(host, port, f"/api/rooms/{room['id']}")["owner"] == "user"
    assert (
        next(r for r in _http(host, port, "/api/rooms") if r["id"] == room["id"])["owner"] == "user"
    )


def test_unaddressed_group_update_streams_and_persists_each_reply(server):
    host, port, _ = server
    room = _http(
        host, port, "/api/rooms", "POST", {"title": "Updates", "members": ["atlas", "nova"]}
    )
    client = WSClient(host, port)
    try:
        client.send(
            {
                "type": "chat",
                "room": room["id"],
                "text": "I'd like everyone to give me an update on what was achieved over the past 2 days",
            }
        )
        replies = set()
        deadline = time.monotonic() + 10
        while replies != {"atlas", "nova"} and time.monotonic() < deadline:
            frame = client.recv()
            if frame and frame.get("type") == "final" and frame.get("room") == room["id"]:
                replies.add(frame["bot"])
        assert replies == {"atlas", "nova"}
    finally:
        client.close()
    # Final closes the live stream before the worker appends its transcript
    # row. Check eventual persistence separately, rather than racing that write.
    deadline = time.monotonic() + 3
    while True:
        rows = _http(host, port, f"/api/rooms/{room['id']}/messages")
        if len(rows) >= 3 or time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    assert [r["frm"] for r in rows].count("user") == 1
    assert sorted(r["frm"] for r in rows if r["frm"] != "user") == ["atlas", "nova"]


def test_room_attachment_survives_a_reload(server):
    host, port, _orch = server
    room = _http(host, port, "/api/rooms", "POST", {"title": "Pair", "members": ["atlas", "nova"]})
    a = WSClient(host, port)
    try:
        a.send(
            {
                "type": "chat",
                "room": room["id"],
                "text": "@atlas see the brief",
                "attachments": [{"name": "brief.txt", "path": "nope", "size": 1}],
            }
        )
        _collect(a, 3)
    finally:
        a.close()
    rows = _http(host, port, f"/api/rooms/{room['id']}/messages")
    user_rows = [r for r in rows if r["frm"] == "user"]
    assert user_rows and user_rows[0]["attachments"][0]["name"] == "brief.txt"
    assert user_rows[0].get("message_id")
    bot_rows = [r for r in rows if r["frm"] == "atlas"]
    assert bot_rows and bot_rows[0].get("request_id")


def test_room_turn_holds_plugin_tools_without_an_at_mention(tmp_path):
    """An explicit service name in user chat works without an @mention."""
    from agent.runtime import Agent
    from harness.connectors import Connectors
    from harness.control import Control
    from harness.roster import Bot
    from tests.test_history import RecordingProvider

    class ToolSpy(RecordingProvider):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.tools: list[list[str]] = []

        def complete(self, messages, *, system=None, tools=None, **kw):
            self.tools.append([t.name for t in (tools or [])])
            return super().complete(messages, system=system, tools=tools, **kw)

    paths = _paths(tmp_path)
    Connectors(paths).add("linear", "Linear", secret="lin_api_test")
    room = create_room(paths, "Pair", ["atlas", "nova"])
    append_message(
        paths, room.id, frm="user", text="any open Linear tickets for us?", message_id="m-1"
    )
    provider = ToolSpy()
    agent = Agent(
        paths=paths,
        bot=Bot(name="atlas", role="an assistant", provider="echo"),
        provider=provider,
        memory=Memory(paths=paths, bot="atlas"),
        control=Control(paths),
        stream_delay=0.0,
    )
    agent._produce(
        "user", "any open Linear tickets for us?", room=room.id, message_id="m-1", turn_id="t-1"
    )
    assert any(name.startswith("linear_") for name in provider.tools[-1])


def test_room_role_and_other_speakers_do_not_select_plugins(tmp_path):
    """Neither the bot's role nor another speaker selects the user's tools."""
    from agent.runtime import Agent
    from harness.connectors import Connectors
    from harness.control import Control
    from harness.roster import Bot
    from tests.test_history import RecordingProvider

    class ToolSpy(RecordingProvider):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.tools: list[list[str]] = []

        def complete(self, messages, *, system=None, tools=None, **kw):
            self.tools.append([t.name for t in (tools or [])])
            return super().complete(messages, system=system, tools=tools, **kw)

    paths = _paths(tmp_path)
    Connectors(paths).add("github", "GitHub", secret="ghp_test")
    room = create_room(paths, "Pair", ["atlas", "nova"])
    append_message(paths, room.id, frm="nova", text="Use GitHub", message_id="other")
    append_message(paths, room.id, frm="user", text="what did everyone do today?", message_id="m-1")
    provider = ToolSpy()
    agent = Agent(
        paths=paths,
        bot=Bot(name="atlas", role="a PR reviewer for our GitHub repos", provider="echo"),
        provider=provider,
        memory=Memory(paths=paths, bot="atlas"),
        control=Control(paths),
        stream_delay=0.0,
    )
    agent._produce(
        "user", "what did everyone do today?", room=room.id, message_id="m-1", turn_id="t-1"
    )
    assert not any(name.startswith("github_") for name in provider.tools[-1])
