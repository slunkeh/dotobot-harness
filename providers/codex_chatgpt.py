"""Codex over the ChatGPT backend, signed in via the local Codex CLI.

No API key and no harness-run OAuth: the bearer token and ChatGPT account id
come from the Codex CLI's own login (`providers/codex_login.py`), expired
tokens are refreshed through a 401-retry-once wrapper around every HTTP call
(plugged into `Auth.bearer()` from `providers/base.py`), and model /
reasoning-effort defaults come from `~/.codex/config.toml`.

The wire format is the Responses API at
`https://chatgpt.com/backend-api/codex/responses` (the only endpoint ChatGPT
plan tokens are valid for), always streamed and folded into the
vendor-neutral `Completion` shape. HTTP plumbing (`_post_stream`, headers,
bearer resolution) is shared with `OpenAIChatProvider`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from . import codex_login, responses
from .base import Auth, Completion, Message, ProviderError, ToolSpec
from .openai_compat import OpenAIChatProvider

_DEFAULT_MODEL = "gpt-5.6-sol"


class CodexChatGPTProvider(OpenAIChatProvider):
    id = "codex-chatgpt"
    label = "Codex (ChatGPT)"
    default_model = _DEFAULT_MODEL
    default_url = "https://chatgpt.com/backend-api/codex/responses"
    missing_credentials = codex_login.SIGN_IN_HINT

    def __init__(self, model: str = "", auth: Auth | None = None, **options: Any) -> None:
        home = options.pop("codex_home", None)
        #: None -> resolve $CODEX_HOME / ~/.codex at call time
        self._home: Path | None = Path(home) if home else None
        self._transport = options.pop("transport", None)
        self._creds: codex_login.CodexCredentials | None = None
        #: Harness-stored ChatGPT account id (subscription OAuth). When set we
        #: never read ~/.codex/auth.json.
        self._account_id = str(options.pop("account_id", "") or "").strip() or None
        self._force_refresh = options.pop("force_refresh", None)
        model = model or codex_login.configured_model(self._home) or self.default_model
        effort = options.pop("reasoning_effort", None) or codex_login.configured_reasoning_effort(
            self._home
        )
        self.reasoning_effort = (str(effort).strip() if effort else "") or None
        auth = auth or Auth()
        if auth.refresh is None:
            # The Auth.bearer() hook: every request re-resolves the current
            # access token, so rotations written by the CLI (or by our own
            # refresh) are picked up without restarting the bot.
            auth.refresh = self._access_token
        super().__init__(model, auth, **options)

    # -- credentials -------------------------------------------------------
    def _using_harness_oauth(self) -> bool:
        return bool(self._account_id)

    def _credentials(self) -> codex_login.CodexCredentials:
        if self._creds is None:
            self._creds = codex_login.load_credentials(self._home)
        return self._creds

    def _access_token(self) -> str:
        if self._using_harness_oauth():
            token = self.auth.oauth_token
            if token:
                return token
            raise ProviderError(self.missing_credentials)
        return self._credentials().access_token

    def _refresh_login(self) -> None:
        if callable(self._force_refresh):
            self._force_refresh()
            return
        if self._using_harness_oauth():
            return
        self._creds = codex_login.refresh_credentials(
            self._credentials(), transport=self._transport
        )

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        headers["accept"] = "text/event-stream"
        account = self._account_id or self._credentials().account_id
        headers["chatgpt-account-id"] = account
        return headers

    # -- request/response (Responses API) ----------------------------------
    def _body(
        self,
        messages: list[Message],
        system: str | None,
        tools: list[ToolSpec] | None,
        max_tokens: int,
        temperature: float,
        stream: bool,
    ) -> dict[str, Any]:
        # Subscription endpoint rejects sampling and output-limit parameters.
        return responses.request_body(self.model, messages, system, tools, self.reasoning_effort)

    def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> Completion:
        # The backend only streams; fold the stream without a delta callback.
        return self.stream_completion(
            messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
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
    ) -> Completion:
        body = self._body(messages, system, tools, max_tokens, temperature, stream=True)

        def attempt() -> Completion:
            return self._parse_stream(self._post_stream(body), on_delta, model=self.model)

        # A 401 surfaces before any SSE data arrives, so the retried attempt
        # never repeats deltas the caller already saw.
        return codex_login.retry_once_on_401(attempt, self._refresh_login)

    @staticmethod
    def _parse_stream(
        lines: Iterable[bytes | str],
        on_delta: Callable[[str], None] | None,
        *,
        model: str | None = None,
    ) -> Completion:
        return responses.parse_stream(lines, on_delta, label="Codex (ChatGPT)", model=model)
