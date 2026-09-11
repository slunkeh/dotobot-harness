"""GitHub connector runtime: repos, issues, and pull requests over the REST API.

Stdlib-only (urllib). Auth is a personal access token (classic `ghp_…` or
fine-grained `github_pat_…`), the same PAT that works from a Pi or laptop.
The optional `org` config field is the default owner when a tool is passed
a bare repo name.
"""

from __future__ import annotations

import base64
import binascii
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from providers.base import ToolSpec

from .base import ConnectorContext, ConnectorTool

API = "https://api.github.com"
FALLBACK_SECRET = "GITHUB_TOKEN"
_MAX_RESULTS = 50


class GitHubError(RuntimeError):
    """A GitHub API call failed (HTTP, network, or JSON)."""


def _request(
    token: str,
    method: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    """One REST call. Module-level so tests can monkeypatch it."""
    url = API + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "dotobot/0.1",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise GitHubError(f"GitHub API HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GitHubError(f"could not reach GitHub: {exc}") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GitHubError("GitHub API returned invalid JSON") from exc


def _limit(args: dict[str, Any]) -> int:
    try:
        n = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        n = 10
    return max(1, min(n, _MAX_RESULTS))


def _key(ctx: ConnectorContext) -> str | None:
    return ctx.secret(fallback=FALLBACK_SECRET)


def _repo(ctx: ConnectorContext, args: dict[str, Any]) -> tuple[str, str] | str:
    raw = str(args.get("repo") or "").strip()
    org = ctx.config("org")
    if not raw:
        return "error: needs 'repo' (owner/name, or a name if org is configured)"
    if "/" in raw:
        owner, name = raw.split("/", 1)
        owner, name = owner.strip(), name.strip()
        if owner and name:
            return owner, name
        return "error: repo must look like owner/name"
    if not org:
        return "error: pass repo as owner/name, or set the connector org field"
    return org, raw


def _fmt_repo(row: dict) -> str:
    vis = "private" if row.get("private") else "public"
    return f"- {row.get('full_name', '?')} · {vis} · {row.get('html_url', '')}"


def _fmt_issue(row: dict) -> str:
    kind = "PR" if row.get("pull_request") else "issue"
    state = row.get("state") or "?"
    title = row.get("title") or ""
    n = row.get("number", "?")
    repo = ((row.get("repository") or {}).get("full_name")) or ""
    prefix = f"{repo}#" if repo else "#"
    return f"- {kind} {prefix}{n} · {title} · {state} · {row.get('html_url', '')}"


def _fmt_pull(row: dict) -> str:
    n = row.get("number", "?")
    state = row.get("state") or "?"
    return f"- PR #{n} · {row.get('title', '')} · {state} · {row.get('html_url', '')}"


def _pull_state(row: dict) -> str:
    if row.get("merged_at"):
        return "merged"
    if row.get("draft"):
        return "draft"
    return str(row.get("state") or "open")


def _pull_card(repo: str, row: dict, checks: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": row.get("title") or "",
        "number": row.get("number"),
        "state": _pull_state(row),
        "repo": repo,
        "author": (row.get("user") or {}).get("login") or "",
        "url": row.get("html_url") or "",
    }
    base = (row.get("base") or {}).get("ref")
    head = (row.get("head") or {}).get("ref")
    if base:
        payload["base"] = base
    if head:
        payload["head"] = head
    if checks:
        payload["checks"] = checks
    # Carried only when GitHub already computed it for this row (the single-PR
    # GET does; list rows usually don't, and we never fetch extra for it).
    if row.get("mergeable") is not None:
        payload["mergeable"] = bool(row["mergeable"])
    return payload


def _pull_card_id(repo: str, number: Any) -> str:
    """Stable card id for one PR, shared by get/list/search/merge, so a
    re-emit updates the card the user is looking at instead of adding one."""
    return f"github-pull-{repo.replace('/', '-')}-{number}"


def _item_repo(row: dict) -> str:
    """owner/name for a /search/issues item (list rows carry no repository)."""
    url = str(row.get("repository_url") or "")
    if "/repos/" in url:
        tail = url.split("/repos/", 1)[1].strip("/")
        if tail.count("/") == 1:
            return tail
    parts = urllib.parse.urlsplit(str(row.get("html_url") or "")).path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] and parts[1]:
        return f"{parts[0]}/{parts[1]}"
    return ""


def _issue_card(repo: str, row: dict) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": row.get("title") or "",
        "number": row.get("number"),
        "state": str(row.get("state") or "open"),
        "repo": repo,
        "author": (row.get("user") or {}).get("login") or "",
        "url": row.get("html_url") or "",
    }
    labels = [
        str(label.get("name"))
        for label in (row.get("labels") or [])
        if isinstance(label, dict) and label.get("name")
    ]
    if labels:
        payload["labels"] = labels[:6]
    assignee = (row.get("assignee") or {}).get("login")
    if assignee:
        payload["assignee"] = assignee
    return payload


def _checks_summary(key: str, owner: str, name: str, sha: str) -> str | None:
    """Best-effort CI rollup for a PR head; None when unavailable."""
    if not sha:
        return None
    try:
        data = _request(
            key,
            "GET",
            f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}/commits/{urllib.parse.quote(sha)}/check-runs",
            query={"per_page": 50},
        )
    except GitHubError:
        return None
    runs = (data or {}).get("check_runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        return None
    if not runs:
        return "none"
    conclusions = [str(r.get("conclusion") or "") for r in runs if isinstance(r, dict)]
    if any(c in ("failure", "timed_out", "cancelled", "action_required") for c in conclusions):
        return "failing"
    if any(not c for c in conclusions):  # still queued / in progress
        return "pending"
    return "passing"


def _get_pull(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    key = _key(ctx)
    if not key:
        return ctx.missing_secret(FALLBACK_SECRET)
    repo = _repo(ctx, args)
    if isinstance(repo, str):
        return repo
    number = str(args.get("number") or "").strip()
    if not number:
        return "error: github_get_pull needs 'number'"
    owner, name = repo
    try:
        row = _request(
            key,
            "GET",
            f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}/pulls/{urllib.parse.quote(number)}",
        )
    except GitHubError as exc:
        return f"error: {exc}"
    if not isinstance(row, dict) or not row.get("number"):
        return f"error: no PR #{number} in {owner}/{name}"
    checks = _checks_summary(key, owner, name, (row.get("head") or {}).get("sha") or "")
    repo_full = f"{owner}/{name}"
    ctx.card(
        "github_pull",
        _pull_card(repo_full, row, checks),
        card_id=_pull_card_id(repo_full, row.get("number")),
    )
    return f"{_fmt_pull(row).lstrip('- ')}\n(a rich PR card is already shown to the user)"


def _get_issue(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    key = _key(ctx)
    if not key:
        return ctx.missing_secret(FALLBACK_SECRET)
    repo = _repo(ctx, args)
    if isinstance(repo, str):
        return repo
    number = str(args.get("number") or "").strip()
    if not number:
        return "error: github_get_issue needs 'number'"
    owner, name = repo
    try:
        row = _request(
            key,
            "GET",
            f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}/issues/{urllib.parse.quote(number)}",
        )
    except GitHubError as exc:
        return f"error: {exc}"
    if not isinstance(row, dict) or not row.get("number"):
        return f"error: no issue #{number} in {owner}/{name}"
    if row.get("pull_request"):
        return _get_pull(ctx, {**args, "number": number})
    ctx.card("github_issue", _issue_card(f"{owner}/{name}", row))
    return f"{_fmt_issue(row).lstrip('- ')}\n(a rich issue card is already shown to the user)"


def _list_repos(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    key = _key(ctx)
    if not key:
        return ctx.missing_secret(FALLBACK_SECRET)
    org = str(args.get("org") or "").strip() or ctx.config("org")
    try:
        if org:
            rows = _request(
                key,
                "GET",
                f"/orgs/{urllib.parse.quote(org)}/repos",
                query={"per_page": _limit(args), "sort": "updated"},
            )
        else:
            rows = _request(
                key,
                "GET",
                "/user/repos",
                query={
                    "per_page": _limit(args),
                    "sort": "updated",
                    "affiliation": "owner,collaborator,organization_member",
                },
            )
    except GitHubError as exc:
        return f"error: {exc}"
    if not isinstance(rows, list) or not rows:
        return "(no repos visible to this token)"
    return "\n".join(_fmt_repo(r) for r in rows if isinstance(r, dict))


def _get_file(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    key = _key(ctx)
    if not key:
        return ctx.missing_secret(FALLBACK_SECRET)
    repo = _repo(ctx, args)
    if isinstance(repo, str):
        return repo
    path = str(args.get("path") or "")
    if len(path) > 1024 or path.startswith("/") or ".." in path.split("/") or "\x00" in path:
        return "error: path must be relative to the repository"
    try:
        offset = int(args.get("offset") or 0)
        if offset < 0:
            raise ValueError
    except (ValueError, TypeError):
        return "error: offset must be a non-negative integer"
    owner, name = (urllib.parse.quote(p, safe="") for p in repo)
    url = f"/repos/{owner}/{name}/contents/{urllib.parse.quote(path, safe='/')}"
    ref = str(args.get("ref") or "")
    try:
        row = _request(key, "GET", url, query={"ref": ref or None})
    except GitHubError as exc:
        return f"error: {exc}. Private source access needs repository Contents: read permission."
    if isinstance(row, list):
        entries = []
        cursor, chars = offset, 0
        for r in row[offset : offset + 50]:
            line = (
                f"{r.get('type')}: {str(r.get('path') or '')[:1024]}"
                if isinstance(r, dict)
                else "(invalid directory entry)"
            )
            if entries and chars + len(line) > 8000:
                break
            entries.append(line)
            chars += len(line) + 1
            cursor += 1
        next_offset = cursor if cursor < len(row) else "end"
        lines = [f"Directory entries; next_offset={next_offset}"]
        if len(row) >= 1000:
            lines.append(
                "GitHub limits this endpoint to 1000 entries; this directory may be incomplete."
            )
        lines.extend(entries)
        return "\n".join(lines)
    if not isinstance(row, dict) or row.get("type") != "file" or row.get("encoding") != "base64":
        return "error: no inline text content; upload an archive for binary or large files"
    try:
        encoded = "".join((row.get("content") or "").split())
        content = base64.b64decode(encoded, validate=True).decode("utf-8")
        if "\x00" in content:
            raise ValueError("binary content")
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        return "error: repository file is not valid UTF-8 text; upload it as an attachment"
    page = content[offset : offset + 8000]
    next_offset = offset + len(page) if offset + len(page) < len(content) else "end"
    return f"File {path}; sha={row.get('sha', '')}; next_offset={next_offset}\n\n{page}"


def _list_issues(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    key = _key(ctx)
    if not key:
        return ctx.missing_secret(FALLBACK_SECRET)
    repo = _repo(ctx, args)
    if isinstance(repo, str):
        return repo
    owner, name = repo
    state = str(args.get("state") or "open").strip() or "open"
    try:
        rows = _request(
            key,
            "GET",
            f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}/issues",
            query={"state": state, "per_page": _limit(args)},
        )
    except GitHubError as exc:
        return f"error: {exc}"
    issues = [r for r in (rows or []) if isinstance(r, dict) and "pull_request" not in r]
    if not issues:
        return "(no issues match)"
    return "\n".join(_fmt_issue(i) for i in issues)


def _search_issues(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    key = _key(ctx)
    if not key:
        return ctx.missing_secret(FALLBACK_SECRET)
    query = str(args.get("query") or "").strip()
    if not query:
        return "error: github_search_issues needs 'query'"
    org = str(args.get("org") or "").strip() or ctx.config("org")
    q = query
    if org and "org:" not in q and "repo:" not in q:
        q = f"{q} org:{org}"
    try:
        data = _request(key, "GET", "/search/issues", query={"q": q, "per_page": _limit(args)})
    except GitHubError as exc:
        return f"error: {exc}"
    items = (data or {}).get("items") if isinstance(data, dict) else None
    if not items:
        return f"(no issues match {query!r})"
    # PR items get a rich card each; plain issues stay text lines. Search
    # rows only say whether a PR is merged (pull_request.merged_at), so lift
    # that into the shape _pull_card reads; no per-row API calls here.
    cards = 0
    for item in items:
        if not isinstance(item, dict) or "pull_request" not in item:
            continue
        repo_full = _item_repo(item)
        if not repo_full:
            continue
        row = dict(item)
        merged = (item.get("pull_request") or {}).get("merged_at")
        if merged and not row.get("merged_at"):
            row["merged_at"] = merged
        ctx.card(
            "github_pull",
            _pull_card(repo_full, row),
            card_id=_pull_card_id(repo_full, row.get("number")),
        )
        cards += 1
    lines = "\n".join(_fmt_issue(i) for i in items if isinstance(i, dict))
    if cards:
        lines += "\n(rich PR cards are already shown to the user)"
    return lines


def _create_issue(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    key = _key(ctx)
    if not key:
        return ctx.missing_secret(FALLBACK_SECRET)
    repo = _repo(ctx, args)
    if isinstance(repo, str):
        return repo
    title = str(args.get("title") or "").strip()
    if not title:
        return "error: github_create_issue needs 'title'"
    owner, name = repo
    body = str(args.get("body") or "").strip()
    payload: dict[str, Any] = {"title": title}
    if body:
        payload["body"] = body
    try:
        row = _request(
            key,
            "POST",
            f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}/issues",
            body=payload,
        )
    except GitHubError as exc:
        return f"error: {exc}"
    if not isinstance(row, dict) or not row.get("number"):
        return "error: GitHub did not return the created issue"
    ctx.card("github_issue", _issue_card(f"{owner}/{name}", row))
    return (
        f"ok: created {owner}/{name}#{row.get('number')} · {row.get('title', '')} · "
        f"{row.get('html_url', '')}"
    )


def _comment(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    key = _key(ctx)
    if not key:
        return ctx.missing_secret(FALLBACK_SECRET)
    repo = _repo(ctx, args)
    if isinstance(repo, str):
        return repo
    number = str(args.get("issue") or args.get("number") or "").strip()
    body = str(args.get("body") or "").strip()
    if not number or not body:
        return "error: github_comment needs 'repo', 'issue' (number), and 'body'"
    owner, name = repo
    try:
        row = _request(
            key,
            "POST",
            f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}/issues/{urllib.parse.quote(number)}/comments",
            body={"body": body},
        )
    except GitHubError as exc:
        return f"error: {exc}"
    url = row.get("html_url") if isinstance(row, dict) else ""
    return f"ok: commented on {owner}/{name}#{number} · {url}"


def _list_pulls(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    key = _key(ctx)
    if not key:
        return ctx.missing_secret(FALLBACK_SECRET)
    repo = _repo(ctx, args)
    if isinstance(repo, str):
        return repo
    owner, name = repo
    state = str(args.get("state") or "open").strip() or "open"
    try:
        rows = _request(
            key,
            "GET",
            f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}/pulls",
            query={"state": state, "per_page": _limit(args)},
        )
    except GitHubError as exc:
        return f"error: {exc}"
    pulls = [r for r in (rows or []) if isinstance(r, dict)]
    if not pulls:
        return "(no pull requests match)"
    # One card per row from the list payload alone — CI rollups would be an
    # extra request per PR, and belong to github_get_pull.
    repo_full = f"{owner}/{name}"
    for row in pulls:
        ctx.card(
            "github_pull",
            _pull_card(repo_full, row),
            card_id=_pull_card_id(repo_full, row.get("number")),
        )
    lines = "\n".join(_fmt_pull(p) for p in pulls)
    return f"{lines}\n(rich PR cards are already shown to the user)"


def _merge_pull(ctx: ConnectorContext, args: dict[str, Any]) -> str:
    """Merge a PR with the connector's stored PAT — the write classification
    (CURATED_WRITES) is what puts a confirm in front of this, never code here."""
    key = _key(ctx)
    if not key:
        return ctx.missing_secret(FALLBACK_SECRET)
    repo = _repo(ctx, args)
    if isinstance(repo, str):
        return repo
    number = str(args.get("number") or "").strip()
    if not number:
        return "error: github_merge_pull needs 'number'"
    method = str(args.get("method") or "merge").strip().lower() or "merge"
    if method not in ("merge", "squash", "rebase"):
        return "error: 'method' must be merge, squash, or rebase"
    owner, name = repo
    try:
        _request(
            key,
            "PUT",
            f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}/pulls/{urllib.parse.quote(number)}/merge",
            body={"merge_method": method},
        )
    except GitHubError as exc:
        # 405/409 (not mergeable, dirty, or checks pending) surface GitHub's
        # own message so the model can say why.
        return f"error: {exc}"
    # Flip the PR card the user is looking at to merged: the card id is the
    # same one _list_pulls/_get_pull emitted, so this updates it in place.
    repo_full = f"{owner}/{name}"
    try:
        row = _request(
            key,
            "GET",
            f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}/pulls/{urllib.parse.quote(number)}",
        )
    except GitHubError:
        row = None
    if not isinstance(row, dict) or not row.get("number"):
        row = {
            "number": int(number) if number.isdigit() else number,
            "html_url": f"https://github.com/{owner}/{name}/pull/{number}",
        }
    payload = _pull_card(repo_full, row)
    payload["state"] = "merged"
    ctx.card("github_pull", payload, card_id=_pull_card_id(repo_full, row.get("number")))
    return (
        f"ok: merged {repo_full}#{number} ({method}). "
        "The PR card in chat already shows the merged state."
    )


def tools() -> list[ConnectorTool]:
    return [
        ConnectorTool(
            ToolSpec(
                name="github_get_file",
                description=(
                    "Read source text or list a directory in a connected GitHub repository. "
                    "Use path for README, SKILL.md, scripts or directories; omit it for the root. "
                    "Continue at next_offset until end (characters for files, entries for directories). "
                    "Use ref to select a commit, tag or branch and compare sha across file chunks. "
                    "Private repos require Contents: read. Binary/large files need an uploaded archive."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "owner/name"},
                        "path": {"type": "string", "description": "repository-relative path"},
                        "ref": {"type": "string", "description": "optional commit, tag or branch"},
                        "offset": {
                            "type": "integer",
                            "description": "continuation offset; default 0",
                        },
                    },
                    "required": ["repo"],
                },
            ),
            _get_file,
        ),
        ConnectorTool(
            ToolSpec(
                name="github_list_repos",
                description=(
                    "List GitHub repositories this token can see, newest first. "
                    "Pass org to list an organization's repos; otherwise uses the "
                    "connector org field or the authenticated user."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "org": {"type": "string", "description": "organization login"},
                        "limit": {"type": "integer", "description": "max results (default 10)"},
                    },
                },
            ),
            _list_repos,
        ),
        ConnectorTool(
            ToolSpec(
                name="github_list_issues",
                description=(
                    "List issues in a GitHub repo (not pull requests). repo is "
                    "owner/name, or a name if the connector org is set."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "owner/name or name"},
                        "state": {"type": "string", "description": "open, closed, or all"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["repo"],
                },
            ),
            _list_issues,
        ),
        ConnectorTool(
            ToolSpec(
                name="github_search_issues",
                description=(
                    "Search GitHub issues and pull requests. query is GitHub search "
                    "syntax or plain words; org is added from the connector if missing. "
                    "Matching pull requests are already shown to the user as rich "
                    "cards — don't repeat their details in your reply."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "org": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
            ),
            _search_issues,
        ),
        ConnectorTool(
            ToolSpec(
                name="github_create_issue",
                description="Create a GitHub issue. repo is owner/name or a name if org is set.",
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string", "description": "markdown body"},
                    },
                    "required": ["repo", "title"],
                },
            ),
            _create_issue,
        ),
        ConnectorTool(
            ToolSpec(
                name="github_comment",
                description="Comment on a GitHub issue or pull request by number.",
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string"},
                        "issue": {"type": "string", "description": "issue or PR number"},
                        "body": {"type": "string"},
                    },
                    "required": ["repo", "issue", "body"],
                },
            ),
            _comment,
        ),
        ConnectorTool(
            ToolSpec(
                name="github_get_pull",
                description=(
                    "Fetch one pull request and show it to the user as a rich "
                    "card (title, state, author, branches, CI). Use this when "
                    "the user asks about a specific PR. The card is already "
                    "visible — don't repeat its details, but do include the PR "
                    "URL in your reply."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "owner/name or name"},
                        "number": {"type": "string", "description": "PR number"},
                    },
                    "required": ["repo", "number"],
                },
            ),
            _get_pull,
        ),
        ConnectorTool(
            ToolSpec(
                name="github_get_issue",
                description=(
                    "Fetch one issue and show it to the user as a rich card "
                    "(title, state, labels, assignee). The card is already "
                    "visible — don't repeat its details, but do include the "
                    "issue URL in your reply."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "owner/name or name"},
                        "number": {"type": "string", "description": "issue number"},
                    },
                    "required": ["repo", "number"],
                },
            ),
            _get_issue,
        ),
        ConnectorTool(
            ToolSpec(
                name="github_list_pulls",
                description=(
                    "List pull requests in a GitHub repo and show each one to "
                    "the user as a rich card (title, state, branches, author). "
                    "The cards are already visible — don't repeat their details "
                    "in your reply. CI status comes from github_get_pull."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string"},
                        "state": {"type": "string", "description": "open, closed, or all"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["repo"],
                },
            ),
            _list_pulls,
        ),
        ConnectorTool(
            ToolSpec(
                name="github_merge_pull",
                description=(
                    "Merge a pull request (the harness merges with the stored "
                    "GitHub credential). Optional method: merge (default), "
                    "squash, or rebase. On success the PR card in chat flips "
                    "to merged."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "description": "owner/name or name"},
                        "number": {"type": "string", "description": "PR number"},
                        "method": {
                            "type": "string",
                            "description": "merge, squash, or rebase (default merge)",
                        },
                    },
                    "required": ["repo", "number"],
                },
            ),
            _merge_pull,
        ),
    ]
