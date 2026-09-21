"""Registry mapping connector catalog types to their tool runtimes.

A runtime is a zero-arg callable returning the service's ConnectorTools.
Types without a static entry still bind: MCP tools when connected, or the
generic API-key HTTP runtime (`connectors/generic.py`) when the operator
pasted a key.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from harness.paths import HarnessPaths
from providers.base import ToolSpec

from .base import ConnectorContext, ConnectorTool

#: (spec, run) where run takes the model's arguments and returns the tool result
BoundTool = tuple[ToolSpec, Callable[[dict[str, Any]], str]]

_STATIC_TOOLS: dict[str, list[ConnectorTool]] = {}


def runtimes() -> dict[str, Callable[[], list[ConnectorTool]]]:
    from . import github, gmail, linear

    return {"linear": linear.tools, "github": github.tools, "gmail": gmail.tools}


def _static_tools(type_: str) -> list[ConnectorTool]:
    cached = _STATIC_TOOLS.get(type_)
    if cached is not None:
        return cached
    maker = runtimes().get(type_)
    tools = maker() if maker is not None else []
    _STATIC_TOOLS[type_] = tools
    return tools


_SLUG = re.compile(r"[^a-z0-9]+")


def account_slug(record: dict[str, Any]) -> str:
    """Stable extra prefix when two records share a catalog type.

    From the record's name ("Gmail work" → `work`; an address as the name
    → `ada_example_com`), falling back to the record id. Never ends in an
    underscore, so a truncated slug still joins cleanly to the tool rest.
    """
    type_ = str(record.get("type") or "")
    name = str(record.get("name") or "").strip()
    cat = type_.replace("_", " ")
    rest = name
    if name.lower().startswith(cat.lower()):
        rest = name[len(cat) :].strip(" -()")
    slug = _SLUG.sub("_", rest.lower()).strip("_")
    if not slug:
        slug = str(record.get("id") or "acct")[:8]
    return slug[:24].rstrip("_")


def tool_prefix(record: dict[str, Any], type_counts: Counter[str] | None = None) -> str:
    type_ = str(record.get("type") or "")
    if type_counts is None or type_counts.get(type_, 0) <= 1:
        return type_
    return f"{type_}_{account_slug(record)}"


def tool_prefixes(records: list[dict[str, Any]], type_counts: Counter[str]) -> dict[str, str]:
    """Record id → tool prefix, unique across `records`.

    Two accounts whose names slug alike ("Gmail Work" and "gmail-work!")
    would otherwise bind the same tool names and the later one would
    silently replace the first; the second such record gets its id
    appended instead.
    """
    out: dict[str, str] = {}
    used: set[str] = set()
    for record in records:
        rid = str(record.get("id") or "")
        if not rid or rid in out:
            continue
        prefix = tool_prefix(record, type_counts)
        if prefix in used and prefix != str(record.get("type") or ""):
            prefix = f"{prefix}_{rid[:4]}"
        used.add(prefix)
        out[rid] = prefix
    return out


def _catalog_entry(type_: str) -> dict[str, Any]:
    from harness.connectors import CATALOG

    return next((c for c in CATALOG if c.get("type") == type_), {})


def _wants_oauth(type_: str) -> bool:
    cat = _catalog_entry(type_)
    return bool(cat.get("mcp_url") or cat.get("auth") == "oauth")


def _prefer_static(type_: str) -> bool:
    """True when the static runtime should win over a connected MCP server.

    Gmail MCP is Developer Preview and rejects tools/call until the Cloud
    project is enrolled; REST with the same OAuth token works today.
    """
    return bool(_catalog_entry(type_).get("prefer_static"))


def _namespace_static(
    tools: list[ConnectorTool], type_: str, prefix: str, account: str = ""
) -> list[ConnectorTool]:
    """Rename `<type>_<rest>` to `<prefix>_<rest>` for a second account of a
    type, and lead each description with the account so the model can tell
    `gmail_work_send` from `gmail_home_send` without decoding the name."""
    if prefix == type_:
        return tools
    out: list[ConnectorTool] = []
    head = type_ + "_"
    for tool in tools:
        raw = tool.spec.name
        rest = raw[len(head) :] if raw.startswith(head) else raw
        description = tool.spec.description
        if account:
            description = f"{account}: {description}"
        out.append(
            ConnectorTool(
                replace(tool.spec, name=f"{prefix}_{rest}", description=description),
                tool.handler,
            )
        )
    return out


def tool_names(type_: str) -> list[str]:
    maker = runtimes().get(type_)
    if maker is not None:
        return [t.spec.name for t in maker()]
    from harness.connectors import CATALOG

    cat = next((c for c in CATALOG if c.get("type") == type_), {})
    if cat.get("mcp_url") or cat.get("mcp_url_template"):
        return []
    if cat.get("auth") == "api_key" or cat.get("oauth_supported"):
        from . import generic

        return generic.tool_names(type_)
    return []


def tools_for_bot(
    paths: HarnessPaths,
    bot: str,
    emit_card: Callable[..., str | None] | None = None,
    record_ids: set[str] | None = None,
) -> dict[str, BoundTool]:
    """Bound tools for every configured connector this bot may use.

    Resolved from `connectors.json` on each call so newly added connectors
    work without a bot restart. A record scopes itself with `enabled_for`
    (None = every bot). When several records share a type (two Gmail
    inboxes), each binds under its own prefix (`tool_prefixes`:
    `gmail_<account>_*`) so every account's tools are offered, with the
    account name leading each description. A record whose catalog type has a
    remote MCP server binds the server's own tools (connectors/mcp.py)
    once OAuth is connected, or — for types with no static runtime — when
    a pasted API key can be the bearer. Otherwise the static api-key
    runtime, if any, applies. Never raises: connector failures surface as
    tool-result strings, not crashed turns.
    """
    from harness import mcp_oauth
    from harness.connectors import Connectors, mcp_url

    out: dict[str, BoundTool] = {}
    seen: set[str] = set()
    makers = runtimes()
    try:
        records = Connectors(paths).records()
    except Exception:  # malformed connectors.json must not kill the turn
        return out
    visible = [
        r
        for r in records
        if str(r.get("id") or "")
        and not (isinstance(r.get("enabled_for"), list) and bot not in r["enabled_for"])
    ]
    type_counts: Counter[str] = Counter(str(r.get("type") or "") for r in visible)
    prefixes = tool_prefixes(visible, type_counts)
    for record in visible:
        try:
            type_ = str(record.get("type", ""))
            rid = str(record.get("id") or "")
            served = mcp_url(type_, record.get("config"))
            maker = makers.get(type_)
            wants_oauth = _wants_oauth(type_)
            if rid in seen:
                continue
            if record_ids is not None and rid not in record_ids:
                continue
            seen.add(rid)
            prefix = prefixes.get(rid) or tool_prefix(record, type_counts)
            account = str(record.get("name") or "").strip() if prefix != type_ else ""
            ctx = ConnectorContext(paths=paths, bot=bot, record=record, emit_card=emit_card)
            oauth_connected = mcp_oauth.connected(paths, rid)
            # MCP when OAuth is done, or when this type has no static runtime
            # and a pasted API key can be the bearer (Cloudflare). Types with
            # a hand-built runtime (GitHub, Linear, Gmail) keep using that
            # for a key. Remaining api-key stubs get generic HTTPS tools.
            use_mcp = (
                bool(served)
                and not _prefer_static(type_)
                and (oauth_connected or (maker is None and bool(ctx.secret())))
            )
            if use_mcp:
                from . import mcp

                bound = mcp.bind_tools(ctx, served, name_prefix=prefix)
                # Empty MCP bind (server down, tools/list failed) must not
                # hide the static runtime — a new bot would then only have
                # computer tools for a GitHub job.
                if not bound and maker is not None:
                    bound = _namespace_static(_static_tools(type_), type_, prefix, account)
                elif not bound:
                    from .authorize import connect_stub

                    bound = [connect_stub(type_, str(record.get("name") or type_))]
            elif maker is not None:
                bound = _namespace_static(_static_tools(type_), type_, prefix, account)
            elif wants_oauth and not _prefer_static(type_) and not ctx.secret():
                from .authorize import connect_stub

                bound = [connect_stub(type_, str(record.get("name") or type_))]
            else:
                from . import generic

                bound = _namespace_static(generic.tools(type_), type_, prefix, account)
        except Exception:  # skip a broken record, keep the rest
            continue
        for tool in bound:

            def run(args: dict[str, Any], _h=tool.handler, _ctx=ctx) -> str:
                try:
                    return _h(_ctx, args)
                except Exception as exc:  # a connector bug must not kill the turn
                    return f"error: {exc}"

            out[tool.spec.name] = (tool.spec, run)
    return out
