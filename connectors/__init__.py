"""Connector tool runtimes (the follow-on to `harness/connectors.py`).

`harness.connectors` owns the catalog and UI-managed *configuration* of
integrations; this package owns the *runtime*: for each configured connector,
tool specs + handlers a bot can call. Mirrors the `providers/` layout — one
module per service, stdlib-only HTTP via urllib.

Entry points:
    registry.tools_for_bot(paths, bot) -> bound (spec, run) pairs
    registry.tool_names(type)          -> advertised tool names for a type
"""
