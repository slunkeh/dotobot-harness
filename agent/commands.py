"""Slash commands: `/skill` and built-in per-bot memory/soul helpers.

A leading `/name rest` is a command. Built-ins (`memory`, `remember`, `soul`,
`skills`) run against the receiving bot's private store. Any other name is a
SKILL.md invoke — the skill body is injected into that bot's turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from harness.paths import HarnessPaths

from .skills import Skill, load_skills, slash_name

_SLASH = re.compile(r"^\s*/(?P<name>[A-Za-z0-9_-]+)(?:\s+(?P<rest>.*))?$", re.DOTALL)
# Inline `/skill` in a sentence (composer chips). Do not match `http://`.
_INLINE_SLASH = re.compile(r"(?:^|(?<=\s))/(?P<name>[A-Za-z0-9_-]+)(?=\s|$|[.,;:!?])")

BUILTIN_COMMANDS: tuple[tuple[str, str], ...] = (
    ("memory", "Search this bot's private memory"),
    ("remember", "Store a fact in this bot's private memory"),
    ("soul", "Show this bot's soul / identity"),
    ("skills", "List skills available to this bot"),
    ("stop", "Stop the current turn (older unanswered chats still run after)"),
    ("queue", "Show, clear, or drop queued follow-ups"),
)


@dataclass
class SlashCommand:
    name: str
    rest: str
    raw: str


def parse_slash(text: str) -> SlashCommand | None:
    raw = text or ""
    match = _SLASH.match(raw)
    if match:
        return SlashCommand(
            name=match.group("name").lower(),
            rest=(match.group("rest") or "").strip(),
            raw=raw,
        )
    inline = _INLINE_SLASH.search(raw)
    if not inline:
        return None
    rest = (raw[: inline.start()] + raw[inline.end() :]).strip()
    return SlashCommand(
        name=inline.group("name").lower(),
        rest=rest,
        raw=raw,
    )


def find_skill(paths: HarnessPaths, bot: str, name: str) -> Skill | None:
    """Resolve a skill by name for this bot (shared + private). Hyphens = underscores."""
    want = name.lower().replace("_", "-")
    for skill in load_skills(paths, bot):
        if skill.name.lower().replace("_", "-") == want:
            return skill
        if skill.path.parent.name.lower().replace("_", "-") == want:
            return skill
    return None


def catalog(paths: HarnessPaths, bot: str) -> list[dict]:
    """Picker payload: built-in commands first, then this bot's enabled skills."""
    items = [
        {
            "name": name,
            "description": desc,
            "kind": "command",
            "source": "builtin",
        }
        for name, desc in BUILTIN_COMMANDS
    ]
    seen = {i["name"].lower() for i in items}
    for skill in load_skills(paths, bot, enabled_only=True):
        token = slash_name(skill)
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "name": token,
                "description": skill.description,
                "when_to_use": skill.when_to_use,
                "kind": "skill",
                "source": skill.source,
            }
        )
    return items


def is_builtin(name: str) -> bool:
    return name.lower() in {n for n, _ in BUILTIN_COMMANDS}
