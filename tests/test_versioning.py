"""Version plumbing: /api/health fields, WS hello frame, run-file stamps."""

from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest

from harness.orchestrator import Orchestrator
from harness.server import make_server
from harness.version import __version__

ROSTER = """
[[bots]]
name = "atlas"
role = "a terse research assistant"
provider = "echo"
"""


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


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())


def test_health_reports_version_and_boot(server):
    host, port, _orch = server
    health = _get(f"http://{host}:{port}/api/health")
    assert health["ok"] is True
    assert health["version"] == __version__
    assert health["boot_id"]
    assert health["started_at"] > 0
    # no app_release.json -> pins are null, keys present
    assert health["min_app_version"] is None
    assert health["latest_app_version"] is None
    assert health["app_download_url"] is None


def test_health_surfaces_app_release_pins(server):
    host, port, orch = server
    (orch.paths.home / "app_release.json").write_text(
        json.dumps(
            {
                "min_app_version": "0.1.0",
                "latest_app_version": "0.3.0",
                "app_download_url": "https://example.com/Dotobot.zip",
            }
        ),
        encoding="utf-8",
    )
    health = _get(f"http://{host}:{port}/api/health")
    assert health["min_app_version"] == "0.1.0"
    assert health["latest_app_version"] == "0.3.0"
    assert health["app_download_url"] == "https://example.com/Dotobot.zip"


def test_ws_first_frame_is_hello(server):
    from tests.test_ws import WSClient

    host, port, _orch = server
    c = WSClient(host, port)
    try:
        hello = c.hello  # consumed by WSClient during the handshake
        assert hello["type"] == "hello"
        assert hello["version"] == __version__
        assert hello["boot_id"]
        assert "latest_app_version" in hello
    finally:
        c.close()


def test_run_file_stamps_agent_code_version(server):
    _host, _port, orch = server
    data = json.loads(orch.paths.run_file("atlas").read_text(encoding="utf-8"))
    assert data["version"] == __version__
    assert data["started"] > 0
    # adoption keeps the recorded version in the handle meta
    handle = orch.backend.load("atlas")
    assert handle.meta.get("version") == __version__


def test_legacy_run_file_without_version_still_loads(tmp_path):
    from harness.paths import HarnessPaths
    from isolation.process import ProcessBackend

    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    paths.run.mkdir(parents=True, exist_ok=True)
    paths.run_file("atlas").write_text(
        json.dumps({"bot": "atlas", "backend": "process", "pid": 1}), encoding="utf-8"
    )
    handle = ProcessBackend(paths).load("atlas")
    assert handle is not None
    assert handle.meta.get("version") is None


def test_serve_json_carries_version_and_pid(server):
    _host, _port, orch = server
    info = json.loads((orch.paths.home / "serve.json").read_text(encoding="utf-8"))
    assert info["version"] == __version__
    assert info["pid"] > 0
