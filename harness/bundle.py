"""A configured harness, as one reviewable file.

Everything that makes a deployment *this* deployment — its bots, their souls
and skills, their routines, which connectors exist and who may use them — is
imperative state accreted through the API into `$HARNESS_HOME`. There was no
way to see it all at once, diff it, put it in a repository, or stand a second
one up the same. `roster.example.toml` covers the roster, which is the smallest
slice of it.

Adapted from openbot's tenant package, which declares a whole deployment as
version-controllable YAML and validates every field at load. The shape here is
one JSON document rather than six files, because this harness has no build step
to assemble them and `json` is stdlib where a YAML parser is not.

**No secret is ever in a bundle, and that is enforced rather than intended.**
`$HARNESS_HOME/credentials/`, the link key, OAuth tokens and connector
credentials are excluded by construction — the exporter names the fields it
copies rather than copying a record and deleting the dangerous ones, because a
deny-list stays correct only until a vendor adds a field. Any connector value
whose key looks credential-shaped is dropped as well, so a token somebody typed
into a config box does not travel either. `tests/test_bundle.py` plants secrets
in every store and asserts none reach the output.

Importing is additive and never destructive: a bot that already exists is left
alone and reported, not overwritten. Restoring a deployment onto a running one
should not be able to delete somebody's work by accident, and "skipped 3" is a
sentence an operator can act on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.skills import skill_slug

from .paths import HarnessPaths

#: Bumped when the shape changes incompatibly. An importer that does not know a
#: version refuses rather than guessing at it.
BUNDLE_VERSION = 1

#: Bot fields that travel. Named explicitly: copying `Bot.to_dict()` wholesale
#: would carry `auth_ref`, and while that is a reference rather than a secret,
#: it names which credential to look for and belongs to the machine it was set
#: up on, not to the bundle.
BOT_FIELDS = ("name", "role", "personality", "provider", "model", "avatar", "title", "color")

#: Connector fields that travel. `config` is filtered further, below.
CONNECTOR_FIELDS = ("id", "type", "name", "enabled_for")

#: Routine fields that travel.
ROUTINE_FIELDS = ("id", "title", "prompt", "cron", "once_at", "enabled", "bot")

#: Substrings that make a config key credential-shaped. Checked case-insensitively
#: against every connector config key, and anything matching is dropped rather
#: than exported. Over-broad on purpose: a dropped setting is an operator
#: retyping one value, and a leaked one is a credential in a file somebody
#: emails around.
SECRET_KEY_HINTS = (
    "auth",
    "cert",
    "credential",
    "key",
    "pass",
    "private",
    "secret",
    "session",
    "signature",
    "token",
)


class BundleError(ValueError):
    """A bundle this harness will not import. Names what is wrong."""


@dataclass
class ImportReport:
    added_bots: list[str] = field(default_factory=list)
    skipped_bots: list[str] = field(default_factory=list)
    added_skills: list[str] = field(default_factory=list)
    skipped_skills: list[str] = field(default_factory=list)
    added_routines: int = 0
    skipped_routines: int = 0
    connectors_declared: list[str] = field(default_factory=list)
    #: name -> why it could not be added. Kept apart from `skipped_*`, which
    #: means "already here": a thing that failed and a thing that was already
    #: correct look identical in a count, and only one of them needs somebody
    #: to do something.
    failed: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            f"bots:     {len(self.added_bots)} added, {len(self.skipped_bots)} already here",
            f"skills:   {len(self.added_skills)} added, {len(self.skipped_skills)} already here",
            f"routines: {self.added_routines} added, {self.skipped_routines} already here",
        ]
        if self.connectors_declared:
            lines.append(
                f"connectors: {len(self.connectors_declared)} declared "
                "— each needs its credential supplied here before it will work "
                f"({', '.join(self.connectors_declared)})"
            )
        if self.skipped_bots:
            lines.append(f"already here: {', '.join(self.skipped_bots)}")
        if self.failed:
            lines.append("")
            lines.append("FAILED — these were in the bundle and could not be added:")
            for name, why in sorted(self.failed.items()):
                lines.append(f"  {name}: {why}")
        return "\n".join(lines)


def _looks_secret(key: str) -> bool:
    low = str(key).lower()
    return any(hint in low for hint in SECRET_KEY_HINTS)


def _safe_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    return {k: v for k, v in config.items() if not _looks_secret(k)}


def _pick(row: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    return {k: row[k] for k in fields if k in row and row[k] not in (None, "")}


def export(paths: HarnessPaths, roster_bots: list[Any]) -> dict[str, Any]:
    """The deployment as a plain dict, ready for `json.dumps`."""
    from agent.skills import load_skills

    from . import connectors as connectors_mod
    from . import routines as routines_mod

    bots: list[dict[str, Any]] = []
    skills: list[dict[str, Any]] = []
    routines: list[dict[str, Any]] = []

    for bot in roster_bots:
        row = bot.to_dict() if hasattr(bot, "to_dict") else dict(bot)
        bots.append(_pick(row, BOT_FIELDS))
        name = row.get("name") or ""

        # A bot's soul is what it has learned about itself and is part of who it
        # is, so it travels. It is written by the bot, so it is data rather than
        # configuration — but a deployment restored without souls is not the
        # same deployment.
        try:
            from agent.soul import load_soul

            soul = (load_soul(paths, name) or "").strip()
            if soul:
                bots[-1]["soul"] = soul
        except Exception:
            pass

        try:
            for skill in load_skills(paths, name):
                if skill.source != "private":
                    continue
                skills.append(
                    {
                        "bot": name,
                        "id": skill.skill_id,
                        "name": skill.name,
                        "description": skill.description,
                        "when_to_use": skill.when_to_use,
                        "tools": list(getattr(skill, "tools", []) or []),
                        "body": skill.body,
                    }
                )
        except Exception:
            pass

        try:
            for routine in routines_mod.list_routines(paths, name):
                picked = _pick(routine, ROUTINE_FIELDS)
                picked["bot"] = name
                routines.append(picked)
        except Exception:
            pass

    connectors: list[dict[str, Any]] = []
    try:
        for record in connectors_mod.Connectors(paths)._load():
            picked = _pick(record, CONNECTOR_FIELDS)
            safe = _safe_config(record.get("config"))
            if safe:
                picked["config"] = safe
            connectors.append(picked)
    except Exception:
        pass

    return {
        "version": BUNDLE_VERSION,
        "bots": bots,
        "skills": skills,
        "routines": routines,
        "connectors": connectors,
    }


def dumps(bundle: dict[str, Any]) -> str:
    return json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def loads(text: str) -> dict[str, Any]:
    """Parse and validate. Refuses by name rather than importing half of it."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise BundleError(f"not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise BundleError("a bundle must be a JSON object")
    version = data.get("version")
    if version != BUNDLE_VERSION:
        raise BundleError(
            f"bundle version {version!r} is not {BUNDLE_VERSION}; "
            "it was written by a different version of the harness"
        )
    for key in ("bots", "skills", "routines", "connectors"):
        value = data.get(key)
        if value is not None and not isinstance(value, list):
            raise BundleError(f"{key} must be a list, got {type(value).__name__}")
    for bot in data.get("bots") or []:
        if not isinstance(bot, dict) or not str(bot.get("name") or "").strip():
            raise BundleError("every bot in a bundle needs a name")
    return data


def _path_shaped(name: str) -> bool:
    return (
        name in (".", "..")
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or name.startswith(".")
    )


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().parent == root.resolve()
    except OSError:
        return False


def apply(paths: HarnessPaths, bundle: dict[str, Any], orch: Any) -> ImportReport:
    """Add what is missing. Never overwrites, never deletes.

    Restoring a deployment onto a running one must not be able to lose
    somebody's work, so an existing bot, skill or routine is reported and left
    exactly as it is. "skipped 3" is a sentence an operator can act on;
    "overwrote 3" is one they find out about later.
    """
    from agent.soul import load_soul, save_soul

    from . import routines as routines_mod

    report = ImportReport()
    existing = {b.name for b in orch.bots()}

    for row in bundle.get("bots") or []:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        if name in existing:
            report.skipped_bots.append(name)
            continue
        fields = {k: v for k, v in row.items() if k in BOT_FIELDS}
        try:
            # Registered, not started. An import writes configuration; a
            # deployment is brought up by `harness up`. On the machines backend
            # spawning provisions a container, so starting here made importing
            # a roster require Docker.
            created = orch.add_bot(start=False, **fields)
        except Exception as exc:
            # NOT `skipped`: that word means "already here and left alone".
            # Reporting a failure as a skip is how an import can claim to have
            # done nothing wrong while having done nothing at all.
            report.failed[name] = f"{type(exc).__name__}: {exc}"
            continue
        existing.add(created.name)
        report.added_bots.append(created.name)

        soul = str(row.get("soul") or "").strip()
        # Only onto a bot this import created, and only when it has nothing of
        # its own — a soul is written by the bot about itself, and clobbering
        # one is the most personal thing this could get wrong.
        if soul and not (load_soul(paths, created.name) or "").strip():
            try:
                save_soul(paths, created.name, soul)
            except OSError:
                pass

    for row in bundle.get("skills") or []:
        bot = str(row.get("bot") or "").strip()
        raw_id = str(row.get("id") or row.get("name") or "").strip()
        if not bot or not raw_id or bot not in existing:
            continue
        # The id is a directory name under memory/<bot>/skills/. A bundle is
        # a file people share, so an id shaped like a path (`/tmp/x`,
        # `../../skills/x`) is a failed row, never a write somewhere else.
        skills_root = paths.bot_memory(bot) / "skills"
        slug = skill_slug(raw_id)
        folder = skills_root / slug
        if _path_shaped(raw_id) or not _inside(folder, skills_root):
            report.failed[f"{bot}/{raw_id}"] = "ValueError: skill id is not a bare name"
            continue
        if (folder / "SKILL.md").is_file():
            report.skipped_skills.append(f"{bot}/{slug}")
            continue
        head = [
            "---",
            f"name: {row.get('name') or slug}",
            f"description: {row.get('description') or ''}",
            f"when_to_use: {row.get('when_to_use') or ''}",
        ]
        tools = [str(t).strip() for t in (row.get("tools") or []) if str(t).strip()]
        if tools:
            head.append(f"tools: {', '.join(tools)}")
        head.append("---")
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "SKILL.md").write_text(
                "\n".join(head) + "\n\n" + str(row.get("body") or "").strip() + "\n",
                encoding="utf-8",
            )
            report.added_skills.append(f"{bot}/{slug}")
        except OSError as exc:
            report.failed[f"{bot}/{slug}"] = f"{type(exc).__name__}: {exc}"

    for row in bundle.get("routines") or []:
        bot = str(row.get("bot") or "").strip()
        if not bot or bot not in existing:
            continue
        title = str(row.get("title") or "").strip()
        try:
            have = {
                str(r.get("title") or "").strip() for r in routines_mod.list_routines(paths, bot)
            }
            if title and title in have:
                report.skipped_routines += 1
                continue
            once_raw = row.get("once_at")
            once_at = None
            if once_raw not in (None, ""):
                try:
                    once_at = float(once_raw)
                except (TypeError, ValueError):
                    once_at = None
            routines_mod.add_routine(
                paths,
                bot,
                title=title,
                prompt=str(row.get("prompt") or ""),
                when=str(row.get("cron") or ""),
                once_at=once_at,
                enabled=bool(row.get("enabled")) if "enabled" in row else None,
            )
            report.added_routines += 1
        except Exception as exc:
            report.failed[f"routine {title or '(untitled)'}"] = f"{type(exc).__name__}: {exc}"

    # Connectors are DECLARED, never created. Each needs a credential that by
    # design is not in the bundle, so importing one silently would produce a
    # connector that looks configured and fails on every call — the shape of
    # failure that is hardest to diagnose. Naming them instead tells the
    # operator exactly what is left to do.
    for row in bundle.get("connectors") or []:
        label = str(row.get("name") or row.get("type") or "").strip()
        if label:
            report.connectors_declared.append(label)

    return report
