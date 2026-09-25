"""Which of a bot's tools go in front of the model for one turn.

A model picks the right tool reliably out of about ten and unreliably out of
thirty. This harness ships 34 built-in tools before a single connector is
enabled, and a bot with GitHub and Linear connected is past seventy — so every
turn asks the model to choose from a menu long enough that the choosing itself
becomes the failure.

Adapted from openbot's per-run tool selection, with one deliberate change: they
ask a model which skills a message needs, which costs a round trip per turn.
This harness already narrows its *prompt* with deterministic keyword gating
(`agent/runtime.py`'s `_mentions`) and that costs nothing, so the same approach
is used here on the *tool list*. A skill declares the tools it needs in its
`SKILL.md` frontmatter, the turn text is matched against what the skill says it
is for, and the offer is the matching skills' tools plus every tool no skill
claims.

**Narrowing is not a boundary, and must never be mistaken for one.** What a bot
may call is decided by `agent/govern.py` and `agent/policy.py`; this decides
only what the model can *see*. The distinction matters because of how the two
fail: a boundary must fail closed, and this must fail **open**.

Every failure mode here lands on the whole catalogue:

* no skill declares any tools (every deployment, on day one)
* the catalogue is already small enough to choose from
* the message matches no skill
* a declared tool name matches nothing the bot actually holds
* anything in here raises

A narrowing that failed closed would silently remove capability an operator
granted, and the symptom — a bot that had the right tool and answered from
memory instead — is close to undiagnosable. That is also why the choice is
recorded: `tools_offered` in the audit trail says what was offered, out of how
much, and why, so "why did it not call that" has an answer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

#: Below this many tools, narrowing is off. A catalogue this size is one a model
#: chooses from reliably, so the machinery would add risk and save nothing.
DEFAULT_FLOOR = 12

#: Words too common to carry a signal. Deliberately short: this is a matcher
#: over a handful of skills, not a search engine, and an over-eager stop list
#: costs recall — which here means silently hiding a tool.
_STOPWORDS = frozenset(
    """
    a an and are as at be but by can do does for from has have how i if in into is it
    its me my need of on or should that the their them then there these this to use
    used using want was what when where which who why will with you your
    """.split()
)

_WORD = re.compile(r"[a-z0-9_]+")

# Citation instructions change the answer, so require an actual request rather
# than the seeded skill's broad metadata words ("user", "fact", "sources").
_CITE_REQUEST = re.compile(
    r"^(?:(?:please|also|always)\s+)*"
    r"(?:(?:(?:can|could|would|will)\s+you|"
    r"i\s+(?:want|need|would\s+like)(?:\s+you)?\s+to)\s+)?(?:please\s+)?"
    r"(?:"
    r"(?:(?:use|run|load|apply|follow)\s+(?:the\s+)?)?/cite[-_]sources\b"
    r"|(?:use|run|load|apply|follow)\s+(?:the\s+)?cite[-_]sources\b"
    r"|cite[-_]sources(?=$|\s+(?:please|for|on|to|with)\b)"
    r"|(?:cite|reference)\s+(?:(?:your|the|these|those|relevant|reliable|primary)\s+)*"
    r"(?:sources?|references?|evidence)\b"
    r"|(?:include|add|provide|give\s+me|show\s+me|i\s+(?:want|need))\s+"
    r"(?:(?:some|your|the)\s+)?(?:citations?|references?|source\s+links?|sources?)\b"
    r")"
)


def _citation_requested(text: str) -> bool:
    from harness.connectors import instruction_text

    clean = instruction_text(text).lower().replace("’", "'")
    for sentence in re.split(r"[.!?;\n]|\bbut\b", clean):
        indirect = False
        for clause in re.split(r"\band\b", sentence):
            if not indirect and _CITE_REQUEST.match(clause.strip(" \t-*`")):
                return True
            # Carry a negative or reported instruction across "and" ("do not
            # browse and cite sources"), but let a new sentence stand alone.
            indirect |= bool(re.search(
                r"\b(?:not|never|without|avoid|stop|no|don't|whether|said|says)\b", clause
            ))
    return False


@dataclass(frozen=True)
class Selection:
    """What one turn was offered, and why — the shape the audit row wants."""

    tools: tuple[str, ...]
    #: how many the bot actually holds
    total: int
    #: "narrowed" | "no-skill-tools" | "below-floor" | "no-match" | "error"
    reason: str
    #: skills whose tools were pulled in
    skills: tuple[str, ...] = ()

    @property
    def narrowed(self) -> bool:
        return self.reason == "narrowed"


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOPWORDS and len(w) > 2}


def declared_tools(skill: Any) -> tuple[str, ...]:
    """Tool names a skill says it needs, from its `tools:` frontmatter key.

    A declaration grants nothing. The offer is always intersected with what the
    bot already holds, so writing a skill can never hand anybody a tool it was
    not given — which is what makes it safe for a skill to be a plain file a
    bot can propose.
    """
    raw = getattr(skill, "tools", None) or ()
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    return tuple(str(name).strip() for name in raw if str(name).strip())


def _name_mentioned(skill: Any, text: str) -> bool:
    """True when the turn names this skill (slash-free 'use check-amazon-orders')."""
    raw = (text or "").lower().replace("_", "-")
    for token in (getattr(skill, "name", ""), getattr(skill, "skill_id", "")):
        token = str(token or "").lower().replace("_", "-")
        if len(token) >= 4 and token in raw:
            return True
    return False


def _opt_in_skill(skill: Any) -> bool:
    """Skills that must not fire on a passing content-word overlap."""
    for token in (getattr(skill, "name", ""), getattr(skill, "skill_id", "")):
        if str(token or "").lower().replace("_", "-") == "harness-tips":
            return True
    return False


_NAME_ONLY_SKILLS = frozenset({"learn-from-demonstration", "add-connector"})


def _name_only_skill(skill: Any) -> bool:
    """Skills whose when_to_use would match ordinary chat.

    learn-from-demo talks about recordings and skills; add-connector names
    every plugin and the word "chat". Either would fire on a passing word
    and inject its body into a turn that never asked (echo replies changed,
    scheduler/ws tests timed out waiting for the plain echo). They load on
    `/name`, on the name in plain words, or via load_skill — never on a
    content-word overlap.
    """
    for token in (getattr(skill, "name", ""), getattr(skill, "skill_id", "")):
        if str(token or "").lower().replace("_", "-") in _NAME_ONLY_SKILLS:
            return True
    return False


def _matches(skill: Any, text: str, turn_words: set[str]) -> bool:
    """Does this turn look like it needs this skill?

    Matched against what the skill says it is *for* — its name, description and
    `when_to_use` — rather than its body, because the body is prose written for
    the model and matching it turns every skill into a match for everything.
    Naming the skill in the message always matches.
    """
    if _opt_in_skill(skill):
        from agent.tips import ASK_RE

        return bool(ASK_RE.search(text or ""))
    if any(str(getattr(skill, attr, "") or "").lower().replace("_", "-") == "cite-sources"
           for attr in ("name", "skill_id")):
        return _citation_requested(text)
    if _name_mentioned(skill, text):
        return True
    if _name_only_skill(skill):
        return False
    haystack = " ".join(
        str(getattr(skill, attr, "") or "") for attr in ("name", "description", "when_to_use")
    )
    signal = _words(haystack)
    if not signal:
        return False
    return bool(signal & turn_words)


def matching_skills(skills: Iterable[Any], text: str) -> list[Any]:
    """Skills this turn should load the body of (slash-free)."""
    turn_words = _words(text)
    return [skill for skill in skills if _matches(skill, text, turn_words)]


def select(
    granted: Iterable[str],
    skills: Iterable[Any],
    text: str,
    *,
    floor: int = DEFAULT_FLOOR,
) -> Selection:
    """The tools to offer this turn. Never raises; every failure is open."""
    try:
        return _select(granted, skills, text, floor=floor)
    except Exception:
        held = tuple(granted)
        return Selection(held, len(held), "error")


def _select(
    granted: Iterable[str],
    skills: Iterable[Any],
    text: str,
    *,
    floor: int,
) -> Selection:
    held = tuple(dict.fromkeys(granted))  # de-duped, order preserved
    total = len(held)

    if total <= floor:
        return Selection(held, total, "below-floor")

    skill_list = list(skills)
    claimed: set[str] = set()
    for skill in skill_list:
        claimed.update(declared_tools(skill))
    # Only names the bot actually holds count as claimed. A skill naming a tool
    # that does not exist here (a typo, a connector since disabled) must not
    # make that name disappear from the "unclaimed" set and take a real tool
    # with it.
    claimed &= set(held)
    if not claimed:
        return Selection(held, total, "no-skill-tools")

    turn_words = _words(text)
    matched = [s for s in skill_list if _matches(s, text, turn_words)]
    if not matched:
        return Selection(held, total, "no-match")

    wanted: set[str] = set()
    for skill in matched:
        wanted.update(declared_tools(skill))

    # The offer: what the matching skills asked for, plus everything no skill
    # claims at all. That second half is what keeps narrowing from removing
    # capability — a tool nobody wrote a skill for stays on the menu.
    offered = tuple(name for name in held if name in wanted or name not in claimed)
    if len(offered) >= total:
        return Selection(held, total, "no-match")

    names = tuple(str(getattr(s, "name", "") or getattr(s, "skill_id", "")) for s in matched)
    return Selection(offered, total, "narrowed", names)


def apply(tools: Mapping[str, Any], selection: Selection) -> dict[str, Any]:
    """The tool map for one turn, in the catalogue's own order."""
    if not selection.narrowed:
        return dict(tools)
    keep = set(selection.tools)
    return {name: tool for name, tool in tools.items() if name in keep}
