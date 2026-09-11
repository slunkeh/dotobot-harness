"""Per-machine screen + takeover input routing (server) and takeover parity."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
import urllib.error
import urllib.request

import pytest

from harness import hostinput, machine_view
from harness.orchestrator import Orchestrator
from harness.server import make_server
from tests.test_ws import WSClient

ROSTER = '[[bots]]\nname = "atlas"\nprovider = "echo"\n'


@pytest.fixture
def server(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    httpd = make_server(orch, "127.0.0.1", 0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield ("127.0.0.1", port, orch)
    finally:
        httpd.shutdown()


def _assign_machine(orch, bot="atlas", machine="harness-machine-0"):
    orch.paths.run_file(bot).write_text(
        json.dumps({"bot": bot, "backend": "machines", "pid": 1, "machine": machine}),
        encoding="utf-8",
    )


def test_screen_endpoint_routes_to_bot_machine(server, monkeypatch):
    host, port, orch = server
    _assign_machine(orch)
    monkeypatch.setattr("harness.machine_view.capture_png", lambda machine: b"MACHINE-PNG")
    with urllib.request.urlopen(f"http://{host}:{port}/api/screen/atlas", timeout=5) as r:
        assert r.read() == b"MACHINE-PNG"


def test_screen_endpoint_names_machine_on_failure(server, monkeypatch):
    host, port, orch = server
    _assign_machine(orch)
    monkeypatch.setattr("harness.machine_view.capture_png", lambda machine: None)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"http://{host}:{port}/api/screen/atlas", timeout=5)
    assert exc.value.code == 503
    assert "harness-machine-0" in exc.value.read().decode()


def test_screen_endpoint_keeps_host_display_for_process_bots(server, monkeypatch):
    host, port, _orch = server
    monkeypatch.setenv("HARNESS_SCREENSHOT_CMD", "printf HOSTPNG")
    with urllib.request.urlopen(f"http://{host}:{port}/api/screen/atlas", timeout=5) as r:
        assert r.read() == b"HOSTPNG"


def test_ws_screen_start_selects_machine_source(server, monkeypatch):
    host, port, orch = server
    _assign_machine(orch)
    recorded = {}

    def fake_stream(fps, stop, *, source=None):
        recorded["fps"] = fps
        recorded["source"] = source
        return iter(())

    monkeypatch.setattr("harness.server.stream_frames", fake_stream)
    monkeypatch.setattr("harness.machine_view.screen_source", lambda machine: f"src:{machine}")

    c = WSClient(host, port)
    try:
        c.send({"type": "screen_start", "bot": "atlas", "fps": 7})
        reply = c.recv()
        assert reply["type"] == "screen_started"
        assert reply["input"] is True
        assert reply["epoch"] and reply["seq"]  # fenced like every frame
        deadline = time.time() + 5
        while "source" not in recorded and time.time() < deadline:
            time.sleep(0.02)
        assert recorded["source"] == "src:harness-machine-0"
        assert recorded["fps"] == 7
    finally:
        c.close()


def test_ws_screen_start_restarts_on_new_bot(server, monkeypatch):
    host, port, orch = server
    _assign_machine(orch, bot="atlas", machine="harness-machine-0")
    orch.paths.run_file("nova").write_text(
        json.dumps(
            {"bot": "nova", "backend": "machines", "pid": 2, "machine": "harness-machine-1"}
        ),
        encoding="utf-8",
    )
    sources: list[str] = []

    def fake_stream(fps, stop, *, source=None):
        sources.append(source)
        while not stop.is_set():
            time.sleep(0.02)
            yield from ()

    monkeypatch.setattr("harness.server.stream_frames", fake_stream)
    monkeypatch.setattr("harness.machine_view.screen_source", lambda machine: f"src:{machine}")

    c = WSClient(host, port)
    try:
        c.send({"type": "screen_start", "bot": "atlas", "fps": 5})
        assert c.recv().get("type") == "screen_started"
        deadline = time.time() + 5
        while len(sources) < 1 and time.time() < deadline:
            time.sleep(0.02)
        c.send({"type": "screen_start", "bot": "nova", "fps": 5})
        assert c.recv().get("type") == "screen_started"
        deadline = time.time() + 5
        while len(sources) < 2 and time.time() < deadline:
            time.sleep(0.02)
        assert sources[-1] == "src:harness-machine-1"
    finally:
        c.close()


def test_ws_input_routes_into_machine(server, monkeypatch):
    host, port, orch = server
    _assign_machine(orch)
    orch.control.take_over("atlas")
    seen: list[tuple[dict, str | None]] = []
    monkeypatch.setattr(
        "harness.hostinput.dispatch", lambda msg, machine=None: seen.append((msg, machine)) or True
    )
    c = WSClient(host, port)
    try:
        c.send({"type": "input", "bot": "atlas", "action": "click", "x": 0.5, "y": 0.5})
        c.send({"type": "ping"})
        assert c.recv()["type"] == "pong"  # input succeeded silently
        assert seen and seen[0][1] == "harness-machine-0"
    finally:
        c.close()
        orch.control.return_control("atlas")


# -- takeover parity: identical xdotool streams, host vs machine ------------

FAKE_XDOTOOL = """#!/bin/sh
if [ "$1" = "getdisplaygeometry" ]; then echo "1280 800"; exit 0; fi
if [ "$1" = "getmouselocation" ]; then echo "X=1\nY=1\nSCREEN=0\nWINDOW=0"; exit 0; fi
cat >> "{log}"
"""

FAKE_DOCKER_INPUT = """#!/bin/sh
for a in "$@"; do
  if [ "$a" = "getdisplaygeometry" ]; then echo "1280 800"; exit 0; fi
  if [ "$a" = "getmouselocation" ]; then echo "X=1\nY=1\nSCREEN=0\nWINDOW=0"; exit 0; fi
done
cat >> "{log}"
"""

PARITY_EVENTS = [
    {"action": "down", "x": 0.25, "y": 0.5, "button": 1},
    {"action": "move", "x": 0.5, "y": 0.5},
    {"action": "move", "x": 0.5, "y": 0.5},  # duplicate: must be skipped (drag fix)
    {"action": "up", "x": 0.5, "y": 0.5, "button": 1},
    {"action": "scroll", "amount": 2},
    {"action": "key", "key": "ctrl+l"},
    {"action": "type", "text": "hello world"},
    {"action": "click", "x": 0.0, "y": 0.0, "px": 100, "py": 120},
]


def test_takeover_command_stream_identical_on_machine(tmp_path, monkeypatch):
    """The exact xdotool protocol the host takeover produces must reach a
    machine display unchanged — same commands, same order, same drag-motion
    skip — only the transport (docker exec) differs."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    host_log = tmp_path / "host.log"
    machine_log = tmp_path / "machine.log"
    for name, script, log in (
        ("xdotool", FAKE_XDOTOOL, host_log),
        ("docker", FAKE_DOCKER_INPUT, machine_log),
    ):
        path = bindir / name
        path.write_text(script.format(log=log), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.delenv("HARNESS_CONTAINER_ENGINE", raising=False)

    host_session = hostinput._Session()
    machine_session = hostinput._Session(tuple(machine_view.input_argv("harness-machine-0")))
    monkeypatch.setattr(hostinput, "_session", host_session)
    monkeypatch.setattr(hostinput, "_machine_sessions", {"harness-machine-0": machine_session})
    monkeypatch.setattr(hostinput, "_size_cache", None)
    monkeypatch.setattr(hostinput, "_clip_tool", lambda machine=None: None)
    machine_view._geom_cache.clear()

    for event in PARITY_EVENTS:
        assert hostinput.dispatch(dict(event)) is True
    for event in PARITY_EVENTS:
        assert hostinput.dispatch(dict(event), machine="harness-machine-0") is True

    host_session.close()
    machine_session.close()
    deadline = time.time() + 5
    while time.time() < deadline:
        if host_log.exists() and machine_log.exists():
            host_cmds = host_log.read_text(encoding="utf-8")
            if host_cmds and host_cmds == machine_log.read_text(encoding="utf-8"):
                break
        time.sleep(0.05)

    host_cmds = host_log.read_text(encoding="utf-8").splitlines()
    machine_cmds = machine_log.read_text(encoding="utf-8").splitlines()
    assert host_cmds == machine_cmds
    # spot-check the semantics the takeover UX depends on
    assert "mousedown 1" in host_cmds
    assert host_cmds.count("mousemove 640 400") == 1  # crbug drag fix preserved
    assert "mouseup 1" in host_cmds
    assert "click --repeat 2 5" in host_cmds
    assert "key --clearmodifiers ctrl+l" in host_cmds
    assert "type --clearmodifiers -- hello world" in host_cmds
    assert "mousemove 100 120" in host_cmds  # pixel coords honored
