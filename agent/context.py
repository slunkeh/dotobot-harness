"""Bound connector schema offers without losing access to selected tools.

The catalogue contains only connectors already selected by human chat. Search
never discovers services, reads memory, or changes that selection.
"""

from __future__ import annotations

import json
from dataclasses import replace

from providers.base import Message, ToolSpec

from .history import estimate_message_tokens, estimate_tokens, trim_loop_messages
from .tools import Tool


def schema_tokens(specs: list[ToolSpec]) -> int:
    return estimate_tokens(
        json.dumps(
            [
                {"name": s.name, "description": s.description, "parameters": s.parameters}
                for s in specs
            ]
        )
    )


def protected_loop_tokens(messages: list[Message], history: list[Message]) -> int:
    """Reserve actual non-discardable input before offering connector schemas.

    Work on shallow message copies: trimming changes content/images fields,
    never the shared protocol objects. The live conversation stays intact.
    """
    copies = [replace(message) for message in messages]
    history_ids = {id(message) for message in history}
    replay = [
        copy for source, copy in zip(messages, copies, strict=True) if id(source) in history_ids
    ]
    trim_loop_messages(copies, 0, replayed_history=replay)
    return sum(estimate_message_tokens(message) for message in copies)


class ConnectorCatalogue:
    def __init__(self, available: dict):
        self.available = available
        self.selected: list[str] = []
        self.eligible = set(available)
        self.schema_budget = 0
        self.deferred = False
        self.tool = Tool(
            ToolSpec(
                name="load_connector_tools",
                description=(
                    "Find and load tools from this task's user-selected connectors. "
                    "Use a query or exact tool names. No other services can be selected. "
                    "Loaded schemas become available on your next step."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "names": {"type": "array", "items": {"type": "string"}},
                    },
                },
            ),
            self.load,
        )

    def offer(self, tools: dict, budget: int) -> list[ToolSpec]:
        base = [
            t.spec
            for name, t in tools.items()
            if name not in self.available and name != self.tool.spec.name
        ]
        self.eligible = set(self.available).intersection(tools)
        all_specs = base + [t.spec for n, t in self.available.items() if n in self.eligible]
        if schema_tokens(all_specs) <= budget:
            self.deferred = False
            return all_specs
        self.deferred = bool(self.eligible)
        if not self.deferred:
            return base
        base.append(self.tool.spec)
        self.schema_budget = max(0, budget - schema_tokens(base))
        chosen = []
        for name in self.selected:
            if name not in self.eligible:
                continue
            candidate = chosen + [self.available[name].spec]
            if schema_tokens(candidate) <= self.schema_budget:
                chosen = candidate
        return base + chosen

    def load(self, ctx, args: dict) -> str:
        names = args.get("names") or []
        if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
            return "error: names must be an array of tool names"
        if names and any(n not in self.eligible for n in names):
            return "error: a requested tool is outside this task's selected connectors"
        words = str(args.get("query") or "").lower().split()
        if not names:
            names = [
                name
                for name, tool in self.available.items()
                if name in self.eligible
                and all(w in (name + " " + tool.spec.description).lower() for w in words)
            ]
        names = list(dict.fromkeys(names))[:8]
        selected = []
        for name in names:
            candidate = selected + [name]
            if schema_tokens([self.available[n].spec for n in candidate]) <= self.schema_budget:
                selected = candidate
        if not selected:
            return "error: no matching tools fit; request a narrower query or a smaller tool"
        # Give the explicit request priority, then keep earlier schemas while
        # they still fit. Loading one service must not silently unload another.
        for name in self.selected:
            if name in selected or name not in self.eligible:
                continue
            candidate = selected + [name]
            if schema_tokens([self.available[n].spec for n in candidate]) <= self.schema_budget:
                selected = candidate
        dropped = [name for name in self.selected if name not in selected]
        self.selected = selected
        result = "Loaded tools for the next step:\n" + "\n".join(
            f"- {name}: {self.available[name].spec.description[:240]}" for name in selected
        )
        if dropped:
            result += "\nDeferred again to fit the context budget: " + ", ".join(dropped)
        return result
