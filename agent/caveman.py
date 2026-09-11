"""Caveman mode (caveman.so): terse replies that keep every technical fact.

Two switches, each able to override the other:

- the account default, `caveman` in `$HARNESS_HOME/settings.json`
  (`GET/PATCH /api/settings`, `harness/prefs.py`);
- a per-bot override, `caveman` on the roster entry: `true` / `false`, or
  `null` to follow the account (`PATCH /api/bots/<name>`).

Account on + bot off = off. Account off + bot on = on. Bot unset = whatever
the account says. The resolved value is `effective()`; the agent reads both
sides on every turn (`enabled_for`), so a toggle applies to the next reply
with no restart — `caveman` is a live profile field like `dreaming`.

The style block below is the "full" level of the vendored caveman skill
(`.claude/skills/caveman/SKILL.md`), rewritten as instructions to a bot: it
compresses the chat voice only. Anything that leaves the chat — code,
commits, documents, messages to other people, memory — stays normal prose,
and the block itself drops out for security warnings and irreversible
confirmations, where a missing word costs more than it saves.
"""

from __future__ import annotations

from harness import prefs
from harness.paths import HarnessPaths

PROMPT = (
    "Caveman mode is on for this chat. Reply terse, like a smart caveman: "
    "every technical fact stays, only filler dies.\n"
    "Drop articles (a/an/the), filler (just/really/basically/actually/simply), "
    "pleasantries (sure/certainly/of course/happy to) and hedging. Fragments "
    'are fine. Prefer short synonyms (big, not extensive; fix, not "implement '
    'a solution for"). No narration of what you are about to do, no decorative '
    "tables or emoji, no long raw error dumps unless asked — quote the one "
    "decisive line. Well-known acronyms (DB, API, HTTP) are fine; never invent "
    "abbreviations (cfg, impl, req) and never use arrows — they save nothing "
    "and cost clarity.\n"
    "Never drop not / never / no / only / except: flipping the meaning is worse "
    "than any word saved. Numbers, units, names, code, commands and error "
    "strings stay exact. Never add words to sound caveman; if the caveman "
    "phrasing is not shorter, use the plain one. Reply in the user's language.\n"
    "Pattern: [thing] [action] [reason]. [next step]. "
    'Not: "Sure! I\'d be happy to help. The issue is likely caused by..." '
    'Yes: "Bug in auth middleware. Token expiry check uses < not <=. Fix:"\n'
    "Drop caveman for security warnings, confirmations of irreversible actions, "
    "multi-step sequences where the order could be misread, anything the "
    "compression makes ambiguous, and when the user asks you to clarify; "
    "resume after. Anything that lives outside this chat — code, comments, "
    "commits, documents, tickets, messages to other people, memory — stays "
    "normal prose.\n"
    'Do not announce the mode or prefix replies with "Caveman:"; if asked what '
    "mode you are in, say so plainly."
)


def effective(bot_setting: bool | None, account_default: bool) -> bool:
    """The per-bot override wins when set; otherwise the account decides."""
    if bot_setting is None:
        return bool(account_default)
    return bool(bot_setting)


def enabled_for(bot, paths: HarnessPaths) -> bool:
    """Resolve caveman for one bot right now (both sides re-read each turn)."""
    return effective(getattr(bot, "caveman", None), prefs.caveman_default(paths))
