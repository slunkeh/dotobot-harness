"""Unified workflow store: skills + routines as one record type.

A *workflow* is anything a bot can be handed as a procedure: a seeded or
shared SKILL.md, a private SKILL.md, or a scheduled routine. This module
merges all of them into one `Workflow` record so clients get a single
catalog (`GET /api/workflows`) instead of stitching `/api/skills` and the
routines API together. The model-facing distinction is unchanged: routines
stay scheduled jobs delivered as user messages, skills stay prompt-injected
procedures.

Creating a workflow auto-routes on the trigger: a spec WITH a time/cron
becomes a routine (`harness/routines.py`), one without becomes a private
SKILL.md (`agent/skills.py`). Same record shape either way.

Per-bot enablement is negative space: `memory/<bot>/disabled-workflows.json`
stores only a `disabled` list, so every skill — including ones seeded or
shared AFTER the bot exists — is ON by default with no backfill step.
Routines keep their own `enabled` flag (they already had one); toggling a
routine-sourced workflow delegates to it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.skills import Skill, default_skill_names, load_skills, propose_skill
from harness.fsutil import write_atomic
from harness.paths import HarnessPaths
from harness.routines import add_routine, list_routines, update_routine
from harness.routines import public as routine_public

SOURCES = ("seeded", "shared", "private", "routine")


class WorkflowError(ValueError):
    """Bad workflow spec or unknown workflow id."""


@dataclass
class Workflow:
    name: str
    description: str
    source: str  # "seeded" | "shared" | "private" | "routine"
    path: Path | None
    trigger: str | None  # 5-field cron for routines; None for plain skills
    owner_bot: str | None  # None for seeded/shared skills
    id: str = ""  # skill folder name, or routine id
    when_to_use: str = ""
    helpers: list[str] = field(default_factory=list)  # folder-skill sibling files
    enabled: bool = True  # for the bot the record was listed for

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "path": str(self.path) if self.path else None,
            "trigger": self.trigger,
            "owner_bot": self.owner_bot,
            "when_to_use": self.when_to_use,
            "helpers": list(self.helpers),
            "enabled": self.enabled,
        }


# -- per-bot enablement (negative space) -----------------------------------
def _enablement_file(paths: HarnessPaths, bot: str) -> Path:
    return paths.bot_memory(bot) / "disabled-workflows.json"


def disabled_workflows(paths: HarnessPaths, bot: str) -> set[str]:
    """The ONLY stored enablement state: ids this bot switched off."""
    path = _enablement_file(paths, bot)
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    raw = data.get("disabled") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return set()
    return {item for item in raw if isinstance(item, str)}


def is_enabled(paths: HarnessPaths, bot: str, workflow_id: str) -> bool:
    """Default ON: anything not on the disabled list — new seeds included."""
    return workflow_id not in disabled_workflows(paths, bot)


def set_enabled(paths: HarnessPaths, bot: str, workflow_id: str, enabled: bool) -> None:
    off = disabled_workflows(paths, bot)
    if enabled:
        if workflow_id not in off:
            return
        off.discard(workflow_id)
    else:
        if workflow_id in off:
            return
        off.add(workflow_id)
    write_atomic(
        _enablement_file(paths, bot),
        json.dumps({"disabled": sorted(off)}, ensure_ascii=False, indent=2) + "\n",
    )


# -- record adapters --------------------------------------------------------
def _skill_workflow(paths: HarnessPaths, bot: str, skill: Skill) -> Workflow:
    sid = skill.skill_id
    source = skill.source
    if source == "shared" and sid in default_skill_names():
        source = "seeded"
    return Workflow(
        id=sid,
        name=skill.name,
        description=skill.description,
        source=source,
        path=skill.path,
        trigger=None,
        owner_bot=bot if skill.source == "private" else None,
        when_to_use=skill.when_to_use,
        helpers=[str(p) for p in skill.helpers],
        enabled=is_enabled(paths, bot, sid),
    )


def _routine_workflow(paths: HarnessPaths, bot: str, row: dict[str, Any]) -> Workflow:
    return Workflow(
        id=str(row.get("id") or ""),
        name=str(row.get("title") or "") or "(untitled routine)",
        description=str(row.get("prompt") or ""),
        source="routine",
        path=paths.bot_routines(bot),
        trigger=str(row.get("cron") or "") or None,
        owner_bot=bot,
        enabled=bool(row.get("enabled", True)),
    )


# -- store ------------------------------------------------------------------
def list_workflows(paths: HarnessPaths, bot: str) -> list[Workflow]:
    """The merged catalog for one bot: seeded/shared, private, then routines."""
    out = [_skill_workflow(paths, bot, s) for s in load_skills(paths, bot)]
    out.extend(_routine_workflow(paths, bot, routine_public(r)) for r in list_routines(paths, bot))
    return out


def get_workflow(paths: HarnessPaths, bot: str, workflow_id: str) -> Workflow | None:
    rows = list_workflows(paths, bot)
    for w in rows:
        if w.id == workflow_id:
            return w
    for w in rows:  # fall back to the display name (skill frontmatter rename)
        if w.name == workflow_id:
            return w
    return None


def create_workflow(
    paths: HarnessPaths,
    bot: str,
    *,
    name: str,
    description: str = "",
    body: str = "",
    when_to_use: str = "",
    trigger: str | None = None,
) -> Workflow:
    """One create path for both shapes.

    With a `trigger` (a time like "8am" or a 5-field cron) the spec routes to
    a routine; without one it lands as a private SKILL.md for `bot`.
    """
    name = (name or "").strip()
    if not name:
        raise WorkflowError("workflow needs a name")
    trigger = (trigger or "").strip()
    if trigger:
        row = add_routine(
            paths, bot, title=name, prompt=(body or description).strip(), when=trigger
        )
        return _routine_workflow(paths, bot, row)
    if not (body or "").strip():
        raise WorkflowError("workflow needs a body (or a trigger to become a routine)")
    path = propose_skill(
        paths, bot, name=name, description=description, body=body, when_to_use=when_to_use
    )
    return _skill_workflow(paths, bot, Skill.from_file(path, "private"))


def set_workflow_enabled(
    paths: HarnessPaths, bot: str, workflow_id: str, enabled: bool
) -> Workflow:
    """Per-bot toggle. Skills use the disabled list; routines their own flag."""
    workflow = get_workflow(paths, bot, workflow_id)
    if workflow is None:
        raise WorkflowError(f"no workflow {workflow_id!r} for {bot!r}")
    if workflow.source == "routine":
        update_routine(paths, bot, workflow.id, enabled=enabled)
    else:
        set_enabled(paths, bot, workflow.id, enabled)
    refreshed = get_workflow(paths, bot, workflow.id)
    return refreshed if refreshed is not None else workflow
