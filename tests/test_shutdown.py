"""Graceful shutdown: clients get a shutdown frame + clean close; port rebinds."""

from __future__ import annotations

import struct
import threading
import time

import pytest

from harness import ws as wsproto
from harness.orchestrator import Orchestrator
from harness.server import make_server
from tests.test_ws import WSClient

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
        yield ("127.0.0.1", port, orch, httpd)
    finally:
        httpd.shutdown()
        httpd.server_close()
        orch.down()


def test_hub_shutdown_sends_notice_then_clean_close(server):
    host, port, orch, _httpd = server
    c = WSClient(host, port)
    try:
        time.sleep(0.2)  # let the handler register with the hub
        orch.ws_hub.shutdown(retry_in=2.0)

        notice = c.recv()
        assert notice["type"] == "shutdown"
        assert notice["reason"] == "update"
        assert notice["retry_in"] == 2.0
        # Every frame carries the boot epoch + a sequence number.
        assert notice["epoch"]
        assert notice["seq"] >= 1

        op, payload = wsproto.read_frame(c.rfile)
        assert op == wsproto.OP_CLOSE
        assert struct.unpack("!H", payload[:2])[0] == wsproto.CLOSE_GOING_AWAY
    finally:
        c.close()


def test_port_rebinds_immediately_after_shutdown(server):
    host, port, orch, httpd = server
    httpd.shutdown()
    httpd.server_close()
    # A replacement server (the updated harness) must bind the same port at once.
    httpd2 = make_server(orch, host, port)
    try:
        assert httpd2.server_address[1] == port
    finally:
        httpd2.server_close()


def test_bots_survive_server_shutdown_and_are_adopted(server):
    _host, _port, orch, httpd = server
    pids = {h.bot: h.pid for h in orch.status()}
    httpd.shutdown()
    httpd.server_close()
    # The server is gone; agents keep running and a fresh spawn adopts them.
    for h in orch.up():
        assert h.pid == pids[h.bot]
        assert h.status.value == "running"
