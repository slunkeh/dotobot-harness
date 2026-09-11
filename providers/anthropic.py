"""Anthropic Claude provider — the one fully-implemented backend.

Uses only the Python standard library (`urllib`) so the harness stays
dependency-free. Supports non-streaming completions with tool calls and a
streaming reader over the Messages SSE API.

Auth: API key today (`x-api-key`). OAuth is not implemented here, but
`Auth.bearer()` is the drop-in seam — when a refresh hook is present
we send `Authorization: Bearer ...` instead of an API key.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from .anthropic_oauth import CLAUDE_CODE_SYSTEM_PREFIX, OAUTH_BETAS, TOKEN_USER_AGENT
from .base import (
    Auth,
    Completion,
    Message,
    Provider,
    ProviderError,
    ReasoningContent,
    ReasoningDelta,
    ReasoningPart,
    ReasoningSignatureDelta,
    RedactedReasoningDelta,
    RedactedReasoningPart,
    StreamChunk,
    TextDelta,
    ToolCall,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolSpec,
    chunk_dispatcher,
    merge_reasoning,
    reasoning_for_model,
)


def _anthropic_content(m: Message) -> Any:
    if not m.images:
        return m.content
    parts: list[dict[str, Any]] = []
    if m.content:
        parts.append({"type": "text", "text": m.content})
    for mime, data in m.images:
        parts.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": base64.b64encode(data).decode("ascii"),
                },
            }
        )
    return parts


def _mark_cache_breakpoints(body: dict[str, Any]) -> None:
    """Stamp `cache_control` on the tools, system, and last-message tails.

    A string system becomes one text block (the API only takes the marker on
    blocks); a string last message likewise. Only the LAST message is
    touched, and only a user-side block type (text, image, tool_result) —
    thinking blocks may not carry the marker.
    """
    tools = body.get("tools")
    if tools:
        tools[-1]["cache_control"] = dict(_CACHE_MARK)
    system = body.get("system")
    if isinstance(system, str) and system:
        body["system"] = [{"type": "text", "text": system, "cache_control": dict(_CACHE_MARK)}]
    elif isinstance(system, list) and system:
        system[-1]["cache_control"] = dict(_CACHE_MARK)
    messages = body.get("messages") or []
    if not messages:
        return
    last = messages[-1]
    content = last.get("content")
    if isinstance(content, str):
        if content:
            last["content"] = [
                {"type": "text", "text": content, "cache_control": dict(_CACHE_MARK)}
            ]
    elif isinstance(content, list) and content:
        block = content[-1]
        if isinstance(block, dict) and block.get("type") in {"text", "image", "tool_result"}:
            block["cache_control"] = dict(_CACHE_MARK)


_API_URL = "https://api.anthropic.com/v1/messages"
#: Prompt-cache breakpoint marker. Three are set per request (the last tool,
#: the last system block, the last block of the last message) so the tool
#: specs, the system prompt, and the whole conversation-so-far are all
#: reusable on the next tool-loop step; option `prompt_cache = false` opts
#: a bot out. Prompts under the API's minimum are simply not cached.
_CACHE_MARK = {"type": "ephemeral"}
_API_VERSION = "2023-06-01"
_DEFAULT_MODEL = "claude-sonnet-5"

# Claude 4.7+ (and unnamed later Claude) 400 if `temperature` is sent at all
# ("temperature is deprecated for this model"). 3.x / 4.0 / 4.1 / 4.5 / 4.6
# still accept it. Non-Claude Anthropic-compatible models (MiniMax, …) keep it.
_TEMPERATURE_OK_CLAUDE = ("claude-3", "-4-0", "-4-1", "-4-5", "-4-6", "-4-2024", "-4-2025")


def _sends_temperature(model: str) -> bool:
    m = (model or "").lower().replace(".", "-")
    if "claude" not in m:
        return True
    return any(token in m for token in _TEMPERATURE_OK_CLAUDE)


class AnthropicProvider(Provider):
    id = "claude"
    context_window = 200_000

    def __init__(
        self, model: str = _DEFAULT_MODEL, auth: Auth | None = None, **options: Any
    ) -> None:
        super().__init__(model, auth, **options)
        self.base_url = options.get("base_url", _API_URL)

    # -- request building -------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "anthropic-version": _API_VERSION,
        }
        bearer = self.auth.bearer()
        if bearer:
            headers["authorization"] = f"Bearer {bearer}"
            if self.options.get("claude_oauth"):
                headers["anthropic-beta"] = ",".join(OAUTH_BETAS)
                headers["user-agent"] = TOKEN_USER_AGENT
                headers["x-app"] = "cli"
        elif self.auth.api_key:
            headers["x-api-key"] = self.auth.header_key()
        else:
            raise ProviderError(
                "Anthropic provider needs an API key (ANTHROPIC_API_KEY) or an OAuth token."
            )
        return headers

    def _body(
        self,
        messages: list[Message],
        system: str | None,
        tools: list[ToolSpec] | None,
        max_tokens: int,
        temperature: float,
        stream: bool,
    ) -> dict[str, Any]:
        api_messages: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                # Anthropic takes system as a top-level field, fold it in.
                system = f"{system}\n\n{m.content}" if system else m.content
                continue
            if m.role == "tool":
                api_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id or "",
                                "content": m.content,
                            }
                        ],
                    }
                )
                continue
            if m.role == "assistant" and (m.tool_calls or m.reasoning):
                # A prior assistant turn that called tools must round-trip as
                # tool_use blocks or the following tool_result is rejected;
                # with extended thinking the thinking blocks must lead the
                # turn, signatures intact. Only blocks this same model
                # produced are replayed — a foreign or untagged
                # block is dropped, never sent across vendors.
                content: list[dict[str, Any]] = []
                for part in reasoning_for_model(m.reasoning, self.model):
                    if isinstance(part, RedactedReasoningPart):
                        content.append({"type": "redacted_thinking", "data": part.data})
                        continue
                    thinking: dict[str, Any] = {"type": "thinking", "thinking": part.text}
                    if part.signature:
                        thinking["signature"] = part.signature
                    content.append(thinking)
                if m.content:
                    content.append({"type": "text", "text": m.content})
                for tc in m.tool_calls or []:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                api_messages.append({"role": "assistant", "content": content})
                continue
            api_messages.append({"role": m.role, "content": _anthropic_content(m)})

        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": api_messages,
        }
        if _sends_temperature(self.model):
            body["temperature"] = temperature
        if self.options.get("claude_oauth"):
            # Subscription OAuth is classified onto the Claude Code pool only
            # when the Claude Code identity is a leading system *block* and
            # the UA matches claude-cli. A concatenated string plus
            # `claude-code/…` is a persistent 429 with message "Error".
            blocks = [{"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX}]
            if system:
                blocks.append({"type": "text", "text": system})
            body["system"] = blocks
        elif system:
            body["system"] = system
        if tools:
            body["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters or {"type": "object", "properties": {}},
                }
                for t in tools
            ]
        if self.options.get("prompt_cache", True):
            _mark_cache_breakpoints(body)
        # Extended thinking: cheap opt-in via a roster option; the parsers
        # already carry thinking blocks either way.
        budget = self.options.get("thinking_budget")
        modern = re.match(r"^claude-(?:[a-z]+-)?([0-9]+)", self.model)
        adaptive = bool(modern and int(modern.group(1)) >= 5)
        effort = self.options.get("reasoning_effort")
        if adaptive:
            if budget or effort:
                body["thinking"] = {"type": "adaptive"}
            if effort:
                body["output_config"] = {"effort": effort}
        elif budget:
            body["thinking"] = {"type": "enabled", "budget_tokens": int(budget)}
        if stream:
            body["stream"] = True
        return body

    # -- calls ------------------------------------------------------------
    def _post(self, body: dict[str, Any]) -> Any:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base_url, data=data, headers=self._headers(), method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            detail = exc.read().decode("utf-8", "replace")
            raise ProviderError(f"Anthropic HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover - network path
            raise ProviderError(f"Anthropic request failed: {exc.reason}") from exc

    def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> Completion:
        body = self._body(messages, system, tools, max_tokens, temperature, stream=False)
        _resp, payload = self._post(body)
        return self._parse(payload, model=self.model)

    @staticmethod
    def _parse(payload: dict[str, Any], model: str | None = None) -> Completion:
        producer = str(payload.get("model") or model or "") or None
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        reasoning: list[ReasoningContent] = []
        for block in payload.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "thinking":
                reasoning.append(
                    ReasoningPart(
                        text=block.get("thinking", ""),
                        signature=block.get("signature") or None,
                        model=producer,
                    )
                )
            elif block.get("type") == "redacted_thinking":
                reasoning.append(RedactedReasoningPart(data=block.get("data", ""), model=producer))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        arguments=block.get("input", {}) or {},
                    )
                )
        return Completion(
            text="".join(text_parts),
            tool_calls=tool_calls,
            finish_reason=payload.get("stop_reason"),
            usage=payload.get("usage", {}),
            raw=payload,
            reasoning=reasoning,
        )

    def _post_stream(self, body: dict[str, Any]) -> Iterator[bytes]:  # pragma: no cover - network
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base_url, data=data, headers=self._headers(), method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                yield from resp
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise ProviderError(f"Anthropic HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"Anthropic request failed: {exc.reason}") from exc

    def stream_completion(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        on_delta: Callable[[str], None] | None = None,
        on_chunk: Callable[[StreamChunk], None] | None = None,
    ) -> Completion:
        body = self._body(messages, system, tools, max_tokens, temperature, stream=True)
        return self._parse_stream(self._post_stream(body), on_delta, on_chunk, model=self.model)

    @staticmethod
    def _parse_stream(
        lines: Iterable[bytes | str],
        on_delta: Callable[[str], None] | None = None,
        on_chunk: Callable[[StreamChunk], None] | None = None,
        *,
        model: str | None = None,
    ) -> Completion:
        """Fold a Messages SSE stream into a Completion, emitting typed chunks.

        tool_use blocks stream as content_block_start (id/name) followed by
        input_json_delta fragments, parsed when the block stops; thinking
        blocks stream as thinking_delta / signature_delta and fold into
        reasoning parts under the seal rule.

        Fail-closed: a stream that ends without its terminal event
        (message_stop / a stop_reason) or with a partial trailing event
        raises instead of returning a silently truncated answer. On any
        error, pending side-channels (open tool_use slots) are discarded —
        an announced tool_call_start never gets a terminal tool_call chunk
        after a failure, so a half-parsed call can never execute.
        """
        emit = chunk_dispatcher(on_delta, on_chunk)
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        reasoning: list[ReasoningContent] = []
        blocks: dict[int, dict[str, str]] = {}
        finish: str | None = None
        usage: dict[str, Any] = {}
        producer = model
        terminal = False
        partial_event: str | None = None
        try:
            for raw_line in lines:
                line = (
                    raw_line.decode("utf-8", "replace") if isinstance(raw_line, bytes) else raw_line
                ).strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[len("data:") :].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    event = json.loads(chunk)
                except json.JSONDecodeError:
                    # remember it: a bad payload mid-stream is vendor noise,
                    # but one at EOF is a truncated event and must fail.
                    partial_event = chunk
                    continue
                partial_event = None
                etype = event.get("type")
                if etype == "message_start":
                    message = event.get("message") or {}
                    usage = message.get("usage") or usage
                    producer = str(message.get("model") or "") or producer
                elif etype == "content_block_start":
                    idx = int(event.get("index", 0))
                    block = event.get("content_block") or {}
                    blocks[idx] = {
                        "type": block.get("type", ""),
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "partial": "",
                    }
                    if block.get("type") == "tool_use" and emit:
                        emit(ToolCallStart(id=block.get("id", ""), name=block.get("name", "")))
                    elif block.get("type") == "redacted_thinking":
                        redacted = RedactedReasoningDelta(
                            data=block.get("data", ""), model=producer
                        )
                        merge_reasoning(reasoning, redacted)
                        if emit:
                            emit(redacted)
                elif etype == "content_block_delta":
                    idx = int(event.get("index", 0))
                    delta = event.get("delta") or {}
                    dtype = delta.get("type")
                    if dtype == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            text_parts.append(text)
                            if emit:
                                emit(TextDelta(text))
                    elif dtype == "thinking_delta":
                        thinking = ReasoningDelta(text=delta.get("thinking", ""), model=producer)
                        merge_reasoning(reasoning, thinking)
                        if emit:
                            emit(thinking)
                    elif dtype == "signature_delta":
                        signature = ReasoningSignatureDelta(signature=delta.get("signature", ""))
                        merge_reasoning(reasoning, signature)
                        if emit:
                            emit(signature)
                    elif dtype == "input_json_delta":
                        slot = blocks.setdefault(
                            idx, {"type": "tool_use", "id": "", "name": "", "partial": ""}
                        )
                        piece = delta.get("partial_json", "")
                        slot["partial"] += piece
                        if piece and emit:
                            emit(ToolCallDelta(id=slot["id"], arguments=piece))
                elif etype == "content_block_stop":
                    block = blocks.pop(int(event.get("index", 0)), None)
                    if block and block.get("type") == "tool_use":
                        raw_args = block["partial"] or "{}"
                        try:
                            arguments = json.loads(raw_args)
                        except json.JSONDecodeError:
                            arguments = {"_raw": raw_args}
                        call = ToolCall(id=block["id"], name=block["name"], arguments=arguments)
                        tool_calls.append(call)
                        if emit:
                            emit(ToolCallEnd(call))
                elif etype == "message_delta":
                    delta = event.get("delta") or {}
                    if delta.get("stop_reason"):
                        finish = delta["stop_reason"]
                        terminal = True
                    if event.get("usage"):
                        usage = {**usage, **event["usage"]}
                elif etype == "message_stop":
                    terminal = True
        except Exception:
            blocks.clear()  # fail pending side-channels before re-raising
            raise
        if partial_event is not None:
            raise ProviderError("Anthropic stream ended mid-event (truncated SSE payload).")
        if not terminal:
            raise ProviderError(
                "Anthropic stream ended without message_stop/stop_reason (truncated)."
            )
        return Completion(
            text="".join(text_parts),
            tool_calls=tool_calls,
            finish_reason=finish,
            usage=usage,
            reasoning=reasoning,
        )
