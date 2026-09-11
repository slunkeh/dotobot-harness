"""xAI Grok provider.

OpenAI-compatible Chat Completions at https://api.x.ai/v1. Auth is an OAuth
bearer from the Hermes device-code flow (`providers.xai_oauth`) or `XAI_API_KEY`.
All wire plumbing lives in `OpenAIChatProvider`.
"""

from __future__ import annotations

from typing import Any

from .base import Auth
from .openai_compat import OpenAIChatProvider
from .xai_oauth import INFERENCE_BASE

_DEFAULT_MODEL = "grok-4.6"


class GrokProvider(OpenAIChatProvider):
    id = "grok"
    label = "Grok"
    default_model = _DEFAULT_MODEL
    default_url = f"{INFERENCE_BASE}/chat/completions"
    missing_credentials = (
        "Grok needs an xAI OAuth login (Manage → Sign in with OAuth) or XAI_API_KEY."
    )

    def __init__(
        self, model: str = _DEFAULT_MODEL, auth: Auth | None = None, **options: Any
    ) -> None:
        super().__init__(model, auth, **options)
