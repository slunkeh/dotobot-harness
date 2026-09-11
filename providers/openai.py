"""OpenAI / Codex provider.

Current agent models use Responses to preserve assistant phases and reasoning
through tool calls. Older models retain Chat Completions. Override `base_url`
for compatible gateways. Auth is `OPENAI_API_KEY`; without a key,
`agent.runtime.build_agent` falls back to reusing the host's Codex CLI ChatGPT
login via `providers.codex_chatgpt`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import responses
from .base import (
    Auth,
    Completion,
    Message,
    StreamChunk,
    TextDelta,
    ToolCallEnd,
    ToolSpec,
    chunk_dispatcher,
)
from .openai_compat import OpenAIChatProvider

_DEFAULT_MODEL = "gpt-5.4-mini"


class OpenAIProvider(OpenAIChatProvider):
    id = "codex"
    label = "OpenAI"
    default_model = _DEFAULT_MODEL
    default_url = "https://api.openai.com/v1/chat/completions"
    embedding_model = "text-embedding-3-small"
    missing_credentials = "OpenAI/Codex needs OPENAI_API_KEY or a ChatGPT OAuth token."

    def __init__(
        self, model: str = _DEFAULT_MODEL, auth: Auth | None = None, **options: Any
    ) -> None:
        self.uses_responses = model.startswith(
            ("gpt-6", "gpt-5.6", "gpt-5.5", "gpt-5.4", "gpt-5.3-codex")
        )
        if self.uses_responses:
            # Current GPT agent workflows use Responses; Astra requires it
            # for tools. Keep gateways on their configured host, replacing
            # only the endpoint suffix.
            url = options.get("base_url", self.default_url).rstrip("/")
            options["base_url"] = url.removesuffix("/chat/completions").rstrip("/") + (
                "" if url.rstrip("/").endswith("/responses") else "/responses"
            )
        self.reasoning_effort = str(options.get("reasoning_effort") or "").strip() or None
        super().__init__(model, auth, **options)

    def _sampling(self, max_tokens: int, temperature: float) -> dict[str, Any]:
        # gpt-5* and o* reasoning models reject `max_tokens` and any
        # non-default temperature on Chat Completions.
        if self.model.startswith(("gpt-5", "o1", "o3", "o4")):
            params: dict[str, Any] = {"max_completion_tokens": max_tokens}
            if self.reasoning_effort:
                params["reasoning_effort"] = self.reasoning_effort
            return params
        return {"max_tokens": max_tokens, "temperature": temperature}

    def _body(
        self,
        messages: list[Message],
        system: str | None,
        tools: list[ToolSpec] | None,
        max_tokens: int,
        temperature: float,
        stream: bool,
    ) -> dict[str, Any]:
        if not self.uses_responses:
            return super()._body(messages, system, tools, max_tokens, temperature, stream)
        body = responses.request_body(self.model, messages, system, tools, self.reasoning_effort)
        body["max_output_tokens"] = max_tokens
        return body

    def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> Completion:
        if self.uses_responses:
            return self.stream_completion(
                messages, system=system, tools=tools, max_tokens=max_tokens, temperature=temperature
            )
        return super().complete(
            messages, system=system, tools=tools, max_tokens=max_tokens, temperature=temperature
        )

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
        if not self.uses_responses:
            return super().stream_completion(
                messages,
                system=system,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
                on_delta=on_delta,
                on_chunk=on_chunk,
            )
        body = self._body(messages, system, tools, max_tokens, temperature, True)
        emit = chunk_dispatcher(on_delta, on_chunk)
        result = responses.parse_stream(
            self._post_stream(body),
            (lambda text: emit(TextDelta(text))) if emit else None,
            label=self.label,
            model=self.model,
        )
        if emit:
            for call in result.tool_calls:
                emit(ToolCallEnd(call))
        return result

    def _embeddings_url(self) -> str:
        if self.uses_responses:
            return self.base_url.removesuffix("/responses") + "/embeddings"
        return super()._embeddings_url()
