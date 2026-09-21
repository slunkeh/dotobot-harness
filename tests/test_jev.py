"""Jev is optional, keyless in tests, and cannot discard persisted state."""

import io
import json
import urllib.error

import pytest

from agent.memory import Memory
from harness import cdp, jev, jev_features, prefs
from harness.paths import HarnessPaths
from harness.secrets import get_secret


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.delenv(jev.KEY, raising=False)
    paths = HarnessPaths.resolve(tmp_path)
    paths.ensure_layout(["atlas"])
    return paths


@pytest.fixture
def upstream(monkeypatch):
    calls = []
    real_opener = jev.urllib.request.build_opener()
    monkeypatch.setattr(jev.urllib.request, "_opener", real_opener)

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            questions = json.loads(request.data)["questions"]
            return io.BytesIO(
                json.dumps(
                    {
                        "answers": {
                            k: {"noul": 1}
                            if k == "connected"
                            else {"choice": "keep", "confidence": 0.99}
                            for k in questions
                        }
                    }
                ).encode()
            )

    monkeypatch.setattr(jev.urllib.request, "build_opener", lambda *a: Opener())
    return calls


class Writer:
    def __init__(self):
        self.events = []

    def status(self, value):
        pass

    def tool(self, *event):
        self.events.append(event)


def test_connect_enable_disable_disconnect(paths, upstream):
    assert jev.status(paths) == {
        "enabled": False,
        "configured": False,
        "source": None,
        "features": {name: False for name in jev_features.FEATURES},
        "browser": {"cdp_enabled": cdp.enabled()},
    }
    prefs.save(paths, {"caveman": True})
    result = jev.connect(paths, "customer-jev-secret")
    assert result["configured"] and not result["enabled"]
    assert "customer-jev-secret" not in json.dumps(result)
    assert (paths.credentials / jev.KEY).stat().st_mode & 0o777 == 0o600
    assert get_secret(jev.KEY, paths) == "customer-jev-secret"
    assert jev.set_enabled(paths, True)["enabled"]
    assert prefs.load(paths)["caveman"]
    assert not jev.set_enabled(paths, False)["enabled"]
    assert jev.disconnect(paths) == {
        "enabled": False,
        "configured": False,
        "source": None,
        "features": {name: False for name in jev_features.FEATURES},
        "browser": {"cdp_enabled": cdp.enabled()},
    }
    assert len(upstream) == 1


def test_disabled_or_missing_key_never_calls(paths, monkeypatch):
    monkeypatch.setattr(jev, "_request", lambda *a: pytest.fail("must not call Jev"))
    items = [{"text": "A preference"}]
    assert jev.select_context(paths, "request", items) is items
    prefs.save(paths, {"jev_enabled": True})
    assert jev.select_context(paths, "request", items) is items
    with pytest.raises(jev.JevError):
        jev.set_enabled(paths, True)


def test_only_confident_irrelevant_background_omitted(paths, upstream, monkeypatch):
    jev.connect(paths, "customer-key")
    jev.set_enabled(paths, True)
    items = [{"text": t} for t in ["keep", "irrelevant", "uncertain", "low certainty"]]
    answers = {
        str(i): {"choice": c, "confidence": p}
        for i, (c, p) in enumerate(
            [("keep", 0.99), ("omit", 0.99), ("uncertain", 0.99), ("omit", 0.7)]
        )
    }
    monkeypatch.setattr(jev, "_request", lambda *a: answers)
    writer = Writer()
    assert jev.select_context(paths, "request", items, writer=writer) == [
        items[0],
        items[2],
        items[3],
    ]
    assert [e[1] for e in writer.events] == ["active", "done"]
    assert len(items) == 4


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"0": []},
        {"0": {"choice": "omit", "confidence": float("nan")}},
        {"0": {"choice": "made up", "confidence": 1}},
    ],
)
def test_malformed_selection_preserves_all(paths, upstream, monkeypatch, response):
    jev.connect(paths, "key")
    jev.set_enabled(paths, True)
    monkeypatch.setattr(jev, "_request", lambda *a: response)
    items = [{"text": "original"}]
    writer = Writer()
    assert jev.select_context(paths, "request", items, writer=writer) is items
    assert writer.events[-1][1] == "error"


def test_unavailable_preserves_all_and_does_not_leak(paths, upstream, monkeypatch):
    jev.connect(paths, "customer-key")
    jev.set_enabled(paths, True)

    class Broken:
        def open(self, *a, **kw):
            raise urllib.error.HTTPError(
                jev.API, 401, "customer-key", {}, io.BytesIO(b"customer-key")
            )

    monkeypatch.setattr(jev.urllib.request, "build_opener", lambda *a: Broken())
    items = [{"text": "original"}]
    writer = Writer()
    assert jev.select_context(paths, "request", items, writer=writer) is items
    assert "customer-key" not in repr(writer.events)
    with pytest.raises(jev.JevError, match="API key rejected"):
        jev.connect(paths, "replacement")
    assert get_secret(jev.KEY, paths) == "customer-key"


def test_oversized_context_does_not_call_or_announce(paths, upstream):
    jev.connect(paths, "key")
    jev.set_enabled(paths, True)
    items = [{"text": "x" * jev.MAX_INPUT_BYTES}]
    writer = Writer()
    assert jev.select_context(paths, "request", items, writer=writer) is items
    assert not writer.events
    assert len(upstream) == 1


def test_environment_key_cannot_be_silently_replaced(paths, monkeypatch):
    monkeypatch.setenv(jev.KEY, "environment-key")
    assert jev.status(paths)["source"] == "env"
    with pytest.raises(jev.JevError):
        jev.connect(paths, "replacement")
    with pytest.raises(jev.JevError):
        jev.disconnect(paths)
    assert not jev.set_enabled(paths, False)["enabled"]


def test_memory_filter_does_not_change_stored_facts(paths):
    memory = Memory(paths, "atlas")
    memory.remember("Friday preference")
    original = memory.context_block()
    assert "Friday preference" in original
    assert memory.context_block(select_context=lambda items: []) == ""
    assert memory.context_block() == original


def test_no_redirect_with_provider_credential():
    assert (
        jev._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example") is None
    )


def test_authenticated_api_lifecycle(paths, upstream):
    import threading
    import urllib.request

    from harness.orchestrator import Orchestrator
    from harness.server import make_server

    roster = paths.home / "roster.toml"
    roster.write_text('[[bots]]\nname="atlas"\nprovider="echo"\n')
    orch = Orchestrator.create(home=paths.home, roster_path=roster, backend="process")
    orch.init()
    server = make_server(orch, "127.0.0.1", 0, token="test-link-key")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(path="", method="GET", data=None, auth=True):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer test-link-key"
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/jev{path}",
            data=json.dumps(data).encode() if data is not None else None,
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    try:
        for path, method, data in [
            ("", "GET", None),
            ("/key", "POST", {"key": "key"}),
            ("", "PATCH", {"enabled": True}),
            ("/key", "DELETE", None),
            ("/test", "POST", {}),
        ]:
            with pytest.raises(urllib.error.HTTPError) as error:
                call(path, method, data, auth=False)
            assert error.value.code == 401
        assert not call()["configured"]
        assert call("/key", "POST", {"key": "customer-key"})["configured"]
        assert call("", "PATCH", {"enabled": True})["enabled"]
        assert call("/test", "POST", {})["enabled"]
        configured = call("", "PATCH", {"features": {"compaction": True}})
        assert configured["features"]["compaction"] and not configured["features"]["memory"]
        with pytest.raises(urllib.error.HTTPError) as invalid:
            call("", "PATCH", {"features": {"unknown": True}})
        assert invalid.value.code == 400
        assert call()["features"] == configured["features"]
        with pytest.raises(urllib.error.HTTPError) as error:
            call("", "PATCH", [])
        assert error.value.code == 400
        assert not call("/key", "DELETE")["configured"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        orch.down()


def test_agent_uses_jev_and_streams_only_when_enabled(tmp_path, upstream, monkeypatch):
    from agent.streaming import StreamReader, StreamWriter
    from providers.echo import EchoProvider
    from tests.test_compaction import _agent

    agent, paths = _agent(tmp_path, EchoProvider())
    agent.memory.remember("Friday updates should be short")
    jev.connect(paths, "key")
    jev.set_enabled(paths, True)
    writer = StreamWriter(paths, "jev-turn")
    reply = agent._produce("user", "What do I prefer for Friday updates?", writer=writer)
    assert reply
    events = list(StreamReader(paths, "jev-turn").events(timeout=1))
    assert any(e.type == "status" and e.value == "working" for e in events)
    assert [(e.name, e.value) for e in events if e.type == "tool"] == [
        ("jev_context", "active"),
        ("jev_context", "done"),
    ]
    assert len(upstream) == 2
    jev.set_enabled(paths, False)
    assert agent._produce("user", "Friday updates?")
    assert len(upstream) == 2


@pytest.mark.parametrize(
    "error", [TimeoutError(), urllib.error.HTTPError(jev.API, 429, "limit", {}, None)]
)
def test_timeout_and_rate_limit_fall_back(paths, upstream, monkeypatch, error):
    jev.connect(paths, "key")
    jev.set_enabled(paths, True)

    class Broken:
        def open(self, *args, **kwargs):
            raise error

    monkeypatch.setattr(jev.urllib.request, "build_opener", lambda *a: Broken())
    items = [{"text": "preserve me"}]
    assert jev.select_context(paths, "request", items) is items
