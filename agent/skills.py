"""SKILL.md-style procedural memory.

A skill is a directory containing a `SKILL.md` file with a small frontmatter
block and a body:

    ---
    name: search-web
    description: How to find and cite a fact online
    when_to_use: user asks for a current fact
    ---
    1. Open the shared browser session...

The directory may also carry sibling helper files (scripts, reference docs)
next to `SKILL.md`; the catalog still lists the folder as ONE skill and the
turn injection names the helper paths so the bot can read them.

Skills load from two roots:
    shared/skills/            shared across all bots
    memory/<bot>/skills/      private to one bot

`propose_skill()` is the thin autonomous-authoring hook: after a
task, a bot can write a new private skill without a human authoring it.

Per-bot enablement is negative-space: `harness/workflows.py` keeps
only a `disabled` list per bot, so every skill — including ones seeded or
shared later — is ON by default; `skills_prompt` reflects it.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from harness.paths import HarnessPaths

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)

#: Headings a reusable skill must name (Grok Bot masterclass six-part body).
PROCEDURE_HEADINGS = (
    "When to use",
    "Required inputs and access",
    "Sequence",
    "How to validate",
    "What to return",
    "What requires approval",
)


class SkillError(ValueError):
    """Skill body is missing the six-part procedure."""


def _heading_key(line: str) -> str:
    return line.strip().lstrip("#").strip().rstrip(":").lower()


def missing_procedure_headings(body: str) -> list[str]:
    """Which of the six procedure headings are absent from `body`."""
    present = {_heading_key(line) for line in (body or "").splitlines() if line.strip()}
    return [h for h in PROCEDURE_HEADINGS if h.lower() not in present]


def procedure_body(
    *,
    when: str = "",
    inputs: str = "",
    sequence: str = "",
    validate: str = "",
    returns: str = "",
    approval: str = "",
) -> str:
    """Render a six-part SKILL.md body. Empty sections get an honest stub."""
    sections = {
        "When to use": when,
        "Required inputs and access": inputs,
        "Sequence": sequence,
        "How to validate": validate,
        "What to return": returns,
        "What requires approval": approval,
    }
    parts = []
    for heading in PROCEDURE_HEADINGS:
        text = (sections[heading] or "").strip() or "(not specified)"
        parts.append(f"## {heading}\n{text}")
    return "\n\n".join(parts)


def wrap_procedure(body: str, *, when_to_use: str = "") -> str:
    """Guarantee the six headings. Incomplete bodies become the Sequence."""
    text = (body or "").strip()
    if not missing_procedure_headings(text):
        return text
    return procedure_body(
        when=when_to_use,
        sequence=text,
        approval="Ask the user before sending external messages or spending credentials.",
    )


def procedure_template() -> str:
    """Shown to the model when propose_skill is rejected."""
    return procedure_body(
        when="when this procedure applies",
        inputs="accounts, files, connectors, or logins required",
        sequence="1. numbered steps",
        validate="how to check the result is right",
        returns="what to hand back (file, summary, card)",
        approval="what must wait for the user",
    )


@dataclass
class Skill:
    name: str
    description: str
    when_to_use: str
    body: str
    source: str  # "shared" | "private"
    path: Path
    helpers: list[Path] = field(default_factory=list)  # sibling files in the skill folder
    #: tool names this skill needs, from a `tools:` frontmatter key. Used to
    #: narrow what a turn is offered (`agent/toolselect.py`). A declaration
    #: grants nothing — the offer is always intersected with what the bot was
    #: already given, so writing a skill can never hand anybody a tool.
    tools: list[str] = field(default_factory=list)

    @property
    def skill_id(self) -> str:
        """Stable id: the skill's folder name (survives frontmatter renames)."""
        return self.path.parent.name

    @classmethod
    def from_file(cls, path: Path, source: str) -> Skill:
        raw = path.read_text(encoding="utf-8")
        meta: dict[str, str] = {}
        body = raw
        m = _FRONTMATTER.match(raw)
        if m:
            for line in m.group(1).splitlines():
                if ":" in line:
                    key, _, val = line.partition(":")
                    meta[key.strip().lower()] = val.strip()
            body = m.group(2).strip()
        return cls(
            name=meta.get("name", path.parent.name),
            description=meta.get("description", ""),
            when_to_use=meta.get("when_to_use", ""),
            body=body,
            source=source,
            path=path,
            helpers=_helper_files(path),
            tools=[t for t in meta.get("tools", "").replace(",", " ").split() if t],
        )


def _helper_files(skill_md: Path) -> list[Path]:
    """Sibling files that ride along with a folder skill (everything but SKILL.md)."""
    try:
        return sorted(
            p for p in skill_md.parent.iterdir() if p.is_file() and p.name != skill_md.name
        )
    except OSError:
        return []


_DEFAULT_CITE = """\
---
name: cite-sources
description: Cite evidence in factual answers and review context
when_to_use: factual research, source verification, or /cite-sources
---
1. Find relevant sources for factual claims in your answer to the user. Link or
   quote the evidence and say when a claim cannot be verified.
2. Keep supporting sources in the explanation or review context. Review context
   is separate from an outgoing message and is not posted with it.
3. Keep exact approved outgoing text unchanged. General citation guidance does
   not authorize adding links, source annotations, or other text after approval.
   Do not refuse an approved question solely because it has no citation.
4. If the user explicitly requests citations in the outgoing message, include
   them in the proposed text before approval. Adding a citation after approval
   requires a revised proposal and new approval.
"""

# Exact bytes of the shipped seed, not a name-based claim that a file is ours.
_LEGACY_CITE = """\
---
name: cite-sources
description: Always cite sources when stating a fact
when_to_use: user asks for a fact or /cite-sources
---
1. Find a source for each claim.
2. Quote or link the source in the reply.
3. If you cannot cite, say so.
"""


#: Grok Bot ships an `add-connector` skill ("Walk through connecting a
#: service"); this is the harness one. The flow is the `add_connector` tool
#: plus the sign-in card / `request_secret`, so the skill is short and never
#: sends the user to Settings.
_DEFAULT_ADD_CONNECTOR = """\
---
name: add-connector
description: Walk through connecting a plugin (Gmail, Linear, GitHub, Notion, Slack…) from chat
when_to_use: user wants to connect, authorize, or add a plugin or service, or /add-connector
---
## Purpose
Connect a service so its tools are available in chat, without leaving the conversation.

## Inputs
- Which service. If the user did not name one, ask with ask_user_choice listing a
  few catalog plugins (Gmail, Linear, GitHub, Notion, Other) and wait.

## Steps
1. If the service is already connected, say so and offer to use it.
2. If it is added but not signed in, call its `<type>_connect` tool so the
   sign-in card appears, then ask the user to tap Authorize.
3. Otherwise confirm with ask_user_choice ("Add <name>" / "Not now"), then call
   add_connector with the catalog type.
4. OAuth types: the sign-in card is now in chat — wait for Authorize.
   API-key types: call request_secret with the name the tool result gives you.
5. Once connected, do the thing the user originally asked for.

## Rules
- A service is a plugin, never a bot. Do not offer to create a bot for it.
- Never ask for a key or token in plain chat; request_secret only.
- Do not send the user to Settings; the card and the secure box are in chat.

## Output
The service connected (or the card waiting), then the original request done.

## Verification
Its `<type>_*` tools appear on the next turn; a call succeeds without a
sign-in error.
"""


def _default_skills() -> dict[str, str]:
    from .learn_demo import SKILL_ID as LEARN_ID
    from .learn_demo import SKILL_MD as LEARN_MD
    from .tips import tips_skill_md

    return {
        "cite-sources": _DEFAULT_CITE,
        "harness-tips": tips_skill_md(),
        "add-connector": _DEFAULT_ADD_CONNECTOR,
        LEARN_ID: LEARN_MD,
    }


def default_skill_names() -> frozenset[str]:
    """Folder names of the harness-seeded shared skills (source `seeded`)."""
    return frozenset(_default_skills())


def _upgrade_citation_seed(path: Path, text: str) -> None:
    """Replace only the old seed, atomically and with its existing read access."""
    with path.open("rb") as original:
        if original.read() != _LEGACY_CITE.encode("utf-8"):
            return
        mode = stat.S_IMODE(os.fstat(original.fileno()).st_mode)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as updated:
            updated.write(text)
            updated.flush()
            os.fchmod(updated.fileno(), mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def ensure_default_skills(paths: HarnessPaths) -> None:
    """Seed the shared default skills so `/` has something to pick.

    Only the exact old citation seed is upgraded. Other existing files, edited
    skills and symlink destinations remain untouched; private skills and per-bot
    enablement are stored separately and are never changed here.
    """
    if paths.skills.is_symlink():
        return
    paths.skills.mkdir(parents=True, exist_ok=True)
    for name, text in _default_skills().items():
        dest = paths.skills / name
        skill_md = dest / "SKILL.md"
        if dest.is_symlink() or skill_md.is_symlink():
            continue
        if skill_md.is_file():
            if name == "cite-sources":
                try:
                    _upgrade_citation_seed(skill_md, text)
                except OSError:
                    pass  # A read-only skill must not prevent startup.
            continue
        dest.mkdir(parents=True, exist_ok=True)
        skill_md.write_text(text, encoding="utf-8")


def _load_from(root: Path, source: str) -> list[Skill]:
    skills: list[Skill] = []
    if not root.is_dir():
        return skills
    for skill_md in sorted(root.glob("*/SKILL.md")):
        try:
            skills.append(Skill.from_file(skill_md, source))
        except OSError:
            continue
    return skills


def load_skills(paths: HarnessPaths, bot: str, *, enabled_only: bool = False) -> list[Skill]:
    """Shared skills first, then this bot's private skills.

    `enabled_only=True` drops skills this bot has switched off (negative-space
    disabled list). The default keeps every skill so explicit
    `/skill-name` invocation and management surfaces still see them all.
    """
    private_root = paths.bot_memory(bot) / "skills"
    skills = _load_from(paths.skills, "shared") + _load_from(private_root, "private")
    if enabled_only:
        from harness.workflows import disabled_workflows

        off = disabled_workflows(paths, bot)
        skills = [s for s in skills if s.skill_id not in off]
    return skills


def _helpers_block(skill: Skill) -> str:
    lines = [f"- {p}" for p in skill.helpers]
    return "Helper files for this skill (read them when the steps need them):\n" + "\n".join(lines)


def skill_turn_block(skill: Skill, rest: str) -> str:
    """Render a slash-invoked skill as the user turn the bot should follow."""
    parts = [
        f"Use your skill {skill.name!r} ({skill.source}). Follow it exactly.",
        skill.body.strip(),
    ]
    if skill.helpers:
        parts.append(_helpers_block(skill))
    if rest:
        parts.append("User request:\n" + rest)
    return "\n\n".join(parts)


def skills_prompt(paths: HarnessPaths, bot: str, *, skills: list[Skill] | None = None) -> str:
    """Render a compact skills index for the system prompt (enabled skills only).

    Pass `skills` when the enabled list is already loaded this turn — the
    per-message pre-flight otherwise scans and parses the skill folders twice.
    """
    if skills is None:
        skills = load_skills(paths, bot, enabled_only=True)
    if not skills:
        return ""
    lines = [
        "Available skills (matching ones are included in the turn; "
        "otherwise call load_skill with the name — there is no read_file tool). "
        "Do not volunteer a skill the user did not ask for."
    ]
    for s in skills:
        hint = f" — use when: {s.when_to_use}" if s.when_to_use else ""
        line = f"- {s.name} ({s.source}): {s.description}{hint}"
        if s.helpers:
            line += " [helper files: " + ", ".join(str(p) for p in s.helpers) + "]"
        lines.append(line)
    return "\n".join(lines)


def skill_slug(name: str) -> str:
    """Slash-command id: kebab-case `[a-z0-9_-]+`."""
    return re.sub(r"[^a-z0-9_-]+", "-", (name or "").lower()).strip("-") or "skill"


def slash_name(skill: Skill) -> str:
    """Token the composer `/` picker and `parse_slash` both accept."""
    raw = (skill.name or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]+", raw):
        return raw
    return skill.skill_id


def propose_skill(
    paths: HarnessPaths,
    bot: str,
    *,
    name: str,
    description: str,
    body: str,
    when_to_use: str = "",
    strict: bool = False,
) -> Path:
    """Write a new private SKILL.md for `bot` (autonomous skill creation).

    `strict=True` (the tool) rejects a body missing the six procedure
    headings so the model fills them. Internal callers wrap instead.
    The folder id is the slash token (`/{slug}`) so `/` works as soon as
    the file lands; frontmatter `name` may stay a display title.
    """
    text = (body or "").strip()
    missing = missing_procedure_headings(text)
    if missing:
        if strict:
            raise SkillError(
                "propose_skill body must include these headings: "
                + ", ".join(PROCEDURE_HEADINGS)
                + ". Missing: "
                + ", ".join(missing)
                + ".\n\nTemplate:\n"
                + procedure_template()
            )
        text = wrap_procedure(text, when_to_use=when_to_use)
    safe = skill_slug(name)
    skill_dir = paths.bot_memory(bot) / "skills" / safe
    skill_dir.mkdir(parents=True, exist_ok=True)
    front = [
        "---",
        f"name: {name}",
        f"description: {description}",
        f"when_to_use: {when_to_use}",
        "---",
        "",
    ]
    path = skill_dir / "SKILL.md"
    path.write_text("\n".join(front) + text + "\n", encoding="utf-8")
    return path
