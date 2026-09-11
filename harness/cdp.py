"""Chrome DevTools Protocol client for computer use.

Screenshot + xdotool stay the required path. When the bot's Chrome was
started with the loopback debug port, this module can:

* dump a compact accessibility tree next to a screenshot
* click an AX node by id (Designer chrome, not canvas pixels)

If Chrome is down, the port file is missing, or CDP errors, every public
entry returns None / False and the caller falls back to x,y. Stdlib only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import select
import socket
import struct
import subprocess
import threading
import time
import urllib.request
from typing import Any, Protocol

_TIMEOUT = 5.0
# getFullAXTree on a heavy SPA (Webflow Designer is a demanding use case) can
# be tens of MB of JSON. Too small a cap silently disables the feature on
# exactly the pages it exists for, so err high; the frame is short-lived.
_WS_MAX = 64 * 1024 * 1024
_SNAPSHOT_CHARS = 4000
_SNAPSHOT_NODES = 180

#: Discovery (port file, /json/version, profile ownership) is re-verified at
#: most this often per profile. On the machines backend every one of those
#: is a `docker exec`, so re-running discovery on each screenshot and each
#: node click was most of the tree's cost. Any CDP failure drops the entry.
_PORT_TTL = 30.0
#: The tree fetched for a snapshot is kept this long for a following
#: click_node on the same tab, so one screenshot-then-click step costs one
#: full-tree fetch, not two. A stale entry cannot misclick: a backend node id
#: from a document that has since navigated no longer resolves, so the box
#: lookup fails and the click falls back to a fresh tree (then to x,y).
_TREE_TTL = 20.0
#: Wall budget a screenshot waits for its AX snapshot (see snapshot_budget).
DEFAULT_SNAPSHOT_BUDGET = 2.0
_BUDGET_ENV = "HARNESS_CHROME_CDP_BUDGET"

_cache_lock = threading.Lock()
_port_cache: dict[tuple[str, str], tuple[float, int]] = {}
_tree_cache: dict[tuple[str, str], tuple[float, str, list[Any]]] = {}

_INTERACTIVE = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "combobox",
        "tab",
        "menuitem",
        "checkbox",
        "radio",
        "switch",
        "slider",
        "treeitem",
        "option",
        "cell",
        "gridcell",
        "columnheader",
        "rowheader",
        "heading",
        "menuitemcheckbox",
        "menuitemradio",
    }
)


def enabled() -> bool:
    """Opt in with HARNESS_CHROME_CDP=1; visual computer use needs no debugger."""
    return os.environ.get("HARNESS_CHROME_CDP", "0") not in ("0", "false", "no")


def snapshot_budget() -> float:
    """Seconds a screenshot waits for the AX snapshot before going without it.

    The snapshot runs alongside the frame capture; past this deadline the
    frame is delivered tree-less rather than holding the whole step on a
    heavy page. $HARNESS_CHROME_CDP_BUDGET overrides; 0 means never wait.
    """
    raw = os.environ.get(_BUDGET_ENV, "")
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SNAPSHOT_BUDGET
    return value if value >= 0 else DEFAULT_SNAPSHOT_BUDGET


# -- caches -----------------------------------------------------------------


def _cache_key(machine: str | None, user_data_dir: str | None) -> tuple[str, str]:
    return (machine or "", user_data_dir or "")


def reset_caches() -> None:
    """Forget every cached port and tree (tests, Chrome relaunch)."""
    with _cache_lock:
        _port_cache.clear()
        _tree_cache.clear()


def _forget(key: tuple[str, str]) -> None:
    with _cache_lock:
        _port_cache.pop(key, None)
        _tree_cache.pop(key, None)


def _cached_port(key: tuple[str, str]) -> int | None:
    with _cache_lock:
        hit = _port_cache.get(key)
    if hit is None or time.monotonic() - hit[0] >= _PORT_TTL:
        return None
    return hit[1]


def _remember_port(key: tuple[str, str], port: int) -> None:
    with _cache_lock:
        _port_cache[key] = (time.monotonic(), port)


def _cached_tree(key: tuple[str, str], path: str) -> list[Any] | None:
    with _cache_lock:
        hit = _tree_cache.get(key)
    if hit is None or hit[1] != path or time.monotonic() - hit[0] >= _TREE_TTL:
        return None
    return hit[2]


def _remember_tree(key: tuple[str, str], path: str, nodes: list[Any]) -> None:
    with _cache_lock:
        _tree_cache[key] = (time.monotonic(), path, nodes)


def snapshot(*, machine: str | None = None, user_data_dir: str | None = None) -> str | None:
    """Compact AX dump for the front Chrome page, or None if CDP is unavailable."""
    if not enabled():
        return None
    key = _cache_key(machine, user_data_dir)
    try:
        port = _discover_port(machine, user_data_dir)
        if port is None:
            return None
        tabs = _http_json(machine, port, "/json/list")
        if not isinstance(tabs, list) or not tabs:
            return None
        page = _pick_page(tabs)
        if page is None:
            return None
        ws_url = str(page.get("webSocketDebuggerUrl") or "")
        path = _ws_path(ws_url)
        if not path:
            return None
        with _Session(machine, port, path) as session:
            session.call("Accessibility.enable")
            result = session.call("Accessibility.getFullAXTree") or {}
        nodes = result.get("nodes") if isinstance(result, dict) else None
        if not isinstance(nodes, list):
            return None
        _remember_tree(key, path, nodes)
        title = str(page.get("title") or "") or "tab"
        url = str(page.get("url") or "")
        body = _format_ax(nodes)
        if not body:
            return None
        header = f"Chrome AX ({title}"
        if url:
            header += f" — {url}"
        header += "):"
        return (
            f"{header}\n{body}\n"
            "Click computer_click node=<id> for a listed control; x,y still works."
        )
    except (CdpError, OSError, TimeoutError, ValueError, json.JSONDecodeError):
        _forget(key)
        return None


def click_node(
    node: str,
    *,
    machine: str | None = None,
    user_data_dir: str | None = None,
) -> bool:
    """Click an AX / backend DOM node in Chrome. False means use x,y instead."""
    if not enabled():
        return False
    raw = (node or "").strip().lstrip("#")
    if not raw.isdigit():
        return False
    target = int(raw)
    key = _cache_key(machine, user_data_dir)
    try:
        port = _discover_port(machine, user_data_dir)
        if port is None:
            return False
        tabs = _http_json(machine, port, "/json/list")
        if not isinstance(tabs, list):
            return False
        page = _pick_page(tabs)
        if page is None:
            return False
        path = _ws_path(str(page.get("webSocketDebuggerUrl") or ""))
        if not path:
            return False
        # The tree the last snapshot listed this id from goes first; a miss
        # (unknown id, or a node that no longer resolves) fetches a fresh one.
        cached = _cached_tree(key, path)
        attempts: list[list[Any] | None] = ([cached] if cached is not None else []) + [None]
        with _Session(machine, port, path) as session:
            session.call("Accessibility.enable")
            session.call("DOM.enable")
            for nodes in attempts:
                if nodes is None:
                    tree = session.call("Accessibility.getFullAXTree") or {}
                    nodes = tree.get("nodes") if isinstance(tree, dict) else None
                    if isinstance(nodes, list):
                        _remember_tree(key, path, nodes)
                backend = _backend_id(nodes, target)
                if backend is None:
                    continue
                center = _box_center(session, backend)
                if center is None:
                    continue
                x, y = center
                for kind in ("mousePressed", "mouseReleased"):
                    session.call(
                        "Input.dispatchMouseEvent",
                        type=kind,
                        x=x,
                        y=y,
                        button="left",
                        clickCount=1,
                    )
                return True
        return False
    except (CdpError, OSError, TimeoutError, ValueError, json.JSONDecodeError, TypeError):
        _forget(key)
        return False


def _box_center(session: _Session, backend: int) -> tuple[float, float] | None:
    """Viewport center of a backend DOM node; None when it does not resolve."""
    try:
        session.call("DOM.scrollIntoViewIfNeeded", backendNodeId=backend)
    except CdpError:
        pass
    try:
        box = session.call("DOM.getBoxModel", backendNodeId=backend) or {}
    except CdpError:
        return None
    model = box.get("model") if isinstance(box, dict) else None
    content = (model or {}).get("content") if isinstance(model, dict) else None
    if not (isinstance(content, list) and len(content) >= 6):
        return None
    return (
        (float(content[0]) + float(content[4])) / 2.0,
        (float(content[1]) + float(content[5])) / 2.0,
    )


class CdpError(Exception):
    """A CDP command failed or the socket closed."""


# -- discovery --------------------------------------------------------------


def _discover_port(machine: str | None, user_data_dir: str | None) -> int | None:
    """`_probe_port`, cached per profile for `_PORT_TTL` seconds."""
    key = _cache_key(machine, user_data_dir)
    port = _cached_port(key)
    if port is not None:
        return port
    port = _probe_port(machine, user_data_dir)
    if port is not None:
        _remember_port(key, port)
    return port


def _probe_port(machine: str | None, user_data_dir: str | None) -> int | None:
    """Port from our profile's DevToolsActivePort, owned by this user_data_dir.

    Never probe 9222 blind. A live ``/json/version`` is not enough: a stale
    port file on a shared host can point at another bot's Chrome. The
    browser websocket path Chrome writes next to the port must match the
    debugger, and a host listener that advertises ``--user-data-dir=``
    must be this profile.
    """
    text = _read_active_port(machine, user_data_dir)
    if not text:
        return None
    lines = text.splitlines()
    first = lines[0].strip() if lines else ""
    if not first.isdigit():
        return None
    port = int(first)
    if not (0 < port < 65536):
        return None
    browser_path = lines[1].strip() if len(lines) > 1 else ""
    if not browser_path.startswith("/devtools/browser/"):
        return None
    try:
        version = _http_json(machine, port, "/json/version")
    except (CdpError, OSError, TimeoutError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(version, dict):
        return None
    ws = str(version.get("webSocketDebuggerUrl") or "")
    if _ws_path(ws).rstrip("/") != browser_path.rstrip("/"):
        return None
    if not _owns_user_data_dir(machine, port, user_data_dir):
        return None
    return port


def _owns_user_data_dir(machine: str | None, port: int, user_data_dir: str | None) -> bool:
    """False when a listener on this port is a Chrome for another profile.

    Machines keep one netns per bot, so the port-file browser path is the
    attach proof. On the shared host, also reject a process that bound
    the port with a different ``--user-data-dir``. No advertised profile
    (tests, non-Chrome) leaves the path match as the gate.
    """
    if machine or not user_data_dir:
        return True
    advertised = _listener_user_data_dirs(port)
    if not advertised:
        return True
    want = _norm_profile(user_data_dir)
    return any(_norm_profile(path) == want for path in advertised)


def _norm_profile(path: str) -> str:
    return os.path.normpath(path.rstrip("/") or "/")


def _listener_user_data_dirs(port: int) -> list[str]:
    """``--user-data-dir`` values of processes listening on ``port``."""
    inodes = _listen_inodes(port)
    if not inodes:
        return []
    dirs: list[str] = []
    for pid in _pids_holding(inodes):
        got = _cmdline_user_data_dir(pid)
        if got:
            dirs.append(got)
    return dirs


def _listen_inodes(port: int) -> set[int]:
    needle = f":{port:04X}"
    found: set[int] = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            body = open(path, encoding="ascii", errors="replace").read().splitlines()
        except OSError:
            continue
        for line in body[1:]:
            parts = line.split()
            if len(parts) < 10 or parts[3] != "0A":
                continue
            if not parts[1].upper().endswith(needle):
                continue
            try:
                found.add(int(parts[9]))
            except ValueError:
                continue
    return found


def _pids_holding(inodes: set[int]) -> list[int]:
    want = {f"socket:[{n}]" for n in inodes}
    pids: list[int] = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return pids
    for name in names:
        if not name.isdigit():
            continue
        fd_dir = f"/proc/{name}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(f"{fd_dir}/{fd}") in want:
                    pids.append(int(name))
                    break
            except OSError:
                continue
    return pids


def _cmdline_user_data_dir(pid: int) -> str | None:
    try:
        raw = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
    except OSError:
        return None
    for arg in raw:
        if arg.startswith(b"--user-data-dir="):
            return arg.split(b"=", 1)[1].decode("utf-8", "replace")
    return None


def _read_active_port(machine: str | None, user_data_dir: str | None) -> str:
    if not user_data_dir:
        return ""
    path = f"{user_data_dir.rstrip('/')}/DevToolsActivePort"
    if machine:
        try:
            from harness.machine_view import exec_prefix

            proc = subprocess.run(
                [*exec_prefix(machine), "cat", path],
                capture_output=True,
                timeout=_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError, Exception):
            return ""
        if proc.returncode != 0:
            return ""
        return proc.stdout.decode("utf-8", "replace")
    try:
        return open(path, encoding="utf-8").read()
    except OSError:
        return ""


def _pick_page(tabs: list[Any]) -> dict[str, Any] | None:
    pages = [t for t in tabs if isinstance(t, dict) and t.get("type") == "page"]
    usable = [
        t
        for t in pages
        if not str(t.get("url") or "").startswith(
            ("chrome://", "devtools://", "chrome-extension://", "about:")
        )
    ]
    return (usable or pages or [None])[0]


def _ws_path(url: str) -> str:
    if not url:
        return ""
    if "://" in url:
        rest = url.split("://", 1)[1]
        slash = rest.find("/")
        return rest[slash:] if slash >= 0 else "/"
    return url if url.startswith("/") else f"/{url}"


def _int_field(node: dict[str, Any], key: str) -> int | None:
    try:
        return int(node.get(key))
    except (TypeError, ValueError):
        return None


def _backend_id(nodes: Any, target: int) -> int | None:
    """Map a snapshot id to DOM.backendNodeId.

    The dump prints Accessibility ``nodeId`` (``#12``). That counter is
    not ``backendDOMNodeId`` — they overlap in real Chrome — so a
    first-hit ``nid == target or bid == target`` can click a different
    element, including an ignored node the dump never listed. Prefer a
    non-ignored nodeId; only then a non-ignored backendDOMNodeId.
    """
    if not isinstance(nodes, list):
        return None
    from_node: int | None = None
    found_node = False
    saw_ignored_node = False
    from_backend: int | None = None
    for node in nodes:
        if not isinstance(node, dict):
            continue
        nid = _int_field(node, "nodeId")
        bid = _int_field(node, "backendDOMNodeId")
        ignored = bool(node.get("ignored"))
        if nid == target:
            if ignored:
                saw_ignored_node = True
            elif not found_node:
                found_node = True
                from_node = bid
        if bid == target and not ignored and from_backend is None:
            from_backend = bid
    if found_node:
        return from_node
    if saw_ignored_node:
        return None
    return from_backend


# -- AX formatting ----------------------------------------------------------


def _ax_role(node: dict[str, Any]) -> str:
    role = node.get("role")
    if isinstance(role, dict):
        return str(role.get("value") or "")
    return str(role or "")


def _ax_name(node: dict[str, Any]) -> str:
    name = node.get("name")
    if isinstance(name, dict):
        return str(name.get("value") or "").strip()
    return str(name or "").strip()


def _format_ax(nodes: list[Any]) -> str:
    useful: list[tuple[int, str, str, int]] = []
    for node in nodes:
        if not isinstance(node, dict) or node.get("ignored"):
            continue
        try:
            nid = int(node.get("nodeId"))
        except (TypeError, ValueError):
            continue
        role = _ax_role(node).strip()
        if not role or role.lower() in {"none", "ignored", "inlinetextbox", "linebreak"}:
            continue
        name = _ax_name(node)
        interactive = role.lower() in _INTERACTIVE
        if not interactive and not name:
            continue
        useful.append((0 if interactive else 1, role, name, nid))
        if len(useful) >= _SNAPSHOT_NODES * 2:
            break
    useful.sort(key=lambda row: row[0])
    lines: list[str] = []
    used = 0
    # `#<id>` is Accessibility nodeId — the only id click_node resolves
    # as the snapshot key. backendDOMNodeId is a different Chrome space.
    for _, role, name, nid in useful:
        line = f"  {role} {json.dumps(name)} #{nid}" if name else f"  {role} #{nid}"
        if used + len(line) + 1 > _SNAPSHOT_CHARS:
            omitted = len(useful) - len(lines)
            if omitted > 0:
                lines.append(f"  … {omitted} more nodes omitted")
            break
        lines.append(line)
        used += len(line) + 1
        if len(lines) >= _SNAPSHOT_NODES:
            omitted = len(useful) - len(lines)
            if omitted > 0:
                lines.append(f"  … {omitted} more nodes omitted")
            break
    return "\n".join(lines)


# -- HTTP / WS --------------------------------------------------------------


def _http_json(machine: str | None, port: int, path: str) -> Any:
    url = f"http://127.0.0.1:{port}{path}"
    if machine:
        return json.loads(_http_via_exec(machine, url))
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


_HTTP_EXEC = (
    "import sys, urllib.request; "
    "print(urllib.request.urlopen(sys.argv[1], timeout=4).read().decode())"
)


def _http_via_exec(machine: str, url: str) -> str:
    from harness.machine_view import exec_prefix

    proc = subprocess.run(
        [*exec_prefix(machine), "python3", "-c", _HTTP_EXEC, url],
        capture_output=True,
        timeout=_TIMEOUT + 2,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip() or "http get failed"
        raise CdpError(err)
    return proc.stdout.decode("utf-8", "replace")


class _Session:
    def __init__(self, machine: str | None, port: int, path: str) -> None:
        self._machine = machine
        self._port = port
        self._path = path
        self._sock: socket.socket | None = None
        self._proc: subprocess.Popen | None = None
        self._io: _ByteIO | None = None
        self._lock = threading.Lock()
        self._next_id = 0

    def __enter__(self) -> _Session:
        raw = self._connect()
        self._upgrade(raw, self._path)
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        sock, proc = self._sock, self._proc
        self._sock, self._proc = None, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.kill()
            except OSError:
                pass

    def call(self, method: str, **params: Any) -> dict[str, Any] | None:
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            payload: dict[str, Any] = {"id": msg_id, "method": method}
            if params:
                payload["params"] = params
            self._send_text(json.dumps(payload, separators=(",", ":")))
            until = time.monotonic() + _TIMEOUT
            while True:
                remaining = until - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"cdp {method} timed out")
                raw = self._recv_json(remaining)
                if raw.get("id") == msg_id:
                    if raw.get("error"):
                        raise CdpError(str(raw["error"]))
                    result = raw.get("result")
                    return result if isinstance(result, dict) or result is None else {}

    def _connect(self) -> _ByteIO:
        if self._machine:
            return self._connect_exec()
        sock = socket.create_connection(("127.0.0.1", self._port), timeout=_TIMEOUT)
        sock.settimeout(_TIMEOUT)
        self._sock = sock
        return _SocketIO(sock)

    def _connect_exec(self) -> _ByteIO:
        from harness.machine_view import exec_prefix

        proc = subprocess.Popen(
            [
                *exec_prefix(self._machine or "", interactive=True),
                "python3",
                "-u",
                "-c",
                _BRIDGE,
                str(self._port),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._proc = proc
        if proc.stdin is None or proc.stdout is None:
            raise CdpError("docker exec cdp bridge has no pipes")
        return _ProcIO(proc)

    def _upgrade(self, raw: _ByteIO, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self._port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        raw.sendall(req.encode("ascii"))
        header = _read_http_headers(raw)
        if not header.startswith("HTTP/1.1 101"):
            raise CdpError(f"websocket upgrade failed: {header.splitlines()[:1]}")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        accept = ""
        for line in header.split("\r\n"):
            if line.lower().startswith("sec-websocket-accept:"):
                accept = line.split(":", 1)[1].strip()
        if accept and accept != expected:
            raise CdpError("websocket accept mismatch")
        self._io = raw

    def _send_text(self, text: str) -> None:
        if self._io is None:
            raise CdpError("cdp session closed")
        payload = text.encode("utf-8")
        header = bytearray([0x81])  # fin + text
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", n))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", n))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._io.sendall(bytes(header) + masked)

    def _recv_json(self, remaining: float) -> dict[str, Any]:
        payload = self._recv_frame(remaining)
        return json.loads(payload.decode("utf-8"))

    def _recv_frame(self, remaining: float) -> bytes:
        until = time.monotonic() + remaining
        chunks: list[bytes] = []
        while True:
            left = until - time.monotonic()
            if left <= 0:
                raise TimeoutError("cdp websocket timed out")
            opcode, data, fin = self._read_one_frame(left)
            if opcode == 0x8:
                raise CdpError("websocket closed")
            if opcode == 0x9:  # ping
                self._send_pong(data)
                continue
            if opcode == 0xA:
                continue
            chunks.append(data)
            if fin:
                blob = b"".join(chunks)
                if len(blob) > _WS_MAX:
                    raise CdpError("cdp frame too large")
                return blob

    def _send_pong(self, data: bytes) -> None:
        n = len(data)
        header = bytearray([0x8A, 0x80 | n if n < 126 else 0])
        if n < 126:
            pass
        else:
            return
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if self._io is None:
            return
        try:
            self._io.sendall(bytes(header) + masked)
        except OSError:
            pass

    def _read_one_frame(self, remaining: float) -> tuple[int, bytes, bool]:
        hdr = self._read_exact(2, remaining)
        fin = bool(hdr[0] & 0x80)
        opcode = hdr[0] & 0x0F
        masked = bool(hdr[1] & 0x80)
        n = hdr[1] & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._read_exact(2, remaining))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._read_exact(8, remaining))[0]
        if n > _WS_MAX:
            raise CdpError("cdp frame too large")
        mask = self._read_exact(4, remaining) if masked else b""
        data = self._read_exact(n, remaining) if n else b""
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return opcode, data, fin

    def _read_exact(self, n: int, remaining: float) -> bytes:
        if self._io is None:
            raise CdpError("cdp session closed")
        until = time.monotonic() + remaining
        out = bytearray()
        while len(out) < n:
            left = until - time.monotonic()
            if left <= 0:
                raise TimeoutError("cdp read timed out")
            self._io.settimeout(max(left, 0.05))
            chunk = self._io.recv(n - len(out))
            if not chunk:
                raise CdpError("cdp socket closed")
            out.extend(chunk)
        return bytes(out)


_BRIDGE = r"""
import os, select, socket, sys
port = int(sys.argv[1])
s = socket.create_connection(("127.0.0.1", port), 5)
stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
try:
    while True:
        ready, _, _ = select.select([s, sys.stdin], [], [], 30)
        if not ready:
            break
        if sys.stdin in ready:
            data = os.read(sys.stdin.fileno(), 65536)
            if not data:
                break
            s.sendall(data)
        if s in ready:
            data = s.recv(65536)
            if not data:
                break
            stdout.write(data)
            stdout.flush()
finally:
    s.close()
"""


class _ByteIO(Protocol):
    def sendall(self, data: bytes) -> None: ...
    def recv(self, n: int) -> bytes: ...
    def settimeout(self, timeout: float) -> None: ...


class _SocketIO:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    def sendall(self, data: bytes) -> None:
        self._sock.sendall(data)

    def recv(self, n: int) -> bytes:
        return self._sock.recv(n)

    def settimeout(self, timeout: float) -> None:
        self._sock.settimeout(timeout)


class _ProcIO:
    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc
        self._timeout = _TIMEOUT

    def sendall(self, data: bytes) -> None:
        stdin = self._proc.stdin
        if stdin is None:
            raise CdpError("cdp bridge stdin closed")
        stdin.write(data)
        stdin.flush()

    def recv(self, n: int) -> bytes:
        # os.read on the bridge's pipe blocks with no deadline of its own;
        # without this select a wedged Chrome pins the tool call until the
        # bridge's idle timeout, not _TIMEOUT.
        stdout = self._proc.stdout
        if stdout is None:
            raise CdpError("cdp bridge stdout closed")
        fd = stdout.fileno()
        ready, _, _ = select.select([fd], [], [], self._timeout)
        if not ready:
            raise TimeoutError("cdp bridge read timed out")
        return os.read(fd, n)

    def settimeout(self, timeout: float) -> None:
        self._timeout = timeout


def _read_http_headers(raw: _ByteIO) -> str:
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = raw.recv(1)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > 16384:
            raise CdpError("http header too large")
    return bytes(buf).decode("iso-8859-1", "replace")
