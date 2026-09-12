"""Live-talk token mint + 1:1 voice_turn logging."""

from __future__ import annotations

import json
import threading

import pytest

from agent.history import user_thread
from agent.memory import Memory
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.server import make_server
from harness.voice import (
    VoiceError,
    apply_to_handler,
    log_voice_event,
    log_voice_turn,
    mint_session,
    resolve_backend,
    run_voice_tool,
    set_voice_fallback,
    voice_status,
)
from tests.test_transcript_merge import merge_history
from tests.test_ws import WSClient, _http


def test_mint_session_requires_xai(tmp_path, monkeypatch):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    monkeypatch.setattr("harness.voice._xai_token", lambda _p: None)
    monkeypatch.setattr("harness.voice._openai_token", lambda _p: None)
    with pytest.raises(VoiceError, match="Grok or OpenAI"):
        mint_session(paths, bot="atlas")


def test_voice_hidden_when_only_claude_is_configured(tmp_path, monkeypatch):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    monkeypatch.setattr("harness.voice._xai_token", lambda _p: None)
    monkeypatch.setattr("harness.voice._openai_token", lambda _p: None)
    st = voice_status(paths)
    assert st["available"] is False
    assert st["providers"] == []
    assert resolve_backend(paths, "claude") is None


def test_claude_bot_uses_voice_fallback(tmp_path, monkeypatch):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    monkeypatch.setattr("harness.voice._xai_token", lambda _p: "xai")
    monkeypatch.setattr("harness.voice._openai_token", lambda _p: "sk")
    assert resolve_backend(paths, "claude") == "grok"
    set_voice_fallback(paths, "openai")
    assert voice_status(paths)["fallback"] == "openai"
    assert resolve_backend(paths, "claude") == "openai"
    assert resolve_backend(paths, "grok") == "grok"


def test_mint_openai_when_fallback_is_openai(tmp_path, monkeypatch):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    monkeypatch.setattr("harness.voice._xai_token", lambda _p: None)
    monkeypatch.setattr("harness.voice._openai_token", lambda _p: "sk-test")
    captured = {}

    class _Resp:
        def read(self):
            return json.dumps({"value": "ek_1"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_open(req, timeout=30):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr("harness.voice.urllib.request.urlopen", fake_open)
    out = mint_session(paths, bot="claude-bot", bot_provider="claude")
    assert out["backend"] == "openai"
    assert out["auth"] == "bearer"
    assert out["token"] == "ek_1"
    assert "api.openai.com" in out["ws_url"]
    assert captured["url"].endswith("/v1/realtime/client_secrets")
    assert captured["body"]["session"]["model"] == "gpt-realtime"


def test_mint_session_posts_ephemeral_secret(tmp_path, monkeypatch):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    monkeypatch.setattr("harness.voice._xai_token", lambda _p: "master-key")
    monkeypatch.setattr("harness.voice.load_soul", lambda *_a, **_k: "You are Atlas.")

    captured = {}

    class _Resp:
        def read(self):
            return json.dumps({"value": "ephem-1", "expires_at": 1700000000}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_open(req, timeout=30):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        captured["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr("harness.voice.urllib.request.urlopen", fake_open)
    out = mint_session(paths, bot="atlas", display_name="Atlas")
    assert out["token"] == "ephem-1"
    assert out["bot"] == "atlas"
    assert "grok-voice-latest" in out["ws_url"]
    assert "Atlas" in out["instructions"]
    assert "You are Atlas." in out["instructions"]
    assert "act" in out["instructions"]
    assert "Past conversation" in out["instructions"] or "1:1" in out["instructions"]
    assert any(t.get("name") == "act" for t in out["tools"])
    assert captured["url"].endswith("/v1/realtime/client_secrets")
    assert captured["auth"] == "Bearer master-key"
    assert captured["body"]["session"]["model"] == "grok-voice-latest"


def test_log_voice_turn_lands_on_user_thread(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    out = log_voice_turn(paths, "atlas", user="hello there", assistant="hi, I'm here")
    assert out["ok"] is True
    mem = Memory(paths=paths, bot="atlas")
    rows = user_thread(mem, peer="user")
    texts = [r["text"] for r in rows]
    assert "hello there" in texts
    assert "hi, I'm here" in texts
    speakers = {r["text"]: r["frm"] for r in rows}
    assert speakers["hello there"] == "user"
    assert speakers["hi, I'm here"] == "atlas"
    assert all(r.get("origin") == "voice" for r in rows)


@pytest.mark.parametrize(
    "turns",
    [
        [
            {"user": "Hey", "assistant": "Hey there. How can I help today?"},
            {"user": "Are you using Grok AI or Codex AI?", "assistant": "I'm built on xAI's Grok."},
        ],
        [{"user": "Hey", "assistant": "Hello"}] * 2,
        [{"user": "Hey"}],
        [{"assistant": "Hello"}],
    ],
    ids=["conversation", "repeated-utterance", "user-only", "assistant-only"],
)
def test_voice_live_updates_match_saved_history(tmp_path, turns):
    """Reloading history must reconcile live voice rows, not duplicate them."""
    rp = tmp_path / "roster.toml"
    rp.write_text('[[bots]]\nname = "atlas"\nprovider = "echo"\n', encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    httpd = make_server(orch, "127.0.0.1", 0)
    apply_to_handler(httpd.RequestHandlerClass)
    host, port = httpd.server_address
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    client = None
    try:
        client = WSClient(host, port)
        live = []
        expected = []
        for turn in turns:
            out = _http(host, port, "/api/bots/atlas/voice_turn", "POST", turn)
            assert out["ok"] is True
            assert out["user"] is bool(turn.get("user"))
            assert out["assistant"] is bool(turn.get("assistant"))
            frames = client.recv_until("final" if turn.get("assistant") else "user")
            for frame in frames:
                if frame["type"] not in {"user", "final"}:
                    continue
                assert frame["bot"] == "atlas"
                assert frame["origin"] == "voice"
                assert frame["mutation"] == "appended"
                live.append(
                    {
                        "id": f"live-{len(live)}",
                        "kind": "user" if frame["type"] == "user" else "assistant",
                        "author": None if frame["frm"] == "user" else frame["frm"],
                        "text": frame["text"],
                        "origin": frame["origin"],
                        "message_id": frame.get("message_id"),
                    }
                )
            expected.extend(turn[k] for k in ("user", "assistant") if turn.get(k))
            history = _http(host, port, "/api/bots/atlas/history")
            assert [row["text"] for row in history] == expected
            assert [row["text"] for row in live] == expected
            # IDs must agree across both transports, even for identical words.
            assert [row["message_id"] for row in live] == [row["message_id"] for row in history]
            assert len({row["message_id"] for row in live}) == len(live)
            mapped = [
                {
                    "id": f"history-{i}",
                    "kind": "user" if row["frm"] == "user" else "assistant",
                    "author": None if row["frm"] == "user" else row["frm"],
                    "text": row["text"],
                    "origin": row["origin"],
                    "message_id": row["message_id"],
                }
                for i, row in enumerate(history)
            ]
            merged = merge_history(live, mapped)
            assert [row["text"] for row in merged] == expected
            assert [row["id"] for row in merged] == [row["id"] for row in live]
            assert merge_history(merged, mapped) == merged
    finally:
        if client is not None:
            client.rfile.close()
            client.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        orch.down()


def test_log_voice_event_marks_start_and_end(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    started = log_voice_event(paths, "atlas", "started")
    log_voice_turn(paths, "atlas", user="hi", assistant="hello")
    ended = log_voice_event(paths, "atlas", "ended")
    assert started["event"] == "started"
    assert ended["event"] == "ended"
    rows = user_thread(Memory(paths=paths, bot="atlas"), peer="user")
    texts = [r["text"] for r in rows]
    assert texts[0] == "Voice chat started"
    assert texts[-1] == "Voice chat ended"
    assert "hi" in texts and "hello" in texts
    assert all(r.get("origin") == "voice" for r in rows)
    with pytest.raises(VoiceError, match="started or ended"):
        log_voice_event(paths, "atlas", "paused")


def test_log_voice_turn_rejects_empty(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    with pytest.raises(VoiceError, match="user"):
        log_voice_turn(paths, "atlas")


def test_mint_includes_prior_chat(tmp_path, monkeypatch):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    log_voice_turn(paths, "atlas", user="check reddit for me", assistant="opening it")
    monkeypatch.setattr("harness.voice._xai_token", lambda _p: "master-key")

    class _Resp:
        def read(self):
            return json.dumps({"value": "ephem-1"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("harness.voice.urllib.request.urlopen", lambda *a, **k: _Resp())
    out = mint_session(paths, bot="atlas", display_name="Atlas")
    assert "check reddit for me" in out["instructions"]
    assert "opening it" in out["instructions"]


def test_voice_recall_and_remember(tmp_path):
    from harness.orchestrator import Orchestrator

    rp = tmp_path / "roster.toml"
    rp.write_text('[[bots]]\nname = "atlas"\nrole = "r"\nprovider = "echo"\n')
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    assert "nothing" in run_voice_tool(orch, "atlas", "recall", {"query": "reddit"}).lower()
    assert (
        run_voice_tool(orch, "atlas", "remember", {"fact": "User wants Reddit in Chrome"})
        == "remembered"
    )
    hit = run_voice_tool(orch, "atlas", "recall", {"query": "reddit chrome"})
    assert "Reddit in Chrome" in hit


def test_explicit_openai_voice_default_overrides_native_backend(tmp_path, monkeypatch):
    from harness.voice import set_voice_settings

    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    monkeypatch.setattr("harness.voice._xai_token", lambda _p: "xai")
    monkeypatch.setattr("harness.voice._openai_token", lambda _p: "openai")
    result = set_voice_settings(paths, {"provider": "openai"})
    assert result["provider"] == "openai"
    assert result["available"] is True
    assert resolve_backend(paths, "grok") == "openai"
    assert resolve_backend(paths, "claude") == "openai"
    assert resolve_backend(paths, "grok", "grok") == "grok"
    assert resolve_backend(paths, "grok", "auto") == "grok"
    monkeypatch.setattr("harness.voice._openai_token", lambda _p: None)
    assert voice_status(paths)["available"] is False
    with pytest.raises(VoiceError, match="OpenAI"):
        resolve_backend(paths, "grok")
