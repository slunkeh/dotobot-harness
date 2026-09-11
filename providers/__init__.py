"""Provider registry.

Adding a provider is a new adapter registered here, not a fork of the agent
loop.
"""

from __future__ import annotations

from typing import Any

from .anthropic import AnthropicProvider
from .base import (
    Auth,
    Completion,
    InputLimitError,
    Message,
    OutputLimitError,
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
    merge_reasoning,
    reasoning_for_model,
)
from .codex_chatgpt import CodexChatGPTProvider
from .echo import EchoProvider
from .grok import GrokProvider
from .openai import OpenAIProvider
from .vendors import (
    DeepSeekProvider,
    GLMProvider,
    KimiProvider,
    MiniMaxProvider,
    QwenProvider,
)

_REGISTRY: dict[str, type] = {
    "echo": EchoProvider,  # tests / local harness loops; hidden from the UI catalog
    "claude": AnthropicProvider,
    "anthropic": AnthropicProvider,
    "codex": OpenAIProvider,
    "openai": OpenAIProvider,
    "codex-chatgpt": CodexChatGPTProvider,
    "chatgpt": CodexChatGPTProvider,
    "grok": GrokProvider,
    "deepseek": DeepSeekProvider,
    "qwen": QwenProvider,
    "glm": GLMProvider,
    "zai": GLMProvider,
    "kimi": KimiProvider,
    "minimax": MiniMaxProvider,
}


def available_providers() -> list[str]:
    return sorted(_REGISTRY)


def build_provider(
    name: str,
    model: str | None = None,
    *,
    auth: Auth | None = None,
    **options: Any,
) -> Provider:
    """Instantiate a provider by id.

    Raises ProviderError for unknown providers so the orchestrator can fail
    with a clear message instead of a KeyError.
    """
    try:
        cls = _REGISTRY[name.lower()]
    except KeyError as exc:
        raise ProviderError(
            f"Unknown provider {name!r}. Known: {', '.join(available_providers())}"
        ) from exc
    kwargs: dict[str, Any] = {"auth": auth, **options}
    if model:
        kwargs["model"] = model
    return cls(**kwargs)


__all__ = [
    "Auth",
    "Completion",
    "InputLimitError",
    "Message",
    "OutputLimitError",
    "Provider",
    "ProviderError",
    "ReasoningContent",
    "ReasoningDelta",
    "ReasoningPart",
    "ReasoningSignatureDelta",
    "RedactedReasoningDelta",
    "RedactedReasoningPart",
    "StreamChunk",
    "TextDelta",
    "ToolCall",
    "ToolCallDelta",
    "ToolCallEnd",
    "ToolCallStart",
    "ToolSpec",
    "available_providers",
    "build_provider",
    "merge_reasoning",
    "reasoning_for_model",
]
