"""Real-time control: input injection + live WS frame streaming."""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading
import time

import pytest

from harness import hostinput
from harness import ws as wsproto
from harness.orchestrator import Orchestrator
from harness.server import make_server

ROSTER = '[[bots]]\nname = "atlas"\nprovider = "echo"\n'


# -- input command construction (no display needed) -----------------------
def test_hostinput_builds_xdotool_commands(monkeypatch):
    sent: list[tuple] = []
    hostinput._session._last = None
    monkeypatch.setattr(hostinput.shutil, "which", lambda _n: "/usr/bin/xdotool")
    monkeypatch.setattr(hostinput, "display_size", lambda machine=None: (1000, 800))
    monkeypatch.setattr(hostinput, "_pointer_window", lambda machine=None: None)
    monkeypatch.setattr(hostinput, "_clip_tool", lambda machine=None: None)
    monkeypatch.setattr(
        hostinput._session, "send", lambda *parts: sent.append(("send", *parts)) or True
    )
    monkeypatch.setattr(
        hostinput._session,
        "move_to",
        lambda x, y: sent.append(("move", x, y)) or True,
    )

    assert hostinput.move(0.5, 0.5) is True
    assert sent[-1] == ("move", 500, 400)

    assert hostinput.click(0.1, 0.2, button=3) is True
    assert sent[-2] == ("move", 100, 160)
    assert sent[-1] == ("send", "sleep", "0.03", "click", "3")

    assert hostinput.down(0.0, 0.0, button=1) is True
    assert sent[-2] == ("move", 0, 0)
    assert sent[-1] == ("send", "mousedown", "1")
    assert hostinput.up(1.0, 1.0, button=1) is True
    assert sent[-2] == ("move", 999, 799)
    assert sent[-1] == ("send", "mouseup", "1")

    assert hostinput.type_text("hi there") is True
    assert sent[-1] == ("send", "type", "--clearmodifiers", "--", "hi there")

    assert hostinput.key("Return") is True
    assert sent[-1] == ("send", "key", "--clearmodifiers", "Return")

    assert hostinput.scroll(-4) is True
    assert ("move", 500, 496) in sent
    assert sent[-1] == ("send", "click", "--repeat", "4", "4")
    assert hostinput.scroll(0) is True
    assert sent[-1] == ("send", "click", "--repeat", "4", "4")


@pytest.mark.parametrize(
    "axis,amount,button", [("vertical", 12, "5"), ("horizontal", -8, "6"), ("horizontal", 20, "7")]
)
@pytest.mark.parametrize("wait", [False, True])
def test_targeted_scroll_uses_full_amount_and_machine_screenshot(
    monkeypatch, axis, amount, button, wait
):
    sent = []
    machine = "harness-machine-0"
    session = hostinput._Session()
    monkeypatch.setattr(hostinput, "_machine_sessions", {machine: session})
    monkeypatch.setattr(hostinput, "_shot_size", {machine: (1000, 500)})
    monkeypatch.setattr(hostinput, "display_size", lambda machine=None: (2000, 1000))
    monkeypatch.setattr(session, "move_to", lambda x, y: sent.append(("move", x, y)) or True)
    monkeypatch.setattr(session, "send", lambda *parts: sent.append(parts) or True)
    monkeypatch.setattr(
        session, "run_sync", lambda *parts, **kwargs: sent.append(("sync", *parts)) or True
    )

    assert hostinput.scroll(amount, machine, nx=250, ny=125, axis=axis, wait=wait)
    if wait:
        assert sent == [
            ("sync", "mousemove", "500", "250", "click", "--repeat", str(abs(amount)), button)
        ]
    else:
        assert sent == [("move", 500, 250), ("click", "--repeat", str(abs(amount)), button)]


@pytest.mark.parametrize(
    "kwargs",
    [{"amount": 21}, {"amount": -21}, {"amount": 1, "axis": "diagonal"}, {"amount": 1, "nx": 0.5}],
)
def test_invalid_scroll_has_no_partial_movement(monkeypatch, kwargs):
    monkeypatch.setattr(
        hostinput, "session_for", lambda machine: pytest.fail("invalid scroll must not act")
    )
    assert hostinput.scroll(**kwargs) is False


def test_double_click_preserves_button_and_machine_coordinates(monkeypatch):
    sent = []
    machine = "harness-machine-1"
    session = hostinput._Session()
    monkeypatch.setattr(hostinput, "_machine_sessions", {machine: session})
    monkeypatch.setattr(hostinput, "_shot_size", {machine: (1000, 500)})
    monkeypatch.setattr(hostinput, "display_size", lambda machine=None: (2000, 1000))
    monkeypatch.setattr(hostinput, "_dock_hit", lambda *a: None)
    monkeypatch.setattr(hostinput, "_focus_pointer_window", lambda *a: None)
    monkeypatch.setattr(session, "move_to", lambda x, y: sent.append(("move", x, y)) or True)
    monkeypatch.setattr(session, "send", lambda *parts: sent.append(parts) or True)
    assert hostinput.click(250, 125, machine=machine, clicks=2)
    assert sent == [
        ("move", 500, 250),
        ("sleep", "0.03", "click", "--repeat", "2", "--delay", "100", "1"),
    ]


def test_drag_is_one_machine_gesture_and_leaves_pointer_at_destination(monkeypatch):
    commands = []
    machine = "harness-machine-0"
    session = hostinput._Session(("docker", "exec", "-i", machine, "xdotool", "-"))
    monkeypatch.setattr(hostinput, "_machine_sessions", {machine: session})
    monkeypatch.setattr(hostinput, "_shot_size", {machine: (1000, 500)})
    monkeypatch.setattr(hostinput, "display_size", lambda machine=None: (2000, 1000))
    monkeypatch.setattr(session, "_drain", lambda: commands.append("drain preceding input") or True)

    def run(argv, **kwargs):
        commands.append(argv)
        assert kwargs["timeout"] == 5
        return hostinput.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(hostinput.subprocess, "run", run)

    assert hostinput.drag([(100, 100), (250, 125), (500, 200)], machine=machine)
    assert len(commands) == 2
    assert commands[0] == "drain preceding input"
    assert commands[1][:5] == ["docker", "exec", "-i", machine, "xdotool"]
    gesture = " ".join(commands[1][5:])
    assert gesture.startswith("mousemove 200 200 mousedown 1 sleep 0.05")
    assert "mousemove 500 250" in gesture
    assert gesture.endswith("mousemove 1001 400 sleep 0.05 mouseup 1")
    assert session.move_to(1001, 400)
    assert len(commands) == 2  # already there; no spurious motion after release


def test_observation_flush_does_not_start_an_input_session(monkeypatch):
    monkeypatch.setattr(hostinput, "_machine_sessions", {})
    monkeypatch.setattr(
        hostinput,
        "session_for",
        lambda machine: pytest.fail("read-only observation must not start input"),
    )
    assert hostinput.flush("harness-machine-9") is True


def test_observation_timeout_keeps_pending_input_and_refuses_new_actions(monkeypatch):
    import io

    class PendingInput:
        stdin = io.StringIO()
        finished = False

        def poll(self):
            return 0 if self.finished else None

        def wait(self, timeout):
            if not self.finished:
                raise hostinput.subprocess.TimeoutExpired("typing", timeout)
            return 0

        def terminate(self):
            pytest.fail("observation must not truncate pending typing")

    proc = PendingInput()
    session = hostinput._Session()
    session._proc = proc
    monkeypatch.setattr(session, "_start", lambda: pytest.fail("must not overtake pending input"))
    assert session.flush() is False
    assert session._proc is proc
    assert session.send("key", "Return") is False
    assert session.run_sync("click", "1") is False
    proc.finished = True
    assert session.flush() is True
    assert session._proc is None


def test_observation_flush_reports_failed_input_process():
    from types import SimpleNamespace

    session = hostinput._Session()
    session._proc = SimpleNamespace(stdin=None, wait=lambda timeout: 1)
    assert session.flush() is False


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_drag_attempts_release_when_gesture_fails(monkeypatch, failure):
    commands = []
    session = hostinput._Session()
    monkeypatch.setattr(hostinput, "_session", session)
    monkeypatch.setattr(hostinput, "display_size", lambda machine=None: (1000, 500))

    def run(argv, **kwargs):
        commands.append(argv)
        if failure == "timeout" and len(commands) == 1:
            raise hostinput.subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return hostinput.subprocess.CompletedProcess(argv, 1 if len(commands) == 1 else 0)

    monkeypatch.setattr(hostinput.subprocess, "run", run)
    assert not hostinput.drag([(0.1, 0.1), (0.2, 0.2)])
    assert commands[-1] == ["xdotool", "mouseup", "1"]


@pytest.mark.parametrize(
    "path", [[], [(0.1, 0.1)], [(0.1, 0.1)] * 51, [(0.1, 0.1), (float("nan"), 0.2)]]
)
def test_invalid_drag_has_no_partial_input(monkeypatch, path):
    monkeypatch.setattr(
        hostinput, "session_for", lambda machine: pytest.fail("invalid drag must not act")
    )
    monkeypatch.setattr(
        hostinput, "display_size", lambda machine=None: pytest.fail("validate before I/O")
    )
    assert hostinput.drag(path) is False


def test_click_on_tint2_dock_targets_the_panel(monkeypatch):
    """XTEST clicks pass through tint2 to the wallpaper; dock hits must use --window."""
    sent: list[tuple] = []
    hostinput._dock_cache.clear()
    hostinput._session._last = None
    monkeypatch.setattr(hostinput, "display_size", lambda machine=None: (1280, 800))
    monkeypatch.setattr(
        hostinput,
        "_dock_rects",
        lambda machine=None: [("8388612", 535, 724, 210, 62)],
    )
    monkeypatch.setattr(
        hostinput._session, "move_to", lambda x, y: sent.append(("move", x, y)) or True
    )
    monkeypatch.setattr(
        hostinput._session, "send", lambda *parts: sent.append(("send", *parts)) or True
    )

    assert hostinput.down(580 / 1279, 750 / 799, px=580, py=750) is True
    assert ("move", 580, 750) in sent
    assert ("send", "mousemove", "--window", "8388612", "45", "26") in sent
    assert ("send", "mousedown", "--window", "8388612", "1") in sent

    sent.clear()
    assert hostinput.up(580 / 1279, 750 / 799, px=580, py=750) is True
    assert ("send", "mouseup", "--window", "8388612", "1") in sent

    sent.clear()
    assert hostinput.click(580 / 1279, 750 / 799, px=580, py=750) is True
    assert ("send", "sleep", "0.03", "click", "--window", "8388612", "1") in sent

    sent.clear()
    assert hostinput.down(0.1, 0.1, px=80, py=80) is True
    assert ("send", "mousedown", "1") in sent
    assert not any("--window" in str(p) for p in sent)


def test_pos_trusts_fractions_when_retina_px_disagrees(monkeypatch):
    """Mac retina used JPEG px at 2×; those won and missed the dock."""
    monkeypatch.setattr(hostinput, "display_size", lambda machine=None: (1280, 800))
    # 0.45 of 1280 is ~575; px 1150 is the 2× mapping clamped/skewed.
    pos = hostinput._pos(0.45, 0.94, 1150, 1500)
    assert pos is not None
    assert abs(pos[0] - round(0.45 * 1279)) <= 1
    assert abs(pos[1] - round(0.94 * 799)) <= 1
    # Explicit JPEG pixels with no usable fractions still win (bot clicks).
    assert hostinput._pos(0.0, 0.0, 100, 120) == (100, 120)


def test_hostinput_maps_screenshot_pixels_onto_display(monkeypatch):
    """Vision models click JPEG pixels; those used to be treated as 0..1."""
    moved: list[tuple[int, int]] = []
    hostinput._shot_size.clear()
    monkeypatch.setattr(hostinput, "display_size", lambda machine=None: (1920, 1080))
    monkeypatch.setattr(hostinput._session, "move_to", lambda x, y: moved.append((x, y)) or True)
    monkeypatch.setattr(hostinput._session, "send", lambda *parts: True)

    hostinput.remember_shot((1280, 720))
    assert hostinput.click(640, 360) is True
    assert moved[-1] == (960, 540)

    moved.clear()
    assert hostinput.click(0.5, 0.5) is True
    assert moved[-1] == (960, 540)

    moved.clear()
    hostinput._shot_size.clear()
    assert hostinput.click(100, 200) is True
    assert moved[-1] == (100, 200)


def test_hostinput_prefers_framebuffer_pixels(monkeypatch):
    moved: list[tuple[int, int]] = []
    monkeypatch.setattr(hostinput, "display_size", lambda machine=None: (1920, 1080))
    monkeypatch.setattr(hostinput, "_pointer_window", lambda machine=None: None)
    monkeypatch.setattr(hostinput._session, "move_to", lambda x, y: moved.append((x, y)) or True)
    assert hostinput.move(0.0, 0.0, px=640, py=360) is True
    assert moved == [(640, 360)]


def test_session_skips_motion_when_already_there():
    """Chromium crbug.com/138075: motion between down and up is a drag."""
    writes: list[str] = []
    session = hostinput._Session()
    session._write = lambda line: writes.append(line) or True  # type: ignore[method-assign]
    session._last = None
    assert session.move_to(100, 200) is True
    assert session.move_to(100, 200) is True
    assert writes == ["mousemove 100 200"]
    assert session.move_to(101, 200) is True
    assert writes == ["mousemove 100 200", "mousemove 101 200"]


def test_session_write_detects_instantly_dead_process():
    """A doomed session (stopped container, dead engine, missing xdotool)
    exits right after accepting one buffered line. That must surface as
    failure — historically every event returned True into the void."""
    session = hostinput._Session(argv=("sh", "-c", "exit 0"))
    try:
        assert session.send("mousemove", "1", "2") is False
    finally:
        session.close()


def test_session_write_keeps_healthy_process():
    session = hostinput._Session(argv=("cat",))
    try:
        assert session.send("mousemove", "1", "2") is True
        assert session.send("mousedown", "1") is True
    finally:
        session.close()


def test_stream_frames_falls_back_to_capture_when_pipe_dies():
    """A machine stream whose ffmpeg dies (or never starts) must degrade to
    periodic screenshots, not freeze the view on the last frame."""
    from harness.screen import ScreenSource, stream_frames

    stop = threading.Event()
    source = ScreenSource(
        stream_argv=lambda fps: ["sh", "-c", "exit 0"],  # instant EOF, no frames
        capture=lambda: b"PNGDATA",
    )
    frames = stream_frames(5, stop, source=source)
    frame, mime = next(iter(frames))
    stop.set()
    assert frame == b"PNGDATA"
    assert mime == "image/png"


def test_mjpeg_pipe_stops_promptly_while_idle():
    """`stop` must win even when the pipe produces nothing (wedged ffmpeg)."""
    from harness.screen import _mjpeg_pipe

    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    start = time.monotonic()
    frames = list(_mjpeg_pipe(["sleep", "30"], stop))
    assert frames == []
    assert time.monotonic() - start < 5


def test_pointer_window_parses_shell_output(monkeypatch):
    monkeypatch.setattr(
        hostinput.subprocess,
        "run",
        lambda *a, **k: type(
            "R", (), {"returncode": 0, "stdout": b"X=10\nY=20\nSCREEN=0\nWINDOW=4242\n"}
        )(),
    )
    assert hostinput._pointer_window() == "4242"
    monkeypatch.setattr(
        hostinput.subprocess,
        "run",
        lambda *a, **k: type(
            "R", (), {"returncode": 0, "stdout": b"X=0\nY=0\nSCREEN=0\nWINDOW=0\n"}
        )(),
    )
    assert hostinput._pointer_window() is None


def test_pointer_window_uses_client_inside_openbox_frame(monkeypatch):
    """Openbox can report its frame under the pointer instead of Chrome."""
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        output = (
            b"X=640\nY=496\nWINDOW=6292298\n"
            if argv[0] == "xdotool"
            else b'''  56 children:\n     0x800004 "Google - Google Chrome": ("google-chrome" "Google-chrome") 1280x724+0+0\n     0x600396 (has no name): () 1x1+0+0\n'''
        )
        return type("R", (), {"returncode": 0, "stdout": output})()

    monkeypatch.setattr(hostinput.subprocess, "run", run)
    assert hostinput._pointer_window() == str(0x800004)
    assert calls[1] == ["xwininfo", "-id", "6292298", "-children"]

    def root_run(argv, **kwargs):
        output = (
            b"X=640\nY=496\nWINDOW=543\n"
            if argv[0] == "xdotool"
            else b"Root window id: 0x21f\nParent window id: 0x0 (none)\n"
        )
        return type("R", (), {"returncode": 0, "stdout": output})()

    monkeypatch.setattr(hostinput.subprocess, "run", root_run)
    assert hostinput._pointer_window() is None


def test_type_and_click_focus_window_under_pointer(monkeypatch):
    """XTEST clicks do not give Chrome keyboard focus; we must activate."""
    sent: list[tuple] = []
    hostinput._session._last = None
    hostinput._focused_wid.clear()
    monkeypatch.setattr(hostinput, "display_size", lambda machine=None: (1000, 800))
    monkeypatch.setattr(hostinput, "_pointer_window", lambda machine=None: "99")
    monkeypatch.setattr(hostinput, "_clip_tool", lambda machine=None: None)
    monkeypatch.setattr(
        hostinput._session, "send", lambda *parts: sent.append(("send", *parts)) or True
    )
    monkeypatch.setattr(
        hostinput._session, "move_to", lambda x, y: sent.append(("move", x, y)) or True
    )

    assert hostinput.click(0.5, 0.5) is True
    assert ("send", "windowactivate", "99") in sent
    assert ("send", "windowactivate", "--sync", "99") not in sent
    assert sent[-1] == ("send", "sleep", "0.03", "click", "1")

    sent.clear()
    hostinput._focused_wid.clear()
    assert hostinput.type_text("a") is True
    assert sent[0] == ("send", "windowactivate", "99")
    assert sent[1] == ("send", "windowfocus", "99")
    assert sent[-1] == ("send", "type", "--clearmodifiers", "--", "a")

    sent.clear()
    assert hostinput.type_text("b") is True
    assert ("send", "windowactivate", "99") not in sent
    assert sent[-1] == ("send", "type", "--clearmodifiers", "--", "b")

    sent.clear()
    hostinput._focused_wid.clear()
    hostinput._dock_cache.clear()
    monkeypatch.setattr(hostinput, "_dock_rects", lambda machine=None: [])
    assert hostinput.down(0.5, 0.5) is True
    assert ("send", "windowactivate", "99") in sent
    assert ("send", "mousedown", "1") in sent


def test_type_text_pastes_multichar_when_clipboard_exists(monkeypatch):
    sent: list[tuple] = []
    wrote: list[str] = []
    monkeypatch.setattr(hostinput, "_pointer_window", lambda machine=None: None)
    monkeypatch.setattr(hostinput, "_clip_tool", lambda machine=None: ("xclip", "xclip"))
    monkeypatch.setattr(
        hostinput, "clipboard_write", lambda t, machine=None: wrote.append(t) or True
    )
    monkeypatch.setattr(
        hostinput._session, "send", lambda *parts: sent.append(("send", *parts)) or True
    )

    assert hostinput.type_text("alex@example.com") is True
    assert wrote == ["alex@example.com"]
    assert sent[-1] == ("send", "key", "--clearmodifiers", "ctrl+v")


def test_dispatch_routes_actions(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        hostinput,
        "move",
        lambda x, y, px=None, py=None, machine=None: (
            seen.setdefault("move", (x, y, px, py)) or True
        ),
    )
    monkeypatch.setattr(
        hostinput, "type_text", lambda t, machine=None: seen.setdefault("type", t) or True
    )
    monkeypatch.setattr(
        hostinput,
        "down",
        lambda x, y, button=1, px=None, py=None, machine=None: (
            seen.setdefault("down", (x, y, button)) or True
        ),
    )
    monkeypatch.setattr(
        hostinput,
        "up",
        lambda x, y, button=1, px=None, py=None, machine=None: (
            seen.setdefault("up", (x, y, button)) or True
        ),
    )
    assert hostinput.dispatch({"action": "move", "x": 0.3, "y": 0.4})
    assert hostinput.dispatch({"action": "type", "text": "abc"})
    assert hostinput.dispatch({"action": "down", "x": 0.1, "y": 0.2, "button": 1})
    assert hostinput.dispatch({"action": "up", "x": 0.1, "y": 0.2, "button": 1})
    assert seen["move"] == (0.3, 0.4, None, None)
    assert seen["type"] == "abc"
    assert seen["down"] == (0.1, 0.2, 1)
    assert seen["up"] == (0.1, 0.2, 1)
    seen.clear()
    assert hostinput.dispatch({"action": "move", "x": 0, "y": 0, "px": 10, "py": 20})
    assert seen["move"] == (0.0, 0.0, 10, 20)


def test_dispatch_paste(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        hostinput, "paste_to_display", lambda t, machine=None: seen.setdefault("paste", t) or True
    )
    assert hostinput.dispatch({"action": "paste", "text": "hello"})
    assert seen["paste"] == "hello"


def test_copy_from_display_requires_clipboard_tool(monkeypatch):
    monkeypatch.setattr(
        hostinput.shutil, "which", lambda n: "/usr/bin/xdotool" if n == "xdotool" else None
    )
    assert hostinput.copy_from_display() is None


def test_paste_falls_back_to_type(monkeypatch):
    typed: list[str] = []
    monkeypatch.setattr(hostinput, "clipboard_write", lambda _t, machine=None: False)
    monkeypatch.setattr(hostinput, "type_text", lambda t, machine=None: typed.append(t) or True)
    assert hostinput.paste_to_display("abc") is True
    assert typed == ["abc"]


# -- live server + WS client ---------------------------------------------
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


class WSClient:
    def __init__(self, host, port):
        self.sock = socket.create_connection((host, port), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                f"GET /ws HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        self.rfile = self.sock.makefile("rb")
        assert b"101" in self.rfile.readline()
        while self.rfile.readline() not in (b"\r\n", b"\n", b""):
            pass
        # The server greets every connection with a version hello, then a
        # state snapshot (reseed) closed by a `ready` frame.
        self.hello = json.loads(self.recv_raw()[1].decode())
        assert self.hello["type"] == "hello", self.hello
        while True:
            frame = json.loads(self.recv_raw()[1].decode())
            if frame.get("type") == "ready":
                break

    def send(self, obj):
        self.sock.sendall(_client_frame(json.dumps(obj).encode()))

    def recv_raw(self):
        return wsproto.read_frame(self.rfile)  # (opcode, payload) | None

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


def test_input_does_not_require_takeover(server, monkeypatch):
    """Shared computer: pointer events are accepted without Take control."""
    host, port, orch = server
    monkeypatch.setattr("harness.hostinput.dispatch", lambda msg, machine=None: True)
    c = WSClient(host, port)
    try:
        c.send({"type": "input", "bot": "atlas", "action": "move", "x": 0.5, "y": 0.5})
        c.sock.settimeout(0.4)
        extra = None
        try:
            extra = c.recv_raw()
        except (TimeoutError, OSError):
            extra = None
        if extra is not None:
            _op, payload = extra
            msg = json.loads(payload.decode())
            assert msg.get("type") != "input_rejected"
            assert "take over" not in str(msg).lower()
    finally:
        c.close()


@pytest.mark.parametrize("button", [1, 3])
def test_ws_pointer_press_and_release_preserve_button_on_bot_machine(server, monkeypatch, button):
    host, port, _ = server
    machine = "harness-machine-secondary-click"
    session = hostinput._Session()
    sent = []
    released = threading.Event()

    def send(*parts):
        sent.append(parts)
        if parts[0] == "mouseup":
            released.set()
        return True

    monkeypatch.setattr("harness.machine_view.machine_for_bot", lambda paths, bot: machine)
    monkeypatch.setattr(hostinput, "_machine_sessions", {machine: session})
    monkeypatch.setattr(hostinput, "display_size", lambda machine=None: (1000, 800))
    monkeypatch.setattr(hostinput, "_dock_rects", lambda machine=None: [])
    monkeypatch.setattr(hostinput, "_pointer_window", lambda machine=None: None)
    monkeypatch.setattr(session, "move_to", lambda x, y: sent.append(("move", x, y)) or True)
    monkeypatch.setattr(session, "send", send)
    c = WSClient(host, port)
    try:
        for action in ("down", "up"):
            c.send(
                {
                    "type": "input",
                    "bot": "atlas",
                    "action": action,
                    "x": 0.5,
                    "y": 0.5,
                    "px": 500,
                    "py": 400,
                    "button": button,
                }
            )
        assert released.wait(2), "WebSocket input never released the remote button"
        assert sent == [
            ("move", 500, 400),
            ("mousedown", str(button)),
            ("move", 500, 400),
            ("mouseup", str(button)),
        ]
    finally:
        c.close()


def test_input_rejection_names_the_machine(server, monkeypatch):
    """A failed machine dispatch must say WHICH machine refused input, not
    blame a missing host tool — the user needs to know their bot's computer
    is the problem."""
    host, port, orch = server
    monkeypatch.setattr(
        "harness.machine_view.machine_for_bot", lambda paths, bot: "harness-machine-7"
    )
    monkeypatch.setattr("harness.hostinput.dispatch", lambda msg, machine=None: False)
    c = WSClient(host, port)
    try:
        c.send({"type": "input", "bot": "atlas", "action": "down", "x": 0.5, "y": 0.5})
        op, payload = c.recv_raw()
        msg = json.loads(payload.decode())
        assert msg["type"] == "input_rejected"
        assert "harness-machine-7" in msg["reason"]
    finally:
        c.close()


def test_live_frames_stream_over_ws(server, monkeypatch):
    host, port, _ = server
    # deterministic frame source (no real display needed)
    os.environ["HARNESS_SCREENSHOT_CMD"] = "printf PNGDATA"
    try:
        c = WSClient(host, port)
        try:
            c.send({"type": "screen_start", "fps": 5})
            got_binary = None
            for _ in range(30):
                frame = c.recv_raw()
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == wsproto.OP_BINARY:
                    got_binary = payload
                    break
            assert got_binary == b"PNGDATA"
            c.send({"type": "screen_stop"})
        finally:
            c.close()
    finally:
        del os.environ["HARNESS_SCREENSHOT_CMD"]
