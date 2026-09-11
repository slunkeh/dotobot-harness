"""Fake Chrome debugger: AX snapshot and node click without a real browser."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import subprocess
import sys
import threading
import time

import pytest

from agent.computer import HostComputer
from agent.memory import Memory
from agent.tools import ToolContext, default_tools
from harness import cdp
from harness.control import Control
from harness.paths import HarnessPaths


@pytest.fixture(autouse=True)
def _opt_in_to_debugging(monkeypatch):
    monkeypatch.setenv("HARNESS_CHROME_CDP", "1")


_AX_NODES = [
    {
        "nodeId": 1,
        "backendDOMNodeId": 101,
        "ignored": False,
        "role": {"value": "RootWebArea"},
        "name": {"value": "Webflow Designer"},
        "childIds": [2, 3],
    },
    {
        "nodeId": 2,
        "backendDOMNodeId": 102,
        "ignored": False,
        "role": {"value": "button"},
        "name": {"value": "Publish"},
        "childIds": [],
    },
    {
        "nodeId": 3,
        "backendDOMNodeId": 103,
        "ignored": True,
        "role": {"value": "generic"},
        "name": {"value": ""},
        "childIds": [],
    },
    {
        "nodeId": 4,
        "backendDOMNodeId": 104,
        "ignored": False,
        "role": {"value": "generic"},
        "name": {"value": ""},
        "childIds": [],
    },
]


class FakeChrome:
    def __init__(self, nodes: list | None = None) -> None:
        self.calls: list[str] = []
        self.box_ids: list[int] = []
        self.nodes = list(nodes) if nodes is not None else list(_AX_NODES)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.port = 0
        self._srv: socket.socket | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(2)

    def stop(self) -> None:
        self._stop.set()
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
        self._thread.join(timeout=2)

    def _run(self) -> None:
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        self.port = srv.getsockname()[1]
        srv.listen(8)
        srv.settimeout(0.2)
        self._srv = srv
        self._ready.set()
        while not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(2)
        try:
            req = _read_http(conn)
            first = req.split("\r\n", 1)[0]
            if "Upgrade: websocket" in req or "upgrade: websocket" in req.lower():
                key = ""
                for line in req.split("\r\n"):
                    if line.lower().startswith("sec-websocket-key:"):
                        key = line.split(":", 1)[1].strip()
                accept = base64.b64encode(
                    hashlib.sha1(
                        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
                    ).digest()
                ).decode("ascii")
                conn.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n"
                        "\r\n"
                    ).encode("ascii")
                )
                self._cdp_loop(conn)
                return
            if first.startswith("GET /json/version"):
                _http_json(
                    conn,
                    {"Browser": "fake", "webSocketDebuggerUrl": self._browser_ws()},
                )
                return
            if first.startswith("GET /json"):
                _http_json(
                    conn,
                    [
                        {
                            "description": "",
                            "id": "1",
                            "title": "Webflow Designer",
                            "type": "page",
                            "url": "https://webflow.com/design/x",
                            "webSocketDebuggerUrl": self._ws(),
                        }
                    ],
                )
                return
            _http_json(conn, {}, status="404 Not Found")
        except (OSError, TimeoutError, json.JSONDecodeError, struct.error):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _ws(self) -> str:
        return f"ws://127.0.0.1:{self.port}/devtools/page/1"

    def _browser_ws(self) -> str:
        return f"ws://127.0.0.1:{self.port}/devtools/browser/fake"

    def _cdp_loop(self, conn: socket.socket) -> None:
        while not self._stop.is_set():
            msg = _ws_recv(conn)
            if msg is None:
                return
            method = str(msg.get("method") or "")
            self.calls.append(method)
            mid = msg.get("id")
            result: dict = {}
            if method == "Accessibility.getFullAXTree":
                result = {"nodes": self.nodes}
            elif method == "DOM.getBoxModel":
                params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
                try:
                    self.box_ids.append(int(params.get("backendNodeId")))
                except (TypeError, ValueError):
                    pass
                result = {"model": {"content": [10.0, 20.0, 50.0, 20.0, 50.0, 40.0, 10.0, 40.0]}}
            _ws_send(conn, {"id": mid, "result": result})


def _read_http(conn: socket.socket) -> str:
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(1)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > 16384:
            break
    return bytes(buf).decode("iso-8859-1", "replace")


def _http_json(conn: socket.socket, body: object, *, status: str = "200 OK") -> None:
    raw = json.dumps(body).encode("utf-8")
    conn.sendall(
        f"HTTP/1.1 {status}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(raw)}\r\n"
        "Connection: close\r\n"
        "\r\n".encode("ascii")
        + raw
    )


def _ws_send(conn: socket.socket, obj: dict) -> None:
    payload = json.dumps(obj).encode("utf-8")
    n = len(payload)
    if n < 126:
        header = bytes([0x81, n])
    else:
        header = bytes([0x81, 126]) + struct.pack("!H", n)
    conn.sendall(header + payload)


def _ws_recv(conn: socket.socket) -> dict | None:
    hdr = _recvall(conn, 2)
    if hdr is None or len(hdr) < 2:
        return None
    n = hdr[1] & 0x7F
    masked = bool(hdr[1] & 0x80)
    if n == 126:
        ext = _recvall(conn, 2)
        if ext is None:
            return None
        n = struct.unpack("!H", ext)[0]
    elif n == 127:
        ext = _recvall(conn, 8)
        if ext is None:
            return None
        n = struct.unpack("!Q", ext)[0]
    mask = _recvall(conn, 4) if masked else b""
    if masked and mask is None:
        return None
    data = _recvall(conn, n) if n else b""
    if data is None:
        return None
    if masked and mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return json.loads(data.decode("utf-8"))


def _recvall(conn: socket.socket, n: int) -> bytes | None:
    out = bytearray()
    while len(out) < n:
        chunk = conn.recv(n - len(out))
        if not chunk:
            return None
        out.extend(chunk)
    return bytes(out)


def _profile(tmp_path, port: int):
    profile = tmp_path / "chrome-profile"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/fake\n")
    return profile


def test_snapshot_none_without_port_file(tmp_path):
    assert cdp.snapshot(user_data_dir=str(tmp_path / "missing")) is None


def test_snapshot_none_without_browser_path(tmp_path):
    fake = FakeChrome()
    fake.start()
    try:
        profile = tmp_path / "chrome-profile"
        profile.mkdir()
        (profile / "DevToolsActivePort").write_text(f"{fake.port}\n")
        assert cdp.snapshot(user_data_dir=str(profile)) is None
    finally:
        fake.stop()


def test_snapshot_none_when_debugger_is_another_browser(tmp_path):
    fake = FakeChrome()
    fake.start()
    try:
        profile = tmp_path / "chrome-profile"
        profile.mkdir()
        (profile / "DevToolsActivePort").write_text(
            f"{fake.port}\n/devtools/browser/other-instance\n"
        )
        assert cdp.snapshot(user_data_dir=str(profile)) is None
    finally:
        fake.stop()


def test_snapshot_none_when_listener_has_other_user_data_dir(tmp_path, monkeypatch):
    fake = FakeChrome()
    fake.start()
    try:
        profile = _profile(tmp_path, fake.port)
        monkeypatch.setattr(
            cdp,
            "_listener_user_data_dirs",
            lambda port: [str(tmp_path / "someone-else")],
        )
        assert cdp.snapshot(user_data_dir=str(profile)) is None
    finally:
        fake.stop()


def test_listener_user_data_dirs_reads_listening_cmdline(tmp_path):
    profile = tmp_path / "owned-profile"
    profile.mkdir()
    script = (
        "import socket, sys, time\n"
        "s = socket.socket()\n"
        "s.bind(('127.0.0.1', 0))\n"
        "print(s.getsockname()[1], flush=True)\n"
        "s.listen()\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script, f"--user-data-dir={profile}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert proc.stdout is not None
        port = int(proc.stdout.readline().strip())
        until = time.monotonic() + 2
        dirs: list[str] = []
        while time.monotonic() < until:
            dirs = cdp._listener_user_data_dirs(port)
            if dirs:
                break
            time.sleep(0.05)
        assert any(cdp._norm_profile(d) == cdp._norm_profile(str(profile)) for d in dirs)
        assert cdp._owns_user_data_dir(None, port, str(tmp_path / "other")) is False
        assert cdp._owns_user_data_dir(None, port, str(profile)) is True
    finally:
        proc.kill()
        proc.wait(timeout=2)


def test_snapshot_ok_when_listener_advertises_this_profile(tmp_path, monkeypatch):
    fake = FakeChrome()
    fake.start()
    try:
        profile = _profile(tmp_path, fake.port)
        monkeypatch.setattr(cdp, "_listener_user_data_dirs", lambda port: [str(profile)])
        text = cdp.snapshot(user_data_dir=str(profile))
    finally:
        fake.stop()
    assert text is not None
    assert 'button "Publish" #2' in text


def test_snapshot_none_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_CHROME_CDP", "0")
    fake = FakeChrome()
    fake.start()
    try:
        profile = _profile(tmp_path, fake.port)
        assert cdp.snapshot(user_data_dir=str(profile)) is None
    finally:
        fake.stop()


def test_snapshot_rejects_foreign_chrome(tmp_path):
    """A live port whose browser uuid differs from our DevToolsActivePort is
    another Chrome (stale file, another bot) — never attach."""
    fake = FakeChrome()
    fake.start()
    try:
        profile = tmp_path / "chrome-profile"
        profile.mkdir()
        (profile / "DevToolsActivePort").write_text(
            f"{fake.port}\n/devtools/browser/someone-else\n"
        )
        assert cdp.snapshot(user_data_dir=str(profile)) is None
        assert cdp.click_node("2", user_data_dir=str(profile)) is False
    finally:
        fake.stop()


def test_snapshot_lists_interactive_nodes(tmp_path):
    fake = FakeChrome()
    fake.start()
    try:
        profile = _profile(tmp_path, fake.port)
        text = cdp.snapshot(user_data_dir=str(profile))
    finally:
        fake.stop()
    assert text is not None
    assert "Webflow Designer" in text
    assert 'button "Publish" #2' in text
    assert "computer_click node=" in text
    assert "generic" not in text  # unnamed generic skipped


def test_click_node_dispatches_mouse_events(tmp_path):
    fake = FakeChrome()
    fake.start()
    try:
        profile = _profile(tmp_path, fake.port)
        assert cdp.click_node("2", user_data_dir=str(profile)) is True
    finally:
        fake.stop()
    assert "Input.dispatchMouseEvent" in fake.calls
    assert fake.calls.count("Input.dispatchMouseEvent") == 2


def test_click_node_unknown_falls_open(tmp_path):
    fake = FakeChrome()
    fake.start()
    try:
        profile = _profile(tmp_path, fake.port)
        assert cdp.click_node("999", user_data_dir=str(profile)) is False
    finally:
        fake.stop()


def test_backend_id_prefers_ax_node_over_overlapping_backend():
    # Real Chrome: AX nodeId 4 and some other node's backendDOMNodeId 4
    # coexist. Snapshot prints #4; click must hit the AX node, not the
    # first backendDOMNodeId match (an ignored wrapper here).
    nodes = [
        {"nodeId": 1, "backendDOMNodeId": 4, "ignored": True},
        {"nodeId": 4, "backendDOMNodeId": 200, "ignored": False},
        {"nodeId": 5, "backendDOMNodeId": 201, "ignored": False},
    ]
    assert cdp._backend_id(nodes, 4) == 200


def test_backend_id_does_not_steal_via_nonignored_backend_overlap():
    nodes = [
        {"nodeId": 1, "backendDOMNodeId": 2, "ignored": False},
        {"nodeId": 2, "backendDOMNodeId": 200, "ignored": False},
    ]
    assert cdp._backend_id(nodes, 2) == 200


def test_backend_id_ignored_ax_id_does_not_fall_through_to_backend():
    nodes = [
        {"nodeId": 7, "backendDOMNodeId": 300, "ignored": True},
        {"nodeId": 8, "backendDOMNodeId": 7, "ignored": False},
    ]
    assert cdp._backend_id(nodes, 7) is None


def test_backend_id_falls_back_to_backend_dom_id():
    nodes = [
        {"nodeId": 1, "backendDOMNodeId": 101, "ignored": False},
        {"nodeId": 2, "backendDOMNodeId": 102, "ignored": True},
    ]
    assert cdp._backend_id(nodes, 101) == 101
    assert cdp._backend_id(nodes, 102) is None


def test_click_node_uses_ax_backend_when_ids_overlap(tmp_path):
    nodes = [
        {
            "nodeId": 1,
            "backendDOMNodeId": 4,
            "ignored": True,
            "role": {"value": "generic"},
            "name": {"value": ""},
        },
        {
            "nodeId": 4,
            "backendDOMNodeId": 200,
            "ignored": False,
            "role": {"value": "button"},
            "name": {"value": "Publish"},
        },
    ]
    fake = FakeChrome(nodes)
    fake.start()
    try:
        profile = _profile(tmp_path, fake.port)
        assert cdp.click_node("4", user_data_dir=str(profile)) is True
    finally:
        fake.stop()
    assert fake.box_ids == [200]


def test_host_click_prefers_node_then_falls_back_to_xy(tmp_path, monkeypatch):
    clicks: list[tuple] = []
    monkeypatch.setattr("harness.cdp.click_node", lambda *a, **k: True)
    monkeypatch.setattr("harness.hostinput.click", lambda *a, **k: clicks.append((a, k)) or True)
    monkeypatch.setattr("harness.hostinput._pos", lambda *a, **k: (1, 2))
    out = HostComputer(paths=HarnessPaths(tmp_path), bot="atlas").act(
        "click", node="2", x=0.5, y=0.5
    )
    assert out == "ok: click node 2"
    assert clicks == []

    monkeypatch.setattr("harness.cdp.click_node", lambda *a, **k: False)
    out = HostComputer().act("click", node="2", x=0.4, y=0.6)
    assert out.startswith("ok: click")
    assert clicks


def test_host_click_node_only_errors_when_cdp_misses(monkeypatch):
    monkeypatch.setattr("harness.cdp.click_node", lambda *a, **k: False)
    monkeypatch.setattr(
        "harness.hostinput.click", lambda *a, **k: (_ for _ in ()).throw(AssertionError("xy"))
    )
    out = HostComputer().act("click", node="2")
    assert out.startswith("error: chrome node click failed")


def test_host_click_without_node_or_xy_errors(monkeypatch):
    """Schema no longer forces x,y (node-only calls are legal), so a bare
    click must error instead of hitting the 0.5,0.5 screen center."""
    monkeypatch.setattr(
        "harness.hostinput.click", lambda *a, **k: (_ for _ in ()).throw(AssertionError("xy"))
    )
    out = HostComputer().act("click")
    assert out.startswith("error: computer_click needs x,y")


def test_screenshot_tool_appends_tree_when_present(tmp_path):
    class FakeComputer:
        def screenshot(self):
            return (b"\xff\xd8fakejpeg", "image/jpeg")

        def chrome_snapshot(self):
            return 'Chrome AX (t):\n  button "Publish" #2\nClick computer_click node=<id>'

    paths = HarnessPaths(tmp_path)
    paths.ensure_layout(["atlas"])
    ctx = ToolContext(
        paths=paths,
        bot="atlas",
        memory=Memory(paths=paths, bot="atlas"),
        computer=FakeComputer(),
        control=Control(paths),
    )
    out = default_tools()["computer_screenshot"].handler(ctx, {})
    assert out.startswith("ok:")
    assert 'button "Publish" #2' in out
    assert ctx.images == [("image/jpeg", b"\xff\xd8fakejpeg")]


def test_screenshot_tool_unchanged_without_tree(tmp_path):
    class FakeComputer:
        def screenshot(self):
            return (b"\xff\xd8fakejpeg", "image/jpeg")

    paths = HarnessPaths(tmp_path)
    paths.ensure_layout(["atlas"])
    ctx = ToolContext(
        paths=paths,
        bot="atlas",
        memory=Memory(paths=paths, bot="atlas"),
        computer=FakeComputer(),
    )
    out = default_tools()["computer_screenshot"].handler(ctx, {})
    assert out.startswith("ok: screenshot attached")
    assert "Chrome AX" not in out
    assert "node=" not in out
