"""API-key HTTP runtime for catalog types with no MCP and no static tools.

A configured stub (secret stored in the UI) binds `{type}_get` (read) and
`{type}_request` (write). Calls are HTTPS-only and the resolved URL's host
must match the catalog `api_base` / `api_base_template` (or a small known
list for featured stubs). A missing key asks via request_secret. A missing
base returns the docs URL — the bot must not invent a host.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from harness.redaction import resolve_outbound
from providers.base import ToolSpec

from .base import ConnectorContext, ConnectorTool

_MAX_RESULT = 12_000
_TIMEOUT = 30
_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")
_MAILCHIMP_DC = re.compile(r"us\d+$")

#: Featured stubs whose REST host is known without a catalog `api_base`.
#: value is (base_url or template, auth_style).
KNOWN_BASES: dict[str, tuple[str, str]] = {
    "twilio": ("https://api.twilio.com", "basic"),
    "whatsapp": ("https://graph.facebook.com", "bearer"),
    "telegram": ("https://api.telegram.org", "telegram"),
}

_STYLES = ("bearer", "basic", "telegram", "header", "basic_key", "4dem", "query", "header_pair")


def tool_names(type_: str) -> list[str]:
    t = str(type_ or "").strip()
    if not t:
        return []
    return [f"{t}_get", f"{t}_request"]


def tools(type_: str) -> list[ConnectorTool]:
    t = str(type_ or "").strip() or "connector"
    return [
        ConnectorTool(
            ToolSpec(
                name=f"{t}_get",
                description=(
                    f"GET a path on the {t} REST API. Path is relative to the "
                    "connector's API host. Use this to list or fetch records."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "path under the API host, e.g. /accounts",
                        },
                        "query": {
                            "type": "object",
                            "description": "optional query values; arrays repeat a parameter",
                        },
                    },
                    "required": ["path"],
                },
            ),
            _get,
        ),
        ConnectorTool(
            ToolSpec(
                name=f"{t}_request",
                description=(
                    f"Call the {t} REST API (POST/PATCH/PUT/DELETE). Path is "
                    "relative to the connector's API host."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "method": {
                            "type": "string",
                            "description": "POST, PATCH, PUT, or DELETE",
                        },
                        "path": {"type": "string"},
                        "body": {
                            "type": "object",
                            "description": "Body object, encoded as required by the provider",
                        },
                    },
                    "required": ["method", "path"],
                },
            ),
            _request,
        ),
    ]


def resolve_base(ctx: ConnectorContext) -> str | None:
    """The HTTPS API origin this connector may call, or None."""
    from harness.connectors import _CATALOG_TYPES

    type_ = str(ctx.record.get("type") or "")
    cat = _CATALOG_TYPES.get(type_) or {}
    if type_ == "airship":
        config = ctx.record.get("config") or {}
        region = config.get("region", "us")
        mode = config.get("auth_mode", "bearer")
        if region not in ("us", "eu") or mode not in ("bearer", "basic", "oauth"):
            return None
        hosts = (
            ("api.asnapius.com", "api.asnapieu.com")
            if mode == "oauth"
            else ("go.urbanairship.com", "go.airship.eu")
        )
        return "https://" + hosts[region == "eu"]
    url = str(cat.get("api_base") or "").strip()
    if not url:
        template = str(cat.get("api_base_template") or "").strip()
        if template:
            url = _fill(template, cat.get("fields") or [], ctx.record.get("config") or {})
    if not url and type_ == "mailchimp":
        url = _mailchimp_base(ctx.secret())
    if not url:
        known = KNOWN_BASES.get(type_)
        if known:
            url = known[0]
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return url.rstrip("/")


def auth_style(ctx: ConnectorContext) -> str:
    from harness.connectors import _CATALOG_TYPES

    type_ = str(ctx.record.get("type") or "")
    cat = _CATALOG_TYPES.get(type_) or {}
    if type_ == "airship" and (ctx.record.get("config") or {}).get("auth_mode") == "basic":
        return "basic"
    style = str(cat.get("auth_style") or "").strip().lower()
    if style in _STYLES:
        return style
    known = KNOWN_BASES.get(type_)
    if known:
        return known[1]
    return "bearer"


def _fill(template: str, fields: list, config: dict) -> str:
    values: dict[str, str] = {}
    for field in fields:
        if "{" + str(field) + "}" not in template:
            continue
        value = str(config.get(field, "")).strip()
        if not _HOST.match(value):
            return ""
        values[str(field)] = value
    try:
        return str(template).format(**values)
    except (KeyError, IndexError):
        return ""


def _mailchimp_base(secret: str | None) -> str:
    if not secret or "-" not in secret:
        return ""
    dc = secret.rsplit("-", 1)[-1].strip().lower()
    if not _MAILCHIMP_DC.fullmatch(dc):
        return ""
    return f"https://{dc}.api.mailchimp.com/3.0"


def _docs(ctx: ConnectorContext) -> str:
    from harness.connector_docs import DOCS

    type_ = str(ctx.record.get("type") or "")
    extra = DOCS.get(type_) or {}
    return str(extra.get("docs") or "").strip()


def _no_base(ctx: ConnectorContext) -> str:
    docs = _docs(ctx)
    hint = f" Docs: {docs}" if docs else ""
    return (
        "error: this connector has no REST host configured, so HTTP calls "
        "are blocked. The API key is stored. Use the computer if the service "
        "has no public HTTPS API." + hint
    )


def _join(base: str, path: str) -> str | None:
    raw = str(path or "").strip()
    if not raw or raw.lower().startswith("http") or "\\" in raw or ".." in raw:
        return None
    joined = urllib.parse.urljoin(base.rstrip("/") + "/", raw.lstrip("/"))
    want = urllib.parse.urlparse(base)
    got = urllib.parse.urlparse(joined)
    if got.scheme != "https" or got.netloc != want.netloc:
        return None
    return joined


def _headers(ctx: ConnectorContext, secret: str) -> dict[str, str]:
    from harness.connectors import _CATALOG_TYPES

    style = auth_style(ctx)
    cat = _CATALOG_TYPES[str(ctx.record["type"])]
    hdrs = {
        "Accept": str(cat.get("accept", "application/json")),
        "User-Agent": "dotobot/0.2.9",
    }
    if style == "header_pair":
        try:
            pair = json.loads(secret)
        except (ValueError, TypeError):
            pair = None
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or any(
                not isinstance(v, str) or not v or any(ord(c) < 32 or ord(c) > 126 for c in v)
                for v in pair
            )
        ):
            raise ValueError(
                "store the credential as a JSON array of two nonempty printable ASCII strings"
            )
        hdrs.update(zip(cat["auth_headers"], pair, strict=True))
        return hdrs
    if style == "query":
        return hdrs
    if style == "header":
        from harness.connectors import _CATALOG_TYPES

        # Header names and prefixes are trusted catalogue metadata, never tool
        # arguments or user configuration. The stored credential remains sealed
        # until ConnectorContext.secret() resolves it at the request boundary.
        cat = _CATALOG_TYPES[str(ctx.record["type"])]
        value = json.dumps(secret) if cat.get("auth_quote") else secret
        hdrs[cat["auth_header"]] = str(cat.get("auth_prefix", "")) + value
        return hdrs
    if style == "telegram":
        return hdrs
    if style in {"basic", "basic_key"}:
        import base64

        from harness.connectors import _CATALOG_TYPES

        cat = _CATALOG_TYPES[str(ctx.record["type"])]
        credentials = (
            secret + ":" + str(cat.get("basic_password", "")) if style == "basic_key" else secret
        )
        token = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        hdrs["Authorization"] = f"Basic {token}"
        return hdrs
    hdrs["Authorization"] = f"Bearer {secret}"
    return hdrs


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never replay connector credentials or bodies at a redirected destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(req, *, timeout):
    return urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout)


def _fourdem_token(base: str, key: str) -> str:
    """Exchange the stored API key for a short-lived token for this request.

    Do not persist tokens or cache them across connector records. Reauthenticating
    avoids expired tokens and automatically respects API-key rotation.
    """
    request = urllib.request.Request(
        base + "/authenticate",
        data=json.dumps({"APIKey": key}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with _open(request, timeout=_TIMEOUT) as response:
            payload = json.loads(response.read(65_537))
    except urllib.error.HTTPError as exc:
        exc.close()
        raise ValueError(f"4Dem authentication failed (HTTP {exc.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError("could not reach 4Dem authentication") from None
    except (ValueError, UnicodeError):
        raise ValueError("4Dem authentication returned invalid JSON") from None
    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("4Dem authentication returned no usable token")
    return token


def _http(
    ctx: ConnectorContext,
    method: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> str:
    key = ctx.secret()
    if not key:
        return ctx.missing_secret()
    base = resolve_base(ctx)
    if not base:
        return _no_base(ctx)
    style = auth_style(ctx)
    if style == "telegram":
        method_name = path.strip("/")
        if not method_name or "/" in method_name or not method_name.replace("_", "").isalnum():
            return "error: telegram path must be a method name like getMe or sendMessage"
        url = f"{base}/bot{key}/{method_name}"
    else:
        url = _join(base, path)
        if not url:
            return "error: path must stay on the connector's API host"
    if query:
        qs = urllib.parse.urlencode(
            {str(k): v for k, v in query.items() if v is not None}, doseq=True
        )
        url += ("&" if "?" in url else "?") + qs
    if style == "query":
        from harness.connectors import _CATALOG_TYPES

        auth_config = _CATALOG_TYPES[str(ctx.record["type"])]
        parameter = auth_config["auth_query"]
        supplied = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, keep_blank_values=True)
        if any(k == parameter or k.startswith(parameter + "[") for k in supplied) or (
            body is not None and parameter in body
        ):
            return "error: authentication comes from the connector secret store"
        if (auth_config.get("auth_in_form") or auth_config.get("auth_in_json")) and method != "GET":
            body = {**(body or {}), parameter: key}
        else:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode({parameter: key})
    # Final boundary: a sentinel the model echoed into the path,
    # query, or body is substituted with its plaintext here; one that cannot
    # be unsealed raises before any I/O — the request is refused, never
    # forwarded containing the sentinel. (`key` went through ctx.secret().)
    # Only THIS connector's own credential may be spliced in: the registry
    # holds every secret in the process and the model sees every sentinel,
    # so an injected page could otherwise ask a Telegram/webhook connector
    # to "send this hs-v1 token" and have the provider API key delivered in
    # plaintext to a chat the attacker reads.
    where = f"a {ctx.record.get('type') or 'connector'} request"
    own = key

    def _own_secret(plain: str) -> bool:
        return plain == own

    # url_escaped: the URL is already encoded, so the substituted plaintext
    # is percent-encoded — a secret containing `&`, `=`, `+`, or a space
    # cannot split the query or leak into a neighboring parameter.
    url = resolve_outbound(url, where=where, url_escaped=True, allow=_own_secret)
    data = (
        resolve_outbound(
            json.dumps(body), where=where, json_escaped=True, allow=_own_secret
        ).encode("utf-8")
        if body is not None
        else None
    )
    if style == "4dem":
        try:
            auth_key = _fourdem_token(base, key)
        except ValueError as exc:
            return f"error: {exc}"
    else:
        auth_key = key
    try:
        hdrs = _headers(ctx, auth_key)
    except ValueError as exc:
        return f"error: {exc}"
    if ctx.record.get("type") == "discourse":
        username = str((ctx.record.get("config") or {}).get("api_username") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
            return "error: configure Discourse api_username using letters, numbers, dots, hyphens or underscores"
        hdrs["Api-Username"] = username
    if ctx.record.get("type") == "hypeauditor":
        client_id = str((ctx.record.get("config") or {}).get("client_id") or "")
        if not re.fullmatch(r"[0-9]+", client_id):
            return "error: configure HypeAuditor client_id as a numeric account ID"
        hdrs["X-Auth-Id"] = client_id
    from harness.connectors import _CATALOG_TYPES

    cat = _CATALOG_TYPES.get(str(ctx.record.get("type") or "")) or {}
    if data is not None or cat.get("content_type"):
        hdrs["Content-Type"] = str(cat.get("content_type") or "application/json")
        if (
            data is not None
            and (
                cat.get("body_encoding") == "form"
                or any(
                    re.fullmatch(pattern, urllib.parse.urlparse(url).path)
                    for pattern in cat.get("form_body_paths", [])
                )
            )
            and not any(
                re.fullmatch(pattern, urllib.parse.urlparse(url).path)
                for pattern in cat.get("json_body_paths", [])
            )
        ):
            # Resolve redaction sentinels before encoding so substituted secrets
            # cannot introduce form fields through ampersands or equals signs.
            data = urllib.parse.urlencode(_form_fields(json.loads(data))).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"

    if data is not None and cat.get("body_encoding") == "xml":
        payload = json.loads(data)
        if set(payload) != {"xml"} or not isinstance(payload["xml"], str):
            return 'error: XML body must be an object containing only an "xml" string'
        data = payload["xml"].encode("utf-8")
        hdrs["Content-Type"] = "application/xml; charset=utf-8"

    if data is not None and cat.get("body_encoding") == "multipart":
        import uuid

        fields = json.loads(data)
        if any(
            not re.fullmatch(r"[A-Za-z0-9_]+", name) or isinstance(value, (dict, list))
            for name, value in fields.items()
        ):
            return "error: multipart body needs simple field names and scalar values; file uploads are unsupported"
        boundary = "dotobot-" + uuid.uuid4().hex
        parts = []
        for name, value in fields.items():
            value = (
                ""
                if value is None
                else str(value).lower()
                if isinstance(value, bool)
                else str(value)
            )
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            )
        data = ("".join(parts) + f"--{boundary}--\r\n").encode("utf-8")
        hdrs["Content-Type"] = "multipart/form-data; boundary=" + boundary

    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with _open(req, timeout=_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        return f"error: HTTP {exc.code}: {detail}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return (
            "error: could not reach API"
            if style == "query"
            else f"error: could not reach API: {exc}"
        )
    if len(raw) > _MAX_RESULT:
        raw = raw[:_MAX_RESULT] + "\n… (truncated)"
    return f"HTTP {status}\n{raw}" if raw else f"HTTP {status}"


def _form_fields(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    """Encode nested provider fields using PHP-style bracket notation."""
    if isinstance(value, (dict, list)):
        items = value.items() if isinstance(value, dict) else enumerate(value)
        result = []
        for key, child in items:
            name = f"{prefix}[{key}]" if prefix else str(key)
            result.extend(_form_fields(child, name))
        return result
    if isinstance(value, bool):
        value = "true" if value else "false"
    return [(prefix, "" if value is None else str(value))]


def _get(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    path = str(args.get("path") or "").strip()
    if not path:
        return "error: needs 'path'"
    query = args.get("query")
    if query is not None and not isinstance(query, dict):
        return "error: query must be an object"
    return _http(ctx, "GET", path, query=query)


def _request(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    method = str(args.get("method") or "").strip().upper()
    if method not in {"POST", "PATCH", "PUT", "DELETE"}:
        return "error: method must be POST, PATCH, PUT, or DELETE"
    path = str(args.get("path") or "").strip()
    if not path:
        return "error: needs 'path'"
    body = args.get("body")
    if body is not None and not isinstance(body, dict):
        return "error: body must be a JSON object"
    return _http(ctx, method, path, body=body)
