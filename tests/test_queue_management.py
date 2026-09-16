import json
import threading
import urllib.error
import urllib.request

import pytest

from agent import messaging, obligations
from agent.runtime import build_agent
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.roster import Bot
from harness.server import make_server


def setup_queue(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas", "scout"])
    return paths


def test_full_queue_and_order_survive_reload(tmp_path):
    paths = setup_queue(tmp_path)
    items = [messaging.Msg(to="atlas", frm="user", text=str(i) * 180) for i in range(12)]
    for m in items:
        messaging.send(paths, m)
    state = messaging.queue_state(paths, "atlas")
    assert state["queued"] == len(state["items"]) == 12
    assert state["items"][0]["text"] == items[0].text
    with messaging.queue_lock(paths, "atlas"):
        messaging.reorder_queue(paths, "atlas", [m.id for m in reversed(items)])
    messaging._INBOX_CACHE.clear()
    assert [m["id"] for m in messaging.queue_state(paths, "atlas")["items"]] == [
        m.id for m in reversed(items)
    ]


def test_explicit_order_is_used_by_runtime_across_lanes(tmp_path):
    paths = setup_queue(tmp_path)
    agent = build_agent(paths, Bot(name="atlas", role="terse", provider="echo"), stream_delay=0)
    user = messaging.Msg(to="atlas", frm="user", text="user")
    background = messaging.Msg(to="atlas", frm="user", text="routine", origin="routine")
    for msg in [user, background]:
        messaging.send(paths, msg)
    with messaging.queue_lock(paths, "atlas"):
        messaging.reorder_queue(paths, "atlas", [background.id, user.id])
    agent.process_inbox_once()
    assert [m.id for m in messaging.pending(paths, "atlas")] == [user.id]
    assert background.id not in messaging.queue_order(paths, "atlas")


def test_stale_or_invalid_reorder_preserves_every_item(tmp_path):
    paths = setup_queue(tmp_path)
    items = [messaging.Msg(to="atlas", frm="user", text=str(i)) for i in range(3)]
    for msg in items:
        messaging.send(paths, msg)
    for ids in [[m.id for m in items[:2]], [items[0].id] * 3, ["unknown"]]:
        with messaging.queue_lock(paths, "atlas"), pytest.raises(ValueError):
            messaging.reorder_queue(paths, "atlas", ids)
    assert [m.id for m in messaging.pending(paths, "atlas")] == [m.id for m in items]


def test_remove_only_waiting_and_prevent_recovery_redrive(tmp_path):
    paths = setup_queue(tmp_path)
    current, waiting = [
        messaging.Msg(to="atlas", frm="user", text=t) for t in ["current", "waiting"]
    ]
    for msg in [current, waiting]:
        messaging.send(paths, msg)
        obligations.record_send(paths, "atlas", msg.id)
    with messaging.queue_lock(paths, "atlas"):
        assert not messaging.remove_queued(paths, "atlas", current.id, current_id=current.id)
        assert messaging.remove_queued(paths, "atlas", waiting.id, current_id=current.id)
        assert not messaging.remove_queued(paths, "atlas", waiting.id)
    assert [m.id for m in messaging.pending(paths, "atlas")] == [current.id]
    assert obligations.get(paths, "atlas") is not None
    with messaging.queue_lock(paths, "atlas"):
        assert messaging.remove_queued(paths, "atlas", current.id)
    assert obligations.get(paths, "atlas") is None
    assert obligations.maybe_redrive(paths, "atlas", now=10**12, idle=0) is None


def test_manually_ordered_followup_is_not_steered_into_running_turn(tmp_path):
    paths = setup_queue(tmp_path)
    msg = messaging.Msg(to="atlas", frm="user", text="wait for this")
    messaging.send(paths, msg)
    with messaging.queue_lock(paths, "atlas"):
        messaging.reorder_queue(paths, "atlas", [msg.id])
    assert messaging.take_steer(paths, "atlas", "running", after_ts=0) == []


def test_archive_error_does_not_claim_removal(tmp_path, monkeypatch):
    paths = setup_queue(tmp_path)
    msg = messaging.Msg(to="atlas", frm="user", text="keep me")
    messaging.send(paths, msg)

    def fail(*args):
        raise PermissionError("read-only archive")

    monkeypatch.setattr(messaging.os, "replace", fail)
    with messaging.queue_lock(paths, "atlas"), pytest.raises(PermissionError):
        messaging.remove_queued(paths, "atlas", msg.id)
    assert messaging.pending(paths, "atlas")[0].id == msg.id


def test_queue_api_auth_edits_and_active_conflict(tmp_path):
    roster = tmp_path / "roster.toml"
    roster.write_text('[[bots]]\nname="atlas"\nrole="terse"\nprovider="echo"\n')
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=roster, backend="process")
    orch.init()
    httpd = make_server(orch, "127.0.0.1", 0, token="test-only-token")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}/api/bots/atlas/queue"

    def request(method="GET", suffix="", body=None, auth=True):
        headers = {"Authorization": "Bearer test-only-token"} if auth else {}
        data = json.dumps(body).encode() if body is not None else None
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(base + suffix, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=3) as response:
            return json.load(response)

    try:
        one, two = [messaging.Msg(to="atlas", frm="user", text=t) for t in ["one", "two"]]
        for msg in [one, two]:
            messaging.send(orch.paths, msg)
        for method, suffix, body in [
            ("GET", "", None),
            ("PATCH", "", {"ids": [two.id, one.id]}),
            ("DELETE", "/" + one.id, None),
        ]:
            with pytest.raises(urllib.error.HTTPError) as exc:
                request(method, suffix, body, auth=False)
            assert exc.value.code == 401
        assert request()["queued"] == 2
        assert request("PATCH", body={"ids": [two.id, one.id]})["items"][0]["id"] == two.id
        orch.control.set_busy("atlas", two.id)
        with pytest.raises(urllib.error.HTTPError) as exc:
            request("DELETE", "/" + two.id)
        assert exc.value.code == 409
        assert request("DELETE", "/" + one.id)["queued"] == 0
        assert messaging.pending(orch.paths, "atlas")[0].id == two.id
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def test_remove_rejects_an_item_joined_to_the_running_turn(tmp_path):
    from agent.streaming import StreamWriter

    paths = setup_queue(tmp_path)
    msg = messaging.Msg(to="atlas", frm="scout", text="joined task", room="room")
    messaging.send(paths, msg)
    StreamWriter(paths, msg.id, room=msg.room).steered("atlas", "running")
    with messaging.queue_lock(paths, "atlas"):
        assert messaging.queue_state(paths, "atlas", current_id="running")["queued"] == 0
        assert not messaging.remove_queued(paths, "atlas", msg.id, current_id="running")
    assert messaging.pending(paths, "atlas")[0].id == msg.id


def test_manual_order_waits_without_preempting_and_send_now_can_override(tmp_path):
    paths = setup_queue(tmp_path)
    first, urgent = [messaging.Msg(to="atlas", frm="user", text=t) for t in ["first", "urgent"]]
    urgent.now = True
    for msg in [first, urgent]:
        messaging.send(paths, msg)
    with messaging.queue_lock(paths, "atlas"):
        messaging.reorder_queue(paths, "atlas", [first.id, urgent.id])
    assert messaging.newer_user(paths, "atlas", "running") == []
    assert [m.id for m in messaging.ordered_pending(paths, "atlas")] == [first.id, urgent.id]
    messaging.mark_now(paths, "atlas", urgent.id)
    assert [m.id for m in messaging.newer_user(paths, "atlas", "running")] == [urgent.id]
    assert messaging.ordered_pending(paths, "atlas")[0].id == urgent.id


def test_removal_serializes_with_admission(tmp_path, monkeypatch):
    paths = setup_queue(tmp_path)
    agent = build_agent(paths, Bot(name="atlas", role="terse", provider="echo"), stream_delay=0)
    msg = messaging.Msg(to="atlas", frm="user", text="work")
    messaging.send(paths, msg)
    admission = threading.Event()
    allow_admission = threading.Event()
    running = threading.Event()
    finish = threading.Event()
    original = agent.control.set_busy

    def set_busy(*args, **kwargs):
        admission.set()
        assert allow_admission.wait(3)
        return original(*args, **kwargs)

    def produce(*args, **kwargs):
        running.set()
        assert finish.wait(3)
        return "done"

    monkeypatch.setattr(agent.control, "set_busy", set_busy)
    monkeypatch.setattr(agent, "_produce", produce)
    worker = threading.Thread(target=agent.process_inbox_once)
    worker.start()
    assert admission.wait(3)
    result = []

    def remove():
        with messaging.queue_lock(paths, "atlas"):
            result.append(
                messaging.remove_queued(
                    paths, "atlas", msg.id, current_id=agent.control.busy_request("atlas")
                )
            )

    remover = threading.Thread(target=remove)
    remover.start()
    allow_admission.set()
    assert running.wait(3)
    remover.join(timeout=3)
    try:
        assert result == [False]
    finally:
        finish.set()
        worker.join(timeout=3)


def test_manual_order_does_not_trigger_send_now_watchdog(tmp_path):
    from types import SimpleNamespace

    paths = setup_queue(tmp_path)
    agent = build_agent(paths, Bot(name="atlas", role="terse", provider="echo"), stream_delay=0)
    msg = messaging.Msg(to="atlas", frm="user", text="wait", now=True, ts=1)
    messaging.send(paths, msg)
    run = SimpleNamespace(msg_id="running", started=1, last_output=10000)
    assert agent._trip_reason(run, 10000)[0] == "watchdog"
    with messaging.queue_lock(paths, "atlas"):
        messaging.reorder_queue(paths, "atlas", [msg.id])
    assert agent._trip_reason(run, 10000) == (None, None)
