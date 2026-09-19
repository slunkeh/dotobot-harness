import json
import sqlite3
import urllib.error
from unittest.mock import Mock

import pytest

from harness.push import PushRelay, notification


def test_runtime_frame_types_and_routes():
    base = {"bot": "atlas", "request_id": "req-1"}
    for frame in [
        {"type": "final", "text": "Done"},
        {"type": "choice", "id": "choice-1", "question": "Which?"},
        {"type": "secret_request", "name": "password"},
        {"type": "takeover", "reason": "Please sign in"},
        {"type": "card", "card_type": "confirm", "id": "confirm-1"},
        {"type": "card", "card_type": "control_return", "id": "control-1"},
        {"type": "block", "blocking": True, "block_id": "block-1"},
    ]:
        result = notification({**base, **frame})
        assert result["conv"] == "bot:atlas"
        assert result["body"]
        assert notification({**base, **frame, "room": "ops"})["conv"] == "room:ops"
        assert notification({**base, **frame, "mutation": "updated"}) is None
    assert notification({**base, "type": "final", "text": "   "}) is None
    assert notification({**base, "type": "text", "text": "streaming"}) is None
    assert (
        notification({**base, "type": "card", "card_type": "confirm", "resolution": {"done": True}})
        is None
    )
    assert notification({**base, "type": "block", "blocking": True, "status": "settled"}) is None


def test_durable_dedupe_across_restart_and_expired_registration(tmp_path):
    relay = PushRelay(tmp_path, "https://relay.example/push")
    relay.register({"id": "a" * 64, "secret": "b" * 64})
    frame = {"type": "final", "text": "Ready", "bot": "atlas", "request_id": "r1"}
    relay.enqueue(frame)
    PushRelay(tmp_path, relay.url).enqueue(frame)
    with sqlite3.connect(relay.path) as db:
        assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
        db.execute("DELETE FROM subscriptions")
        db.executemany(
            "INSERT INTO subscriptions VALUES (?, ?, 0)",
            [(f"{i:064x}", "b" * 64) for i in range(100)],
        )
    relay.register({"id": "c" * 64, "secret": "d" * 64})
    with sqlite3.connect(relay.path) as db:
        assert db.execute("SELECT count(*) FROM subscriptions").fetchone()[0] == 1


def test_retry_then_success_and_revocation(tmp_path, monkeypatch):
    relay = PushRelay(tmp_path, "https://relay.example/push")
    relay.register({"id": "a" * 64, "secret": "b" * 64})
    relay.enqueue({"type": "choice", "bot": "atlas", "id": "c1"})
    response = Mock(status=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.side_effect = [OSError("offline"), response]
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: opener)
    assert relay.deliver_one()
    with sqlite3.connect(relay.path) as db:
        assert db.execute("SELECT done, attempts FROM outbox").fetchone() == (0, 1)
        db.execute("UPDATE outbox SET next_attempt=0")
    assert relay.deliver_one()
    request = opener.open.call_args.args[0]
    assert request.headers["Authorization"] == "Bearer " + "b" * 64
    assert json.loads(request.data)["conv"] == "bot:atlas"
    assert not relay.deliver_one()
    relay.enqueue({"type": "choice", "bot": "atlas", "id": "c2"})
    opener.open.side_effect = urllib.error.HTTPError(relay.url, 410, "revoked", {}, None)
    assert relay.deliver_one()
    with sqlite3.connect(relay.path) as db:
        assert db.execute("SELECT count(*) FROM subscriptions").fetchone()[0] == 0


def test_config_and_capability_validation(tmp_path):
    with pytest.raises(ValueError):
        PushRelay(tmp_path, "http://relay.example")
    relay = PushRelay(tmp_path, "https://relay.example")
    with pytest.raises(ValueError):
        relay.register({"id": "wrong", "secret": "no"})
    relay.start()
    relay.close()
    assert not relay.thread.is_alive()


def test_server_registers_authenticated_capability_and_enqueues_real_broadcast(
    tmp_path, monkeypatch
):
    import threading
    import urllib.request

    from harness.orchestrator import Orchestrator
    from harness.server import make_server

    monkeypatch.setenv("HARNESS_PUSH_RELAY_URL", "https://relay.example/push")
    monkeypatch.setattr(PushRelay, "start", lambda self: None)
    roster = tmp_path / "roster.toml"
    roster.write_text('[[bots]]\nname="atlas"\nrole="assistant"\nprovider="echo"\n')
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=roster, backend="process")
    orch.init()
    server = make_server(orch, "127.0.0.1", 0, token="owner-key")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/api/push/subscriptions"
    capability = json.dumps({"id": "a" * 64, "secret": "b" * 64}).encode()
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(url, data=capability))
        assert error.value.code == 401
        with urllib.request.urlopen(
            urllib.request.Request(
                url, data=capability, headers={"Authorization": "Bearer owner-key"}
            )
        ) as response:
            assert json.load(response)["registered"] is True
        orch.ws_hub.broadcast(
            {"type": "final", "bot": "atlas", "text": "Finished", "request_id": "r1"}
        )
        with sqlite3.connect(orch.ws_hub.push_relay.path) as db:
            payload = json.loads(db.execute("SELECT payload FROM outbox").fetchone()[0])
        assert payload["conv"] == "bot:atlas"
        assert payload["body"] == "Finished"
    finally:
        server.shutdown()
        server.server_close()
    assert orch.ws_hub.push_relay is None


def test_distinct_input_requests_in_one_turn_survive_replay(tmp_path):
    from agent.streaming import StreamReader, StreamWriter
    from harness.paths import HarnessPaths
    from harness.server import asdict_event

    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    writer = StreamWriter(paths, "one-task")
    writer.secret_request("atlas", "FIRST_TOKEN", "First service")
    writer.secret_request("atlas", "SECOND_TOKEN", "Second service")
    writer.takeover("atlas", "First sign-in")
    writer.takeover("atlas", "Second sign-in")
    relay = PushRelay(paths.home, "https://relay.example/push")
    relay.register({"id": "a" * 64, "secret": "b" * 64})
    events = list(StreamReader(paths, "one-task")._read_new())
    for event in events:
        frame = {**asdict_event(event), "request_id": "one-task"}
        relay.enqueue(frame)
        relay.enqueue(frame)
    with sqlite3.connect(relay.path) as db:
        assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 4
