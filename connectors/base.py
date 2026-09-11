"""Shared interfaces for connector tool runtimes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from harness.paths import HarnessPaths
from harness.redaction import resolve_outbound
from harness.secrets import get_secret
from providers.base import ToolSpec


@dataclass
class ConnectorContext:
    """What a connector tool handler runs against: the harness paths, the bot
    calling the tool, and the configured connector record (id/name/config)."""

    paths: HarnessPaths
    bot: str
    record: dict[str, Any]
    #: emit a rich chat card (card_type, payload, card_id=None) -> card id;
    #: None when the turn has no live stream (headless runs, older callers)
    emit_card: Callable[..., str | None] | None = None

    def card(
        self, card_type: str, payload: dict[str, Any], card_id: str | None = None
    ) -> str | None:
        """Render a card in the chat if a stream is attached; else no-op.

        Cards are upserts keyed by id: passing the same `card_id` again
        replaces that card in place (a PR card flipping to merged), so a
        handler that wants stable identity supplies its own deterministic id.
        """
        if self.emit_card is None:
            return None
        if card_id is None:
            return self.emit_card(card_type, payload)
        return self.emit_card(card_type, payload, card_id)

    @property
    def secret_name(self) -> str:
        return f"connector_{self.record.get('id', '')}"

    def config(self, key: str) -> str:
        return str((self.record.get("config") or {}).get(key, "")).strip()

    def secret(self, fallback: str | None = None) -> str | None:
        """The connector's credential; `fallback` is a conventional env-style
        name (e.g. LINEAR_API_KEY) so headless setups can skip the UI.

        This is the connector-side sentinel boundary: a stored
        sentinel token is unsealed to plaintext here, just before request
        construction; one this process cannot unseal raises
        UnresolvedSentinelError so no request is ever sent carrying it.
        """
        value = get_secret(self.secret_name, self.paths)
        if not value and fallback:
            value = get_secret(fallback, self.paths)
        if value:
            value = resolve_outbound(
                value, where=f"a {self.record.get('type') or 'connector'} request"
            )
        return value

    def missing_secret(self, fallback: str | None = None) -> str:
        name = self.record.get("name") or self.record.get("type") or "connector"
        hint = f" (or the user can store it as {fallback})" if fallback else ""
        return (
            f"error: connector {name!r} has no API key yet. Call request_secret "
            f"with name {self.secret_name!r} and a short title/reason to ask "
            f"the user for one{hint}; they can also add it in Manage > Connectors."
        )


Handler = Callable[[ConnectorContext, dict[str, Any]], str]


@dataclass
class ConnectorTool:
    """One tool a connector runtime contributes: the spec advertised to the
    model plus a handler run against a ConnectorContext."""

    spec: ToolSpec
    handler: Handler
