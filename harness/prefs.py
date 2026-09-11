"""Account prefs in `$HARNESS_HOME/settings.json`.

Not `HARNESS_*` env tunables (`harness/settings.py`). Voice fallback lives
in the same file; writers merge so a PATCH of one field cannot wipe another.
"""

from __future__ import annotations

import json
from typing import Any

from . import persona
from .fsutil import write_atomic
from .paths import HarnessPaths

_UNSET = object()


def path(paths: HarnessPaths):
    return paths.home / "settings.json"


def load(paths: HarnessPaths) -> dict[str, Any]:
    p = path(paths)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save(paths: HarnessPaths, data: dict[str, Any]) -> None:
    write_atomic(path(paths), json.dumps(data, indent=2) + "\n")


def user_avatar(paths: HarnessPaths) -> str:
    """The owner's avatar: a persona string, or `photo:<upload basename>`.
    An account that has none yet (first read after creation, or a
    pre-persona home) gets a random persona, persisted, so every device
    sees the same face from the start."""
    data = load(paths)
    current = str(data.get("user_avatar") or "")
    if persona.is_avatar(current):
        return persona.normalize_avatar(current)
    fresh = persona.random_persona()
    data["user_avatar"] = fresh
    save(paths, data)
    return fresh


def set_user_avatar(paths: HarnessPaths, value: str) -> str:
    """Store the avatar the owner picked — a persona, or `photo:<upload>`.
    Raises `persona.PersonaError`."""
    canonical = persona.normalize_avatar(value)
    data = load(paths)
    data["user_avatar"] = canonical
    save(paths, data)
    return canonical


def account_prefs(paths: HarnessPaths) -> dict[str, Any]:
    """`GET /api/settings`: the LLM defaults, the owner's persona, and the
    account-wide Caveman mode default (`agent/caveman.py`)."""
    out: dict[str, Any] = dict(llm_defaults(paths))
    out["user_avatar"] = user_avatar(paths)
    out["caveman"] = caveman_default(paths)
    return out


def caveman_default(paths: HarnessPaths) -> bool:
    """Account-wide Caveman mode. Off until somebody turns it on; a bot's
    own `caveman` override (roster) wins over this either way."""
    return bool(load(paths).get("caveman", False))


def set_caveman(paths: HarnessPaths, on: bool) -> bool:
    data = load(paths)
    data["caveman"] = bool(on)
    save(paths, data)
    return bool(on)


def llm_defaults(paths: HarnessPaths) -> dict[str, str]:
    data = load(paths)
    return {
        "default_provider": str(data.get("default_provider") or "").strip(),
        "default_model": str(data.get("default_model") or "").strip(),
        "default_reasoning": str(data.get("default_reasoning") or "").strip(),
    }


def resolve_llm(
    paths: HarnessPaths,
    *,
    provider: str | None = None,
    model: str | None = None,
    reasoning: str | None = None,
) -> tuple[str, str, str]:
    """Fill omitted LLM fields from the account default.

    An explicit provider does not inherit the account model (the recipe card
    picker, a named provider on create_bot). Empty provider falls back to
    echo when no account default is set.
    """
    defaults = llm_defaults(paths)
    prov = (provider or "").strip()
    mdl = (model or "").strip()
    reason = (reasoning or "").strip()
    if prov:
        return prov, mdl, reason
    return (
        defaults["default_provider"] or "echo",
        mdl or defaults["default_model"],
        reason or defaults["default_reasoning"],
    )


def set_llm_defaults(
    paths: HarnessPaths,
    *,
    provider: Any = _UNSET,
    model: Any = _UNSET,
    reasoning: Any = _UNSET,
) -> dict[str, str]:
    data = load(paths)
    if provider is not _UNSET:
        data["default_provider"] = str(provider or "").strip()
    if model is not _UNSET:
        data["default_model"] = str(model or "").strip()
    if reasoning is not _UNSET:
        data["default_reasoning"] = str(reasoning or "").strip()
    save(paths, data)
    return llm_defaults(paths)
