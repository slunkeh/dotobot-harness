"""Pluggable model-provider abstraction.

One agent loop, many backends. A bot's roster entry picks a provider + model;
the agent, tools, and memory do not care which vendor answers.

Design goals:
* Streaming and non-streaming chat completions.
* Tool / function calls surfaced in a vendor-neutral shape.
* Auth that covers an API key today and an OAuth token-refresh hook tomorrow
 , without the call sites changing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar


class ProviderError(RuntimeError):
    """Raised when a provider cannot complete a request."""


class OutputLimitError(ProviderError):
    """The reply was cut off by the model's max output tokens.

    Adapters may raise this directly; failures phrased only in vendor error
    text are classified by `agent.repairs.classify_provider_error`.
    """


class InputLimitError(ProviderError):
    """The request no longer fits the model's context window."""


@dataclass
class ReasoningPart:
    """A thinking block as first-class assistant content.

    `model` tags the producing model so a block is only ever replayed to the
    provider/model that made it (Anthropic rejects foreign signatures, and no
    vendor should see another vendor's thinking). `signature` seals the block:
    once set, no more streamed reasoning text may append to it.
    """

    text: str = ""
    signature: str | None = None
    model: str | None = None


@dataclass
class RedactedReasoningPart:
    """Opaque (encrypted) thinking the vendor redacted; replayed verbatim."""

    data: str = ""
    model: str | None = None


#: What a Message/Completion carries as replayable reasoning content.
ReasoningContent = ReasoningPart | RedactedReasoningPart


@dataclass
class ResponsesOutput:
    """Current-turn OpenAI output items for stateless Responses replay.

    Keep assistant item boundaries and phases, tool calls, and encrypted
    reasoning in their original order. Only the Responses adapter consumes
    this state, and only for the exact model that produced it.
    """

    model: str | None
    items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Message:
    """A single chat message in vendor-neutral form."""

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    name: str | None = None  # sender attribution / tool name
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None
    #: optional vision attachments (mime, bytes). Current-turn only — never
    #: persisted in session logs.
    images: list[tuple[str, bytes]] | None = None
    #: assistant-only: thinking blocks from the turn that produced this
    #: message, so extended thinking survives the tool loop. Only
    #: parts tagged with the serving model are ever sent back (see
    #: `reasoning_for_model`).
    reasoning: list[ReasoningContent] | None = None
    responses_output: ResponsesOutput | None = None


@dataclass
class ToolSpec:
    """A tool the model may call, described with a JSON-schema parameter block."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCall:
    """A model's request to invoke a tool."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Completion:
    """The result of a single completion call."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    raw: Any = None
    #: thinking blocks the model produced this turn, in order, model-tagged
    #: Attach to the assistant Message when replaying the turn.
    reasoning: list[ReasoningContent] = field(default_factory=list)
    #: Last assistant item's phase, when supplied by the provider. An
    #: explicit commentary update is not a final answer.
    phase: str | None = None
    responses_output: ResponsesOutput | None = None


# -- typed stream chunks ------------------------------------------
# The streaming wire format between a provider's SSE reader and its consumer.
# `on_delta(str)` remains a thin shim that sees only the text_delta chunks.


@dataclass
class TextDelta:
    """A fragment of assistant-visible text."""

    kind: ClassVar[str] = "text_delta"
    text: str


@dataclass
class ReasoningDelta:
    """A fragment of thinking text (extended thinking / reasoning models)."""

    kind: ClassVar[str] = "reasoning"
    text: str
    model: str | None = None


@dataclass
class RedactedReasoningDelta:
    """Opaque redacted-thinking data; concatenates onto a redacted part."""

    kind: ClassVar[str] = "redacted_reasoning"
    data: str
    model: str | None = None


@dataclass
class ReasoningSignatureDelta:
    """A signature sealing the trailing reasoning block."""

    kind: ClassVar[str] = "reasoning_signature"
    signature: str


@dataclass
class ToolCallStart:
    """A tool call opened; its arguments may stream as tool_call_delta."""

    kind: ClassVar[str] = "tool_call_start"
    id: str
    name: str


@dataclass
class ToolCallDelta:
    """A fragment of a tool call's JSON arguments text."""

    kind: ClassVar[str] = "tool_call_delta"
    id: str
    arguments: str


@dataclass
class ToolCallEnd:
    """Terminal chunk for one tool call, carrying the fully parsed call.

    Every tool call ends with this chunk whether or not its arguments
    streamed first — providers that cannot stream arguments emit only this —
    so consumers handle both paths identically by acting on ToolCallEnd.
    """

    kind: ClassVar[str] = "tool_call"
    call: ToolCall


StreamChunk = (
    TextDelta
    | ReasoningDelta
    | RedactedReasoningDelta
    | ReasoningSignatureDelta
    | ToolCallStart
    | ToolCallDelta
    | ToolCallEnd
)


def merge_reasoning(parts: list[ReasoningContent], chunk: StreamChunk) -> None:
    """Fold one reasoning-flavored chunk into accumulated message parts.

    The seal rule (grok-bot tool-stream-executor): a reasoning delta appends
    to the trailing reasoning part only while that part's signature is unset;
    a signature seals the trailing part; a second signature starts a new
    (empty, sealed) part; redacted data concatenates onto a trailing redacted
    part. Non-reasoning chunks are ignored.
    """
    last = parts[-1] if parts else None
    if isinstance(chunk, ReasoningDelta):
        if isinstance(last, ReasoningPart) and last.signature is None:
            last.text += chunk.text
            if chunk.model:
                last.model = chunk.model
        else:
            parts.append(ReasoningPart(text=chunk.text, model=chunk.model))
    elif isinstance(chunk, RedactedReasoningDelta):
        if isinstance(last, RedactedReasoningPart):
            last.data += chunk.data
            if chunk.model:
                last.model = chunk.model
        else:
            parts.append(RedactedReasoningPart(data=chunk.data, model=chunk.model))
    elif isinstance(chunk, ReasoningSignatureDelta) and isinstance(last, ReasoningPart):
        if last.signature is None:
            last.signature = chunk.signature
        else:
            parts.append(ReasoningPart(text="", signature=chunk.signature))


def reasoning_for_model(
    parts: list[ReasoningContent] | None, model: str | None
) -> list[ReasoningContent]:
    """Only blocks produced by exactly `model` may be replayed to it.

    Never send one vendor's (or model's) thinking to another: untagged or
    foreign-tagged parts are dropped on request build.
    """
    if not parts or not model:
        return []
    return [p for p in parts if p.model == model]


def chunk_dispatcher(
    on_delta: Callable[[str], None] | None,
    on_chunk: Callable[[StreamChunk], None] | None,
) -> Callable[[StreamChunk], None] | None:
    """Fan one typed chunk out to the chunk consumer and the legacy shim.

    `on_delta(str)` stays a thin shim over the chunk stream: it sees exactly
    the non-empty text_delta chunks and nothing else, so channels built on it
    keep working unchanged.
    """
    if on_delta is None and on_chunk is None:
        return None

    def emit(chunk: StreamChunk) -> None:
        if on_chunk is not None:
            on_chunk(chunk)
        if on_delta is not None and isinstance(chunk, TextDelta) and chunk.text:
            on_delta(chunk.text)

    return emit


@dataclass
class Auth:
    """Credentials for a provider.

    `api_key` is the day-one path. `oauth_token` + `refresh` is the seam for
    vendor OAuth (Claude Pro / ChatGPT / x.com) so tokens can be refreshed on
    the self-hosted host without re-login every session. Nothing here is ever
    logged or printed.
    """

    api_key: str | None = None
    oauth_token: str | None = None
    refresh: Callable[[], str] | None = None

    def bearer(self) -> str | None:
        """Return the currently valid bearer token, refreshing if possible."""
        if self.refresh is not None:
            token = self.refresh()
            if token:
                self.oauth_token = token
        return self._outbound(self.oauth_token)

    def header_key(self) -> str | None:
        """The API key as it may go into an auth header (boundary).

        A sentinel token is unsealed to plaintext here; one this process
        cannot unseal raises UnresolvedSentinelError before any network I/O,
        so a request never leaves carrying the sentinel.
        """
        return self._outbound(self.api_key)

    @staticmethod
    def _outbound(value: str | None) -> str | None:
        if not value:
            return value
        from harness.redaction import resolve_outbound

        return resolve_outbound(value, where="a provider auth header")

    def __repr__(self) -> str:  # never leak secrets in logs / tracebacks
        have_key = "set" if self.api_key else "unset"
        have_oauth = "set" if (self.oauth_token or self.refresh) else "unset"
        return f"Auth(api_key={have_key}, oauth={have_oauth})"


class Provider:
    """Base class for model providers.

    Subclasses implement `complete()` (and optionally `stream()`). Everything
    the agent needs is expressed through `Message` / `ToolSpec` / `Completion`.
    """

    #: Short id used in the roster (e.g. "claude", "codex", "grok", "echo").
    id: str = "base"
    #: Approximate context window (tokens), used to budget conversation
    #: history. A conservative default; adapters override, and a
    #: `context_window` option overrides per instance.
    context_window: int = 32_000
    #: Default model for `embed()`. Empty = this provider has no embedding
    #: route (echo and the chat-only vendors), and `embed()` raises.
    embedding_model: str = ""

    def __init__(self, model: str, auth: Auth | None = None, **options: Any) -> None:
        self.model = model
        self.auth = auth or Auth()
        self.options = options
        if options.get("context_window"):
            self.context_window = int(options["context_window"])

    def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> Completion:
        raise NotImplementedError

    def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        """Return one embedding vector per input text.

        Embeddings are an enhancement over keyword recall, never a
        requirement: a provider without an embedding route raises
        ProviderError and callers fall back to the keyword floor. Adapters
        must raise for missing credentials before any network I/O.
        """
        raise ProviderError(f"provider {self.id!r} has no embedding route")

    def stream(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> Iterator[str]:
        """Text-only streaming shim kept for callers that just want deltas."""
        yield self.complete(
            messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        ).text

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
        """Complete while pushing typed chunks / text deltas as they arrive.

        Returns the full Completion (tool calls included) at the end of the
        turn, so the agent's tool loop can stream every turn and only learn
        whether tools were called once the stream finishes. Default shim:
        non-streaming complete(), delivered as a single text_delta plus one
        terminal tool_call chunk per call (no argument streaming — consumers
        must treat that identically to the fragmented path). Providers with
        real SSE support override this.
        """
        completion = self.complete(
            messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        emit = chunk_dispatcher(on_delta, on_chunk)
        if emit is not None:
            for part in completion.reasoning:
                if isinstance(part, RedactedReasoningPart):
                    emit(RedactedReasoningDelta(data=part.data, model=part.model))
                    continue
                if part.text:
                    emit(ReasoningDelta(text=part.text, model=part.model))
                if part.signature:
                    emit(ReasoningSignatureDelta(signature=part.signature))
            if completion.text:
                emit(TextDelta(completion.text))
            for call in completion.tool_calls:
                emit(ToolCallEnd(call))
        return completion
