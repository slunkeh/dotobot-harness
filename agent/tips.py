"""Product tips: the single source of truth for the harness-tips skill.

Iterate on `TIPS` here; everything else renders from it:
  - `providers/echo.py` shows the list as a `show_table` card (keyless demo);
    each `try_phrase` is a literal phrase the echo provider reacts to, so the
    table doubles as a self-demonstrating tour.
  - `agent/skills.py` seeds the shared `harness-tips` skill from
    `tips_skill_md()`, which is how real providers get the full list.

This module must stay stdlib-only and import nothing from the project:
`providers/` cannot import `agent` at module load (import cycle via
`agent.runtime`), so echo lazy-imports it inside the matching branch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Turns that actually asked for the product tour. Shared by the
#: echo demo and by skill matching so "nova" / "try that" never load tips.
ASK_RE = re.compile(
    r"(?i)(?:\b(?:tips?|getting started|what can you do|show me around|"
    r"give me a tour|onboarding)\b|/harness-tips)"
)


@dataclass(frozen=True)
class Tip:
    topic: str
    tip: str
    try_phrase: str = ""  # a literal phrase the keyless echo demo reacts to


TIPS: tuple[Tip, ...] = (
    Tip(
        topic="Hand off",
        tip="Start a message with @<bot> and that bot handles it; the reply comes back to you.",
        try_phrase="@nova say hi",
    ),
    Tip(
        topic="Rich cards",
        tip="Bots draw live cards in chat: progress, tables, confirms, link previews, files.",
        try_phrase="show progress",
    ),
    Tip(
        topic="Charts",
        tip=("Numbers come back as a chart card — line, area, bar, column, pie, donut, scatter."),
        try_phrase="show a bar chart",
    ),
    Tip(
        topic="Choices",
        tip="A bot that needs your decision shows tappable options instead of guessing.",
        try_phrase="pick a snack choose: tea | coffee",
    ),
    Tip(
        topic="Confirm",
        tip="Destructive actions show an approve/cancel card before anything happens.",
        try_phrase="Delete it? confirm: just a demo",
    ),
    Tip(
        topic="Secrets",
        tip=(
            "Keys go into a secure input box, straight to the credentials store — "
            "never the transcript."
        ),
        try_phrase="need secret DEMO_TOKEN",
    ),
    Tip(
        topic="New bots",
        tip="Create bots from chat or Manage in the app; they join the roster live, no restart.",
        try_phrase="create a bot named helper",
    ),
    Tip(
        topic="Routines",
        tip="Describe scheduled work, or a one-off like 'in an hour check staging'. Recurring drafts start disabled; one-shots run then stop.",
        try_phrase="in an hour check staging",
    ),
    Tip(
        topic="Skills & memory",
        tip="/skills lists know-how, /remember stores a fact, /soul shows identity.",
        try_phrase="/skills",
    ),
    Tip(
        topic="Durable files",
        tip="Desktop, Downloads, and Chrome logins survive a restart. apt and /tmp do not.",
        try_phrase="",
    ),
    Tip(
        topic="Shared workspace",
        tip=(
            "Each bot has a private computer; /workspace is the one directory they all "
            "share. Hand a file to a colleague by saving it under /workspace/<project>/ "
            "and messaging them the path. Never put secrets there."
        ),
        try_phrase="",
    ),
)

TABLE_COLUMNS = ["Topic", "Tip", "Try it"]
TABLE_TITLE = "Getting the most out of your bots"


def tips_table_args() -> dict:
    """Exact arguments for a `show_table` tool call rendering the tips."""
    return {
        "title": TABLE_TITLE,
        "columns": list(TABLE_COLUMNS),
        "rows": [[t.topic, t.tip, t.try_phrase] for t in TIPS],
    }


def tips_skill_md() -> str:
    """SKILL.md text for the seeded shared `harness-tips` skill."""
    lines = [
        "---",
        "name: harness-tips",
        "description: Product tour: what the harness can do and how to try each feature",
        'when_to_use: user asks for tips, onboarding, "what can you do", or /harness-tips',
        "---",
    ]
    for t in TIPS:
        entry = f"- **{t.topic}** — {t.tip}"
        if t.try_phrase:
            entry += f" (try: `{t.try_phrase}`)"
        lines.append(entry)
    lines += [
        "",
        "Only present this table when the user asked for tips, onboarding, "
        "what you can do, or /harness-tips. Never volunteer it on an unrelated "
        "message (a name, a connector, a passing word).",
        "When you do present these, prefer a show_table card (columns "
        "Topic / Tip / Try it) over prose. Offer to expand on any row.",
    ]
    return "\n".join(lines) + "\n"
