"""Linear connector runtime: issues and comments over the GraphQL API.

Stdlib-only (urllib). Auth is a personal API key (Settings > Security & access
> Personal API keys in Linear), sent bare in the Authorization header. Tools
resolve human references — team key/name, issue identifier like TEST-123,
workflow state name — into ids so the model never handles UUIDs.
"""

from __future__ import annotations

import http.client
import json
import mimetypes
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from providers.base import ToolSpec

from .base import ConnectorContext, ConnectorTool

API_URL = "https://api.linear.app/graphql"
#: conventional secret name so headless setups can `harness secret set` it
FALLBACK_SECRET = "LINEAR_API_KEY"
_MAX_RESULTS = 50
_PRIORITIES = {"none": 0, "urgent": 1, "high": 2, "medium": 3, "normal": 3, "low": 4}

_ISSUE_FIELDS = "identifier title url priority state { name } assignee { displayName } updatedAt"
_MAX_ATTACH = 8
_MAX_ATTACH_BYTES = 8_000_000
_FILE_UPLOAD = (
    "mutation($filename: String!, $contentType: String!, $size: Int!) { "
    "fileUpload(filename: $filename, contentType: $contentType, size: $size) { "
    "success uploadFile { uploadUrl assetUrl headers { key value } } } }"
)


class LinearError(RuntimeError):
    """A Linear API call failed (HTTP, network, or GraphQL errors)."""


def _graphql(api_key: str, query: str, variables: dict | None = None) -> dict:
    """POST one GraphQL request. Module-level so tests can monkeypatch it."""
    payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": api_key,
            "User-Agent": "dotobot/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise LinearError(f"Linear API HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LinearError(f"could not reach Linear: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise LinearError("Linear API returned invalid JSON") from exc
    if body.get("errors"):
        msgs = "; ".join(str(e.get("message", e)) for e in body["errors"])
        raise LinearError(f"Linear API error: {msgs}")
    return body.get("data") or {}


def _fmt_issue(issue: dict) -> str:
    state = (issue.get("state") or {}).get("name") or "?"
    who = (issue.get("assignee") or {}).get("displayName") or "unassigned"
    return (
        f"- {issue.get('identifier', '?')} · {issue.get('title', '')} · "
        f"{state} · {who} · {issue.get('url', '')}"
    )


def _issue_card(issue: dict) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "identifier": issue.get("identifier") or "",
        "title": issue.get("title") or "",
        "url": issue.get("url") or "",
    }
    status = (issue.get("state") or {}).get("name")
    if status:
        payload["status"] = status
    priority = issue.get("priorityLabel")
    if priority and priority != "No priority":
        payload["priority"] = priority
    assignee = (issue.get("assignee") or {}).get("displayName")
    if assignee:
        payload["assignee"] = assignee
    team = (issue.get("team") or {}).get("key")
    if team:
        payload["team"] = team
    return payload


def _team(api_key: str, ref: str) -> dict:
    """Resolve a team by key (TEST) or name (Example), case-insensitive."""
    data = _graphql(api_key, "query { teams(first: 50) { nodes { id key name } } }")
    teams = (data.get("teams") or {}).get("nodes") or []
    ref_low = ref.strip().lower()
    for t in teams:
        if str(t.get("key", "")).lower() == ref_low or str(t.get("name", "")).lower() == ref_low:
            return t
    known = ", ".join(str(t.get("key", "?")) for t in teams) or "(none visible)"
    raise LinearError(f"no Linear team matching {ref!r}; teams: {known}")


def _issue(api_key: str, identifier: str) -> dict:
    """Resolve an issue by identifier (TEST-123) to its id + team id."""
    data = _graphql(
        api_key,
        "query($id: String!) { issue(id: $id) { id identifier title url team { id } } }",
        {"id": identifier.strip()},
    )
    issue = data.get("issue")
    if not issue:
        raise LinearError(f"no Linear issue {identifier!r}")
    return issue


def _state_id(api_key: str, team_id: str, state_name: str) -> str:
    data = _graphql(
        api_key,
        "query($id: String!) { team(id: $id) { states(first: 50) { nodes { id name } } } }",
        {"id": team_id},
    )
    states = ((data.get("team") or {}).get("states") or {}).get("nodes") or []
    want = state_name.strip().lower()
    for s in states:
        if str(s.get("name", "")).lower() == want:
            return str(s["id"])
    known = ", ".join(str(s.get("name", "?")) for s in states) or "(none)"
    raise LinearError(f"no workflow state {state_name!r} on that team; states: {known}")


def _projects(api_key: str) -> list[dict]:
    data = _graphql(
        api_key,
        "query { projects(first: 100) { nodes { id name slug url } } }",
    )
    return (data.get("projects") or {}).get("nodes") or []


def _project(api_key: str, ref: str) -> dict:
    """Resolve a project by name, slug, or id (case-insensitive)."""
    projects = _projects(api_key)
    ref_low = ref.strip().lower()
    for p in projects:
        if str(p.get("id", "")).lower() == ref_low:
            return p
        if str(p.get("name", "")).lower() == ref_low:
            return p
        if str(p.get("slug", "")).lower() == ref_low:
            return p
    known = ", ".join(str(p.get("name", "?")) for p in projects) or "(none visible)"
    raise LinearError(f"no Linear project matching {ref!r}; projects: {known}")


def _parse_paths(args: dict[str, Any]) -> list[str]:
    raw = args.get("paths") if args.get("paths") not in (None, "") else args.get("attachments")
    if raw is None or raw == "":
        raw = args.get("path")
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = [text]
            raw = parsed
        else:
            raw = [p.strip() for p in text.split(",") if p.strip()]
    if not isinstance(raw, list):
        raw = [raw]
    return [str(p).strip() for p in raw if str(p).strip()]


def _safe_file(ctx: ConnectorContext, path_str: str) -> Path:
    """Only files under workspace/ (uploads live there)."""
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        path = ctx.paths.workspace / path
    try:
        path = path.resolve()
        path.relative_to(ctx.paths.workspace.resolve())
    except (OSError, ValueError) as exc:
        raise LinearError(f"file must be under the workspace: {path_str}") from exc
    if not path.is_file():
        raise LinearError(f"no file at {path}")
    size = path.stat().st_size
    if size <= 0 or size > _MAX_ATTACH_BYTES:
        raise LinearError(f"{path.name} is {size} bytes (max {_MAX_ATTACH_BYTES})")
    return path


def _put_bytes(url: str, data: bytes, headers: dict[str, str]) -> None:
    """PUT file bytes, keeping signed header names as Linear returned them."""
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise LinearError("Linear returned a bad upload URL")
    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    conn = conn_cls(parsed.hostname, parsed.port, timeout=60)
    try:
        conn.putrequest("PUT", path, skip_host=True, skip_accept_encoding=True)
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.endheaders()
        conn.send(data)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status >= 400:
            raise LinearError(
                f"Linear file upload HTTP {resp.status}: {body[:200]!r}"
            )
    finally:
        conn.close()


def _upload_file(api_key: str, path: Path) -> str:
    """Upload one local file to Linear storage; return the asset URL."""
    data = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    payload = _graphql(
        api_key,
        _FILE_UPLOAD,
        {"filename": path.name, "contentType": mime, "size": len(data)},
    )
    upload = (payload.get("fileUpload") or {}).get("uploadFile") or {}
    upload_url = str(upload.get("uploadUrl") or "")
    asset_url = str(upload.get("assetUrl") or "")
    if not upload_url or not asset_url:
        raise LinearError(f"Linear did not return an upload URL for {path.name}")
    headers = {
        str(h.get("key")): str(h.get("value"))
        for h in (upload.get("headers") or [])
        if h.get("key")
    }
    headers.setdefault("Content-Type", mime)
    _put_bytes(upload_url, data, headers)
    return asset_url


def _upload_attachments(ctx: ConnectorContext, api_key: str, args: dict[str, Any]) -> list[tuple[str, str]]:
    paths = _parse_paths(args)
    if not paths:
        return []
    if len(paths) > _MAX_ATTACH:
        raise LinearError(f"at most {_MAX_ATTACH} files per call")
    out: list[tuple[str, str]] = []
    for raw in paths:
        path = _safe_file(ctx, raw)
        out.append((path.name, _upload_file(api_key, path)))
    return out


def _image_markdown(files: list[tuple[str, str]]) -> str:
    return "\n".join(f"![{name}]({url})" for name, url in files)


def _priority(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and 0 <= int(value) <= 4:
        return int(value)
    mapped = _PRIORITIES.get(str(value).strip().lower())
    if mapped is None:
        raise LinearError(f"priority must be 0-4 or urgent/high/medium/low, not {value!r}")
    return mapped


def _limit(args: dict[str, Any]) -> int:
    try:
        n = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        n = 10
    return max(1, min(n, _MAX_RESULTS))


def _key_or_error(ctx: ConnectorContext) -> str | None:
    return ctx.secret(fallback=FALLBACK_SECRET)


def _list_teams(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    key = _key_or_error(ctx)
    if not key:
        return ctx.missing_secret(FALLBACK_SECRET)
    try:
        data = _graphql(key, "query { teams(first: 50) { nodes { key name } } }")
    except LinearError as exc:
        return f"error: {exc}"
    teams = (data.get("teams") or {}).get("nodes") or []
    if not teams:
        return "(no teams visible to this API key)"
    return "\n".join(f"- {t.get('key', '?')} · {t.get('name', '')}" for t in teams)


def _list_projects(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    key = _key_or_error(ctx)
    if not key:
        return ctx.missing_secret(FALLBACK_SECRET)
    try:
        projects = _projects(key)
    except LinearError as exc:
        return f"error: {exc}"
    needle = str(args.get("query") or "").strip().lower()
    if needle:
        projects = [
            p
            for p in projects
            if needle in str(p.get("name") or "").lower()
            or needle in str(p.get("slug") or "").lower()
        ]
    if not projects:
        return "(no projects match)"
    return "\n".join(
        f"- {p.get('name', '?')} · {p.get('slug') or p.get('id', '')} · {p.get('url', '')}"
        for p in projects
    )


_LIST_QUERY = (
    "query($filter: IssueFilter, $first: Int!) { "
    "issues(filter: $filter, first: $first, orderBy: updatedAt) "
    f"{{ nodes {{ {_ISSUE_FIELDS} }} }} }}"
)


def _list_issues(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    key = _key_or_error(ctx)
    if not key:
        return ctx.missing_secret(FALLBACK_SECRET)
    filt: dict[str, Any] = {}
    try:
        team = str(args.get("team") or "").strip()
        if team:
            filt["team"] = {"key": {"eq": str(_team(key, team)["key"])}}
        assignee = str(args.get("assignee") or "").strip()
        if assignee:
            filt["assignee"] = {"displayName": {"containsIgnoreCase": assignee}}
        state = str(args.get("state") or "").strip()
        if state:
            filt["state"] = {"name": {"containsIgnoreCase": state}}
        data = _graphql(key, _LIST_QUERY, {"filter": filt or None, "first": _limit(args)})
    except LinearError as exc:
        return f"error: {exc}"
    issues = (data.get("issues") or {}).get("nodes") or []
    if not issues:
        return "(no issues match)"
    return "\n".join(_fmt_issue(i) for i in issues)


def _search_issues(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    key = _key_or_error(ctx)
    if not key:
        return ctx.missing_secret(FALLBACK_SECRET)
    query = str(args.get("query") or "").strip()
    if not query:
        return "error: linear_search_issues needs 'query'"
    filt = {
        "or": [
            {"title": {"containsIgnoreCase": query}},
            {"description": {"containsIgnoreCase": query}},
        ]
    }
    try:
        data = _graphql(key, _LIST_QUERY, {"filter": filt, "first": _limit(args)})
    except LinearError as exc:
        return f"error: {exc}"
    issues = (data.get("issues") or {}).get("nodes") or []
    if not issues:
        return f"(no issues match {query!r})"
    return "\n".join(_fmt_issue(i) for i in issues)


_CARD_FIELDS = (
    "identifier title url priorityLabel state { name } "
    "assignee { displayName } team { key }"
)


def _get_issue(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    key = _key_or_error(ctx)
    if not key:
        return ctx.missing_secret(FALLBACK_SECRET)
    identifier = str(args.get("id") or "").strip()
    if not identifier:
        return "error: linear_get_issue needs 'id' (like TEST-123)"
    try:
        data = _graphql(
            key,
            f"query($id: String!) {{ issue(id: $id) {{ {_CARD_FIELDS} }} }}",
            {"id": identifier},
        )
    except LinearError as exc:
        return f"error: {exc}"
    issue = data.get("issue")
    if not issue:
        return f"error: no Linear issue {identifier!r}"
    ctx.card("linear_issue", _issue_card(issue))
    return f"{_fmt_issue(issue).lstrip('- ')}\n(a rich ticket card is already shown to the user)"


def _create_issue(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    key = _key_or_error(ctx)
    if not key:
        return ctx.missing_secret(FALLBACK_SECRET)
    title = str(args.get("title") or "").strip()
    team = str(args.get("team") or "").strip() or ctx.config("team")
    if not title or not team:
        return "error: linear_create_issue needs 'title' and 'team' (key or name)"
    files: list[tuple[str, str]] = []
    try:
        input_: dict[str, Any] = {"teamId": str(_team(key, team)["id"]), "title": title}
        description = str(args.get("description") or "").strip()
        project = str(args.get("project") or "").strip()
        if project:
            input_["projectId"] = str(_project(key, project)["id"])
        files = _upload_attachments(ctx, key, args)
        if files:
            md = _image_markdown(files)
            description = f"{description}\n\n{md}".strip() if description else md
        if description:
            input_["description"] = description
        priority = _priority(args.get("priority"))
        if priority is not None:
            input_["priority"] = priority
        data = _graphql(
            key,
            "mutation($input: IssueCreateInput!) { issueCreate(input: $input) "
            "{ success issue { identifier title url } } }",
            {"input": input_},
        )
    except LinearError as exc:
        return f"error: {exc}"
    issue = (data.get("issueCreate") or {}).get("issue") or {}
    if not issue:
        return "error: Linear did not return the created issue"
    extra = ""
    if files:
        extra = f" · {len(files)} file(s) attached"
    return (
        f"ok: created {issue.get('identifier', '?')} · {issue.get('title', '')} · "
        f"{issue.get('url', '')}{extra}"
    )


def _update_issue(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    key = _key_or_error(ctx)
    if not key:
        return ctx.missing_secret(FALLBACK_SECRET)
    identifier = str(args.get("id") or "").strip()
    if not identifier:
        return "error: linear_update_issue needs 'id' (like TEST-123)"
    input_: dict[str, Any] = {}
    try:
        issue = _issue(key, identifier)
        title = str(args.get("title") or "").strip()
        if title:
            input_["title"] = title
        description = str(args.get("description") or "").strip()
        if description:
            input_["description"] = description
        state = str(args.get("state") or "").strip()
        if state:
            input_["stateId"] = _state_id(key, str((issue.get("team") or {}).get("id", "")), state)
        priority = _priority(args.get("priority"))
        if priority is not None:
            input_["priority"] = priority
        project = str(args.get("project") or "").strip()
        if project:
            input_["projectId"] = str(_project(key, project)["id"])
        if not input_:
            return "error: nothing to update (pass title, description, state, priority, or project)"
        data = _graphql(
            key,
            "mutation($id: String!, $input: IssueUpdateInput!) { issueUpdate(id: $id, "
            "input: $input) { success issue { identifier url state { name } } } }",
            {"id": str(issue["id"]), "input": input_},
        )
    except LinearError as exc:
        return f"error: {exc}"
    updated = (data.get("issueUpdate") or {}).get("issue") or {}
    state_name = (updated.get("state") or {}).get("name") or "?"
    return f"ok: updated {updated.get('identifier', identifier)} (state: {state_name}) · {updated.get('url', '')}"


def _comment(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    key = _key_or_error(ctx)
    if not key:
        return ctx.missing_secret(FALLBACK_SECRET)
    identifier = str(args.get("issue") or "").strip()
    body = str(args.get("body") or "").strip()
    if not identifier or not body:
        return "error: linear_comment needs 'issue' (like TEST-123) and 'body'"
    try:
        issue = _issue(key, identifier)
        data = _graphql(
            key,
            "mutation($input: CommentCreateInput!) { commentCreate(input: $input) "
            "{ success comment { url } } }",
            {"input": {"issueId": str(issue["id"]), "body": body}},
        )
    except LinearError as exc:
        return f"error: {exc}"
    comment = (data.get("commentCreate") or {}).get("comment") or {}
    return f"ok: commented on {issue.get('identifier', identifier)} · {comment.get('url', '')}"


def _attach_files(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    key = _key_or_error(ctx)
    if not key:
        return ctx.missing_secret(FALLBACK_SECRET)
    identifier = str(args.get("issue") or args.get("id") or "").strip()
    if not identifier:
        return "error: linear_attach_files needs 'issue' (like TEST-123) and file paths"
    if not _parse_paths(args):
        return "error: linear_attach_files needs 'paths' (workspace files from the chat)"
    try:
        # Resolve files first so a bad path never hits Linear.
        for raw in _parse_paths(args):
            _safe_file(ctx, raw)
        issue = _issue(key, identifier)
        files = _upload_attachments(ctx, key, args)
        md = _image_markdown(files)
        data = _graphql(
            key,
            "query($id: String!) { issue(id: $id) { description } }",
            {"id": str(issue["id"])},
        )
        existing = str(((data.get("issue") or {}).get("description")) or "").rstrip()
        body = f"{existing}\n\n{md}".strip() if existing else md
        _graphql(
            key,
            "mutation($id: String!, $input: IssueUpdateInput!) { issueUpdate(id: $id, "
            "input: $input) { success issue { identifier url } } }",
            {"id": str(issue["id"]), "input": {"description": body}},
        )
    except LinearError as exc:
        return f"error: {exc}"
    names = ", ".join(name for name, _url in files)
    return (
        f"ok: attached {len(files)} file(s) ({names}) to "
        f"{issue.get('identifier', identifier)} · {issue.get('url', '')}"
    )


def tools() -> list[ConnectorTool]:
    return [
        ConnectorTool(
            ToolSpec(
                name="linear_list_teams",
                description="List the Linear teams this workspace's API key can see.",
                parameters={"type": "object", "properties": {}},
            ),
            _list_teams,
        ),
        ConnectorTool(
            ToolSpec(
                name="linear_list_projects",
                description=(
                    "List Linear projects this API key can see. Pass query to "
                    "filter by name (e.g. Dotobot). Use the project name "
                    "with linear_create_issue / linear_update_issue."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "optional name filter"},
                    },
                },
            ),
            _list_projects,
        ),
        ConnectorTool(
            ToolSpec(
                name="linear_list_issues",
                description=(
                    "List recent Linear issues, optionally filtered by team "
                    "(key or name), assignee (person's name), or state (e.g. "
                    "In Progress). Sorted by last update."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "team": {"type": "string", "description": "team key or name"},
                        "assignee": {"type": "string", "description": "assignee name"},
                        "state": {"type": "string", "description": "workflow state name"},
                        "limit": {"type": "integer", "description": "max results (default 10)"},
                    },
                },
            ),
            _list_issues,
        ),
        ConnectorTool(
            ToolSpec(
                name="linear_search_issues",
                description="Search Linear issues by words in the title or description.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "description": "max results (default 10)"},
                    },
                    "required": ["query"],
                },
            ),
            _search_issues,
        ),
        ConnectorTool(
            ToolSpec(
                name="linear_get_issue",
                description=(
                    "Fetch one Linear issue by identifier (TEST-123) and show it "
                    "to the user as a rich ticket card (status, priority, "
                    "assignee, team). The card is already visible — don't "
                    "repeat its details, but do include the issue URL in your "
                    "reply."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "issue identifier like TEST-123"},
                    },
                    "required": ["id"],
                },
            ),
            _get_issue,
        ),
        ConnectorTool(
            ToolSpec(
                name="linear_create_issue",
                description=(
                    "Create a Linear issue. team is the team key (TEST) or name; "
                    "project is the project name (Dotobot); priority is "
                    "0-4 or urgent/high/medium/low. Pass attachments as workspace "
                    "paths from the chat's [Attached files] list — do not re-host "
                    "images on public paste sites."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "team": {"type": "string", "description": "team key or name"},
                        "project": {
                            "type": "string",
                            "description": "project name, slug, or id",
                        },
                        "description": {"type": "string", "description": "markdown body"},
                        "priority": {
                            "type": "string",
                            "description": "0-4 or urgent/high/medium/low",
                        },
                        "attachments": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "workspace file paths from the chat",
                        },
                    },
                    "required": ["title", "team"],
                },
            ),
            _create_issue,
        ),
        ConnectorTool(
            ToolSpec(
                name="linear_update_issue",
                description=(
                    "Update a Linear issue by identifier (TEST-123): title, "
                    "description, workflow state (by name), priority, or project."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "issue identifier like TEST-123"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "state": {"type": "string", "description": "workflow state name"},
                        "priority": {
                            "type": "string",
                            "description": "0-4 or urgent/high/medium/low",
                        },
                        "project": {
                            "type": "string",
                            "description": "project name, slug, or id",
                        },
                    },
                    "required": ["id"],
                },
            ),
            _update_issue,
        ),
        ConnectorTool(
            ToolSpec(
                name="linear_comment",
                description="Add a comment to a Linear issue by identifier (TEST-123).",
                parameters={
                    "type": "object",
                    "properties": {
                        "issue": {"type": "string", "description": "issue identifier like TEST-123"},
                        "body": {"type": "string", "description": "markdown comment body"},
                    },
                    "required": ["issue", "body"],
                },
            ),
            _comment,
        ),
        ConnectorTool(
            ToolSpec(
                name="linear_attach_files",
                description=(
                    "Upload chat/workspace files onto an existing Linear issue "
                    "(embedded as images in the description). Pass the path= "
                    "values from [Attached files]. Do not re-host on paste sites."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "issue": {
                            "type": "string",
                            "description": "issue identifier like TEST-123",
                        },
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "workspace file paths from the chat",
                        },
                    },
                    "required": ["issue", "paths"],
                },
            ),
            _attach_files,
        ),
    ]
