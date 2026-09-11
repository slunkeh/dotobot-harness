"""HTTP+SSE API contract tests.

These exercise the exact endpoints the macOS app consumes, against a live server
with real bot processes (echo provider).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from harness.control import Control
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.server import make_server

ROSTER = """
[[bots]]
name = "atlas"
role = "a terse research assistant"
provider = "echo"

[[bots]]
name = "nova"
role = "a friendly writing partner"
provider = "echo"
"""


@pytest.fixture
def server(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    orch.up()
    # wait for bots
    deadline = time.time() + 10
    while time.time() < deadline and not all(h.status.value == "running" for h in orch.status()):
        time.sleep(0.2)
    httpd = make_server(orch, "127.0.0.1", 0)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        orch.down()


def _control(tmp_path) -> Control:
    """The server fixture's Control, for the bot-side half of a card flow."""
    return Control(HarnessPaths.resolve(tmp_path / "home"))


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())


def _post(url, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _sse(url, payload, timeout=30):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    frames = []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8").strip()
            if line.startswith("data:"):
                frames.append(json.loads(line[len("data:") :].strip()))
                if frames[-1].get("type") == "final":
                    break
    return frames


def test_health_and_bots(server):
    health = _get(f"{server}/api/health")
    assert health["ok"] is True
    assert set(health["bots"]) == {"atlas", "nova"}

    bots = _get(f"{server}/api/bots")
    names = {b["name"]: b for b in bots}
    assert names["atlas"]["status"] == "running"
    assert names["atlas"]["provider"] == "echo"
    assert names["atlas"]["private_browser"] is False
    assert names["nova"]["private_browser"] is False


def test_chat_streams_over_sse(server):
    frames = _sse(f"{server}/api/chat", {"bot": "atlas", "text": "hello there"})
    types = [f["type"] for f in frames]
    assert "status" in types
    assert "delta" in types
    final = [f for f in frames if f["type"] == "final"]
    assert final and "hello there" in final[-1]["text"]
    streamed = "".join(f["text"] for f in frames if f["type"] == "delta")
    assert streamed == final[-1]["text"]


def test_sse_frames_carry_epoch_seq_and_upserts(server):
    """The SSE relay is fenced and self-healing too: every frame is
    stamped {epoch, seq} and message upserts carry the accumulated text."""
    frames = _sse(f"{server}/api/chat", {"bot": "atlas", "text": "fence sse"})
    assert all("epoch" in f and "seq" in f for f in frames)
    assert len({f["epoch"] for f in frames}) == 1
    seqs = [f["seq"] for f in frames if f.get("bot") == "atlas" and not f.get("room")]
    assert seqs == sorted(seqs)
    msgs = [f for f in frames if f["type"] == "message"]
    assert msgs and msgs[0]["mutation"] == "appended"
    final = [f for f in frames if f["type"] == "final"][-1]
    assert msgs[-1]["text"] == final["text"]
    assert msgs[-1]["streaming"] is False


def test_chat_stuck_emits_takeover_frame(server):
    frames = _sse(f"{server}/api/chat", {"bot": "atlas", "text": "I'm stuck, take over"})
    assert any(f["type"] == "takeover" for f in frames)
    state = _get(f"{server}/api/control/atlas")
    assert state["takeover_requested"] is True


def test_control_takeover_and_return(server):
    st = _post(f"{server}/api/control/atlas/takeover", {})
    assert st["mode"] == "takeover"
    st = _get(f"{server}/api/control/atlas")
    assert st["mode"] == "takeover"
    st = _post(f"{server}/api/control/atlas/return", {})
    assert st["mode"] == "bot"


def test_control_return_is_refused_for_a_different_holder(server):
    st = _post(f"{server}/api/control/atlas/takeover", {"user": "alex"})
    assert st["holder"] == "alex"
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(f"{server}/api/control/atlas/return", {"user": "someone-else"})
    assert err.value.code == 403
    assert _get(f"{server}/api/control/atlas")["mode"] == "takeover"
    st = _post(f"{server}/api/control/atlas/return", {"user": "alex"})
    assert st["mode"] == "bot"
    assert st["holder"] is None


def test_control_return_card_accept_hands_control_back(server, tmp_path):
    """A `control_return` accept flips control even with no tool waiting."""
    _post(f"{server}/api/control/atlas/takeover", {"user": "alex"})
    _control(tmp_path).request_return("atlas", "one form left", "card-x")

    out = _post(f"{server}/api/answers", {"id": "card-x", "value": "accept", "bot": "atlas"})
    assert out["ok"] is True
    assert out["mode"] == "bot"
    state = _get(f"{server}/api/control/atlas")
    assert state["mode"] == "bot"
    assert state["return_requested"] is False


def test_control_return_card_accept_is_refused_for_a_stranger(server, tmp_path):
    _post(f"{server}/api/control/atlas/takeover", {"user": "alex"})
    _control(tmp_path).request_return("atlas", "one form left", "card-y")
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(
            f"{server}/api/answers",
            {"id": "card-y", "value": "accept", "bot": "atlas", "user": "stranger"},
        )
    assert err.value.code == 403
    assert _get(f"{server}/api/control/atlas")["mode"] == "takeover"


def test_control_return_card_dismiss_keeps_control(server, tmp_path):
    _post(f"{server}/api/control/atlas/takeover", {"user": "alex"})
    _control(tmp_path).request_return("atlas", "one form left", "card-z")
    out = _post(f"{server}/api/answers", {"id": "card-z", "value": "dismiss", "bot": "atlas"})
    assert out["mode"] == "takeover"
    state = _get(f"{server}/api/control/atlas")
    assert state["mode"] == "takeover"
    assert state["return_requested"] is False


def test_teach_record_start_stop_learns(server, tmp_path, monkeypatch):
    png = tmp_path / "dot.png"
    png.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf"
        b"\xc0\x00\x00\x00\x03\x00\x01\xc4\xae\x08\xa5\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    monkeypatch.setenv("HARNESS_SCREENSHOT_CMD", f"cat {png}")
    monkeypatch.setenv("HARNESS_TEACH_SAMPLE_SECS", "0.05")
    monkeypatch.setattr("harness.teachrec._start_ffmpeg", lambda live, paths: None)
    started = _post(f"{server}/api/control/atlas/teach/record/start", {})
    assert started["mode"] == "teach"
    assert started["teach_recording"] is True
    live = _get(f"{server}/api/control/atlas/teach/record")
    assert live["recording"] is True
    out = _post(f"{server}/api/control/atlas/teach/record/stop", {})
    assert out["mode"] == "bot"
    assert out["teach_recording"] is False
    assert out["attachments"]
    assert any(str(a.get("name", "")).endswith(".png") for a in out["attachments"])


def test_teach_record_double_start_conflicts(server, tmp_path, monkeypatch):
    png = tmp_path / "dot.png"
    png.write_bytes(b"png")
    monkeypatch.setenv("HARNESS_SCREENSHOT_CMD", f"cat {png}")
    monkeypatch.setattr("harness.teachrec._start_ffmpeg", lambda live, paths: None)
    _post(f"{server}/api/control/atlas/teach/record/start", {})
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            _post(f"{server}/api/control/atlas/teach/record/start", {})
        assert err.value.code == 409
    finally:
        _post(f"{server}/api/control/atlas/teach/record/stop", {})


def test_control_teach_saves_skill(server):
    _post(f"{server}/api/control/atlas/teach/start", {})
    _post(f"{server}/api/control/atlas/teach/step", {"step": "open the report"})
    _post(f"{server}/api/control/atlas/teach/step", {"step": "export as CSV"})
    out = _post(
        f"{server}/api/control/atlas/teach/save",
        {"name": "export-report", "description": "monthly export"},
    )
    assert out["skill_path"].endswith("SKILL.md")
    assert out["name"] == "export-report"
    assert out["type"] == "skill_saved"
    assert any(s.get("name") == "export-report" for s in out["skills"])
    body = open(out["skill_path"], encoding="utf-8").read()
    assert "open the report" in body


def test_bots_include_stable_color(server):
    bots = {b["name"]: b for b in _get(f"{server}/api/bots")}
    assert bots["atlas"]["color"].startswith("#")
    assert bots["nova"]["color"].startswith("#")


def test_mention_fans_out_to_named_bot(server):
    frames = _sse(f"{server}/api/chat", {"bot": "atlas", "text": "@nova draft an intro"})
    bots = {f.get("bot") for f in frames if f.get("bot")}
    assert "nova" in bots
    finals = [f for f in frames if f["type"] == "final"]
    assert finals
    assert any("intro" in (f.get("text") or "") for f in finals)


def test_group_chat_mentioned_bot_replies(server):
    room = _post(f"{server}/api/rooms", {"title": "Pair", "members": ["atlas", "nova"]})
    frames = _sse(
        f"{server}/api/chat",
        {"room": room["id"], "text": "@atlas what is your name"},
    )
    finals = [f for f in frames if f["type"] == "final"]
    assert finals
    assert all(f.get("bot") == "atlas" or f.get("frm") == "atlas" for f in finals)
    assert all(f.get("room") == room["id"] for f in frames if f.get("type") == "final")


def test_slash_memory_uses_that_bot(server):
    _sse(f"{server}/api/chat", {"bot": "atlas", "text": "/remember the launch is Tuesday"})
    frames = _sse(f"{server}/api/chat", {"bot": "atlas", "text": "/memory launch"})
    final = [f for f in frames if f["type"] == "final"][-1]
    assert "Tuesday" in final["text"]
    nova = _sse(f"{server}/api/chat", {"bot": "nova", "text": "/memory launch"})
    nova_final = [f for f in nova if f["type"] == "final"][-1]
    assert "Tuesday" not in nova_final["text"]


def test_upload_and_attachment_in_reply(server):
    # upload a text file, then reference it in a chat and see it reflected
    data = b"the launch date is March 3rd"
    req = urllib.request.Request(
        f"{server}/api/upload",
        data=data,
        headers={"X-Filename": "brief.txt", "Content-Type": "application/octet-stream"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        up = json.loads(r.read().decode())
    assert up["name"] == "brief.txt"
    assert up["size"] == len(data)

    frames = _sse(
        f"{server}/api/chat",
        {"bot": "atlas", "text": "what is here", "attachments": [up]},
    )
    final = [f for f in frames if f["type"] == "final"][-1]
    assert "brief.txt" in final["text"]
    assert "launch date is March 3rd" in final["text"]

    got = urllib.request.urlopen(f"{server}/api/uploads/{Path(up['path']).name}", timeout=10)
    assert got.status == 200
    assert got.read() == data


def test_upload_image_is_served_and_image_only_chat_works(server):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
    req = urllib.request.Request(
        f"{server}/api/upload",
        data=png,
        headers={"X-Filename": "tile.png", "Content-Type": "image/png"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        up = json.loads(r.read().decode())
    assert up["mime"] == "image/png"
    name = Path(up["path"]).name
    with urllib.request.urlopen(f"{server}/api/uploads/{name}", timeout=10) as r:
        assert r.headers.get_content_type() == "image/png"
        assert r.read() == png
    frames = _sse(f"{server}/api/chat", {"bot": "atlas", "text": "", "attachments": [up]})
    assert any(f.get("type") == "final" for f in frames)


def test_unknown_bot_chat_returns_404(server):
    import urllib.error

    with pytest.raises(urllib.error.HTTPError) as exc:
        _sse(f"{server}/api/chat", {"bot": "ghost", "text": "hi"})
    assert exc.value.code == 404


def test_send_now_promotes_a_queued_message(server, tmp_path):
    """POST /api/bots/<bot>/queue/<rid>/now flags the message now=True."""
    from agent import messaging

    paths = HarnessPaths.resolve(tmp_path / "home")
    ctrl = Control(paths)
    # Pause atlas (human takeover) so its live process leaves the queued
    # message untouched while the endpoint promotes it.
    ctrl.take_over("atlas")
    try:
        queued = messaging.Msg(to="atlas", frm="user", text="jump the queue")
        messaging.send(paths, queued)
        req = urllib.request.Request(
            f"{server}/api/bots/atlas/queue/{queued.id}/now", method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read().decode())
        assert body == {"ok": True, "queued": True}
        promoted = messaging.newer_user(paths, "atlas", None)
        assert [m.id for m in promoted] == [queued.id]
    finally:
        ctrl.return_control("atlas")


def test_send_now_after_pickup_reports_not_queued(server):
    req = urllib.request.Request(f"{server}/api/bots/atlas/queue/gone-already/now", method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        body = json.loads(r.read().decode())
    assert body == {"ok": False, "queued": False}
