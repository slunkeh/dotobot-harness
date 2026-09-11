"""Per-bot soul / identity (Hermes-style SOUL.md, private).

Each bot has `memory/<bot>/SOUL.md`. It is seeded from the roster personality
the first time it is read, then owned by that bot — group chats never share
or overwrite another bot's soul.
"""

from __future__ import annotations

from pathlib import Path

from harness.paths import HarnessPaths


def soul_path(paths: HarnessPaths, bot: str) -> Path:
    return paths.bot_memory(bot) / "SOUL.md"


def load_soul(paths: HarnessPaths, bot: str, *, personality: str = "") -> str:
    """Return this bot's soul, seeding from `personality` if the file is missing."""
    path = soul_path(paths, bot)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    text = (personality or "").strip() or f"I am {bot}."
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + ("" if text.endswith("\n") else "\n"), encoding="utf-8")
    return path.read_text(encoding="utf-8")


def save_soul(paths: HarnessPaths, bot: str, text: str) -> Path:
    path = soul_path(paths, bot)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = text.strip() + "\n"
    path.write_text(body, encoding="utf-8")
    return path


def soul_block(paths: HarnessPaths, bot: str, *, personality: str = "") -> str:
    text = load_soul(paths, bot, personality=personality).strip()
    if not text:
        return ""
    return "Your soul (private identity — stay true to this, never speak as another bot):\n" + text
