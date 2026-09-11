"""Chat card + stub tool so a bot can ask the user to authorize a connector."""

from __future__ import annotations

from typing import Any

from providers.base import ToolSpec

from .base import ConnectorContext, ConnectorTool


def card_payload(record: dict[str, Any]) -> dict[str, Any]:
    from harness.connectors import CATALOG

    type_ = str(record.get("type") or "")
    cat = next((c for c in CATALOG if c.get("type") == type_), {})
    title = str(record.get("name") or cat.get("name") or type_ or "Connector")
    desc = str(cat.get("description") or "")
    if type_ == "google":
        low = title.lower()
        if "gmail" in low:
            desc = "Read and send email, plus Calendar and Drive."
        else:
            desc = "Search, read, create, and share files."
    return {
        "title": title,
        "description": desc,
        "type": type_,
        "icon": cat.get("icon"),
        "connector_id": str(record.get("id") or ""),
    }


def emit_sign_in_card(ctx: ConnectorContext) -> str | None:
    return ctx.card("connector", card_payload(ctx.record))


def connect_stub(type_: str, name: str) -> ConnectorTool:
    """One tool that only shows the Authorize card (OAuth not connected yet)."""

    def handler(ctx: ConnectorContext, args: dict[str, Any]) -> str:
        emit_sign_in_card(ctx)
        title = str(ctx.record.get("name") or name or type_)
        return (
            f"ok: a sign-in card for {title} is in chat. "
            "Wait for the user to tap Authorize. Do not send them to Settings."
        )

    spec = ToolSpec(
        name=f"{type_}_connect",
        description=(
            f"Show a chat card so the user can authorize {name}. "
            "Call this when the user wants to use this service and it is not signed in."
        ),
        parameters={"type": "object", "properties": {}},
    )
    return ConnectorTool(spec, handler)
