"""Codex CLI ChatGPT login reuse — no live network."""

from __future__ import annotations

import base64
import io
import json
import os
import urllib.error
import urllib.request

import pytest

from providers import build_provider, codex_login
from providers.base import Message, ProviderError, ToolCall, ToolSpec
from providers.codex_chatgpt import CodexChatGPTProvider
from providers.codex_login import (
    CodexLoginError,
    configured_model,
    configured_reasoning_effort,
    jwt_audience,
    load_credentials,
    login_available,
    refresh_credentials,
    retry_once_on_401,
)


def _jwt(payload: dict) -> str:
    def seg(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg(payload)}.sig"


def _auth_doc(**over) -> dict:
    doc = {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "id_token": _jwt({"aud": "app_client-1"}),
            "account_id": "acct-9",
        },
        "OPENAI_API_KEY": None,
    }
    doc.update(over)
    return doc


@pytest.fixture
def home(tmp_path):
    d = tmp_path / "codex-home"
    d.mkdir()
    return d


def _write_auth(home, doc=None, mode=0o600):
    path = home / "auth.json"
    path.write_text(json.dumps(_auth_doc() if doc is None else doc), encoding="utf-8")
    os.chmod(path, mode)
    return path


class FakeRefresh(codex_login.Transport):
    """Stub for auth.openai.com/oauth/token."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    def post_form(self, url, data):
        self.calls.append((url, dict(data)))
        return self.payload


# -- reading auth.json through the permission gate ---------------------------
def test_loads_chatgpt_credentials(home):
    _write_auth(home)
    creds = load_credentials(home)
    assert creds.access_token == "at-1"
    assert creds.refresh_token == "rt-1"
    assert creds.account_id == "acct-9"
    assert creds.path == home / "auth.json"
    assert login_available(home) is True


def test_credentials_repr_never_leaks_tokens(home):
    _write_auth(home)
    assert "at-1" not in repr(load_credentials(home))


def test_missing_auth_file_says_run_codex_login(home):
    with pytest.raises(CodexLoginError, match="codex login"):
        load_credentials(home)
    assert login_available(home) is False


def test_gate_refuses_symlink(home, tmp_path):
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps(_auth_doc()), encoding="utf-8")
    os.chmod(real, 0o600)
    (home / "auth.json").symlink_to(real)
    with pytest.raises(CodexLoginError, match="regular file, not a symlink"):
        load_credentials(home)


def test_gate_refuses_group_or_world_access(home):
    for mode in (0o640, 0o604, 0o644, 0o660):
        _write_auth(home, mode=mode)
        with pytest.raises(CodexLoginError, match="chmod 600"):
            load_credentials(home)
    # owner-only variants pass the gate
    for mode in (0o600, 0o400):
        _write_auth(home, mode=mode)
        assert load_credentials(home).access_token == "at-1"


def test_gate_refuses_non_regular_file(home):
    (home / "auth.json").mkdir()
    with pytest.raises(CodexLoginError, match="regular file"):
        load_credentials(home)


def test_requires_chatgpt_auth_mode_and_all_tokens(home):
    _write_auth(home, doc=_auth_doc(auth_mode="apikey"))
    with pytest.raises(CodexLoginError, match="codex login"):
        load_credentials(home)

    doc = _auth_doc()
    doc["tokens"]["account_id"] = ""
    _write_auth(home, doc=doc)
    with pytest.raises(CodexLoginError, match="codex login"):
        load_credentials(home)

    _write_auth(home, doc={"auth_mode": "chatgpt"})
    with pytest.raises(CodexLoginError, match="codex login"):
        load_credentials(home)


def test_corrupt_auth_json_says_run_codex_login(home):
    path = home / "auth.json"
    path.write_text("not json", encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(CodexLoginError, match="codex login"):
        load_credentials(home)


# -- JWT aud extraction ------------------------------------------------------
def test_jwt_audience_string():
    assert jwt_audience(_jwt({"aud": "app_client-1"})) == "app_client-1"


def test_jwt_audience_list_picks_first_string():
    assert jwt_audience(_jwt({"aud": [None, "app_x", "other"]})) == "app_x"


def test_jwt_audience_handles_unpadded_base64url():
    # payload length chosen so base64url encoding needs padding added back
    token = _jwt({"aud": "ab"})
    assert jwt_audience(token) == "ab"


def test_jwt_audience_garbage_is_none():
    assert jwt_audience("") is None
    assert jwt_audience("only-one-segment") is None
    assert jwt_audience("a.!!!not-base64!!!.c") is None
    assert jwt_audience(_jwt({"sub": "no aud"})) is None
    assert jwt_audience(_jwt({"aud": 42})) is None


# -- refresh + atomic writeback ----------------------------------------------
def test_refresh_rotates_and_writes_back_atomically(home):
    _write_auth(home)
    transport = FakeRefresh({"access_token": "at-2"})
    creds = refresh_credentials(load_credentials(home), transport=transport)

    url, form = transport.calls[0]
    assert url == codex_login.TOKEN_URL
    assert form == {
        "grant_type": "refresh_token",
        "refresh_token": "rt-1",
        "client_id": "app_client-1",  # recovered from the id_token aud claim
    }
    assert creds.access_token == "at-2"
    # old refresh/id tokens preserved when the response omits them
    assert creds.refresh_token == "rt-1"
    assert creds.id_token == _jwt({"aud": "app_client-1"})

    on_disk = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    assert on_disk["tokens"]["access_token"] == "at-2"
    assert on_disk["tokens"]["refresh_token"] == "rt-1"
    assert on_disk["auth_mode"] == "chatgpt"  # unrelated keys survive
    assert "last_refresh" in on_disk
    # private + atomic: 0600 result, no temp files left behind
    assert os.stat(home / "auth.json").st_mode & 0o777 == 0o600
    assert [p.name for p in home.iterdir()] == ["auth.json"]


def test_refresh_adopts_rotated_refresh_and_id_tokens(home):
    _write_auth(home)
    new_id = _jwt({"aud": "app_client-1", "v": 2})
    transport = FakeRefresh({"access_token": "at-2", "refresh_token": "rt-2", "id_token": new_id})
    creds = refresh_credentials(load_credentials(home), transport=transport)
    assert creds.refresh_token == "rt-2"
    assert creds.id_token == new_id


def test_refresh_with_invalid_identity_is_actionable(home):
    doc = _auth_doc()
    doc["tokens"]["id_token"] = "not.a-jwt"
    _write_auth(home, doc=doc)
    with pytest.raises(CodexLoginError, match="codex login"):
        refresh_credentials(load_credentials(home), transport=FakeRefresh({}))


def test_refresh_without_access_token_is_actionable(home):
    _write_auth(home)
    with pytest.raises(CodexLoginError, match="codex login"):
        refresh_credentials(load_credentials(home), transport=FakeRefresh({"error": "nope"}))
    # the failed refresh must not have clobbered the stored login
    assert load_credentials(home).access_token == "at-1"


# -- the 401-retry-once wrapper ----------------------------------------------
def _unauthorized() -> ProviderError:
    err = ProviderError("Codex (ChatGPT) HTTP 401: expired")
    err.__cause__ = urllib.error.HTTPError("https://x", 401, "unauthorized", None, io.BytesIO())
    return err


def test_retry_once_on_401_refreshes_then_retries():
    calls = {"n": 0, "refreshed": 0}

    def call():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _unauthorized()
        return "ok"

    def refresh():
        calls["refreshed"] += 1

    assert retry_once_on_401(call, refresh) == "ok"
    assert calls == {"n": 2, "refreshed": 1}


def test_retry_once_on_401_does_not_touch_other_errors():
    def call():
        raise ProviderError("HTTP 500: boom")

    def refresh():
        raise AssertionError("refresh must not run for non-401 errors")

    with pytest.raises(ProviderError, match="500"):
        retry_once_on_401(call, refresh)


def test_retry_once_on_401_gives_up_after_one_refresh():
    calls = {"n": 0, "refreshed": 0}

    def call():
        calls["n"] += 1
        raise _unauthorized()

    def refresh():
        calls["refreshed"] += 1

    with pytest.raises(ProviderError, match="401"):
        retry_once_on_401(call, refresh)
    assert calls == {"n": 2, "refreshed": 1}


# -- config.toml niceties ----------------------------------------------------
def test_config_toml_model_and_reasoning_effort(home):
    (home / "config.toml").write_text(
        'model = "gpt-5-codex"\nmodel_reasoning_effort = "high"\n', encoding="utf-8"
    )
    assert configured_model(home) == "gpt-5-codex"
    assert configured_reasoning_effort(home) == "high"


def test_config_toml_missing_or_invalid_is_none(home):
    assert configured_model(home) is None
    assert configured_reasoning_effort(home) is None
    (home / "config.toml").write_text(
        'model = 42\nmodel_reasoning_effort = "extreme"\n[broken', encoding="utf-8"
    )
    assert configured_model(home) is None  # malformed toml -> no defaults
    (home / "config.toml").write_text(
        'model = 42\nmodel_reasoning_effort = "extreme"\n', encoding="utf-8"
    )
    assert configured_model(home) is None  # wrong types are ignored
    # Effort names are whatever the CLI wrote — not a harness allow-list.
    assert configured_reasoning_effort(home) == "extreme"


# -- the provider adapter -----------------------------------------------------
def test_provider_requires_login_before_any_network(home):
    p = CodexChatGPTProvider(codex_home=home)
    with pytest.raises(ProviderError, match="codex login"):
        p.complete([Message(role="user", content="hi")])


def test_provider_defaults_come_from_codex_config(home):
    _write_auth(home)
    (home / "config.toml").write_text(
        'model = "gpt-5-codex"\nmodel_reasoning_effort = "medium"\n', encoding="utf-8"
    )
    p = CodexChatGPTProvider(codex_home=home)
    assert p.model == "gpt-5-codex"
    assert p.reasoning_effort == "medium"
    assert p.base_url == "https://chatgpt.com/backend-api/codex/responses"
    # explicit choices still win over the CLI config
    p = CodexChatGPTProvider(model="gpt-5", codex_home=home, reasoning_effort="low")
    assert p.model == "gpt-5"
    assert p.reasoning_effort == "low"


def test_provider_is_registered():
    p = build_provider("codex-chatgpt", codex_home="/nonexistent")
    assert isinstance(p, CodexChatGPTProvider)


def test_provider_headers_carry_bearer_and_account(home):
    _write_auth(home)
    p = CodexChatGPTProvider(codex_home=home)
    headers = p._headers()
    assert headers["authorization"] == "Bearer at-1"
    assert headers["chatgpt-account-id"] == "acct-9"
    assert headers["accept"] == "text/event-stream"


def test_provider_body_speaks_responses_api(home):
    _write_auth(home)
    p = CodexChatGPTProvider(model="gpt-5", codex_home=home, reasoning_effort="high")
    tools = [ToolSpec(name="remember", description="store", parameters={"type": "object"})]
    body = p._body(
        [
            Message(role="user", content="hi"),
            Message(
                role="assistant",
                content="opening",
                tool_calls=[ToolCall(id="c1", name="computer_open", arguments={"app": "files"})],
            ),
            Message(role="tool", content="ok", tool_call_id="c1", name="computer_open"),
        ],
        system="be useful",
        tools=tools,
        max_tokens=64,
        temperature=0.2,
        stream=True,
    )
    assert body["model"] == "gpt-5"
    assert body["instructions"] == "be useful"
    assert body["stream"] is True and body["store"] is False
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert body["tools"][0]["name"] == "remember"
    assert body["tool_choice"] == "auto"
    items = body["input"]
    assert items[0] == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "hi"}],
    }
    assert items[1]["content"] == [{"type": "output_text", "text": "opening"}]
    assert items[2] == {
        "type": "function_call",
        "call_id": "c1",
        "name": "computer_open",
        "arguments": '{"app": "files"}',
    }
    assert items[3] == {"type": "function_call_output", "call_id": "c1", "output": "ok"}


def _sse(*events) -> list[bytes]:
    lines: list[bytes] = []
    for e in events:
        payload = e if isinstance(e, str) else json.dumps(e)
        lines.append(f"data: {payload}\n".encode())
        lines.append(b"\n")
    return lines


def test_provider_parses_responses_stream():
    deltas: list[str] = []
    out = CodexChatGPTProvider._parse_stream(
        _sse(
            {"type": "response.created"},
            {"type": "response.output_text.delta", "delta": "hel"},
            {"type": "response.output_text.delta", "delta": "lo"},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "recall",
                    "arguments": '{"query": "x"}',
                },
            },
            {
                "type": "response.completed",
                "response": {
                    "id": "resp-1",
                    "usage": {"input_tokens": 3, "output_tokens": 9},
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "c1",  # duplicate of the observed item
                            "name": "recall",
                            "arguments": '{"query": "x"}',
                        }
                    ],
                },
            },
            "[DONE]",
        ),
        deltas.append,
    )
    assert deltas == ["hel", "lo"]
    assert out.text == "hello"
    assert out.finish_reason == "tool_calls"
    assert len(out.tool_calls) == 1
    assert out.tool_calls[0].id == "c1"
    assert out.tool_calls[0].arguments == {"query": "x"}
    assert out.usage == {"input_tokens": 3, "output_tokens": 9}


def test_provider_stream_failure_raises():
    with pytest.raises(ProviderError, match="failed"):
        CodexChatGPTProvider._parse_stream(
            _sse({"type": "response.failed", "response": {"error": {"message": "quota"}}}),
            None,
        )
    with pytest.raises(ProviderError, match="response.completed"):
        CodexChatGPTProvider._parse_stream(_sse({"type": "response.created"}), None)


# -- end to end: 401 -> refresh -> retried call (mock HTTP) -------------------
class _FakeResp:
    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._lines)


def test_provider_401_refreshes_login_once_and_retries(home, monkeypatch):
    _write_auth(home)
    transport = FakeRefresh({"access_token": "at-2", "refresh_token": "rt-2"})
    p = CodexChatGPTProvider(model="gpt-5", codex_home=home, transport=transport)

    seen_bearers: list[str | None] = []

    def fake_urlopen(req, timeout=0):
        seen_bearers.append(req.get_header("Authorization"))
        if req.get_header("Authorization") == "Bearer at-1":
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", None, io.BytesIO(b""))
        assert req.get_header("Chatgpt-account-id") == "acct-9"
        return _FakeResp(
            _sse(
                {"type": "response.output_text.delta", "delta": "hi"},
                {"type": "response.completed", "response": {"id": "r1", "usage": {}}},
            )
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    deltas: list[str] = []
    out = p.stream_completion([Message(role="user", content="hey")], on_delta=deltas.append)

    assert out.text == "hi"
    assert deltas == ["hi"]
    assert seen_bearers == ["Bearer at-1", "Bearer at-2"]
    assert len(transport.calls) == 1  # refreshed exactly once
    # the rotated login was written back for the Codex CLI to reuse
    on_disk = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    assert on_disk["tokens"]["access_token"] == "at-2"
    assert on_disk["tokens"]["refresh_token"] == "rt-2"


def test_provider_survives_non_401_errors_without_refresh(home, monkeypatch):
    _write_auth(home)
    transport = FakeRefresh({"access_token": "at-2"})
    p = CodexChatGPTProvider(model="gpt-5", codex_home=home, transport=transport)

    def fake_urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", None, io.BytesIO(b"oops"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ProviderError, match="500"):
        p.complete([Message(role="user", content="hey")])
    assert transport.calls == []


# -- build_agent routing ------------------------------------------------------
def test_build_agent_reuses_codex_login_without_api_key(home, tmp_path, monkeypatch):
    from agent.runtime import build_agent
    from harness.paths import HarnessPaths
    from harness.roster import Bot

    _write_auth(home)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    paths = HarnessPaths.resolve(tmp_path / "hh")
    paths.ensure_layout(["atlas"])

    agent = build_agent(paths, Bot(name="atlas", role="t", provider="codex"), stream_delay=0.0)
    assert isinstance(agent.provider, CodexChatGPTProvider)

    # with an API key configured, the plain API-key adapter still wins
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    agent = build_agent(paths, Bot(name="atlas", role="t", provider="codex"), stream_delay=0.0)
    assert not isinstance(agent.provider, CodexChatGPTProvider)
    assert agent.provider.auth.api_key == "sk-x"


@pytest.mark.parametrize("response_model", [None, "gpt-5.6-sol"])
def test_subscription_tool_loop_tags_reasoning_when_response_omits_model(
    home, monkeypatch, response_model
):
    provider = CodexChatGPTProvider(model="gpt-6-astra", codex_home=home)
    item = {"type": "reasoning", "id": "rs1", "encrypted_content": "opaque", "summary": []}
    payload = {
        "output": [
            item,
            {"type": "function_call", "call_id": "c1", "name": "recall", "arguments": "{}"},
        ]
    }
    if response_model:
        payload["model"] = response_model
    monkeypatch.setattr(
        provider,
        "_post_stream",
        lambda body: _sse(
            {"type": "response.completed", "response": payload},
        ),
    )
    result = provider.stream_completion([Message(role="user", content="Recall it")])
    assert result.reasoning[0].model == (response_model or provider.model)
    history = [
        Message(
            role="assistant",
            content=result.text,
            tool_calls=result.tool_calls,
            reasoning=result.reasoning,
        ),
        Message(role="tool", content="found", tool_call_id="c1"),
    ]
    body = provider._body(history, None, None, 1024, 0.7, True)
    assert (item in body["input"]) == (response_model is None)
