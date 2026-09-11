"""Generic MCP connector runtime: bot tools served by a remote MCP server.

A connector whose catalog entry carries `mcp_url` and that has completed the
OAuth connect flow (`harness/mcp_oauth.py`) gets its tools from the server
itself: `tools/list` becomes the ToolSpecs (namespaced `<type>_<tool>`) and
each call forwards to `tools/call`. Vendors maintain the tool surface; the
harness only speaks the protocol.

Stdlib only. Speaks MCP Streamable HTTP: JSON-RPC 2.0 over POST, responses
as plain JSON or a single SSE stream. Sessions (`Mcp-Session-Id`) and the
tool list are cached in-process per connector; a 401 forces a token refresh
and one retry, a dead session re-initializes, and any failure surfaces as a
tool-result string — never a crashed turn.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from harness import mcp_oauth
from providers.base import ToolSpec

from .base import ConnectorContext, ConnectorTool

PROTOCOL_VERSION = "2025-06-18"
TOOLS_CACHE_SECONDS = 300
_MAX_DESCRIPTION = 1024
_MAX_RESULT = 12_000


def _clip_description(description: str, schema: dict[str, Any]) -> str:
    """Keep Code Mode sandbox docs intact; clip ordinary tool blurbs.

    Cloudflare (and similar) search/execute tools put the JS API in the
    description (`spec.paths` vs `cloudflare.request`). Cutting that at 1k
    chars makes the model call search with `cloudflare`, which is not defined
    in the search sandbox.
    """
    props = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(props, dict) and "code" in props:
        return description
    if len(description) > _MAX_DESCRIPTION:
        return description[:_MAX_DESCRIPTION] + "…"
    return description


class MCPError(RuntimeError):
    """An MCP request failed (HTTP, protocol, or JSON)."""


class MCPAuthError(MCPError):
    """The MCP server rejected the bearer token (HTTP 401/403)."""


class MCPSessionExpired(MCPError):
    """The server dropped our Mcp-Session-Id (HTTP 404); re-initialize."""


def _post(
    url: str,
    token: str,
    message: dict[str, Any],
    session_id: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """POST one JSON-RPC message; returns (response-for-our-id, session id).

    Module-level so tests can monkeypatch it. Handles both plain-JSON and
    SSE-framed responses; notifications (no id) accept an empty 202 body.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "User-Agent": "dotobot/0.1",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(
        url, data=json.dumps(message).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            new_session = resp.headers.get("Mcp-Session-Id") or session_id
            content_type = (resp.headers.get("Content-Type") or "").lower()
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if exc.code in (401, 403):
            raise MCPAuthError(f"MCP server rejected credentials (HTTP {exc.code})") from exc
        if exc.code == 404 and session_id:
            raise MCPSessionExpired("MCP session expired") from exc
        raise MCPError(f"MCP server HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MCPError(f"could not reach MCP server: {exc}") from exc
    if "id" not in message:  # notification: any 2xx is success
        return None, new_session
    if "text/event-stream" in content_type:
        return _from_sse(raw, message["id"]), new_session
    if not raw.strip():
        raise MCPError("MCP server returned an empty response")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MCPError("MCP server returned invalid JSON") from exc
    return payload if isinstance(payload, dict) else None, new_session


def _from_sse(raw: str, want_id: Any) -> dict[str, Any] | None:
    """The JSON-RPC response for `want_id` out of an SSE-framed body."""
    match: dict[str, Any] | None = None
    for chunk in raw.replace("\r\n", "\n").split("\n\n"):
        data_lines = [ln[5:].lstrip() for ln in chunk.split("\n") if ln.startswith("data:")]
        if not data_lines:
            continue
        try:
            obj = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("id") == want_id:
            match = obj
    return match


class _Session:
    """One connector's MCP connection: session id + cached tool list."""

    def __init__(self, mcp_url: str) -> None:
        self.mcp_url = mcp_url
        self.session_id: str | None = None
        self.initialized = False
        self.tools: list[dict[str, Any]] = []
        self.tools_at = 0.0
        self.instructions: str = ""
        self.next_id = 0
        self.lock = threading.Lock()

    def _rpc(self, token: str, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        self.next_id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            message["params"] = params
        payload, self.session_id = _post(self.mcp_url, token, message, self.session_id)
        if payload is None:
            raise MCPError(f"MCP server sent no response to {method}")
        if payload.get("error"):
            err = payload["error"]
            raise MCPError(f"MCP {method} failed: {err.get('message') or err}")
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    def _initialize(self, token: str) -> None:
        result = self._rpc(
            token,
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "dotobot", "version": "0.1"},
            },
        )
        self.instructions = str(result.get("instructions") or "").strip()
        _post(
            self.mcp_url,
            token,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            self.session_id,
        )
        self.initialized = True

    def request(self, token: str, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        """One initialized request; transparently recovers a dropped session."""
        if not self.initialized:
            self._initialize(token)
        try:
            return self._rpc(token, method, params)
        except MCPSessionExpired:
            self.session_id = None
            self.initialized = False
            self._initialize(token)
            return self._rpc(token, method, params)

    def list_tools(self, token: str) -> list[dict[str, Any]]:
        if self.tools and time.time() - self.tools_at < TOOLS_CACHE_SECONDS:
            return self.tools
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(10):  # paginated; bounded defensively
            params: dict[str, Any] = {"cursor": cursor} if cursor else {}
            result = self.request(token, "tools/list", params)
            batch = result.get("tools")
            if isinstance(batch, list):
                tools.extend(t for t in batch if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self.tools = tools
        self.tools_at = time.time()
        return tools


_sessions: dict[str, _Session] = {}
_sessions_lock = threading.Lock()


def _session(connector_id: str, mcp_url: str) -> _Session:
    with _sessions_lock:
        sess = _sessions.get(connector_id)
        if sess is None or sess.mcp_url != mcp_url:
            sess = _Session(mcp_url)
            _sessions[connector_id] = sess
        return sess


def reset_sessions() -> None:
    """Drop cached sessions/tool lists (sign-out, tests)."""
    with _sessions_lock:
        _sessions.clear()


def instructions_for(connector_id: str) -> str:
    """Live `initialize.instructions` from the MCP server, if we have a session."""
    with _sessions_lock:
        sess = _sessions.get(connector_id)
        return (sess.instructions or "") if sess is not None else ""


def _token(ctx: ConnectorContext, *, force: bool = False) -> str:
    """The bearer token: OAuth tokens first, stored API key as fallback."""
    connector_id = str(ctx.record.get("id") or "")
    token = mcp_oauth.access_token(ctx.paths, connector_id, force=force)
    if not token:
        token = ctx.secret()
    if not token:
        from .authorize import emit_sign_in_card

        emit_sign_in_card(ctx)
        raise MCPAuthError(
            "connector is not connected; a sign-in card is in chat. "
            "Wait for the user to tap Authorize."
        )
    return token


def _tool_name(type_: str, raw: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]", "_", raw)
    return slug if slug.startswith(f"{type_}_") else f"{type_}_{slug}"


def _format_result(result: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
        elif item.get("type") == "resource":
            resource = item.get("resource") or {}
            parts.append(str(resource.get("text") or resource.get("uri") or ""))
        else:
            parts.append(json.dumps(item, ensure_ascii=False))
    if not parts and isinstance(result.get("structuredContent"), dict):
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    text = "\n".join(p for p in parts if p).strip() or "(empty result)"
    if len(text) > _MAX_RESULT:
        text = text[:_MAX_RESULT] + "\n… (truncated)"
    if result.get("isError"):
        return f"error: {text}"
    return text


def _call(ctx: ConnectorContext, mcp_url: str, tool: str, args: dict[str, Any]) -> str:
    connector_id = str(ctx.record.get("id") or "")
    sess = _session(connector_id, mcp_url)
    with sess.lock:
        try:
            result = sess.request(
                _token(ctx), "tools/call", {"name": tool, "arguments": args or {}}
            )
        except MCPAuthError:
            # Access token likely expired server-side: refresh once and retry.
            sess.session_id = None
            sess.initialized = False
            result = sess.request(
                _token(ctx, force=True), "tools/call", {"name": tool, "arguments": args or {}}
            )
    return _format_result(result)


def tool_names(
    ctx_paths: Any,
    record: dict[str, Any],
    mcp_url: str,
    name_prefix: str | None = None,
) -> list[str]:
    """Namespaced tool names from the server, for catalog/record display.

    Network-touching; callers treat failures as 'unknown yet' (empty list).
    """
    type_ = name_prefix or str(record.get("type") or "connector")
    ctx = ConnectorContext(paths=ctx_paths, bot="", record=record)
    try:
        sess = _session(str(record.get("id") or ""), mcp_url)
        with sess.lock:
            tools = sess.list_tools(_token(ctx))
    except (MCPError, mcp_oauth.OAuthError):
        return []
    return [_tool_name(type_, str(t.get("name") or "")) for t in tools if t.get("name")]


def bind_tools(
    ctx: ConnectorContext,
    mcp_url: str,
    name_prefix: str | None = None,
) -> list[ConnectorTool]:
    """ConnectorTools for one connected record, from the server's tools/list.

    Errors (server down, token revoked) yield an empty list so the bot's
    turn proceeds without the connector rather than failing to start.
    `name_prefix` namespaces tools when two records share a catalog type
    (a second Gmail inbox).
    """
    type_ = name_prefix or str(ctx.record.get("type") or "connector")
    connector_id = str(ctx.record.get("id") or "")
    sess = _session(connector_id, mcp_url)
    try:
        with sess.lock:
            try:
                tools = sess.list_tools(_token(ctx))
            except MCPAuthError:
                sess.session_id = None
                sess.initialized = False
                tools = sess.list_tools(_token(ctx, force=True))
    except (MCPError, mcp_oauth.OAuthError):
        return []

    out: list[ConnectorTool] = []
    for tool in tools:
        raw_name = str(tool.get("name") or "")
        if not raw_name:
            continue
        description = str(tool.get("description") or f"{type_} {raw_name} (via MCP)")
        acct = str(ctx.record.get("name") or "").strip()
        if name_prefix and "_" in str(name_prefix) and acct:
            description = f"{acct}: {description}"
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or not schema:
            schema = {"type": "object", "properties": {}}
        description = _clip_description(description, schema)
        spec = ToolSpec(
            name=_tool_name(type_, raw_name), description=description, parameters=schema
        )

        def handler(c: ConnectorContext, args: dict[str, Any], _raw=raw_name, _url=mcp_url) -> str:
            try:
                return _call(c, _url, _raw, args)
            except (MCPError, mcp_oauth.OAuthError) as exc:
                return f"error: {exc}"

        out.append(ConnectorTool(spec=spec, handler=handler))
    return out
