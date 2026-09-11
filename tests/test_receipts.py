"""Last-read receipts: picker unread state shared across clients."""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from harness import receipts
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.server import make_server

ROSTER = """
[[bots]]
name = "atlas"
role = "a terse research assistant"
provider = "echo"
"""


def _paths(tmp_path) -> HarnessPaths:
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    return paths


def test_empty_when_missing(tmp_path):
    assert receipts.load(_paths(tmp_path)) == {}


def test_set_read_round_trip(tmp_path):
    paths = _paths(tmp_path)
    out = receipts.set_read(paths, "bot:atlas", 1710000000.5)
    assert out["bot:atlas"] == 1710000000.5
    assert receipts.load(paths)["bot:atlas"] == 1710000000.5


def test_merge_overlays_keys(tmp_path):
    paths = _paths(tmp_path)
    receipts.set_read(paths, "bot:atlas", 1.0)
    receipts.merge(paths, {"bot:atlas": 2.0, "room:ops": 3.0, "nope": 9, "bot:x": "bad"})
    reads = receipts.load(paths)
    assert reads["bot:atlas"] == 2.0
    assert reads["room:ops"] == 3.0
    assert "nope" not in reads
    assert "bot:x" not in reads


def test_rejects_bad_key(tmp_path):
    paths = _paths(tmp_path)
    with pytest.raises(receipts.ReceiptError):
        receipts.set_read(paths, "../etc/passwd", 1.0)
    with pytest.raises(receipts.ReceiptError):
        receipts.set_read(paths, "bot:", 1.0)
    assert receipts.load(paths) == {}


def test_corrupt_file_resets(tmp_path):
    paths = _paths(tmp_path)
    paths.receipts.write_text("{not json", encoding="utf-8")
    assert receipts.load(paths) == {}


@pytest.fixture
def server(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    orch.use_json_store()
    httpd = make_server(orch, "127.0.0.1", 0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", orch
    finally:
        httpd.shutdown()
        orch.down()


def _req(url, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def test_http_get_put_receipts(server):
    base, orch = server
    empty = _req(f"{base}/api/receipts")
    assert empty == {"reads": {}}
    one = _req(
        f"{base}/api/receipts",
        "PUT",
        {"key": "bot:atlas", "ts": 1710000001},
    )
    assert one["reads"]["bot:atlas"] == 1710000001
    bulk = _req(
        f"{base}/api/receipts",
        "PUT",
        {"reads": {"bot:atlas": 5, "room:ops": 6}},
    )
    assert bulk["reads"]["bot:atlas"] == 5
    assert bulk["reads"]["room:ops"] == 6
    stored = json.loads(orch.paths.receipts.read_text(encoding="utf-8"))
    assert stored["reads"]["room:ops"] == 6
