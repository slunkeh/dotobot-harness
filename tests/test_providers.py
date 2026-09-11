import pytest

from agent.runtime import SCREENSHOT_NOTE
from providers import Auth, ProviderError, available_providers, build_provider
from providers.anthropic import AnthropicProvider
from providers.base import Message, ToolSpec
from providers.echo import EchoProvider


def test_registry_has_expected_ids():
    ids = available_providers()
    for expected in (
        "echo",
        "claude",
        "codex",
        "grok",
        "deepseek",
        "qwen",
        "glm",
        "kimi",
        "minimax",
    ):
        assert expected in ids


def test_chinese_providers_require_credentials():
    for name in ("deepseek", "qwen", "glm", "kimi", "minimax"):
        p = build_provider(name, auth=Auth())
        with pytest.raises(ProviderError):
            p.complete([Message(role="user", content="hi")])


def test_build_unknown_provider_raises():
    with pytest.raises(ProviderError):
        build_provider("does-not-exist")


def test_echo_completes():
    p = EchoProvider(persona="atlas")
    out = p.complete([Message(role="user", content="hello")])
    assert "hello" in out.text
    assert out.finish_reason == "stop"


def test_echo_emits_message_agent_tool_call_on_mention():
    p = EchoProvider()
    tools = [ToolSpec(name="message_agent", description="", parameters={})]
    out = p.complete([Message(role="user", content="@nova please summarize")], tools=tools)
    assert out.tool_calls, "expected a tool call for @mention"
    call = out.tool_calls[0]
    assert call.name == "message_agent"
    assert call.arguments["to"] == "nova"
    assert "summarize" in call.arguments["text"]


def test_echo_asks_for_the_computer_back():
    p = EchoProvider()
    tools = [
        ToolSpec(name="request_control", description="", parameters={}),
        ToolSpec(name="ask_human", description="", parameters={}),
    ]
    out = p.complete(
        [Message(role="user", content="I'm done, you can have the computer back")], tools=tools
    )
    assert out.tool_calls and out.tool_calls[0].name == "request_control"
    assert out.tool_calls[0].arguments["reason"]


def test_echo_still_asks_a_human_to_take_over_when_stuck():
    p = EchoProvider()
    tools = [
        ToolSpec(name="request_control", description="", parameters={}),
        ToolSpec(name="ask_human", description="", parameters={}),
    ]
    out = p.complete([Message(role="user", content="I'm stuck on this")], tools=tools)
    assert out.tool_calls and out.tool_calls[0].name == "ask_human"


def test_echo_wraps_tool_result():
    p = EchoProvider(persona="atlas")
    msgs = [
        Message(role="user", content="@nova hi"),
        Message(role="tool", content="nova replied: hey", name="message_agent"),
    ]
    out = p.complete(msgs)
    assert "nova: hey" in out.text
    assert "relayed reply" not in out.text


def test_auth_repr_does_not_leak_secret():
    auth = Auth(api_key="super-secret-value")
    assert "super-secret-value" not in repr(auth)
    assert "set" in repr(auth)


def test_anthropic_requires_credentials():
    p = AnthropicProvider(auth=Auth())
    with pytest.raises(ProviderError):
        p.complete([Message(role="user", content="hi")])


def test_anthropic_builds_request_body_and_headers():
    p = AnthropicProvider(model="claude-3-5-sonnet-latest", auth=Auth(api_key="k"))
    tools = [ToolSpec(name="remember", description="store", parameters={"type": "object"})]
    body = p._body(
        [Message(role="user", content="hi")],
        system="be nice",
        tools=tools,
        max_tokens=64,
        temperature=0.5,
        stream=False,
    )
    assert body["model"] == "claude-3-5-sonnet-latest"
    # prompt caching on by default: system and the last message become
    # blocks carrying the breakpoint marker (see test_computer_speed.py)
    assert body["system"] == [
        {"type": "text", "text": "be nice", "cache_control": {"type": "ephemeral"}}
    ]
    assert body["messages"] == [
        {
            "role": "user",
            "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}],
        }
    ]
    assert body["tools"][0]["name"] == "remember"
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["temperature"] == 0.5
    headers = p._headers()
    assert headers["x-api-key"] == "k"
    assert "anthropic-version" in headers


def test_anthropic_omits_temperature_on_modern_claude():
    from providers.anthropic import AnthropicProvider

    legacy = AnthropicProvider(model="claude-sonnet-4-6", auth=Auth(api_key="k"))
    modern = AnthropicProvider(model="claude-opus-4-7", auth=Auth(api_key="k"))
    kwargs = dict(
        messages=[Message(role="user", content="hi")],
        system=None,
        tools=None,
        max_tokens=16,
        temperature=0.3,
        stream=False,
    )
    assert legacy._body(**kwargs)["temperature"] == 0.3
    assert "temperature" not in modern._body(**kwargs)
    # MiniMax speaks Anthropic Messages but is not Claude — keep sampling.
    from providers.vendors import MiniMaxProvider

    mm = MiniMaxProvider(model="MiniMax-M2.5", auth=Auth(api_key="k"))
    assert mm._body(**kwargs)["temperature"] == 0.3


def test_anthropic_parses_text_and_tool_use():
    payload = {
        "content": [
            {"type": "text", "text": "hello "},
            {"type": "tool_use", "id": "t1", "name": "remember", "input": {"text": "x"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 3},
    }
    out = AnthropicProvider._parse(payload)
    assert out.text == "hello "
    assert out.tool_calls[0].name == "remember"
    assert out.tool_calls[0].arguments == {"text": "x"}
    assert out.finish_reason == "tool_use"


def test_anthropic_serializes_assistant_tool_calls():
    from providers.base import ToolCall

    p = AnthropicProvider(auth=Auth(api_key="k"))
    body = p._body(
        [
            Message(role="user", content="open my files"),
            Message(
                role="assistant",
                content="Opening now.",
                tool_calls=[ToolCall(id="t1", name="computer_open", arguments={"app": "files"})],
            ),
            Message(role="tool", content="ok: launched thunar", tool_call_id="t1"),
        ],
        system=None,
        tools=None,
        max_tokens=16,
        temperature=0.2,
        stream=False,
    )
    assistant = body["messages"][1]
    assert assistant["role"] == "assistant"
    blocks = {b["type"]: b for b in assistant["content"]}
    assert blocks["text"]["text"] == "Opening now."
    assert blocks["tool_use"]["id"] == "t1"
    assert blocks["tool_use"]["name"] == "computer_open"
    assert blocks["tool_use"]["input"] == {"app": "files"}
    # the following tool_result must reference the same id
    assert body["messages"][2]["content"][0]["tool_use_id"] == "t1"


def test_codex_requires_credentials():
    # Empty Auth: the credentials check must fail before any network I/O.
    prov = build_provider("codex", auth=Auth())
    with pytest.raises(ProviderError, match="OPENAI_API_KEY"):
        prov.complete([Message(role="user", content="hi")])


def test_openai_builds_body_and_headers():
    from providers.openai import OpenAIProvider

    p = OpenAIProvider(auth=Auth(api_key="sk-x"))
    assert p.model == "gpt-5.4-mini"
    assert p.base_url.startswith("https://api.openai.com/")
    tools = [ToolSpec(name="remember", description="store", parameters={"type": "object"})]
    body = p._body(
        [Message(role="user", content="hi")],
        system="be useful",
        tools=tools,
        max_tokens=64,
        temperature=0.2,
        stream=False,
    )
    assert body["instructions"] == "be useful"
    assert body["input"][0] == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "hi"}],
    }
    assert body["tools"][0]["name"] == "remember"
    assert body["max_output_tokens"] == 64
    assert p._headers()["authorization"] == "Bearer sk-x"


def test_openai_reasoning_models_use_max_completion_tokens():
    from providers.openai import OpenAIProvider

    p = OpenAIProvider(model="gpt-5-mini", auth=Auth(api_key="k"))
    body = p._body(
        [Message(role="user", content="hi")],
        system=None,
        tools=None,
        max_tokens=64,
        temperature=0.2,
        stream=False,
    )
    assert body["max_completion_tokens"] == 64
    assert "max_tokens" not in body
    assert "temperature" not in body


def test_openai_base_url_override():
    from providers.openai import OpenAIProvider

    p = OpenAIProvider(
        model="gpt-5-mini",
        auth=Auth(api_key="k"),
        base_url="https://gw.example/v1/chat/completions",
    )
    assert p.base_url == "https://gw.example/v1/chat/completions"


def test_openai_parses_text_and_tool_calls():
    from providers.openai import OpenAIProvider

    payload = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c9",
                            "function": {"name": "recall", "arguments": '{"query": "demo"}'},
                        }
                    ],
                },
            }
        ],
        "usage": {"total_tokens": 7},
    }
    out = OpenAIProvider._parse(payload)
    assert out.text == ""
    assert out.tool_calls[0].name == "recall"
    assert out.tool_calls[0].arguments == {"query": "demo"}
    assert out.finish_reason == "tool_calls"


@pytest.mark.parametrize("model", ["gpt-5-mini", "gpt-5.4-mini"])
def test_openai_serializes_assistant_tool_calls(model):
    from providers.base import ToolCall
    from providers.openai import OpenAIProvider

    p = OpenAIProvider(model=model, auth=Auth(api_key="k"))
    body = p._body(
        [
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="c1", name="computer_open", arguments={"app": "files"})],
            ),
            Message(role="tool", content="ok", tool_call_id="c1", name="computer_open"),
        ],
        system=None,
        tools=None,
        max_tokens=16,
        temperature=0.2,
        stream=False,
    )
    if model == "gpt-5-mini":
        assert body["messages"][0]["tool_calls"][0]["function"]["name"] == "computer_open"
        assert body["messages"][1]["role"] == "tool"
    else:
        assert body["input"][0]["name"] == "computer_open"
        assert body["input"][1] == {"type": "function_call_output", "call_id": "c1", "output": "ok"}


def test_grok_requires_credentials():
    from providers.grok import GrokProvider

    p = GrokProvider(auth=Auth())
    with pytest.raises(ProviderError):
        p.complete([Message(role="user", content="hi")])


def test_grok_builds_openai_compatible_body_and_headers():
    from providers.grok import GrokProvider

    p = GrokProvider(model="grok-4.6", auth=Auth(api_key="xai-k"))
    tools = [ToolSpec(name="remember", description="store", parameters={"type": "object"})]
    body = p._body(
        [Message(role="user", content="hi")],
        system="be useful",
        tools=tools,
        max_tokens=64,
        temperature=0.2,
        stream=False,
    )
    assert body["model"] == "grok-4.6"
    assert body["messages"][0] == {"role": "system", "content": "be useful"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}
    assert body["tools"][0]["function"]["name"] == "remember"
    headers = p._headers()
    assert headers["authorization"] == "Bearer xai-k"


def test_grok_serializes_assistant_tool_calls():
    from providers.base import ToolCall
    from providers.grok import GrokProvider

    p = GrokProvider(auth=Auth(api_key="k"))
    body = p._body(
        [
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="c1", name="computer_open", arguments={"app": "files"})],
            ),
            Message(
                role="tool", content="ok: launched thunar", tool_call_id="c1", name="computer_open"
            ),
        ],
        system=None,
        tools=None,
        max_tokens=16,
        temperature=0.2,
        stream=False,
    )
    assert body["messages"][0]["tool_calls"][0]["function"]["name"] == "computer_open"
    assert body["messages"][1]["role"] == "tool"


@pytest.mark.parametrize("model", ["gpt-5-mini", "gpt-5.4-mini"])
def test_openai_serializes_screenshot_images(model):
    from providers.openai import OpenAIProvider

    jpeg = b"\xff\xd8fake"
    p = OpenAIProvider(model=model, auth=Auth(api_key="k"))
    body = p._body(
        [
            Message(role="tool", content="ok: screenshot", tool_call_id="s1"),
            Message(
                role="user",
                content=SCREENSHOT_NOTE,
                images=[("image/jpeg", jpeg)],
            ),
        ],
        system=None,
        tools=None,
        max_tokens=16,
        temperature=0.2,
        stream=False,
    )
    if model == "gpt-5-mini":
        assert body["messages"][0]["role"] == "tool"
        assert body["messages"][0]["content"] == "ok: screenshot"
        vision = body["messages"][1]
        kinds = [part["type"] for part in vision["content"]]
        assert "text" in kinds
        assert "image_url" in kinds
        url = next(
            part["image_url"]["url"] for part in vision["content"] if part["type"] == "image_url"
        )
    else:
        assert body["input"][0]["type"] == "function_call_output"
        assert body["input"][0]["output"] == "ok: screenshot"
        vision = body["input"][1]
        kinds = [part["type"] for part in vision["content"]]
        assert "input_text" in kinds
        assert "input_image" in kinds
        url = next(part["image_url"] for part in vision["content"] if part["type"] == "input_image")
    assert vision["role"] == "user"
    assert url.startswith("data:image/jpeg;base64,")


def test_anthropic_serializes_screenshot_images():
    jpeg = b"\xff\xd8fake"
    p = AnthropicProvider(auth=Auth(api_key="k"))
    body = p._body(
        [
            Message(
                role="user",
                content=SCREENSHOT_NOTE,
                images=[("image/jpeg", jpeg)],
            )
        ],
        system=None,
        tools=None,
        max_tokens=16,
        temperature=0.2,
        stream=False,
    )
    parts = body["messages"][0]["content"]
    kinds = [part["type"] for part in parts]
    assert "text" in kinds
    assert "image" in kinds
    image = next(part for part in parts if part["type"] == "image")
    assert image["source"]["media_type"] == "image/jpeg"
    assert image["source"]["type"] == "base64"


def test_grok_parses_text_and_tool_calls():
    from providers.grok import GrokProvider

    payload = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": "calling ",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {"name": "remember", "arguments": '{"text":"x"}'},
                        }
                    ],
                },
            }
        ],
        "usage": {"total_tokens": 9},
    }
    out = GrokProvider._parse(payload)
    assert out.text == "calling "
    assert out.tool_calls[0].name == "remember"
    assert out.tool_calls[0].arguments == {"text": "x"}
    assert out.finish_reason == "tool_calls"


def test_auth_bearer_calls_refresh_each_time():
    n = {"i": 0}

    def refresh():
        n["i"] += 1
        return f"tok-{n['i']}"

    auth = Auth(refresh=refresh)
    assert auth.bearer() == "tok-1"
    assert auth.bearer() == "tok-2"


# -- streaming (pure SSE parsing, no sockets) ------------------------------
def _sse(*events) -> list[bytes]:
    import json as _json

    lines: list[bytes] = []
    for e in events:
        payload = e if isinstance(e, str) else _json.dumps(e)
        lines.append(f"data: {payload}\n".encode())
        lines.append(b"\n")
    return lines


def test_openai_compat_parse_stream_text():
    from providers.openai_compat import OpenAIChatProvider

    deltas: list[str] = []
    out = OpenAIChatProvider._parse_stream(
        _sse(
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": "hel"}}]},
            {"choices": [{"delta": {"content": "lo"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"total_tokens": 5}},
            "[DONE]",
        ),
        deltas.append,
    )
    assert deltas == ["hel", "lo"]
    assert out.text == "hello"
    assert out.finish_reason == "stop"
    assert out.usage == {"total_tokens": 5}
    assert out.tool_calls == []


def test_openai_compat_parse_stream_tool_calls():
    from providers.openai_compat import OpenAIChatProvider

    deltas: list[str] = []
    out = OpenAIChatProvider._parse_stream(
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {"name": "recall", "arguments": ""},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"que'}}]}}
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": 'ry": "x"}'}}]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            "[DONE]",
        ),
        deltas.append,
    )
    assert deltas == []
    assert out.finish_reason == "tool_calls"
    assert len(out.tool_calls) == 1
    assert out.tool_calls[0].id == "c1"
    assert out.tool_calls[0].name == "recall"
    assert out.tool_calls[0].arguments == {"query": "x"}


def test_anthropic_parse_stream_text_and_tool_use():
    deltas: list[str] = []
    out = AnthropicProvider._parse_stream(
        _sse(
            {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "One sec. "},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "t7", "name": "computer_open"},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"app": '},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '"files"}'},
            },
            {"type": "content_block_stop", "index": 1},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 9},
            },
        ),
        deltas.append,
    )
    assert deltas == ["One sec. "]
    assert out.text == "One sec. "
    assert out.finish_reason == "tool_use"
    assert out.tool_calls[0].id == "t7"
    assert out.tool_calls[0].name == "computer_open"
    assert out.tool_calls[0].arguments == {"app": "files"}
    assert out.usage == {"input_tokens": 3, "output_tokens": 9}


def test_base_stream_completion_shim_delivers_one_delta():
    from providers.base import Completion, Provider

    class OneShot(Provider):
        def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
            return Completion(text="whole reply", finish_reason="stop")

    deltas: list[str] = []
    out = OneShot(model="m").stream_completion(
        [Message(role="user", content="hi there")], on_delta=deltas.append
    )
    assert out.text == "whole reply"
    assert deltas == ["whole reply"]


# -- typed chunk stream + reasoning blocks ------------------------
def test_merge_reasoning_seal_rule():
    from providers.base import (
        ReasoningDelta,
        ReasoningPart,
        ReasoningSignatureDelta,
        RedactedReasoningDelta,
        RedactedReasoningPart,
        merge_reasoning,
    )

    parts = []
    merge_reasoning(parts, ReasoningDelta(text="think ", model="m1"))
    merge_reasoning(parts, ReasoningDelta(text="hard"))
    assert parts == [ReasoningPart(text="think hard", model="m1")]
    # a signature seals the trailing block: later deltas start a new one
    merge_reasoning(parts, ReasoningSignatureDelta(signature="sig-a"))
    merge_reasoning(parts, ReasoningDelta(text="fresh", model="m1"))
    assert parts[0].signature == "sig-a"
    assert parts[1] == ReasoningPart(text="fresh", model="m1")
    # a second signature on an already-sealed block starts a new sealed block
    merge_reasoning(parts, ReasoningSignatureDelta(signature="sig-b"))
    merge_reasoning(parts, ReasoningSignatureDelta(signature="sig-c"))
    assert parts[1].signature == "sig-b"
    assert parts[2] == ReasoningPart(text="", signature="sig-c")
    # redacted data concatenates onto a trailing redacted part
    merge_reasoning(parts, RedactedReasoningDelta(data="AAA", model="m1"))
    merge_reasoning(parts, RedactedReasoningDelta(data="BBB"))
    assert parts[3] == RedactedReasoningPart(data="AAABBB", model="m1")


def test_reasoning_for_model_filters_foreign_and_untagged():
    from providers.base import (
        ReasoningPart,
        RedactedReasoningPart,
        reasoning_for_model,
    )

    mine = ReasoningPart(text="a", signature="s", model="claude-x")
    redacted = RedactedReasoningPart(data="zz", model="claude-x")
    foreign = ReasoningPart(text="b", model="gpt-y")
    untagged = ReasoningPart(text="c")
    kept = reasoning_for_model([mine, foreign, untagged, redacted], "claude-x")
    assert kept == [mine, redacted]
    assert reasoning_for_model([mine], None) == []
    assert reasoning_for_model(None, "claude-x") == []


def _anthropic_thinking_events(*, terminal=True):
    events = [
        {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 2}, "model": "claude-t"},
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "hmm "},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "ok"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "sig-1"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "redacted_thinking", "data": "ENC"},
        },
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "text"}},
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "text_delta", "text": "Sure. "},
        },
        {"type": "content_block_stop", "index": 2},
        {
            "type": "content_block_start",
            "index": 3,
            "content_block": {"type": "tool_use", "id": "t1", "name": "recall"},
        },
        {
            "type": "content_block_delta",
            "index": 3,
            "delta": {"type": "input_json_delta", "partial_json": '{"query"'},
        },
        {
            "type": "content_block_delta",
            "index": 3,
            "delta": {"type": "input_json_delta", "partial_json": ': "x"}'},
        },
        {"type": "content_block_stop", "index": 3},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 4},
        },
    ]
    if terminal:
        events.append({"type": "message_stop"})
    return events


def test_anthropic_stream_chunk_union_round_trip():
    from providers.base import (
        ReasoningPart,
        RedactedReasoningPart,
        TextDelta,
        ToolCallEnd,
        merge_reasoning,
    )

    chunks = []
    out = AnthropicProvider._parse_stream(
        _sse(*_anthropic_thinking_events()), on_chunk=chunks.append
    )
    kinds = [c.kind for c in chunks]
    assert kinds == [
        "reasoning",
        "reasoning",
        "reasoning_signature",
        "redacted_reasoning",
        "text_delta",
        "tool_call_start",
        "tool_call_delta",
        "tool_call_delta",
        "tool_call",
    ]
    # round-trip: refolding the chunk stream reproduces the Completion
    refolded_text = "".join(c.text for c in chunks if isinstance(c, TextDelta))
    refolded_reasoning = []
    for c in chunks:
        merge_reasoning(refolded_reasoning, c)
    refolded_calls = [c.call for c in chunks if isinstance(c, ToolCallEnd)]
    assert refolded_text == out.text == "Sure. "
    assert refolded_reasoning == out.reasoning
    assert out.reasoning == [
        ReasoningPart(text="hmm ok", signature="sig-1", model="claude-t"),
        RedactedReasoningPart(data="ENC", model="claude-t"),
    ]
    assert refolded_calls == out.tool_calls
    assert out.tool_calls[0].arguments == {"query": "x"}
    assert out.finish_reason == "tool_use"


def test_anthropic_stream_on_delta_shim_equivalence():
    from providers.base import TextDelta

    deltas: list[str] = []
    chunks = []
    with_delta = AnthropicProvider._parse_stream(_sse(*_anthropic_thinking_events()), deltas.append)
    with_chunks = AnthropicProvider._parse_stream(
        _sse(*_anthropic_thinking_events()), on_chunk=chunks.append
    )
    # on_delta is a thin shim over the chunk stream: exactly the text_delta
    # chunks, nothing else (no reasoning text, no tool-call fragments).
    assert deltas == [c.text for c in chunks if isinstance(c, TextDelta)]
    assert with_delta == with_chunks


def test_anthropic_stream_truncated_raises():
    events = _anthropic_thinking_events(terminal=True)
    # drop the terminal events: clean EOF without message_stop/stop_reason
    with pytest.raises(ProviderError, match="truncated"):
        AnthropicProvider._parse_stream(_sse(*events[:-2]))


def test_anthropic_stream_partial_event_at_eof_raises():
    lines = _sse(*_anthropic_thinking_events())
    lines.append(b'data: {"type": "message_del\n')  # torn mid-event at EOF
    with pytest.raises(ProviderError, match="mid-event"):
        AnthropicProvider._parse_stream(lines)


def test_anthropic_stream_error_fails_pending_side_channels():
    """A wire error mid tool-call stream must re-raise without emitting a
    terminal tool_call for the announced channel (fail closed)."""

    def wire():
        yield from _sse(
            {"type": "message_start", "message": {"model": "claude-t"}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "t9", "name": "recall"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"que'},
            },
        )
        raise ProviderError("connection reset")

    chunks = []
    with pytest.raises(ProviderError, match="connection reset"):
        AnthropicProvider._parse_stream(wire(), on_chunk=chunks.append)
    kinds = [c.kind for c in chunks]
    assert "tool_call_start" in kinds
    assert "tool_call" not in kinds  # never resolve a half-parsed call


def test_openai_stream_truncated_raises():
    from providers.openai_compat import OpenAIChatProvider

    with pytest.raises(ProviderError, match="truncated"):
        OpenAIChatProvider._parse_stream(
            _sse(
                {"choices": [{"delta": {"content": "half an ans"}}]},
            )
        )


def test_openai_stream_partial_event_at_eof_raises():
    from providers.openai_compat import OpenAIChatProvider

    lines = _sse({"choices": [{"delta": {"content": "hello"}}]})
    lines.append(b'data: {"choices": [{"delta": {"content": "wor\n')
    with pytest.raises(ProviderError, match="mid-event"):
        OpenAIChatProvider._parse_stream(lines)


def test_openai_stream_emits_typed_tool_call_chunks():
    from providers.openai_compat import OpenAIChatProvider

    chunks = []
    out = OpenAIChatProvider._parse_stream(
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {"name": "recall", "arguments": ""},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"que'}}]}}
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": 'ry": "x"}'}}]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            "[DONE]",
        ),
        on_chunk=chunks.append,
    )
    kinds = [c.kind for c in chunks]
    assert kinds == ["tool_call_start", "tool_call_delta", "tool_call_delta", "tool_call"]
    assert chunks[0].id == "c1" and chunks[0].name == "recall"
    joined = "".join(c.arguments for c in chunks if c.kind == "tool_call_delta")
    assert joined == '{"query": "x"}'
    assert chunks[-1].call == out.tool_calls[0]
    assert out.tool_calls[0].arguments == {"query": "x"}


def test_base_shim_emits_terminal_tool_call_only():
    """A provider that cannot stream tool args emits only the terminal
    tool_call chunk; a ToolCallEnd-driven consumer sees the same calls as on
    the fragmented path."""
    from providers.base import Completion, Provider, ToolCall, ToolCallEnd

    call = ToolCall(id="c1", name="recall", arguments={"query": "x"})

    class NoStream(Provider):
        def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
            return Completion(text="on it", tool_calls=[call], finish_reason="tool_calls")

    chunks = []
    deltas: list[str] = []
    out = NoStream(model="m").stream_completion(
        [Message(role="user", content="hi")], on_delta=deltas.append, on_chunk=chunks.append
    )
    kinds = [c.kind for c in chunks]
    assert kinds == ["text_delta", "tool_call"]
    assert deltas == ["on it"]
    assert [c.call for c in chunks if isinstance(c, ToolCallEnd)] == out.tool_calls == [call]


def test_anthropic_parse_picks_up_thinking_blocks():
    from providers.base import ReasoningPart, RedactedReasoningPart

    payload = {
        "model": "claude-t",
        "content": [
            {"type": "thinking", "thinking": "let me see", "signature": "sig-9"},
            {"type": "redacted_thinking", "data": "ENC"},
            {"type": "text", "text": "answer"},
        ],
        "stop_reason": "end_turn",
    }
    out = AnthropicProvider._parse(payload)
    assert out.text == "answer"
    assert out.reasoning == [
        ReasoningPart(text="let me see", signature="sig-9", model="claude-t"),
        RedactedReasoningPart(data="ENC", model="claude-t"),
    ]


def test_anthropic_replays_only_its_own_thinking_blocks():
    from providers.base import ReasoningPart, RedactedReasoningPart, ToolCall

    p = AnthropicProvider(model="claude-t", auth=Auth(api_key="k"))
    body = p._body(
        [
            Message(role="user", content="go"),
            Message(
                role="assistant",
                content="Working.",
                tool_calls=[ToolCall(id="t1", name="recall", arguments={"query": "x"})],
                reasoning=[
                    ReasoningPart(text="mine", signature="sig-1", model="claude-t"),
                    RedactedReasoningPart(data="ENC", model="claude-t"),
                    ReasoningPart(text="foreign", signature="sig-2", model="gpt-y"),
                    ReasoningPart(text="untagged"),
                ],
            ),
            Message(role="tool", content="ok", tool_call_id="t1"),
        ],
        system=None,
        tools=None,
        max_tokens=16,
        temperature=0.2,
        stream=False,
    )
    content = body["messages"][1]["content"]
    types = [b["type"] for b in content]
    # thinking blocks lead the turn, signatures intact; foreign and untagged
    # blocks never cross to another model
    assert types == ["thinking", "redacted_thinking", "text", "tool_use"]
    assert content[0] == {"type": "thinking", "thinking": "mine", "signature": "sig-1"}
    assert content[1] == {"type": "redacted_thinking", "data": "ENC"}


def test_anthropic_thinking_budget_option():
    p = AnthropicProvider(model="claude-t", auth=Auth(api_key="k"), thinking_budget=2048)
    body = p._body(
        [Message(role="user", content="hi")],
        system=None,
        tools=None,
        max_tokens=4096,
        temperature=1.0,
        stream=True,
    )
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 2048}


def test_openai_stream_reasoning_content_deltas():
    from providers.base import ReasoningPart
    from providers.openai_compat import OpenAIChatProvider

    chunks = []
    deltas: list[str] = []
    out = OpenAIChatProvider._parse_stream(
        _sse(
            {"choices": [{"delta": {"reasoning_content": "let me "}}]},
            {"choices": [{"delta": {"reasoning_content": "think"}}]},
            {"choices": [{"delta": {"content": "42"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            "[DONE]",
        ),
        deltas.append,
        chunks.append,
        model="deepthink-1",
    )
    assert deltas == ["42"]  # reasoning never leaks into the text shim
    assert out.text == "42"
    assert out.reasoning == [ReasoningPart(text="let me think", model="deepthink-1")]
    assert [c.kind for c in chunks] == ["reasoning", "reasoning", "text_delta"]


@pytest.mark.parametrize("model", ["claude-fable-5-1", "claude-opus-5", "claude-sonnet-5"])
def test_claude_five_uses_adaptive_thinking_and_effort(model):
    provider = AnthropicProvider(
        model=model, auth=Auth(api_key="k"), thinking_budget=2048, reasoning_effort="high"
    )
    body = provider._body([Message(role="user", content="hi")], None, None, 4096, 0.7, True)
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "high"}
    assert "temperature" not in body


@pytest.mark.parametrize(
    "model",
    [
        "gpt-6-astra",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.3-codex",
        "gpt-5.3-codex-spark",
    ],
)
def test_current_openai_api_models_use_responses_for_tools(model, monkeypatch):
    from providers.openai import OpenAIProvider

    provider = OpenAIProvider(model=model, auth=Auth(api_key="api-key"), reasoning_effort="high")
    requests = []

    def post(body):
        requests.append(body)
        return _sse(
            {
                "type": "response.completed",
                "response": {
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "phase": "commentary",
                            "content": [{"type": "output_text", "text": "Looking it up."}],
                        },
                        {
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "recall",
                            "arguments": '{"query":"hi"}',
                        },
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        )

    monkeypatch.setattr(provider, "_post_stream", post)
    result = provider.stream_completion(
        [Message(role="user", content="hi")],
        tools=[ToolSpec(name="recall", description="Recall", parameters={"type": "object"})],
    )
    assert provider.base_url == "https://api.openai.com/v1/responses"
    assert provider._headers()["authorization"] == "Bearer api-key"
    assert "chatgpt-account-id" not in provider._headers()
    assert requests[0]["tools"][0]["name"] == "recall"
    assert requests[0]["reasoning"]["effort"] == "high"
    assert "temperature" not in requests[0] and "max_tokens" not in requests[0]
    assert result.tool_calls[0].arguments == {"query": "hi"}
    assert result.usage["output_tokens"] == 5
    assert result.phase == "commentary"
    assert result.responses_output.model == model


def test_openai_api_key_reasoning_effort_is_sent():
    from providers.openai import OpenAIProvider

    provider = OpenAIProvider(model="gpt-5.6-sol", auth=Auth(api_key="k"), reasoning_effort="high")
    body = provider._body([Message(role="user", content="hi")], None, None, 128, 0.7, True)
    assert body["reasoning"]["effort"] == "high"


@pytest.mark.parametrize(
    "provider_id,model",
    [
        ("deepseek", "deepseek-v4-flash"),
        ("glm", "glm-5.3"),
        ("kimi", "kimi-k3"),
    ],
)
@pytest.mark.parametrize("stream", [False, True])
def test_current_thinking_vendors_replay_tool_reasoning(provider_id, model, stream, monkeypatch):
    from providers.base import ReasoningPart, ToolCall

    provider = build_provider(provider_id, auth=Auth(api_key="k"), reasoning_effort="high")
    assert provider.model == model
    call = {"id": "t1", "function": {"name": "recall", "arguments": "{}"}}
    monkeypatch.setattr(
        provider,
        "_post",
        lambda body: {
            "choices": [
                {
                    "message": {
                        "content": "Reading",
                        "reasoning_content": "Plan",
                        "tool_calls": [call],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    )
    monkeypatch.setattr(
        provider,
        "_post_stream",
        lambda body: _sse(
            {"choices": [{"delta": {"reasoning_content": "Plan"}}]},
            {"choices": [{"delta": {"content": "Reading", "tool_calls": [{"index": 0, **call}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ),
    )
    first = (provider.stream_completion if stream else provider.complete)([Message("user", "hi")])
    assert first.reasoning == [ReasoningPart(text="Plan", model=model)]
    history = [
        Message(
            "assistant",
            first.text,
            tool_calls=first.tool_calls,
            reasoning=first.reasoning + [ReasoningPart(text="foreign", model="other")],
        ),
        Message("tool", "found", tool_call_id="t1"),
    ]
    body = provider._body(history, None, None, 1024, 0.7, True)
    assert body["messages"][0]["reasoning_content"] == "Plan"
    assert body["reasoning_effort"] == "high"
    assert first.tool_calls == [ToolCall("t1", "recall", {})]


@pytest.mark.parametrize(
    "provider_id,expected",
    [
        ("claude", "claude-sonnet-5"),
        ("codex", "gpt-5.4-mini"),
        ("qwen", "qwen3.7-plus"),
        ("minimax", "MiniMax-M3"),
        ("grok", "grok-4.6"),
    ],
)
def test_current_provider_fallbacks_preserve_explicit_models(provider_id, expected):
    assert build_provider(provider_id, auth=Auth()).model == expected
    assert build_provider(provider_id, "pinned-model", auth=Auth()).model == "pinned-model"


@pytest.mark.parametrize(
    "model", ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.5", "gpt-5.4-mini", "gpt-5.3-codex-spark"]
)
@pytest.mark.parametrize(
    "base_url",
    [
        "https://gateway.example/v1/chat/completions",
        "https://gateway.example/v1/chat/completions/",
        "https://gateway.example/v1/responses",
    ],
)
def test_current_openai_models_preserve_gateway_and_embedding_routes(model, base_url):
    from providers.openai import OpenAIProvider

    provider = OpenAIProvider(model=model, auth=Auth(api_key="k"), base_url=base_url)
    assert provider.base_url == "https://gateway.example/v1/responses"
    assert provider._embeddings_url() == "https://gateway.example/v1/embeddings"
