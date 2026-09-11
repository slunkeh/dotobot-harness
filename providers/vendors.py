"""Chinese-lab OpenAI-compatible (and MiniMax Anthropic-compatible) adapters.

Auth is an API key for all of these. MiniMax also accepts the Hermes user-code
OAuth bearer (`providers.minimax_oauth`). Wire plumbing is the shared
OpenAI-compat or Anthropic adapter — these classes only declare identity.
"""

from __future__ import annotations

from typing import Any

from .anthropic import AnthropicProvider
from .base import Auth, ProviderError
from .openai_compat import OpenAIChatProvider


class _ThinkingChatProvider(OpenAIChatProvider):
    """These APIs require reasoning_content when replaying a thinking tool turn."""

    replay_reasoning = True

    def _sampling(self, max_tokens: int, temperature: float) -> dict[str, Any]:
        params = super()._sampling(max_tokens, temperature)
        effort = self.options.get("reasoning_effort")
        if effort and self.model.startswith(("deepseek-v4", "glm-5.3", "kimi-k3")):
            params["reasoning_effort"] = effort
        return params


class DeepSeekProvider(_ThinkingChatProvider):
    id = "deepseek"
    label = "DeepSeek"
    default_model = "deepseek-v4-flash"
    default_url = "https://api.deepseek.com/v1/chat/completions"
    missing_credentials = "DeepSeek needs DEEPSEEK_API_KEY."


class QwenProvider(OpenAIChatProvider):
    id = "qwen"
    label = "Qwen"
    default_model = "qwen3.7-plus"
    default_url = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"
    missing_credentials = "Qwen needs DASHSCOPE_API_KEY (Alibaba Cloud Model Studio)."


class GLMProvider(_ThinkingChatProvider):
    id = "glm"
    label = "GLM"
    default_model = "glm-5.3"
    default_url = "https://api.z.ai/api/paas/v4/chat/completions"
    missing_credentials = "GLM needs GLM_API_KEY (or ZAI_API_KEY)."


class KimiProvider(_ThinkingChatProvider):
    id = "kimi"
    label = "Kimi"
    default_model = "kimi-k3"
    default_url = "https://api.moonshot.ai/v1/chat/completions"
    missing_credentials = "Kimi needs KIMI_API_KEY (Moonshot)."


class MiniMaxProvider(AnthropicProvider):
    """MiniMax Coding Plan — Anthropic Messages at api.minimax.io/anthropic."""

    id = "minimax"
    context_window = 200_000

    def __init__(self, model: str = "MiniMax-M3", auth: Auth | None = None, **options: Any) -> None:
        options.setdefault("base_url", "https://api.minimax.io/anthropic/v1/messages")
        super().__init__(model, auth, **options)

    def _headers(self) -> dict[str, str]:
        token = self.auth.bearer() or self.auth.header_key()
        if not token:
            raise ProviderError(
                "MiniMax needs an OAuth login (Manage → Sign in with OAuth) or MINIMAX_API_KEY."
            )
        return {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "authorization": f"Bearer {token}",
        }
