"""WebSocket codec + live bidirectional API tests (stdlib client)."""

from __future__ import annotations

import base64
import io
import json
import os
import socket
import struct
import threading
import time
import urllib.request

import pytest

from harness import ws as wsproto
from harness.orchestrator import Orchestrator
from harness.server import EpochSequencer, WSHub, make_server

ROSTER = """
[[bots]]
name = "atlas"
role = "a terse research assistant"
provider = "echo"
"""


# -- codec unit tests -----------------------------------------------------
def test_accept_key_rfc_vector():
    # From RFC 6455 section 1.3
    assert wsproto.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def _client_frame(payload: bytes, opcode: int = wsproto.OP_TEXT) -> bytes:
    b1 = 0x80 | opcode
    n = len(payload)
    out = bytearray([b1])
    if n < 126:
        out.append(0x80 | n)
    elif n < 65536:
        out.append(0x80 | 126)
        out.extend(struct.pack("!H", n))
    else:
        out.append(0x80 | 127)
        out.extend(struct.pack("!Q", n))
    mask = os.urandom(4)
    out.extend(mask)
    out.extend(payload[i] ^ mask[i % 4] for i in range(n))
    return bytes(out)


def test_read_masked_client_frame():
    frame = _client_frame(b"hello")
    op, payload = wsproto.read_frame(io.BytesIO(frame))
    assert op == wsproto.OP_TEXT
    assert payload == b"hello"


def test_server_frame_roundtrip():
    buf = io.BytesIO()
    wsproto.send_frame(buf, b"world")
    buf.seek(0)
    op, payload = wsproto.read_frame(buf)
    assert op == wsproto.OP_TEXT
    assert payload == b"world"


def test_read_frame_rejects_huge_declared_length():
    # Header claims 2**32 bytes but carries none: must raise before allocating.
    header = bytes([0x80 | wsproto.OP_TEXT, 0x80 | 127]) + struct.pack("!Q", 2**32)
    with pytest.raises(wsproto.PayloadTooLarge):
        wsproto.read_frame(io.BytesIO(header + b"\x00\x00\x00\x00"))


def test_read_frame_under_cap_still_parses():
    payload = b"x" * 70000  # forces the 127 length branch
    op, got = wsproto.read_frame(io.BytesIO(_client_frame(payload)), max_payload=100_000)
    assert op == wsproto.OP_TEXT
    assert got == payload


def test_fragmented_total_enforced():
    def fragment(payload: bytes, opcode: int, fin: bool) -> bytes:
        b1 = (0x80 if fin else 0x00) | opcode
        out = bytearray([b1, 0x80 | len(payload)])
        mask = os.urandom(4)
        out.extend(mask)
        out.extend(payload[i] ^ mask[i % 4] for i in range(len(payload)))
        return bytes(out)

    stream = fragment(b"a" * 100, wsproto.OP_TEXT, fin=False) + fragment(
        b"b" * 100, wsproto.OP_CONT, fin=True
    )
    op, got = wsproto.read_frame(io.BytesIO(stream), max_payload=250)
    assert op == wsproto.OP_TEXT
    assert got == b"a" * 100 + b"b" * 100
    with pytest.raises(wsproto.PayloadTooLarge):
        wsproto.read_frame(io.BytesIO(stream), max_payload=150)


def test_send_close_packs_code():
    buf = io.BytesIO()
    wsproto.send_close(buf, wsproto.CLOSE_TOO_BIG)
    buf.seek(0)
    op, payload = wsproto.read_frame(buf)
    assert op == wsproto.OP_CLOSE
    assert struct.unpack("!H", payload)[0] == 1009


# -- live server ----------------------------------------------------------
class WSClient:
    def __init__(self, host: str, port: int, path: str = "/ws") -> None:
        self.sock = socket.create_connection((host, port), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        # Collectors wait on socket readiness. Read-ahead would hide complete
        # frames in a Python buffer after the kernel socket has been drained.
        self.rfile = self.sock.makefile("rb", buffering=0)
        status = self.rfile.readline()
        assert b"101" in status, status
        while True:
            line = self.rfile.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        # The server greets every connection with a version hello, then a
        # state snapshot (reseed) closed by a `ready` frame.
        self.hello = self.recv()
        assert self.hello["type"] == "hello", self.hello
        self.seed = []
        while True:
            frame = self.recv()
            assert frame is not None, "socket closed during reseed"
            if frame.get("type") == "ready":
                break
            self.seed.append(frame)

    def send(self, obj) -> None:
        self.sock.sendall(_client_frame(json.dumps(obj).encode()))

    def recv(self):
        frame = wsproto.read_frame(self.rfile)
        return None if frame is None else json.loads(frame[1].decode())

    def recv_until(self, kind: str, limit: int = 200):
        out = []
        for _ in range(limit):
            m = self.recv()
            if m is None:
                break
            out.append(m)
            if m.get("type") == kind:
                break
        return out

    def close(self):
        self.sock.close()


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


def test_ws_routine_result_broadcasts(server):
    """A scheduled/test-run turn must land on the open socket, not only inbox."""
    host, port, orch = server
    from harness.server import relay_bot_turn

    c = WSClient(host, port)
    try:
        time.sleep(0.2)
        relay_bot_turn(orch, "atlas", "[Routine: ping]\nSay the word banana")
        frames = c.recv_until("final", limit=80)
        types = [f["type"] for f in frames]
        assert "routine" in types
        assert "accepted" in types
        assert "final" in types
        routine = [f for f in frames if f["type"] == "routine"][0]
        assert routine["bot"] == "atlas"
        assert "banana" in routine["text"]
        assert routine.get("card_type") == "routine"
        assert routine["payload"]["title"] == "ping"
        assert "banana" in routine["payload"]["detail"]
        final = [f for f in frames if f["type"] == "final"][-1]
        assert final.get("bot") == "atlas"
        assert "banana" in (final.get("text") or "")
    finally:
        c.close()


def test_ws_relayed_user_turn_keeps_history_identity(server):
    """A server-owned human turn has the same identity live and in history."""
    host, port, orch = server
    from harness.server import relay_bot_turn

    c = WSClient(host, port)
    try:
        time.sleep(0.2)
        relay_bot_turn(orch, "atlas", "I mean the project name", origin=None)
        frames = c.recv_until("final", limit=80)
        routine = [f for f in frames if f["type"] == "routine"][0]
        assert routine["bot"] == "atlas"
        assert routine["text"] == "I mean the project name"
        assert routine.get("card_type") != "routine"
        assert "payload" not in routine
        from agent.history import user_thread
        from agent.memory import Memory

        rows = user_thread(Memory(paths=orch.paths, bot="atlas"), peer="user")
        incoming = [r for r in rows if r.get("frm") == "user"]
        assert len(incoming) == 1
        assert routine["message_id"] == incoming[0]["message_id"]
    finally:
        c.close()


@pytest.mark.parametrize("origin", ["welcome", "routine"])
@pytest.mark.parametrize("prompt_type", ["choice", "confirm"])
def test_background_relay_follows_prompt_through_final(tmp_path, origin, prompt_type):
    """The server's broadcast relay stays attached while the user answers.

    No consult poller or reconnect should be needed to see the rest of a
    welcome/routine turn after its first blocking question.
    """
    from types import SimpleNamespace

    from agent.streaming import StreamReader, StreamWriter
    from harness.control import Control
    from harness.paths import HarnessPaths
    from harness.server import relay_bot_turn

    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    rid = "background-question"
    writer = StreamWriter(paths, rid)
    question_seen = threading.Event()
    final_seen = threading.Event()
    frames = []

    def broadcast(frame):
        frames.append(frame)
        if frame.get("id") == "first-question":
            question_seen.set()
        if frame["type"] == "final":
            final_seen.set()

    orch = SimpleNamespace(
        paths=paths,
        control=Control(paths),
        ws_hub=SimpleNamespace(broadcast=broadcast),
        chat_stream=lambda *args, **kwargs: (rid, StreamReader(paths, rid)),
    )
    if prompt_type == "choice":
        writer.choice("atlas", "first-question", "Which platform?", ["X", "Reddit"])
    else:
        writer.card("atlas", "first-question", "confirm", {"question": "Use Reddit?"})
    relay_bot_turn(orch, "atlas", "Set up the bot", origin=origin)
    assert question_seen.wait(2)
    # A real tool writes its resolution and reply only after the human
    # answers. Writing later, after the question reached the hub, catches
    # the premature relay exit even without a network or agent process.
    writer.card_resolution(
        "atlas",
        "first-question",
        prompt_type,
        {},
        {"state": "answered", "responded_value": "Reddit"},
    )
    writer.final("Ready to set up Reddit.", "atlas")
    assert final_seen.wait(2), "background relay stopped at the question"
    assert frames[-1]["text"] == "Ready to set up Reddit."
    assert any(frame.get("resolution", {}).get("responded_value") == "Reddit" for frame in frames)


def test_ws_dream_tick_omits_prompt_and_still_streams(server):
    """Dream turns must not post the scheduler prompt; the stream still binds."""
    host, port, orch = server
    from harness.dreaming import DREAM_PROMPT
    from harness.server import relay_bot_turn

    c = WSClient(host, port)
    try:
        time.sleep(0.2)
        relay_bot_turn(orch, "atlas", DREAM_PROMPT, origin="dream")
        frames = c.recv_until("final", limit=80)
        types = [f["type"] for f in frames]
        assert "routine" in types
        assert "final" in types
        routine = next(f for f in frames if f["type"] == "routine")
        assert routine["bot"] == "atlas"
        assert routine.get("origin") == "dream"
        assert routine.get("request_id")
        assert "text" not in routine
        assert not any(
            f.get("type") == "user"
            and ("Dreaming" in (f.get("text") or "") or "Idle reflection" in (f.get("text") or ""))
            for f in frames
        )
    finally:
        c.close()


def test_inbox_dream_turn_does_not_fan_prompt_as_user(server):
    """Consult-relay must not paint the dream scheduler prompt as a user bubble."""
    from agent.streaming import StreamWriter

    host, port, orch = server
    a = WSClient(host, port)
    b = WSClient(host, port)
    try:
        orch.control.set_busy(
            "atlas",
            "dream-rid",
            frm="user",
            preview="[Dreaming — leftover]\nConsolidate.",
            origin="dream",
        )
        writer = StreamWriter(orch.paths, "dream-rid")
        writer.status("thinking")
        writer.final("Nothing that needs you.", "atlas")
        frames_b = b.recv_until("final")
        frames_a = a.recv_until("final")
        for frames in (frames_a, frames_b):
            assert not any(f.get("type") == "user" for f in frames)
            assert any(f.get("type") == "final" for f in frames)
    finally:
        a.close()
        b.close()


def test_inbox_user_turn_fans_to_every_socket(server):
    """A user turn that never had a sending socket still streams (Codex/Convex)."""
    from agent.streaming import StreamWriter

    host, port, orch = server
    a = WSClient(host, port)
    b = WSClient(host, port)
    try:
        orch.control.set_busy(
            "atlas",
            "inbox-rid",
            frm="user",
            preview="Scan now",
            message_id="client-message-id",
            thread_id="parent-message-id",
        )
        writer = StreamWriter(orch.paths, "inbox-rid")
        writer.delta("working on it")
        writer.final("scan complete", "atlas")
        user_b = _recv_type(b, "user", timeout=3.0)
        assert user_b["text"] == "Scan now"
        assert user_b.get("bot") == "atlas"
        assert user_b.get("frm") == "user"
        assert user_b["message_id"] == "client-message-id"
        assert user_b["thread_id"] == "parent-message-id"
        final_b = _recv_type(b, "final", timeout=3.0)
        assert "scan complete" in (final_b.get("text") or "")
        final_a = _recv_type(a, "final", timeout=3.0)
        assert "scan complete" in (final_a.get("text") or "")
    finally:
        a.close()
        b.close()


def test_ws_consult_relay_broadcasts_handoff(server):
    """A colleague handoff must land on the asked bot's live stream."""
    host, port, orch = server
    from agent.streaming import StreamWriter
    from harness.server import relay_consult_turn

    writer = StreamWriter(orch.paths, "consult-rid")
    writer.handoff("atlas", "nova", "create three tickets")
    writer.final("Done: create three tickets", "atlas")

    c = WSClient(host, port)
    try:
        time.sleep(0.2)
        relay_consult_turn(orch, "atlas", "consult-rid")
        frames = c.recv_until("final", limit=80)
        types = [f["type"] for f in frames]
        assert "accepted" in types
        assert "handoff" in types
        assert "final" in types
        handoff = next(f for f in frames if f["type"] == "handoff")
        assert handoff.get("bot") == "atlas"
        assert handoff.get("frm") == "nova"
        assert handoff.get("text") == "create three tickets"
    finally:
        c.close()


def test_ws_chat_fans_user_line_to_other_socket(server):
    """A send on one app must appear on the other without switching chats."""
    host, port, _orch = server
    sender = WSClient(host, port)
    watcher = WSClient(host, port)
    try:
        sender.send({"type": "chat", "bot": "atlas", "text": "great"})
        user = _recv_type(watcher, "user", timeout=3.0)
        assert user["text"] == "great"
        assert user.get("frm") == "user"
        assert user.get("bot") == "atlas"
        assert user.get("mutation") == "appended"
    finally:
        sender.close()
        watcher.close()


def test_consult_relay_does_not_reannounce_handled_chat(server):
    """The busy-flag poller must never re-post a send a live handler announced.

    A chat over 240 chars whose turn outlived a poller tick used to come back
    as a second user bubble — the truncated busy preview — which the apps
    cannot dedup against the full text. Park the turn on a choice so the
    poller definitely notices it, then check the whole exchange carried
    exactly one user frame.
    """
    host, port, _orch = server
    sender = WSClient(host, port)
    watcher = WSClient(host, port)
    try:
        text = ("build the proposals assistant bot from the repo now " * 6).strip()
        assert len(text) > 240
        text += " choose: yes | no"
        sender.send({"type": "chat", "bot": "atlas", "text": text})
        frames = watcher.recv_until("choice", limit=120)
        # Sit through a few poller ticks while the turn is parked — the
        # window where the duplicate used to broadcast.
        time.sleep(0.7)
        choice = next(f for f in frames if f.get("type") == "choice")
        req = urllib.request.Request(
            f"http://{host}:{port}/api/answers",
            data=json.dumps({"id": choice["id"], "value": "yes"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
        frames += watcher.recv_until("final", limit=120)
        users = [f.get("text") for f in frames if f.get("type") == "user"]
        assert users == [text]
    finally:
        sender.close()
        watcher.close()


def test_inbox_user_turn_relays_full_text(server):
    """A socketless user turn fans out with its full text, never a 240-char
    clip — a clipped bubble can't dedup against history later."""
    from agent import messaging

    host, port, orch = server
    c = WSClient(host, port)
    try:
        long_text = ("please scan the fleet and report every anomaly " * 8).strip()
        assert len(long_text) > 240
        # Park the turn on a choice so it outlives a poller tick — an echo
        # turn is otherwise done before the busy flag is ever observed.
        long_text += " choose: yes | no"
        messaging.send(orch.paths, messaging.Msg(to="atlas", frm="user", text=long_text))
        user = _recv_type(c, "user", timeout=10.0)
        assert user["text"] == long_text
        assert user.get("bot") == "atlas"
        choice = _recv_type(c, "choice", timeout=10.0)
        req = urllib.request.Request(
            f"http://{host}:{port}/api/answers",
            data=json.dumps({"id": choice["id"], "value": "yes"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
        _recv_type(c, "final", timeout=10.0)
    finally:
        c.close()


def test_ws_chat_streams(server):
    host, port, _ = server
    c = WSClient(host, port)
    try:
        c.send({"type": "chat", "bot": "atlas", "text": "hello ws"})
        frames = c.recv_until("final")
        types = [f["type"] for f in frames]
        assert "accepted" in types
        assert "delta" in types
        final = [f for f in frames if f["type"] == "final"][-1]
        assert "hello ws" in final["text"]
    finally:
        c.close()


def test_ws_choice_keeps_streaming_until_final(server):
    """A choice must not drop the WebSocket relay — later picks and the reply
    have to arrive on the same socket (HTTP/SSE still parks)."""
    import urllib.request

    host, port, _orch = server
    c = WSClient(host, port)
    try:
        c.send(
            {
                "type": "chat",
                "bot": "atlas",
                "text": "Deploy target? choose: staging | prod",
            }
        )
        frames = c.recv_until("choice")
        choice = next(f for f in frames if f.get("type") == "choice")
        assert not any(f.get("type") == "final" for f in frames)
        req = urllib.request.Request(
            f"http://{host}:{port}/api/answers",
            data=json.dumps({"id": choice["id"], "value": "prod"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
        rest = c.recv_until("final")
        assert any(f.get("type") == "final" for f in rest)
        final = [f for f in rest if f.get("type") == "final"][-1]
        assert "prod" in (final.get("text") or "")
    finally:
        c.close()


def test_ws_chat_while_paused_still_replies(server):
    """A send during leftover takeover is accepted and replies; no return card."""
    host, port, orch = server
    orch.control.take_over("atlas")
    c = WSClient(host, port)
    try:
        c.send({"type": "chat", "bot": "atlas", "text": "hi"})
        frames = []
        deadline = time.time() + 8
        while time.time() < deadline:
            c.sock.settimeout(max(0.2, deadline - time.time()))
            try:
                frame = c.recv()
            except TimeoutError:
                continue
            if not frame:
                break
            frames.append(frame)
            if frame.get("type") == "final":
                break
        types = [f["type"] for f in frames]
        assert "accepted" in types, frames
        assert any(f.get("type") == "final" for f in frames), frames
        assert not any(
            f.get("type") == "card" and f.get("card_type") == "control_return" for f in frames
        )
        assert "paused" not in [f.get("value") for f in frames if f.get("type") == "status"]
    finally:
        c.close()
        orch.control.return_control("atlas")


def test_ws_return_during_paused_chat_is_immediate(server):
    """Return control must not wait for the in-flight chat stream to time out."""
    host, port, orch = server
    orch.control.take_over("atlas")
    c = WSClient(host, port)
    try:
        c.send({"type": "chat", "bot": "atlas", "text": "hi"})
        deadline = time.time() + 8
        saw_accepted = False
        while time.time() < deadline and not saw_accepted:
            c.sock.settimeout(max(0.2, deadline - time.time()))
            try:
                frame = c.recv()
            except TimeoutError:
                continue
            if not frame:
                break
            if frame.get("type") == "accepted":
                saw_accepted = True
        assert saw_accepted

        t0 = time.time()
        c.send({"type": "return", "bot": "atlas"})
        c.sock.settimeout(0.25)
        st = None
        while time.time() - t0 < 2:
            try:
                frame = c.recv()
            except TimeoutError:
                continue
            if not frame:
                break
            if frame.get("type") == "control_state":
                st = frame
                break
        assert st is not None, "return was blocked behind the chat stream"
        assert st["mode"] == "bot"
        assert time.time() - t0 < 2
        assert orch.control.state("atlas").mode == "bot"
    finally:
        c.close()


def test_ws_takeover_and_bots(server):
    host, port, _ = server
    c = WSClient(host, port)
    try:
        c.send({"type": "bots"})
        bots = c.recv()
        assert bots["type"] == "bots"
        assert any(b["name"] == "atlas" for b in bots["bots"])

        c.send({"type": "takeover", "bot": "atlas"})
        st = c.recv()
        assert st["type"] == "control_state"
        assert st["mode"] == "takeover"

        c.send({"type": "return", "bot": "atlas"})
        assert c.recv()["mode"] == "bot"
    finally:
        c.close()


# -- self-healing stream protocol --------------------------------
def test_epoch_sequencer_monotonic_per_key():
    seqr = EpochSequencer()
    for key in ("bot:atlas", "room:r1", "roster", "screen:atlas"):
        assert [seqr.next(key) for _ in range(5)] == [1, 2, 3, 4, 5]
    # keys are independent: bumping one never rewinds another
    assert seqr.next("bot:atlas") == 6
    assert seqr.next("roster") == 6


def test_epoch_sequencer_threadsafe_monotonic():
    seqr = EpochSequencer()
    got: list[int] = []
    lock = threading.Lock()

    def spin():
        for _ in range(250):
            n = seqr.next("bot:atlas")
            with lock:
                got.append(n)

    threads = [threading.Thread(target=spin) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got) == list(range(1, 1001))  # no duplicates, no gaps


def test_epoch_sequencer_stream_keys():
    sk = EpochSequencer.stream_key
    assert sk({"type": "delta", "bot": "atlas"}) == "bot:atlas"
    assert sk({"type": "final", "frm": "atlas"}) == "bot:atlas"
    assert sk({"type": "delta", "bot": "atlas", "room": "r9"}) == "room:r9"
    assert sk({"type": "bots"}) == "roster"
    assert sk({"type": "hello"}) == "roster"
    assert sk({"type": "screen_started", "bot": "atlas"}) == "screen:atlas"
    # frm="user" is the speaker; the line belongs to the named bot.
    assert sk({"type": "user", "frm": "user", "bot": "atlas"}) == "bot:atlas"
    assert sk({"type": "user", "frm": "user"}) == "roster"


def test_ws_hello_reseed_snapshot(server):
    """Right after hello every socket gets the full state snapshot, so a fresh
    or reconnecting client never renders stale connection state."""
    host, port, _orch = server
    c = WSClient(host, port)
    try:
        by_type = {f["type"]: f for f in c.seed}
        assert by_type["bots"]["mutation"] == "snapshot"
        assert any(b["name"] == "atlas" for b in by_type["bots"]["bots"])
        assert by_type["rooms"]["mutation"] == "snapshot"
        assert by_type["prompts"]["mutation"] == "snapshot"
        controls = [f for f in c.seed if f["type"] == "control_state"]
        assert {f["bot"] for f in controls} == {"atlas"}
        assert all(f["mutation"] == "snapshot" for f in controls)
        # the snapshot burst is fenced like everything else
        assert c.hello["epoch"] == c.hello["boot_id"]
        assert all(f["epoch"] == c.hello["epoch"] for f in c.seed)
    finally:
        c.close()


def test_ws_reseed_carries_settled_prompt_resolution(server):
    """A settled prompt rides the snapshot WITH its resolution, so a
    reconnecting client renders it as settled instead of an open box."""
    from agent.streaming import resolve_prompt, write_prompt

    host, port, orch = server
    write_prompt(
        orch.paths,
        {
            "id": "open1",
            "type": "choice",
            "bot": "atlas",
            "question": "Still open?",
            "options": ["a", "b"],
        },
    )
    write_prompt(
        orch.paths,
        {
            "id": "done1",
            "type": "card",
            "card_type": "confirm",
            "bot": "atlas",
            "payload": {"question": "Merge?"},
        },
    )
    resolve_prompt(
        orch.paths,
        "done1",
        {"state": "answered", "responded_value": "confirm", "approved": True, "skipped": False},
    )
    c = WSClient(host, port)
    try:
        prompts = {p["id"]: p for f in c.seed if f["type"] == "prompts" for p in f["prompts"]}
        assert "resolution" not in prompts["open1"]
        assert prompts["done1"]["resolution"]["responded_value"] == "confirm"
        assert prompts["done1"]["resolution"]["approved"] is True
    finally:
        c.close()


def test_ws_frames_carry_epoch_and_monotonic_seq(server):
    host, port, _ = server
    c = WSClient(host, port)
    try:
        c.send({"type": "chat", "bot": "atlas", "text": "fence me"})
        frames = c.recv_until("final")
        assert all("epoch" in f and "seq" in f for f in frames)
        assert {f["epoch"] for f in frames} == {c.hello["epoch"]}
        # enforce per-key monotonicity exactly the way a client would
        last: dict[str, int] = {}
        for f in frames:
            key = EpochSequencer.stream_key(f)
            assert f["seq"] > last.get(key, 0), (key, f)
            last[key] = f["seq"]
    finally:
        c.close()


def test_ws_chat_relays_message_upserts(server):
    """Chunk-boundary upserts reach the socket and settle on the final text."""
    host, port, _ = server
    c = WSClient(host, port)
    try:
        c.send({"type": "chat", "bot": "atlas", "text": "upsert me"})
        frames = c.recv_until("final")
        msgs = [f for f in frames if f["type"] == "message"]
        assert msgs, "no message upserts relayed over WS"
        assert msgs[0]["mutation"] == "appended"
        assert all(m["mutation"] == "updated" for m in msgs[1:])
        assert len({m["id"] for m in msgs}) == 1
        assert all(m["streaming"] is True for m in msgs[:-1])
        assert msgs[-1]["streaming"] is False
        final = [f for f in frames if f["type"] == "final"][-1]
        assert msgs[-1]["text"] == final["text"]
        # legacy deltas still flow beside the upserts for old clients
        assert any(f["type"] == "delta" for f in frames)
    finally:
        c.close()


def test_ws_chat_with_attachment(server, tmp_path):
    host, port, orch = server
    # place a file into shared uploads and reference it
    upload = orch.paths.uploads
    upload.mkdir(parents=True, exist_ok=True)
    f = upload / "notes.txt"
    f.write_text("quarterly revenue is up 12 percent", encoding="utf-8")

    c = WSClient(host, port)
    try:
        c.send(
            {
                "type": "chat",
                "bot": "atlas",
                "text": "summarize this",
                "attachments": [{"name": "notes.txt", "path": str(f)}],
            }
        )
        final = [f for f in c.recv_until("final") if f["type"] == "final"][-1]
        assert "notes.txt" in final["text"]
        assert "quarterly revenue" in final["text"]
    finally:
        c.close()


# -- live roster / chat-list sync --------------------------------
def _http(host, port, path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"http://{host}:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _recv_type(client, kind, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        # A socket makefile cannot recover after a read timeout. Give each
        # read the remaining budget instead of permanently poisoning it at 1s.
        client.sock.settimeout(max(0.1, deadline - time.time()))
        try:
            frame = client.recv()
        except (TimeoutError, OSError):
            break
        if frame and frame.get("type") == kind:
            return frame
    raise AssertionError(f"no {kind!r} frame within {timeout}s")


def test_recv_type_accepts_a_frame_after_one_second():
    reader, writer = socket.socketpair()
    client = WSClient.__new__(WSClient)
    client.sock = reader
    client.rfile = reader.makefile("rb", buffering=0)
    payload = io.BytesIO()
    wsproto.send_json(payload, {"type": "delayed"})

    def deliver():
        time.sleep(1.25)
        writer.sendall(payload.getvalue())

    delivery = threading.Thread(target=deliver)
    delivery.start()
    try:
        assert _recv_type(client, "delayed", timeout=3.0) == {"type": "delayed"}
    finally:
        delivery.join(timeout=5)
        client.rfile.close()
        reader.close()
        writer.close()


class _FakeWS:
    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.got = None

    def _ws_send_json(self, frame):
        if not self.ok:
            return False
        self.got = frame
        return True


def test_wshub_discards_failed_sends():
    hub = WSHub()
    live = _FakeWS(True)
    dead = _FakeWS(False)
    hub.add(live)
    hub.add(dead)
    hub.broadcast({"type": "bots", "bots": []})
    assert live.got == {"type": "bots", "bots": []}
    assert live in hub._clients
    assert dead not in hub._clients


def test_roster_and_rooms_broadcast_to_every_socket(server):
    """A mutation on one client must land on every other open socket."""
    host, port, _orch = server
    a = WSClient(host, port)
    b = WSClient(host, port)
    try:
        created = _http(
            host,
            port,
            "/api/bots",
            "POST",
            # This tests roster mutations; startup has its own status snapshots.
            {"name": "zephyr", "provider": "echo", "role": "scout", "start": False},
        )
        assert created["name"] == "zephyr"
        for client in (a, b):
            frame = _recv_type(client, "bots")
            assert frame["mutation"] == "snapshot"
            names = {row["name"] for row in frame["bots"]}
            assert names == {"atlas", "zephyr"}

        _http(host, port, "/api/bots/zephyr", "PATCH", {"title": "Zephyr Prime"})
        for client in (a, b):
            frame = _recv_type(client, "bots")
            by_name = {row["name"]: row for row in frame["bots"]}
            assert by_name["zephyr"]["title"] == "Zephyr Prime"

        room = _http(
            host,
            port,
            "/api/rooms",
            "POST",
            {"title": "Pair", "members": ["atlas", "zephyr"]},
        )
        for client in (a, b):
            frame = _recv_type(client, "rooms")
            assert frame["mutation"] == "snapshot"
            assert any(row["id"] == room["id"] for row in frame["rooms"])

        _http(host, port, f"/api/rooms/{room['id']}", "PATCH", {"title": "Paired"})
        for client in (a, b):
            frame = _recv_type(client, "rooms")
            by_id = {row["id"]: row for row in frame["rooms"]}
            assert by_id[room["id"]]["title"] == "Paired"

        _http(host, port, f"/api/rooms/{room['id']}", "DELETE")
        for client in (a, b):
            frame = _recv_type(client, "rooms")
            assert all(row["id"] != room["id"] for row in frame["rooms"])

        _http(host, port, "/api/bots/zephyr", "DELETE")
        for client in (a, b):
            frame = _recv_type(client, "bots")
            assert {row["name"] for row in frame["bots"]} == {"atlas"}
    finally:
        a.close()
        b.close()


def test_chat_fanout_reaches_every_socket(server):
    """A send on one socket must stream to every other live client."""
    host, port, _orch = server
    a = WSClient(host, port)
    b = WSClient(host, port)
    try:
        a.send({"type": "chat", "bot": "atlas", "text": "hello from desktop"})
        user_b = _recv_type(b, "user")
        assert user_b["text"] == "hello from desktop"
        assert user_b["bot"] == "atlas"
        assert user_b["frm"] == "user"
        final_b = _recv_type(b, "final")
        assert "hello from desktop" in (final_b.get("text") or "")
        final_a = _recv_type(a, "final")
        assert "hello from desktop" in (final_a.get("text") or "")
        assert user_b.get("seq") is not None
    finally:
        a.close()
        b.close()


def test_choice_answer_fans_out_resolution_and_lands_in_history(server):
    """A pick on one client must settle the box on every other app immediately,
    and GET /history must carry the pick even before the waiting tool finishes."""
    from agent.streaming import write_prompt

    host, port, orch = server
    write_prompt(
        orch.paths,
        {
            "id": "cho1",
            "type": "choice",
            "bot": "atlas",
            "question": "Deploy where?",
            "options": ["staging", "prod"],
        },
    )
    a = WSClient(host, port)
    b = WSClient(host, port)
    try:
        out = _http(
            host,
            port,
            "/api/answers",
            "POST",
            {"id": "cho1", "value": "prod", "bot": "atlas"},
        )
        assert out.get("ok") is True
        card = _recv_type(b, "card")
        assert card["id"] == "cho1"
        assert card["card_type"] == "choice"
        assert card["mutation"] == "updated"
        assert card["bot"] == "atlas"
        assert card["payload"]["question"] == "Deploy where?"
        assert card["payload"]["options"] == ["staging", "prod"]
        assert card["resolution"]["state"] == "answered"
        assert card["resolution"]["responded_value"] == "prod"
        card_a = _recv_type(a, "card")
        assert card_a["id"] == "cho1"
        assert card_a["resolution"]["responded_value"] == "prod"
        hist = _http(host, port, "/api/bots/atlas/history")
        cards = [r for r in hist if r.get("type") == "card" and r.get("card_id") == "cho1"]
        assert len(cards) == 1
        assert cards[0]["resolution"]["responded_value"] == "prod"
    finally:
        a.close()
        b.close()


def test_room_choice_answer_fans_to_room_not_1to1_history(server):
    """A group pick must settle the room box and stay off GET /history."""
    from agent.streaming import write_prompt

    host, port, orch = server
    write_prompt(
        orch.paths,
        {
            "id": "cho-room",
            "type": "choice",
            "bot": "atlas",
            "room": "standup",
            "question": "Deploy where?",
            "options": ["staging", "prod"],
        },
    )
    a = WSClient(host, port)
    b = WSClient(host, port)
    try:
        out = _http(
            host,
            port,
            "/api/answers",
            "POST",
            {"id": "cho-room", "value": "prod", "bot": "atlas"},
        )
        assert out.get("ok") is True
        card = _recv_type(b, "card")
        assert card["id"] == "cho-room"
        assert card["card_type"] == "choice"
        assert card["mutation"] == "updated"
        assert card["bot"] == "atlas"
        assert card["room"] == "standup"
        assert card["resolution"]["responded_value"] == "prod"
        card_a = _recv_type(a, "card")
        assert card_a["id"] == "cho-room"
        assert card_a["room"] == "standup"
        hist = _http(host, port, "/api/bots/atlas/history")
        cards = [r for r in hist if r.get("type") == "card" and r.get("card_id") == "cho-room"]
        assert cards == []
    finally:
        a.close()
        b.close()


def test_secret_saved_fans_out_without_value(server):
    """A secret saved on one client must settle the box on every other app."""
    from agent.streaming import write_prompt

    host, port, orch = server
    write_prompt(
        orch.paths,
        {
            "id": "sec1",
            "type": "secret_request",
            "bot": "atlas",
            "name": "SMTP_PASSWORD",
            "title": "SMTP password",
            "reason": "to send mail",
        },
    )
    a = WSClient(host, port)
    b = WSClient(host, port)
    try:
        out = _http(
            host,
            port,
            "/api/secrets",
            "POST",
            {"name": "SMTP_PASSWORD", "value": "not-a-real-password"},
        )
        assert out.get("configured") is True
        assert "not-a-real-password" not in json.dumps(out)
        saved = _recv_type(b, "secret_saved")
        assert saved["name"] == "SMTP_PASSWORD"
        assert saved.get("secret_provided") is True
        assert saved.get("bot") == "atlas"
        assert "value" not in saved
        assert "not-a-real-password" not in json.dumps(saved)
        saved_a = _recv_type(a, "secret_saved")
        assert saved_a["name"] == "SMTP_PASSWORD"
        assert "value" not in saved_a
    finally:
        a.close()
        b.close()


def test_reconnect_reseed_sees_roster_mutation(server):
    """A client that dropped during a create must pick it up on the next hello."""
    host, port, _orch = server
    stale = WSClient(host, port)
    stale.close()
    _http(
        host,
        port,
        "/api/bots",
        "POST",
        {"name": "prism", "provider": "echo", "role": "writer"},
    )
    fresh = WSClient(host, port)
    try:
        bots = next(f for f in fresh.seed if f["type"] == "bots")
        assert bots["mutation"] == "snapshot"
        assert {row["name"] for row in bots["bots"]} == {"atlas", "prism"}
    finally:
        fresh.close()


def test_stream_sweep_cannot_claim_busy_turn_before_its_user_bubble(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent.streaming import StreamWriter
    from harness import server as server_module
    from harness.paths import HarnessPaths

    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    loops = []
    frames = []
    relayed = []
    ticks = [0]
    text = ("Please report every anomaly. " * 20).strip()
    info = {"request_id": "inbox-turn", "frm": "user", "preview": text}
    orch = SimpleNamespace(
        paths=paths,
        roster=SimpleNamespace(names=lambda: ["atlas"]),
        control=SimpleNamespace(_busy_info=lambda _: info if ticks[0] >= 5 else None),
        ws_hub=SimpleNamespace(broadcast=frames.append),
    )

    class Thread:
        def __init__(self, *, target, **kwargs):
            loops.append(target)

        def start(self):
            pass

    class Done(BaseException):
        pass

    caller = threading.current_thread()
    real_sleep = time.sleep

    def tick(seconds):
        if threading.current_thread() is not caller:
            return real_sleep(seconds)
        ticks[0] += 1
        if ticks[0] == 5:
            # The stream first becomes visible on the same tick as the busy
            # flag, exactly when the periodic fallback sweep runs.
            StreamWriter(paths, "inbox-turn").final("Answer", "atlas")
        if ticks[0] > 6:
            raise Done()

    monkeypatch.setattr(server_module.threading, "Thread", Thread)
    monkeypatch.setattr(server_module.time, "sleep", tick)
    monkeypatch.setattr(
        server_module, "relay_consult_turn", lambda *args, **kwargs: relayed.append(args[1:])
    )
    server_module.start_consult_relays(orch)
    with pytest.raises(Done):
        loops[0]()
    assert [f["text"] for f in frames if f.get("type") == "user"] == [text]
    assert relayed == [("atlas", "inbox-turn")]
