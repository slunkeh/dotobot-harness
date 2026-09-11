"""Grok Bot chat-shortcut parity: connectors, skills and pickers.

Checked against the Grok Bot Mac app on 2026-09-02: its `/` picker matches
anywhere in a name, its `@` picker lists added-but-unsigned plugins as
"needs auth" (in groups too), naming a catalog plugin nobody added gets an
"Add <name>?" offer rather than a bot, and it ships an `add-connector`
skill. Each test here fails on the pre-parity harness.
"""

from __future__ import annotations

from agent import tools
from agent.govern import EFFECTS
from agent.memory import Memory
from agent.runtime import _CONNECTOR_PROMPT, _connector_note, _service_tools_for_turn
from agent.skills import default_skill_names, ensure_default_skills, load_skills
from agent.tools import Tool, ToolContext, default_tools
from channels.viewmodel import _rank
from harness.connectors import (
    mentioned_unconnected,
    named_catalog_types,
    relevant_connected,
)
from harness.paths import HarnessPaths
from providers.base import ToolSpec


def _rec(type_, name, *, oauth=False, secret=False, cid=None):
    return {
        "id": cid or type_,
        "type": type_,
        "name": name,
        "oauth_configured": oauth,
        "secret_configured": secret,
        "tools": [],
    }


def _tool(name, description="x"):
    return Tool(ToolSpec(name, description), lambda ctx, args: "ok")


def _paths(tmp_path) -> HarnessPaths:
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas"])
    return p


def _ctx(paths) -> ToolContext:
    return ToolContext(
        paths=paths, bot="atlas", memory=Memory(paths=paths, bot="atlas"), session_id="s1"
    )


# -- a "needs auth" plugin the user named holds its sign-in stub ---------------


def test_mentioned_unconnected_needs_a_tag_or_a_name_not_an_intent():
    notion = _rec("notion", "Notion")  # added, OAuth never finished
    assert mentioned_unconnected("@Notion list my pages", [notion]) == [notion]
    assert mentioned_unconnected("list my Notion pages", [notion]) == [notion]
    # A connected record is not this function's business.
    assert mentioned_unconnected("@Notion", [_rec("notion", "Notion", oauth=True)]) == []
    # A generic word never surfaces a plugin the user has not finished adding.
    assert mentioned_unconnected("any pages or docs for me?", [notion]) == []


def test_relevant_connected_offers_the_connect_stub_for_a_named_unsigned_plugin():
    notion = _rec("notion", "Notion")
    linear = _rec("linear", "Linear", oauth=True)
    got = relevant_connected("@Notion list my pages", [linear, notion])
    assert [r["type"] for r in got] == ["notion"]
    # Only the stub is bound before OAuth; that is what the turn should hold.
    tools_ = {
        "notion_connect": _tool("notion_connect"),
        "linear_get_issue": _tool("linear_get_issue"),
    }
    assert set(_service_tools_for_turn(tools_, "@Notion list my pages", [linear, notion])) == {
        "notion_connect"
    }


def test_connector_note_tells_the_bot_to_show_the_sign_in_card():
    notion = _rec("notion", "Notion")
    note = _connector_note({"notion_connect": _tool("notion_connect")}, "@Notion pages", [notion])
    assert "not signed in" in note
    assert "notion_connect" in note
    assert "do not create a bot" in note.lower()


# -- a catalog plugin nobody added: offer to add it, never a bot ---------------


def test_named_catalog_types_finds_a_plugin_nobody_added():
    hits = named_catalog_types("Using Notion, list my 3 most recent pages", [])
    assert [c["type"] for c in hits] == ["notion"]
    # Once any record of the type exists it is no longer catalog-only.
    assert named_catalog_types("Using Notion, list pages", [_rec("notion", "Notion")]) == []
    assert named_catalog_types("nothing to see here", []) == []
    # Word-bounded: "notional" is not Notion.
    assert named_catalog_types("a notional plan", []) == []


def test_connector_note_offers_add_connector_for_a_catalog_plugin():
    note = _connector_note({}, "Using Slack, post hi in #general", [])
    assert "add_connector" in note
    assert "type='slack'" in note
    assert "do not offer to create a bot" in note.lower()
    # Alongside a connected plugin the catalog offer is appended, not lost.
    linear = _rec("linear", "Linear", oauth=True)
    both = _connector_note(
        {"linear_get_issue": _tool("linear_get_issue")}, "@Linear and Slack", [linear]
    )
    assert "Linear" in both and "add_connector(type='slack')" in both


def test_connector_prompt_says_a_service_is_a_plugin_not_a_bot():
    assert "add_connector" in _CONNECTOR_PROMPT
    assert "never a bot to create" in _CONNECTOR_PROMPT


# -- the add_connector tool ----------------------------------------------------


def test_add_connector_is_a_governed_manage_tool():
    assert "add_connector" in default_tools()
    effect = EFFECTS["add_connector"]
    assert effect.ask is True
    assert effect.target({"type": "notion"}) == "notion"


def test_add_connector_posts_the_catalog_type_and_shows_the_sign_in_card(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    calls: list[tuple[str, str, dict]] = []
    cards: list[tuple[str, dict]] = []

    def fake_api(_paths, method, path, payload):
        calls.append((method, path, payload))
        return {"id": "ab12cd34", "type": payload["type"], "name": payload["name"], "auth": "oauth"}

    monkeypatch.setattr(tools, "_api_json", fake_api)
    monkeypatch.setattr(
        tools,
        "_emit_card",
        lambda ctx, card_type, payload, card_id=None: cards.append((card_type, payload)) or "c1",
    )
    out = tools._add_connector(_ctx(paths), {"type": "Notion"})
    assert out.startswith("ok:")
    assert calls == [("POST", "/api/connectors", {"type": "notion", "name": "Notion"})]
    assert cards and cards[0][0] == "connector"
    assert cards[0][1]["connector_id"] == "ab12cd34"
    assert cards[0][1]["type"] == "notion"
    assert "Authorize" in out and "Settings" in out


def test_add_connector_api_key_type_points_at_request_secret(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setattr(
        tools,
        "_api_json",
        lambda _p, m, path, payload: {
            "id": "k1",
            "type": payload["type"],
            "name": payload["name"],
            "auth": "api_key",
        },
    )
    cards: list = []
    monkeypatch.setattr(tools, "_emit_card", lambda *a, **k: cards.append(a) or "c")
    out = tools._add_connector(_ctx(paths), {"type": "stripe"})
    assert out.startswith("ok:")
    assert "request_secret" in out and "connector_k1" in out
    assert cards == []


def test_add_connector_refuses_an_unknown_type_without_a_network_call(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setattr(
        tools, "_api_json", lambda *a: (_ for _ in ()).throw(AssertionError("no call"))
    )
    assert tools._add_connector(_ctx(paths), {"type": "frobnicator"}).startswith("error:")
    assert tools._add_connector(_ctx(paths), {}).startswith("error:")


# -- the seeded add-connector skill --------------------------------------------


def test_add_connector_skill_is_seeded(tmp_path):
    assert "add-connector" in default_skill_names()
    paths = _paths(tmp_path)
    ensure_default_skills(paths)
    found = next(s for s in load_skills(paths, "atlas") if s.skill_id == "add-connector")
    assert "add_connector" in found.body
    assert "request_secret" in found.body
    assert "never a bot" in found.body.lower()


def test_add_connector_skill_loads_only_when_named():
    """Its description names every plugin and the word "chat"; a content-word
    match would inject it into ordinary turns (echo replies changed, the
    scheduler/ws tests timed out waiting for the plain echo)."""
    from types import SimpleNamespace

    from agent import toolselect as ts

    skill = SimpleNamespace(
        name="add-connector",
        skill_id="add-connector",
        description="Walk through connecting a plugin (Gmail, Linear, GitHub, Notion, Slack…) from chat",
        when_to_use="user wants to connect, authorize, or add a plugin or service, or /add-connector",
        body="",
        tools=(),
    )
    assert ts.matching_skills([skill], "let's chat about the linear plugin") == []
    assert ts.matching_skills([skill], "add a note to the service log") == []
    assert ts.matching_skills([skill], "/add-connector") == [skill]
    assert ts.matching_skills([skill], "run add-connector for Notion") == [skill]


# -- pickers match anywhere in the name ------------------------------------------


def test_rank_matches_inside_names_with_prefix_hits_first():
    names = ["soul", "cite-sources", "skills", "stop", "harness-tips"]
    assert _rank("s", names, key=lambda n: n) == [
        "soul",
        "skills",
        "stop",
        "cite-sources",
        "harness-tips",
    ]
    assert _rank("tip", names, key=lambda n: n) == ["harness-tips"]
    assert _rank("", names, key=lambda n: n) == names
    assert _rank("zzz", names, key=lambda n: n) == []


# -- a group mentioned from a 1:1: the bot posts into it (Grok "pinged the group") --


def _room(rid, title, members):
    from harness.rooms import Room

    return Room(id=rid, title=title, members=members)


def test_mentioned_rooms_matches_titles_with_spaces_and_commas():
    from harness.rooms import mentioned_rooms

    pair = _room("pair", "Atlas, Nova, and Kai", ["atlas", "nova", "kai"])
    duo = _room("duo", "Atlas, Nova", ["atlas", "nova"])
    rooms = [duo, pair]
    hits = mentioned_rooms("@Atlas, Nova, and Kai quick test, everyone say hi", rooms)
    assert [r.id for r in hits] == ["pair"]  # longest title wins, no double hit on the prefix
    assert [r.id for r in mentioned_rooms("@atlas, nova say hi", rooms)] == ["duo"]
    assert mentioned_rooms("post this to @pair please", rooms)[0].id == "pair"
    assert mentioned_rooms("@pairing is not a room", rooms) == []
    assert mentioned_rooms("no tag here: Atlas, Nova", rooms) == []
    # Narrowed to the groups the bot belongs to.
    assert mentioned_rooms("@Atlas, Nova hi", rooms, member="kai") == []
    assert [r.id for r in mentioned_rooms("@Atlas, Nova hi", rooms, member="nova")] == ["duo"]


def test_room_mention_note_names_the_tool_and_the_group(tmp_path, monkeypatch):
    from agent.runtime import _room_mention_note
    from harness.rooms import create_room

    paths = _paths(tmp_path)
    paths.ensure_layout(["atlas", "nova"])
    room = create_room(paths, "Atlas and Nova", ["atlas", "nova"])
    note = _room_mention_note(paths, "atlas", "@Atlas and Nova everyone say hi")
    assert "message_room" in note and room.id in note
    assert "not a bot" in note and "nova" in note
    assert _room_mention_note(paths, "atlas", "plain line") == ""
    # Not a member: no note (the bot cannot post there).
    assert _room_mention_note(paths, "kai", "@Atlas and Nova hi") == ""


def test_message_room_posts_through_the_api_as_the_bot(tmp_path, monkeypatch):
    from harness.rooms import create_room

    paths = _paths(tmp_path)
    paths.ensure_layout(["atlas", "nova"])
    room = create_room(paths, "Atlas and Nova", ["atlas", "nova"])
    calls = []
    monkeypatch.setattr(
        tools,
        "_api_json",
        lambda _p, m, path, payload: (
            calls.append((m, path, payload))
            or {"room": room.id, "turns": [{"bot": "nova", "request_id": "r1"}]}
        ),
    )
    out = tools._message_room(_ctx(paths), {"room": "atlas and nova", "text": "Everyone say hi"})
    assert out.startswith("ok:")
    assert calls == [
        ("POST", f"/api/rooms/{room.id}/messages", {"text": "Everyone say hi", "from": "atlas"})
    ]
    assert "nova" in out and "pinged the group" in out
    # By id works too; a prefix of the title is enough when it is unique.
    assert tools._message_room(_ctx(paths), {"room": room.id, "text": "x"}).startswith("ok:")
    assert tools._message_room(_ctx(paths), {"room": "atlas and", "text": "x"}).startswith("ok:")


def test_message_room_refuses_outside_its_groups_and_inside_a_group_turn(tmp_path, monkeypatch):
    from harness.rooms import create_room

    paths = _paths(tmp_path)
    paths.ensure_layout(["atlas", "nova", "kai"])
    create_room(paths, "Nova and Kai", ["nova", "kai"])
    monkeypatch.setattr(
        tools, "_api_json", lambda *a: (_ for _ in ()).throw(AssertionError("no call"))
    )
    out = tools._message_room(_ctx(paths), {"room": "Nova and Kai", "text": "hi"})
    assert out.startswith("error:") and "no group you belong to" in out
    assert tools._message_room(_ctx(paths), {"text": "hi"}).startswith("error:")
    assert tools._message_room(_ctx(paths), {"room": "x"}).startswith("error:")
    ctx = _ctx(paths)
    ctx.room = "somewhere"
    assert "@mention" in tools._message_room(ctx, {"room": "x", "text": "hi"})
    assert "message_room" in default_tools()
    assert EFFECTS["message_room"].target({"room": "pair"}) == "pair"


_ROSTER = """
[[bots]]
name = "atlas"
role = "a"
provider = "echo"

[[bots]]
name = "nova"
role = "b"
provider = "echo"
"""


def test_a_bot_never_answers_its_own_group_post(tmp_path):
    from harness.orchestrator import Orchestrator

    rp = tmp_path / "roster.toml"
    rp.write_text(_ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    room = orch.create_room("Pair", ["atlas", "nova"], owner="atlas")
    # Un-addressed: every other member answers, never the sender.
    turns = orch.dispatch_chat("Everyone say hi", room_id=room.id, frm="atlas")
    assert [name for name, _rid, _r in turns] == ["nova"]
    # @everyone from a bot: everyone but the bot.
    turns = orch.dispatch_chat("@everyone say hi", room_id=room.id, frm="atlas")
    assert [name for name, _rid, _r in turns] == ["nova"]
    # A user line is untouched.
    turns = orch.dispatch_chat("say hi", room_id=room.id, frm="user")
    assert [name for name, _rid, _r in turns] == ["atlas", "nova"]
    from harness.rooms import recent_messages

    assert [r["frm"] for r in recent_messages(orch.paths, room.id)] == ["atlas", "atlas", "user"]


def test_post_room_message_route_fans_out_as_the_bot(tmp_path):
    import time

    from harness.orchestrator import Orchestrator
    from harness.rooms import recent_messages
    from harness.server import make_server
    from tests.test_ws import _http

    rp = tmp_path / "roster.toml"
    rp.write_text(_ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    httpd = make_server(orch, "127.0.0.1", 0)
    host, port = httpd.server_address[:2]
    import threading

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        room = _http(
            host, port, "/api/rooms", "POST", {"title": "Pair", "members": ["atlas", "nova"]}
        )
        out = _http(
            host,
            port,
            f"/api/rooms/{room['id']}/messages",
            "POST",
            {"text": "Everyone say hi", "from": "atlas"},
        )
        assert out["from"] == "atlas"
        assert [t["bot"] for t in out["turns"]] == ["nova"]
        rows = recent_messages(orch.paths, room["id"])
        assert rows[-1]["frm"] == "atlas" and rows[-1]["text"] == "Everyone say hi"
        # Bad sender / missing room are refused before anything is written.
        import urllib.error

        for path, body in (
            (f"/api/rooms/{room['id']}/messages", {"text": "x", "from": "ghost"}),
            ("/api/rooms/nope/messages", {"text": "x"}),
        ):
            try:
                _http(host, port, path, "POST", body)
            except urllib.error.HTTPError as exc:
                assert exc.code in (400, 404)
            else:
                raise AssertionError("expected a refusal")
        time.sleep(0.1)
    finally:
        httpd.shutdown()
