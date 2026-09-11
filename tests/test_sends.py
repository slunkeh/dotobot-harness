"""Idempotent chat sends: nonce ledger + safe-retry semantics."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from harness import sends
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.server import make_server

ROSTER = """
[[bots]]
name = "atlas"
role = "a terse research assistant"
provider = "echo"
"""


# -- ledger unit tests ------------------------------------------------------


def _paths(tmp_path) -> HarnessPaths:
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    return paths


def test_input_digest_is_canonical():
    a = sends.input_digest("hi", [{"name": "a.txt", "path": "/p"}], {"bot": "atlas", "room": None})
    b = sends.input_digest("hi", [{"path": "/p", "name": "a.txt"}], {"room": None, "bot": "atlas"})
    assert a == b  # key order never changes the digest
    assert a != sends.input_digest("hi!", [{"name": "a.txt", "path": "/p"}], {"bot": "atlas"})
    assert a != sends.input_digest("hi", [], {"bot": "atlas", "room": None})


def test_admit_fresh_then_replay(tmp_path):
    paths = _paths(tmp_path)
    digest = sends.input_digest("hello", [], {"bot": "atlas", "room": None})
    assert sends.admit(paths, "n-1", digest) is None  # never seen: dispatch
    sends.record_accept(paths, "n-1", digest, [("atlas", "rid-1")])
    prior = sends.admit(paths, "n-1", digest)
    assert prior is not None
    assert prior["status"] == "accepted"
    assert prior["turns"] == [{"bot": "atlas", "request_id": "rid-1"}]


def test_admit_rejects_nonce_reuse_with_different_input(tmp_path):
    paths = _paths(tmp_path)
    digest = sends.input_digest("hello", [], {"bot": "atlas", "room": None})
    sends.record_accept(paths, "n-1", digest, [("atlas", "rid-1")])
    other = sends.input_digest("goodbye", [], {"bot": "atlas", "room": None})
    with pytest.raises(sends.NonceMismatchError) as err:
        sends.admit(paths, "n-1", other)
    assert err.value.code == sends.NONCE_DIGEST_MISMATCH


def test_ledger_caps_at_256_evicting_oldest(tmp_path):
    paths = _paths(tmp_path)
    for i in range(sends.LEDGER_CAP + 10):
        sends.record_accept(paths, f"n-{i}", f"digest-{i}", [("atlas", f"rid-{i}")])
    data = json.loads(paths.send_ledger().read_text(encoding="utf-8"))
    assert len(data["records"]) == sends.LEDGER_CAP
    assert sends.lookup(paths, "n-0") is None  # oldest evicted
    assert sends.lookup(paths, "n-9") is None
    assert sends.lookup(paths, "n-10") is not None  # cap window survives
    assert sends.lookup(paths, f"n-{sends.LEDGER_CAP + 9}") is not None


def test_corrupt_ledger_resets_instead_of_crashing(tmp_path):
    paths = _paths(tmp_path)
    paths.send_ledger().parent.mkdir(parents=True, exist_ok=True)
    paths.send_ledger().write_text("{not json", encoding="utf-8")
    assert sends.lookup(paths, "n-1") is None
    sends.record_accept(paths, "n-1", "d1", [("atlas", "rid-1")])
    assert sends.lookup(paths, "n-1")["input_digest"] == "d1"


# -- HTTP contract ----------------------------------------------------------


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
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        orch.down()


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
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


def test_repeat_nonce_replays_original_accept(server):
    payload = {"bot": "atlas", "text": "hello once", "client_nonce": "abc123"}
    first = _sse(f"{server}/api/chat", payload)
    accepted = next(f for f in first if f["type"] == "accepted")
    rid = accepted["request_id"]
    final = next(f for f in first if f["type"] == "final")

    retry = _sse(f"{server}/api/chat", payload)
    again = next(f for f in retry if f["type"] == "accepted")
    assert again["request_id"] == rid  # the ORIGINAL request, not a new one
    assert again.get("duplicate") is True
    refinal = next(f for f in retry if f["type"] == "final")
    assert refinal["text"] == final["text"]

    # No duplicate dispatch: the ledger still records exactly one turn.
    record = _get(f"{server}/api/sends/abc123")
    assert record["status"] == "accepted"
    assert record["turns"] == [{"bot": "atlas", "request_id": rid}]


def test_nonce_reuse_with_new_input_is_rejected(server):
    _sse(f"{server}/api/chat", {"bot": "atlas", "text": "hello once", "client_nonce": "n-x"})
    data = json.dumps({"bot": "atlas", "text": "different text", "client_nonce": "n-x"}).encode()
    req = urllib.request.Request(
        f"{server}/api/chat",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=10)
    assert err.value.code == 409
    body = json.loads(err.value.read().decode())
    assert body["code"] == "nonce_digest_mismatch"


def test_send_status_answers_did_that_land(server):
    with pytest.raises(urllib.error.HTTPError) as err:
        _get(f"{server}/api/sends/never-sent")
    assert err.value.code == 404

    frames = _sse(f"{server}/api/chat", {"bot": "atlas", "text": "landed?", "client_nonce": "n-9"})
    rid = next(f for f in frames if f["type"] == "accepted")["request_id"]
    record = _get(f"{server}/api/sends/n-9")
    assert record["status"] == "accepted"
    assert record["turns"][0]["request_id"] == rid


def test_chat_without_nonce_is_unchanged(server):
    frames = _sse(f"{server}/api/chat", {"bot": "atlas", "text": "plain send"})
    final = [f for f in frames if f["type"] == "final"]
    assert final and "plain send" in final[-1]["text"]
