"""Shared OpenAI-compatible Chat Completions base.

xAI Grok and OpenAI/Codex speak the same wire shape; this base holds the
request/response plumbing once and lets each vendor adapter declare only its
identity: label (for error messages), default model, endpoint URL, and how to
find credentials.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from .base import (
    Auth,
    Completion,
    Message,
    Provider,
    ProviderError,
    ReasoningContent,
    ReasoningDelta,
    ReasoningPart,
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


def _openai_content(m: Message) -> Any:
    if not m.images:
        return m.content
    parts: list[dict[str, Any]] = []
    if m.content:
        parts.append({"type": "text", "text": m.content})
    for mime, data in m.images:
        b64 = base64.b64encode(data).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    return parts


class OpenAIChatProvider(Provider):
    """Chat Completions over urllib; subclasses set label/default_model/default_url."""

    label = "OpenAI-compatible"
    default_model = ""
    default_url = ""
    context_window = 128_000
    replay_reasoning = False
    #: raised when neither an OAuth bearer nor an API key is available
    missing_credentials = "no API key or OAuth token configured"

    def __init__(self, model: str = "", auth: Auth | None = None, **options: Any) -> None:
        super().__init__(model or self.default_model, auth, **options)
        self.base_url = options.get("base_url", self.default_url)

    def _token(self) -> str:
        token = self.auth.bearer() or self.auth.header_key()
        if not token:
            raise ProviderError(self.missing_credentials)
        return token

    def _headers(self) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "authorization": f"Bearer {self._token()}",
            "user-agent": "dotobot/0.1",
        }

    def _sampling(self, max_tokens: int, temperature: float) -> dict[str, Any]:
        """Sampling params; a subclass overrides for models that rename them
        (xAI requires max_tokens; max_completion_tokens is OpenAI's newer name)."""
        return {"max_tokens": max_tokens, "temperature": temperature}

    def _reasoning_fields(self, message: Message) -> dict[str, str]:
        if not self.replay_reasoning or message.role != "assistant":
            return {}
        parts = reasoning_for_model(message.reasoning, self.model)
        text = "".join(part.text for part in parts if isinstance(part, ReasoningPart))
        return {"reasoning_content": text} if text else {}

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
        if system:
            api_messages.append({"role": "system", "content": system})
        for m in messages:
            if m.role == "system":
                api_messages.append({"role": "system", "content": m.content})
                continue
            if m.role == "tool":
                msg: dict[str, Any] = {"role": "tool", "content": m.content}
                if m.tool_call_id:
                    msg["tool_call_id"] = m.tool_call_id
                if m.name:
                    msg["name"] = m.name
                api_messages.append(msg)
                continue
            if m.role == "assistant" and m.tool_calls:
                api_messages.append(
                    {
                        "role": "assistant",
                        "content": m.content or None,
                        **self._reasoning_fields(m),
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": json.dumps(tc.arguments),
                                },
                            }
                            for tc in m.tool_calls
                        ],
                    }
                )
                continue
            api_messages.append(
                {"role": m.role, "content": _openai_content(m), **self._reasoning_fields(m)}
            )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            **self._sampling(max_tokens, temperature),
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters or {"type": "object", "properties": {}},
                    },
                }
                for t in tools
            ]
        if stream:
            body["stream"] = True
        return body

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base_url, data=data, headers=self._headers(), method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            detail = exc.read().decode("utf-8", "replace")
            raise ProviderError(f"{self.label} HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover - network path
            raise ProviderError(f"{self.label} request failed: {exc.reason}") from exc

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
        return self._parse(self._post(body), model=self.model)

    def _embeddings_url(self) -> str:
        base = self.base_url
        marker = "/chat/completions"
        if marker in base:
            return base.split(marker, 1)[0] + "/embeddings"
        return base.rstrip("/") + "/embeddings"

    def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        """Embeddings over the sibling `/embeddings` endpoint.

        The timeout is short ($HARNESS_EMBED_TIMEOUT, default 10s), not the
        chat 120s: embeds run synchronously inside turns and are best-effort
        — a hung endpoint should cost seconds before the keyword fallback,
        not a two-minute stall.
        """
        chosen = model or self.options.get("embedding_model") or self.embedding_model
        if not chosen:
            raise ProviderError(f"{self.label} has no embedding route")
        headers = self._headers()  # missing credentials raise before any network I/O
        data = json.dumps({"model": chosen, "input": list(texts)}).encode("utf-8")
        req = urllib.request.Request(
            self._embeddings_url(), data=data, headers=headers, method="POST"
        )
        try:
            timeout = float(os.environ.get("HARNESS_EMBED_TIMEOUT", "10"))
        except ValueError:
            timeout = 10.0
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            detail = exc.read().decode("utf-8", "replace")
            raise ProviderError(f"{self.label} HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover - network path
            raise ProviderError(f"{self.label} request failed: {exc.reason}") from exc
        rows = payload.get("data") or []
        if len(rows) != len(texts):
            raise ProviderError(
                f"{self.label} embeddings returned {len(rows)} vectors for {len(texts)} inputs"
            )
        rows = sorted(rows, key=lambda r: int(r.get("index", 0)))
        return [[float(x) for x in (row.get("embedding") or [])] for row in rows]

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
            raise ProviderError(f"{self.label} HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"{self.label} request failed: {exc.reason}") from exc

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
        """Fold an SSE line stream into a Completion, emitting typed chunks.

        Tool calls arrive fragmented: id/name once at their index (surfaced
        as tool_call_start), then function.arguments as string pieces
        (tool_call_delta), concatenated and parsed into terminal tool_call
        chunks once the stream settles. `reasoning_content` deltas (DeepSeek /
        xAI style) surface as reasoning chunks under the seal rule.

        Fail-closed: a stream that ends without its terminal event ([DONE] or
        a finish_reason) or with a partial trailing event raises instead of
        returning a silently truncated answer. On any error, pending
        side-channels (accumulating tool-call slots) are discarded — an
        announced tool_call_start never gets a terminal tool_call chunk after
        a failure, so a half-parsed call can never execute.
        """
        emit = chunk_dispatcher(on_delta, on_chunk)
        text_parts: list[str] = []
        reasoning: list[ReasoningContent] = []
        pending: dict[int, dict[str, str]] = {}
        started: set[int] = set()
        finish: str | None = None
        usage: dict[str, Any] = {}
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
                if not chunk:
                    continue
                if chunk == "[DONE]":
                    terminal = True
                    continue
                try:
                    event = json.loads(chunk)
                except json.JSONDecodeError:
                    # remember it: a bad payload mid-stream is vendor noise,
                    # but one at EOF is a truncated event and must fail.
                    partial_event = chunk
                    continue
                partial_event = None
                if event.get("usage"):
                    usage = event["usage"]
                choices = event.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
                    terminal = True
                delta = choice.get("delta") or {}
                text = delta.get("content")
                if text:
                    text_parts.append(text)
                    if emit:
                        emit(TextDelta(text))
                thought = delta.get("reasoning_content")
                if thought:
                    piece = ReasoningDelta(text=thought, model=model)
                    merge_reasoning(reasoning, piece)
                    if emit:
                        emit(piece)
                for tc in delta.get("tool_calls") or []:
                    idx = int(tc.get("index", 0))
                    slot = pending.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if idx not in started and (slot["id"] or slot["name"]) and emit:
                        started.add(idx)
                        emit(ToolCallStart(id=slot["id"], name=slot["name"]))
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
                        if emit:
                            emit(ToolCallDelta(id=slot["id"], arguments=fn["arguments"]))
        except Exception:
            pending.clear()  # fail pending side-channels before re-raising
            raise
        if partial_event is not None:
            raise ProviderError("stream ended mid-event (truncated SSE payload)")
        if not terminal:
            raise ProviderError("stream ended without [DONE] or a finish_reason (truncated)")
        tool_calls: list[ToolCall] = []
        for idx in sorted(pending):
            slot = pending[idx]
            raw_args = slot["arguments"] or "{}"
            try:
                arguments = json.loads(raw_args)
            except json.JSONDecodeError:
                arguments = {"_raw": raw_args}
            call = ToolCall(id=slot["id"], name=slot["name"], arguments=arguments)
            tool_calls.append(call)
            if emit:
                emit(ToolCallEnd(call))
        return Completion(
            text="".join(text_parts),
            tool_calls=tool_calls,
            finish_reason=finish,
            usage=usage,
            reasoning=reasoning,
        )

    @staticmethod
    def _parse(payload: dict[str, Any], *, model: str | None = None) -> Completion:
        choices = payload.get("choices") or []
        if not choices:
            raise ProviderError("chat completions response had no choices")
        msg = choices[0].get("message") or {}
        text = msg.get("content") or ""
        tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            if isinstance(raw_args, str):
                try:
                    arguments = json.loads(raw_args or "{}")
                except json.JSONDecodeError:
                    arguments = {"_raw": raw_args}
            else:
                arguments = raw_args or {}
            tool_calls.append(
                ToolCall(
                    id=tc.get("id", ""),
                    name=fn.get("name", ""),
                    arguments=arguments,
                )
            )
        finish = choices[0].get("finish_reason")
        return Completion(
            text=text if isinstance(text, str) else "",
            tool_calls=tool_calls,
            finish_reason=finish,
            usage=payload.get("usage") or {},
            raw=payload,
            reasoning=[
                ReasoningPart(text=msg["reasoning_content"], model=payload.get("model") or model)
            ]
            if isinstance(msg.get("reasoning_content"), str) and msg["reasoning_content"]
            else [],
        )
