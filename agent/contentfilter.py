"""Account-wide content filter (App Review Guideline 1.2 / 2.3.1).

`content_filter` in `$HARNESS_HOME/settings.json` (`GET/PATCH /api/settings`,
`harness/prefs.py`) is on by default. While it is on, every bot's system
prompt carries the block below, read on every turn so the toggle lands on
the next reply with no restart — the same live-read pattern as Caveman
mode. It is a floor for the chat voice, not the tool gate: authorization
stays in `agent/govern.py`.
"""

from __future__ import annotations

from harness import prefs
from harness.paths import HarnessPaths

PROMPT = (
    "Content filter is on for this account. Keep every reply suitable for a "
    "general audience: no sexual content, no graphic violence, no slurs or "
    "harassment, no glorification of self-harm, and no instructions that "
    "would help someone hurt people. Decline such requests plainly and "
    "offer a safe alternative. Facts, technical detail and frank language "
    "about difficult subjects are fine; gratuitous or explicit material is "
    "not. If asked whether a filter is on, say so."
)


def enabled(paths: HarnessPaths) -> bool:
    return prefs.content_filter(paths)
