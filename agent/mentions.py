"""Parse `@bot` mentions out of chat text.

Used by the orchestrator to fan a user message out to the named bots, and by
clients to color mentions with each bot's swatch. Names are `[A-Za-z0-9_-]+`
and matching is case-insensitive against a known roster.
"""

from __future__ import annotations

import re

_MENTION = re.compile(r"(?<![A-Za-z0-9_])@(?P<name>[A-Za-z0-9_-]+)")
_LEADING = re.compile(r"^\s*@(?P<name>[A-Za-z0-9_-]+)(?:\s|$)")


def parse_mentions(text: str) -> list[str]:
    """Return mention names in order of appearance (first-wins, original case)."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _MENTION.finditer(text or ""):
        name = match.group("name")
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def leading_mention(text: str) -> str | None:
    """The `@name` at the start of the message, if any."""
    match = _LEADING.match(text or "")
    return match.group("name") if match else None


def resolve_mentions(text: str, known: list[str] | set[str]) -> list[str]:
    """Mentions that match a known roster name (canonical roster casing)."""
    lookup = {n.lower(): n for n in known}
    resolved: list[str] = []
    seen: set[str] = set()
    for name in parse_mentions(text):
        canon = lookup.get(name.lower())
        if canon and canon not in seen:
            seen.add(canon)
            resolved.append(canon)
    return resolved


def has_everyone(text: str) -> bool:
    """True when the text addresses the whole group (`@everyone`)."""
    return any(n.lower() == "everyone" for n in parse_mentions(text))
