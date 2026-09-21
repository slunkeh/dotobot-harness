"""Does this connector tool read something, or change it?

An operator thinks in effects — "nothing may change anything in Linear" — and a
tool name is a poor proxy for one. Asking somebody to enumerate
`linear_create_issue`, `linear_update_issue`, `linear_comment` and
`linear_attach_files` guarantees a rule that is subtly wrong the first time a
vendor adds a fifth, and there is no way to tell from `agent/govern.py` alone
which of those four is the dangerous one.

So each connector tool is classified `read` or `write`, and
`agent/govern.py` turns that into an intent a policy rule can name.

**The asymmetry is the important part, and it is easy to get backwards.**
Copied from openbot's catalogue, which splits the two cases:

* A **curated** connector is one shipped in this repo, whose tools somebody
  read. For those, the write list below is authoritative: a tool that is not on
  it is a read, because we looked.
* A **custom** server — an MCP endpoint an operator pointed at themselves — has
  tools nobody here has reviewed. For those, anything not positively recognised
  as a read is treated as a **write**. Guessing "read" about an unknown verb is
  how a policy that forbids writes lets one through.

That is fail-closed for the case where we are ignorant, and precise for the case
where we are not. Getting it uniformly "unknown means write" would make every
curated read tool need an allow rule; uniformly "unknown means read" would make
the whole classification decorative.
"""

from __future__ import annotations

EFFECT_READ = "read"
EFFECT_WRITE = "write"

#: Tools that change something, per curated connector type. Everything else a
#: curated connector offers is a read.
#:
#: Keep this in step with `connectors/<type>.py` — `tests/test_connector_effects.py`
#: walks the shipped tool lists and fails on a name that appears in neither this
#: table nor the connector, so a new write tool cannot be added without a
#: decision being recorded here.
CURATED_WRITES: dict[str, frozenset[str]] = {
    "github": frozenset(
        {
            "github_create_issue",
            "github_comment",
            "github_merge_pull",
        }
    ),
    "linear": frozenset(
        {
            "linear_create_issue",
            "linear_update_issue",
            "linear_comment",
            "linear_attach_files",
        }
    ),
    "gmail": frozenset(
        {
            "gmail_create_draft",
            "gmail_send",
            "gmail_send_draft",
        }
    ),
}

#: Verb prefixes that positively indicate a read, for servers nobody reviewed.
#: Deliberately conservative: every entry here is a word that would be actively
#: misleading on a mutating tool, so a vendor using one for a write is doing
#: something no policy could have anticipated.
READ_PREFIXES: tuple[str, ...] = (
    "browse_",
    "check_",
    "describe_",
    "download_",
    "export_",
    "extract_",
    "fetch_",
    "find_",
    "get_",
    "inspect_",
    "list_",
    "lookup_",
    "query_",
    "read_",
    "resolve_",
    "retrieve_",
    "search_",
    "show_",
    "summarize_",
    "view_",
)


def curated(connector_type: str) -> bool:
    """True when this repo ships the connector and somebody read its tools."""
    return str(connector_type or "").strip().lower() in CURATED_WRITES


_SHIPPED: dict[str, frozenset[str]] = {}


def _shipped_rests(type_: str) -> frozenset[str]:
    """The tool names (minus the `<type>_` head) this repo's runtime binds."""
    cached = _SHIPPED.get(type_)
    if cached is not None:
        return cached
    try:
        from .registry import tool_names

        names = tool_names(type_)
    except Exception:
        names = []
    head = type_ + "_"
    rests = frozenset(n[len(head) :] for n in names if n.startswith(head) and len(n) > len(head))
    _SHIPPED[type_] = rests
    return rests


def _shipped(type_: str, name: str) -> bool:
    """Is `name` a tool somebody here actually read?

    A curated *type* is served by more than the curated runtime: Linear (and
    any type with an `mcp_url` and no `prefer_static`) binds whatever the
    remote MCP server's `tools/list` returns, namespaced `linear_<tool>`.
    Those names were never reviewed, so `linear_save_issue` or
    `linear_merge_diff` must not inherit "absent from the write list means
    read" — that premise holds only for the names in `connectors/<type>.py`.
    The multi-account form `<type>_<acct>_<rest>` counts by its rest.
    """
    rests = _shipped_rests(type_)
    rest = name.split("_", 1)[1] if "_" in name else ""
    if rest in rests:
        return True
    return any(name.endswith("_" + r) for r in rests)


def classify(tool_name: str, connector_type: str = "") -> str:
    """`read` or `write` for one connector tool."""
    name = str(tool_name or "").strip().lower()
    type_ = str(connector_type or "").strip().lower()

    # These shipped REST adapters only expose GET and a separate write tool.
    # Their service and account prefixes contain underscores, so splitting at
    # the first underscore loses the read verb. Unknown operations stay writes.
    from harness.connectors import WORKSPACE_TYPES

    for service in WORKSPACE_TYPES - {"gmail"}:
        if (not type_ or type_ == service) and name.startswith(service + "_"):
            rest = name[len(service) + 1:]
            if rest == "get" or rest.endswith("_get"):
                return EFFECT_READ
            return EFFECT_WRITE

    if not type_:
        # Infer the type from the tool's own prefix, which is how every
        # connector names them (`github_…`, `linear_…`, `<server>_…`).
        type_ = name.split("_", 1)[0] if "_" in name else ""

    if type_ in CURATED_WRITES and _shipped(type_, name):
        writes = CURATED_WRITES[type_]
        if name in writes:
            return EFFECT_WRITE
        # Multi-account prefix: gmail_work_create_draft is still the draft write.
        for write in writes:
            rest = write.split("_", 1)[1] if "_" in write else write
            if name.endswith("_" + rest):
                return EFFECT_WRITE
        return EFFECT_READ

    # Unreviewed server (or an unreviewed tool on a curated type, served by
    # its MCP server): only a positively-recognised read verb is a read, and
    # the type prefix is stripped first so `jira_get_issue` is seen as `get_`.
    bare = name.split("_", 1)[1] + "_" if "_" in name else name + "_"
    if bare.startswith(READ_PREFIXES) or name.startswith(READ_PREFIXES):
        return EFFECT_READ
    return EFFECT_WRITE
