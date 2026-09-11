import json
import threading
import time

from agent import messaging
from agent.memory import Memory
from agent.tools import ToolContext, default_tools
from harness.paths import HarnessPaths


def _paths(tmp_path):
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas", "nova"])
    return p


def test_send_and_read_inbox(tmp_path):
    paths = _paths(tmp_path)
    msg = messaging.Msg(to="atlas", frm="user", text="hi")
    messaging.send(paths, msg)
    items = messaging.read_inbox(paths, "atlas")
    assert len(items) == 1
    _path, got = items[0]
    assert got.text == "hi"
    assert got.frm == "user"


def test_mark_processed_moves_file(tmp_path):
    paths = _paths(tmp_path)
    messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="hi"))
    path, _msg = messaging.read_inbox(paths, "atlas")[0]
    messaging.mark_processed(paths, "atlas", path)
    assert messaging.read_inbox(paths, "atlas") == []
    assert list(paths.processed("atlas").glob("*.json"))


def test_wait_for_reply_matches_correlation(tmp_path):
    paths = _paths(tmp_path)
    original = messaging.Msg(to="atlas", frm="user", text="hi")
    reply = messaging.Msg(to="user", frm="atlas", text="hello", reply_to=original.id)
    messaging.send(paths, reply)
    got = messaging.wait_for_reply(paths, "user", original.id, timeout=2.0)
    assert got is not None
    assert got.text == "hello"
    assert got.frm == "atlas"


def test_pending_skips_replies_and_drop_clears_newest(tmp_path):
    paths = _paths(tmp_path)
    messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="first"))
    messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="second"))
    messaging.send(paths, messaging.Msg(to="user", frm="atlas", text="ack", reply_to="x"))
    waiting = messaging.pending(paths, "atlas")
    assert [m.text for m in waiting] == ["first", "second"]
    n = messaging.drop_pending(paths, "atlas", count=1)
    assert n == 1
    assert [m.text for m in messaging.pending(paths, "atlas")] == ["first"]
    state = messaging.queue_state(paths, "atlas", busy=True)
    assert state["busy"] is True
    assert state["queued"] == 1


def test_queue_state_excludes_the_in_flight_message(tmp_path):
    paths = _paths(tmp_path)
    messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="current"))
    messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="follow-up"))
    current = messaging.pending(paths, "atlas")[0]
    state = messaging.queue_state(paths, "atlas", busy=True, current_id=current.id)
    assert state["queued"] == 1
    assert state["items"][0]["text"] == "follow-up"


def test_take_steer_pops_plain_follow_ups(tmp_path):
    paths = _paths(tmp_path)
    current = messaging.Msg(to="atlas", frm="user", text="first")
    follow = messaging.Msg(to="atlas", frm="user", text="also this")
    messaging.send(paths, current)
    messaging.send(paths, follow)
    got = messaging.take_steer(paths, "atlas", current.id)
    assert [m.id for m in got] == [follow.id]
    assert [m.text for m in messaging.pending(paths, "atlas")] == ["first"]


def test_take_steer_leaves_backlog_from_before_the_turn(tmp_path):
    paths = _paths(tmp_path)
    current = messaging.Msg(to="atlas", frm="user", text="first")
    waiting = messaging.Msg(to="atlas", frm="user", text="already queued")
    messaging.send(paths, current)
    messaging.send(paths, waiting)
    started = time.time() + 1
    assert messaging.take_steer(paths, "atlas", current.id, after_ts=started) == []
    assert [m.text for m in messaging.pending(paths, "atlas")] == ["first", "already queued"]


def test_take_steer_leaves_send_now_and_attachments(tmp_path):
    paths = _paths(tmp_path)
    current = messaging.Msg(to="atlas", frm="user", text="first")
    messaging.send(paths, current)
    messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="now", now=True))
    messaging.send(
        paths,
        messaging.Msg(
            to="atlas",
            frm="user",
            text="pic",
            attachments=[{"name": "x.png", "path": "x.png"}],
        ),
    )
    assert messaging.take_steer(paths, "atlas", current.id) == []
    pending = [m.text for m in messaging.pending(paths, "atlas")]
    assert pending == ["first", "now", "pic"]


def test_mark_now_promotes_a_queued_message(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    queued = messaging.Msg(to="atlas", frm="user", text="waiting")
    messaging.send(paths, queued)
    assert messaging.newer_user(paths, "atlas", None) == []
    assert messaging.mark_now(paths, "atlas", queued.id) is True
    promoted = messaging.newer_user(paths, "atlas", None)
    assert [m.id for m in promoted] == [queued.id]
    assert promoted[0].now is True


def test_newer_user_ignores_background_lane(tmp_path):
    """Routine/dream ticks send as frm="user" but must never preempt.

    A background tick counted here sweeps a parked choice box as skipped and
    the follow-up turn re-asks it — the doubled choice card, with nothing
    visible in the chat to explain it.
    """
    paths = _paths(tmp_path)
    started = time.time() - 1
    for origin in (messaging.ORIGIN_ROUTINE, messaging.ORIGIN_DREAM, messaging.ORIGIN_IDLE):
        messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="tick", origin=origin))
    assert messaging.newer_user(paths, "atlas", None, now_only=False, after_ts=started) == []
    chat = messaging.Msg(to="atlas", frm="user", text="a real chat")
    messaging.send(paths, chat)
    got = messaging.newer_user(paths, "atlas", None, now_only=False, after_ts=started)
    assert [m.id for m in got] == [chat.id]


def test_choice_wait_survives_a_routine_tick(tmp_path):
    """A parked human-input wait is not preempted by scheduled background work."""
    from agent.tools import _turn_preempted

    paths = _paths(tmp_path)
    current = messaging.Msg(to="atlas", frm="user", text="pick something")
    messaging.send(paths, current)
    ctx = ToolContext(
        paths=paths,
        bot="atlas",
        memory=Memory(paths=paths, bot="atlas"),
        turn_id=current.id,
        turn_started=time.time() - 1,
    )
    messaging.send(
        paths,
        messaging.Msg(to="atlas", frm="user", text="[Routine: nightly]", origin="routine"),
    )
    assert _turn_preempted(ctx) is False
    messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="actually, stop"))
    assert _turn_preempted(ctx) is True


def test_mark_now_unknown_message_is_false(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    assert messaging.mark_now(paths, "atlas", "nope") is False


def test_wait_for_reply_times_out(tmp_path):
    paths = _paths(tmp_path)
    got = messaging.wait_for_reply(paths, "user", "missing", timeout=0.3)
    assert got is None


def _roster(paths, bots):
    (paths.home / "roster.json").write_text(
        json.dumps({"bots": bots}, ensure_ascii=False), encoding="utf-8"
    )


def _ctx(paths, bot="atlas", **kwargs):
    return ToolContext(
        paths=paths,
        bot=bot,
        memory=Memory(paths=paths, bot=bot),
        reply_timeout=2.0,
        **kwargs,
    )


def test_message_agent_resolves_display_name_and_wraps_as_handoff(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas", "cloud-engineer"])
    _roster(
        paths,
        [
            {"name": "atlas", "provider": "echo"},
            {
                "name": "cloud-engineer",
                "title": "Cloud Engineer",
                "role": "owns AWS",
                "provider": "echo",
            },
        ],
    )

    def responder():
        deadline = time.time() + 2
        while time.time() < deadline:
            items = messaging.read_inbox(paths, "cloud-engineer")
            if items:
                path, msg = items[0]
                assert "Handoff from atlas" in msg.text
                assert "what env vars do you need?" in msg.text
                assert msg.frm == "atlas"
                messaging.mark_processed(paths, "cloud-engineer", path)
                messaging.send(
                    paths,
                    messaging.Msg(
                        to="atlas",
                        frm="cloud-engineer",
                        text="I need AWS_ACCESS_KEY_ID",
                        reply_to=msg.id,
                    ),
                )
                return
            time.sleep(0.05)

    t = threading.Thread(target=responder)
    t.start()
    out = default_tools()["message_agent"].handler(
        _ctx(paths), {"to": "Cloud Engineer", "text": "what env vars do you need?", "wait": True}
    )
    t.join()
    assert "I need AWS_ACCESS_KEY_ID" in out
    # the reply still goes to the asker, not the user inbox
    assert messaging.read_inbox(paths, "user") == []


def test_message_agent_in_a_room_stays_in_the_room(tmp_path):
    from harness.rooms import create_room

    paths = _paths(tmp_path)
    _roster(paths, [{"name": "atlas", "provider": "echo"}, {"name": "nova", "provider": "echo"}])
    room = create_room(paths, "Pair", ["atlas", "nova"])
    out = default_tools()["message_agent"].handler(
        _ctx(paths, room=room.id), {"to": "nova", "text": "say hi in here"}
    )
    assert "asked nova in this group" in out
    items = messaging.read_inbox(paths, "nova")
    assert len(items) == 1
    _path, msg = items[0]
    assert msg.room == room.id
    assert msg.origin == "room_handoff"
    assert "Handoff from" not in msg.text
    assert messaging.read_inbox(paths, "user") == []


def test_message_agent_in_a_room_refuses_a_non_member(tmp_path):
    from harness.rooms import create_room

    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas", "nova", "guide"])
    _roster(
        paths,
        [
            {"name": "atlas", "provider": "echo"},
            {"name": "nova", "provider": "echo"},
            {"name": "guide", "provider": "echo"},
        ],
    )
    room = create_room(paths, "Pair", ["atlas", "nova"])
    out = default_tools()["message_agent"].handler(
        _ctx(paths, room=room.id), {"to": "guide", "text": "hi"}
    )
    assert out.startswith("error:")
    assert "not in this group" in out or "no member matching" in out
    assert messaging.read_inbox(paths, "guide") == []


def test_message_agent_unknown_bot_asks_to_clarify(tmp_path):
    paths = _paths(tmp_path)
    _roster(paths, [{"name": "atlas", "provider": "echo"}, {"name": "nova", "provider": "echo"}])
    out = default_tools()["message_agent"].handler(_ctx(paths), {"to": "payroll", "text": "hi"})
    assert out.startswith("error:")
    assert "ask_user_choice" in out


def test_message_agent_refuses_a_connected_plugin(tmp_path):
    from harness import mcp_oauth
    from harness.connectors import Connectors

    paths = _paths(tmp_path)
    _roster(paths, [{"name": "atlas", "provider": "echo"}])
    rec = Connectors(paths).add("paypal", "PayPal")
    mcp_oauth.save_tokens(
        paths,
        rec["id"],
        {
            "access_token": "at",
            "refresh_token": "",
            "expires_at": 0,
            "token_endpoint": "https://auth.example.com/token",
            "client_id": "c",
            "resource": "https://mcp.paypal.com/mcp",
        },
    )
    out = default_tools()["message_agent"].handler(
        _ctx(paths), {"to": "PayPal", "text": "check the balance"}
    )
    assert out.startswith("error:")
    assert "plugin" in out.lower()
    assert "message_agent" in out
    assert "ask_user_choice" not in out


def test_consult_refuses_secret_and_choice_boxes(tmp_path):
    paths = _paths(tmp_path)
    ctx = _ctx(paths, bot="cloud-engineer", sender="chief-of-staff")
    tools = default_tools()
    secret = tools["request_secret"].handler(ctx, {"name": "AWS_ACCESS_KEY_ID"})
    assert secret.startswith("error:")
    assert "colleague" in secret
    choice = tools["ask_user_choice"].handler(ctx, {"question": "which?", "options": ["a", "b"]})
    assert choice.startswith("error:")
    assert "colleague" in choice
