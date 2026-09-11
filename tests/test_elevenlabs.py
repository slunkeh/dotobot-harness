"""Opt-in audio uses ordinary bot admission, governance and transcript storage."""

import io
import json
import threading
import urllib.error
import uuid
from contextlib import contextmanager

import pytest

from agent import messaging
from agent.history import user_thread
from agent.runtime import build_agent
from agent.streaming import StreamReader, StreamWriter
from harness import elevenlabs, prefs, sends, voice
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.redaction import SecretRedactionRegistry, scrub
from harness.roster import Bot, RosterError, load_roster
from harness.secrets import get_secret
from harness.server import asdict_event, make_server
from providers.base import Completion, Provider, ToolCall
from tests.test_ws import WSClient, _http


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr(voice, "_xai_token", lambda _: "legacy-grok")
    monkeypatch.setattr(voice, "_openai_token", lambda _: "legacy-openai")
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    return paths


@pytest.fixture
def upstream(monkeypatch):
    calls = []
    real_open = elevenlabs.urllib.request.urlopen

    def open_(req, **kwargs):
        if req.full_url.startswith("http://127.0.0.1:"):
            return real_open(req, **kwargs)
        calls.append(req)
        if "/single-use-token/" in req.full_url:
            value = {"token": "single-use-" + uuid.uuid4().hex}
        elif "/v1/voices/" in req.full_url:
            value = {"voice_id": req.full_url.rsplit("/", 1)[-1]}
        else:
            value = {
                "voices": [{"voice_id": "chosen", "name": "My chosen voice"}],
                "has_more": False,
            }
        return io.BytesIO(json.dumps(value).encode())

    monkeypatch.setattr(elevenlabs.urllib.request, "urlopen", open_)
    return calls


def test_connect_does_not_change_auto_or_choose_voice(paths, upstream):
    before = voice.resolve_backend(paths, "codex")
    elevenlabs.connect(paths, "customer-private-key")
    state = voice.voice_status(paths)
    assert state["provider"] == "auto"
    assert state["elevenlabs_voice_id"] is None
    assert voice.resolve_backend(paths, "codex") == before == "openai"
    assert "customer-private-key" not in json.dumps(state)
    assert (paths.credentials / elevenlabs.KEY).stat().st_mode & 0o777 == 0o600
    assert get_secret(elevenlabs.KEY, paths) == "customer-private-key"
    assert "customer-private-key" not in scrub("customer-private-key")


@pytest.mark.parametrize(
    "account,override,expected",
    [
        ("elevenlabs", None, "elevenlabs"),
        ("grok", "elevenlabs", "elevenlabs"),
        ("elevenlabs", "grok", "grok"),
        ("elevenlabs", "auto", "openai"),
        ("auto", None, "openai"),
        ("grok", None, "grok"),
    ],
)
def test_provider_precedence(paths, upstream, account, override, expected):
    elevenlabs.connect(paths, "customer-key")
    voice.set_voice_settings(paths, {"provider": account})
    assert voice.resolve_backend(paths, "codex", override) == expected


def test_explicit_missing_provider_never_falls_back(paths, monkeypatch):
    with pytest.raises(voice.VoiceError, match="Connect ElevenLabs"):
        voice.resolve_backend(paths, "grok", "elevenlabs")
    monkeypatch.setattr(voice, "_xai_token", lambda _: None)
    with pytest.raises(voice.VoiceError, match="Grok voice is unavailable"):
        voice.resolve_backend(paths, "codex", "grok")


def test_account_and_bot_voice_selection(paths, upstream):
    elevenlabs.connect(paths, "customer-key")
    voice.set_voice_settings(paths, {"provider": "elevenlabs"})
    with pytest.raises(voice.VoiceError, match="Choose"):
        voice.mint_session(paths, bot="atlas")
    voice.set_voice_settings(paths, {"elevenlabs_voice_id": "chosen"})
    assert voice.mint_session(paths, bot="atlas")["voice"] == "chosen"
    result = voice.mint_session(paths, bot="atlas", elevenlabs_voice_id="override")
    assert result["voice"] == "override"
    assert result["backend"] == "elevenlabs"
    assert result["call_id"] and result["model"] == "scribe_v2_realtime"
    assert result["token"] not in scrub(result["token"])
    assert (
        len(elevenlabs.connection(paths, "atlas", result["call_id"], "tts_websocket")["token"]) > 10
    )
    assert any(r.full_url.endswith("/realtime_scribe") for r in upstream)
    assert any(r.full_url.endswith("/tts_websocket") for r in upstream)
    assert all(r.get_header("Xi-api-key") == "customer-key" for r in upstream)
    assert prefs.load(paths)["elevenlabs_voice_id"] == "chosen"


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_upstream_errors_never_echo_keys(paths, upstream, monkeypatch, status):
    elevenlabs.connect(paths, "customer-key")

    def fail(req, **kwargs):
        raise urllib.error.HTTPError(
            req.full_url + "?key=customer-key",
            status,
            "customer-key",
            {},
            io.BytesIO(b"customer-key single-use-token"),
        )

    monkeypatch.setattr(elevenlabs.urllib.request, "urlopen", fail)
    with pytest.raises(voice.VoiceError) as caught:
        elevenlabs.validate_voice(paths, "deleted")
    assert "customer-key" not in str(caught.value)
    assert "single-use-token" not in str(caught.value)


@pytest.mark.parametrize("value", [None, "", "../voice", "a/b", "x?token=a", 12])
def test_invalid_voice_ids_never_make_requests(paths, upstream, value):
    with pytest.raises(voice.VoiceError):
        elevenlabs.validate_voice(paths, value)
    assert not upstream


def test_token_type_and_call_validation(paths, upstream):
    elevenlabs.connect(paths, "customer-key")
    call = elevenlabs.session(paths, "atlas", "chosen")["call_id"]
    with pytest.raises(voice.VoiceError):
        elevenlabs.connection(paths, "other-bot", call, "realtime_scribe")
    with pytest.raises(voice.VoiceError):
        elevenlabs.connection(paths, "atlas", call, "agent_token")
    start = elevenlabs.event(paths, "atlas", call, "started")
    again = elevenlabs.event(paths, "atlas", call, "started")
    assert again["duplicate"] and again["message_id"] == start["message_id"]
    elevenlabs.event(paths, "atlas", call, "ended")
    with pytest.raises(voice.VoiceError, match="ended"):
        elevenlabs.connection(paths, "atlas", call, "tts_websocket")
    rows = user_thread(build_agent(paths, Bot("atlas")).memory, peer="user")
    assert len(rows) == 2
    assert {r["voice_call_id"] for r in rows} == {call}
    assert len({r["message_id"] for r in rows}) == 2


def test_disconnect_keeps_preferences_and_env_has_repairable_error(paths, upstream, monkeypatch):
    elevenlabs.connect(paths, "customer-key")
    voice.set_voice_settings(paths, {"provider": "elevenlabs", "elevenlabs_voice_id": "chosen"})
    assert not elevenlabs.disconnect(paths)["configured"]
    assert voice.voice_status(paths)["provider"] == "elevenlabs"
    assert voice.voice_status(paths)["elevenlabs_voice_id"] == "chosen"
    monkeypatch.setenv(elevenlabs.KEY, "environment-key")
    with pytest.raises(voice.VoiceError, match="environment"):
        elevenlabs.disconnect(paths)


def test_roster_voice_overrides_roundtrip(tmp_path):
    path = tmp_path / "roster.toml"
    path.write_text('[[bots]]\nname="atlas"\nprovider="echo"\n')
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=path, backend="process")
    orch.init()
    assert orch.roster.get("atlas").voice_provider is None
    orch.update_bot("atlas", voice_provider="elevenlabs", elevenlabs_voice_id="chosen")
    saved = load_roster(orch.paths.home / "roster.json").get("atlas")
    assert saved.voice_provider == "elevenlabs" and saved.elevenlabs_voice_id == "chosen"
    orch.update_bot("atlas", voice_provider=None, elevenlabs_voice_id=None)
    assert orch.roster.get("atlas").voice_provider is None
    with pytest.raises(RosterError):
        orch.update_bot("atlas", voice_provider="invalid")
    assert orch.roster.get("atlas").voice_provider is None


@contextmanager
def server(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text('[[bots]]\nname="atlas"\nprovider="echo"\n')
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    httpd = make_server(orch, "127.0.0.1", 0)
    voice.apply_to_handler(httpd.RequestHandlerClass)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        yield orch, httpd.server_address
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=2)
        orch.down()


def test_committed_voice_uses_bot_stream_and_saved_history_once(tmp_path, upstream):
    with server(tmp_path) as (orch, (host, port)):
        _http(host, port, "/api/voice/elevenlabs/key", "POST", {"key": "customer-key"})
        _http(
            host,
            port,
            "/api/voice",
            "PATCH",
            {"provider": "elevenlabs", "elevenlabs_voice_id": "chosen"},
        )
        session = _http(host, port, "/api/voice/session", "POST", {"bot": "atlas"})
        call = session["call_id"]
        agent = build_agent(orch.paths, orch.roster.get("atlas"), stream_delay=0)
        client = WSClient(host, port)
        try:
            all_frames = []
            for index in range(2):
                # Identical spoken text is intentional, each commit has its own ID.
                mid = f"utterance-{index}"
                payload = {
                    "type": "chat",
                    "bot": "atlas",
                    "text": "hello",
                    "voice_call_id": call,
                    "message_id": mid,
                    "client_nonce": mid,
                }
                client.send(payload)
                frames = client.recv_until("user")
                assert frames[-1]["message_id"] == mid
                assert frames[-1]["voice_call_id"] == call
                agent.process_inbox_once()
                frames += client.recv_until("final")
                all_frames += frames
                speech = [f for f in frames if f["type"] == "message"]
                assert speech and speech[-1]["voice_text"]
                assert all(f["voice_input_id"] == mid and f["origin"] == "voice" for f in speech)
                client.send(payload)
                replay = client.recv_until("final")
                assert any(f.get("duplicate") for f in replay)
                assert not messaging.pending(orch.paths, "atlas")
            history = _http(host, port, "/api/bots/atlas/history")
            assert len(history) == 4
            assert len({row["message_id"] for row in history}) == 4
            assert {row["voice_call_id"] for row in history} == {call}
            assert [r["text"] for r in history if r["frm"] == "user"] == ["hello", "hello"]
            for frame in all_frames:
                if frame["type"] == "message" and frame.get("streaming") is False:
                    assert frame["id"] in {row["message_id"] for row in history}
            # Closing does not erase accepted work, and an uncertain send still reconciles.
            _http(
                host,
                port,
                "/api/bots/atlas/voice_event",
                "POST",
                {"event": "ended", "voice_call_id": call},
            )
            assert sends.lookup(orch.paths, "utterance-1")["status"] == "accepted"
            client.send(payload)
            assert any(f.get("duplicate") for f in client.recv_until("final"))
        finally:
            client.rfile.close()
            client.sock.close()


def test_steered_voice_keeps_governed_tools_and_new_speech_context(paths):
    agent = build_agent(paths, Bot("atlas", provider="echo"), stream_delay=0)

    class SteerProvider(Provider):
        n = 0

        def complete(self, messages, **kwargs):
            self.n += 1
            if self.n == 1:
                messaging.send(
                    paths,
                    messaging.Msg(
                        to="atlas",
                        frm="user",
                        text="new request",
                        origin="voice",
                        message_id="second",
                        voice_call_id="call",
                    ),
                )
                return Completion(
                    text="Old response.",
                    tool_calls=[
                        ToolCall(id="remember1", name="remember", arguments={"text": "voice fact"})
                    ],
                )
            assert "new request" in str(messages)
            return Completion(text="New response.")

    agent.provider = SteerProvider("test")
    msg = messaging.Msg(
        to="atlas",
        frm="user",
        text="remember a fact",
        origin="voice",
        message_id="first",
        voice_call_id="call",
    )
    messaging.send(paths, msg)
    agent.process_inbox_once()
    frames = [asdict_event(e) for e in StreamReader(paths, msg.id)._read_new()]
    speech = [f for f in frames if f["type"] == "message" and f["voice_input_id"] == "second"]
    assert speech and "New response." in speech[-1]["voice_text"]
    assert "Old response." not in speech[-1]["voice_text"]
    rows = user_thread(agent.memory, peer="user")
    assert [r["message_id"] for r in rows if r["frm"] == "user"] == ["first", "second"]
    assert {r["voice_call_id"] for r in rows} == {"call"}
    assert not messaging.pending(paths, "atlas")
    assert agent.memory.recall("voice fact")


def test_speech_never_emits_secret_or_partial_secret(paths):
    reg = SecretRedactionRegistry()
    sentinel = reg.register("secret-123456")
    assert reg.speech("Your key is secret-123") == "Your key is "
    assert reg.speech("Your key is secret-123456.") == "Your key is [private]."
    assert reg.speech("Value: " + sentinel) == "Value: [private]"
    assert reg.speech("Value: " + sentinel[:15]) == "Value: "


def test_stream_voice_context_does_not_leak_to_unrelated_reply(paths):
    writer = StreamWriter(paths, "request")
    writer.set_voice_context("call", "first")
    writer.delta("Spoken words")
    writer.set_voice_context(None, "typed-followup")
    writer.final("Unrelated typed reply", "atlas")
    final = [asdict_event(e) for e in StreamReader(paths, "request")._read_new()][-1]
    assert final["voice_call_id"] is None


def test_voice_tool_approval_and_call_close_keep_the_accepted_turn(paths, upstream):
    from agent.streaming import list_prompts, write_answer
    from harness import audit
    from tests.test_approvals import _answer_next_confirm

    elevenlabs.connect(paths, "customer-key")
    call = elevenlabs.session(paths, "atlas", "chosen")["call_id"]
    (paths.home / "policy.toml").write_text('[[ask]]\nintent="write_state"\norigin="voice"\n')
    agent = build_agent(paths, Bot("atlas", provider="echo"), stream_delay=0)

    class ApprovalProvider(Provider):
        n = 0

        def complete(self, messages, **kwargs):
            self.n += 1
            if self.n == 1:
                return Completion(
                    tool_calls=[
                        ToolCall(
                            id="approval-tool",
                            name="remember",
                            arguments={"text": "approved voice fact"},
                        )
                    ]
                )
            return Completion(text="Saved after approval.")

    agent.provider = ApprovalProvider("test")
    msg = messaging.Msg(
        to="atlas",
        frm="user",
        text="remember approved voice fact",
        origin="voice",
        voice_call_id=call,
        message_id="input",
    )
    messaging.send(paths, msg)
    # Closing the audio call is not /stop. An admitted bot turn continues.
    elevenlabs.event(paths, "atlas", call, "ended")
    answer = _answer_next_confirm(paths, "confirm")
    try:
        agent.process_inbox_once()
    finally:
        # Do not leave a waiter behind if an assertion/provider fails.
        for prompt in list_prompts(paths):
            write_answer(paths, prompt["id"], "confirm")
        answer.join(timeout=6)
    frames = [asdict_event(e) for e in StreamReader(paths, msg.id)._read_new()]
    confirms = [f for f in frames if f["card_type"] == "confirm"]
    assert confirms and all(f["voice_call_id"] == call for f in confirms)
    assert all(f["voice_text"] is None for f in confirms)
    assert any(
        row.get("tool") == "remember" and row.get("source") == "ask"
        for row in audit.read(paths, "atlas")
    )
    assert frames[-1]["text"] == "Saved after approval."
    assert agent.memory.recall("approved voice fact")


def test_takeover_voice_turn_retains_control_return_card(paths):
    import time

    from agent.streaming import list_prompts, write_answer

    agent = build_agent(paths, Bot("atlas", provider="echo"), stream_delay=0)
    agent.control.take_over("atlas")
    msg = messaging.Msg(
        to="atlas",
        frm="user",
        text="give control back",
        origin="voice",
        voice_call_id="call",
        message_id="input",
    )
    messaging.send(paths, msg)

    def dismiss():
        until = time.monotonic() + 5
        while time.monotonic() < until:
            for prompt in list_prompts(paths):
                if prompt.get("card_type") == "control_return":
                    write_answer(paths, prompt["id"], "dismiss")
                    return
            time.sleep(0.01)

    answer = threading.Thread(target=dismiss, daemon=True)
    answer.start()
    agent.process_inbox_once()
    answer.join(timeout=5)
    frames = [asdict_event(e) for e in StreamReader(paths, msg.id)._read_new()]
    cards = [f for f in frames if f["card_type"] == "control_return"]
    assert cards and all(f["voice_call_id"] == "call" for f in cards)
    assert all(f["voice_text"] is None for f in cards)
    assert agent.control.state("atlas").mode == "takeover"


def test_voice_steering_into_typed_work_applies_voice_policy(paths):
    from harness import audit

    (paths.home / "policy.toml").write_text('[[deny]]\nintent="write_state"\norigin="voice"\n')
    agent = build_agent(paths, Bot("atlas", provider="echo"), stream_delay=0)

    class VoiceSteer(Provider):
        n = 0

        def complete(self, messages, **kwargs):
            self.n += 1
            if self.n == 1:
                messaging.send(
                    paths,
                    messaging.Msg(
                        to="atlas",
                        frm="user",
                        text="remember a voice fact",
                        origin="voice",
                        voice_call_id="call",
                        message_id="spoken",
                    ),
                )
                return Completion(text="Initial typed response")
            if self.n == 2:
                return Completion(
                    tool_calls=[
                        ToolCall(
                            id="remember", name="remember", arguments={"text": "must not store"}
                        )
                    ]
                )
            return Completion(text="That action needs a different policy.")

    agent.provider = VoiceSteer("test")
    messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="initial typed input"))
    agent.process_inbox_once()
    decisions = [r for r in audit.read(paths, "atlas") if r.get("tool") == "remember"]
    assert decisions and decisions[0]["decision"] == "refuse"
    assert "must not store" not in str(agent.memory.facts())


@pytest.mark.parametrize("ending", ["with", "which", "yeah", "h"])
def test_final_speech_releases_nonsecret_trailing_letters(paths, ending):
    writer = StreamWriter(paths, "final-letters")
    writer.set_voice_context("call", "input")
    writer.delta(ending)
    writer.final(ending, "atlas")
    frames = [asdict_event(e) for e in StreamReader(paths, "final-letters")._read_new()]
    closed = [f for f in frames if f["type"] == "message" and f["streaming"] is False]
    assert closed[0]["voice_text"] == ending
    reg = SecretRedactionRegistry()
    reg.register("secret-key")
    assert reg.speech("secret-key", final=True) == "[private]"


def test_elevenlabs_key_alone_does_not_make_auto_available(paths, upstream, monkeypatch):
    monkeypatch.setattr(voice, "_xai_token", lambda _: None)
    monkeypatch.setattr(voice, "_openai_token", lambda _: None)
    elevenlabs.connect(paths, "customer-key")
    assert voice.voice_status(paths)["available"] is False
    assert voice.resolve_backend(paths) is None
    voice.set_voice_settings(paths, {"provider": "elevenlabs"})
    assert voice.voice_status(paths)["available"] is True
