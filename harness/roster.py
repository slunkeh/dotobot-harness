"""Bot roster schema and persistence.

Define bots once; this is the identity the rest of the control plane hangs off.
The roster is TOML (read with stdlib `tomllib`, no dependency).

Schema per bot:
    name        unique id (also its message-bus address)
    role        short human label
    personality system prompt / character
    provider    claude | codex | grok | echo | ...
    model       provider-specific model id (optional; provider default used)
    reasoning   optional effort (low/medium/high/…); empty = provider default
    auth_ref    secrets-store key / env alias (default: derived from provider)
    avatar      optional emoji / label
    embeddings  optional semantic-recall route "provider[:model]";
                empty = keyword-only recall (the default, and every echo bot)
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .fsutil import write_atomic


def bot_slug(name: str) -> str:
    """Harness id: keep already-valid ids, otherwise hyphenate a display name."""
    raw = (name or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]+", raw):
        return raw
    return re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")


#: A bot name is a path component everywhere (`messages/<name>/inbox`,
#: `memory/<name>`, `browser/sessions/<name>`, the docker container name),
#: so a roster entry whose name could leave those directories is refused
#: outright — however it got into the file.
_PATH_SHAPED = re.compile(r"[/\\\x00-\x1f\x7f]")


def valid_bot_name(name: object) -> bool:
    """True when `name` is one safe path component (never `.`/`..`/empty)."""
    if not isinstance(name, str) or not name or len(name) > 128:
        return False
    if name in (".", "..") or _PATH_SHAPED.search(name):
        return False
    return True


class RosterError(ValueError):
    """Raised when a roster file is missing or malformed."""


@dataclass
class Bot:
    name: str
    role: str = ""
    personality: str = ""
    provider: str = "echo"
    model: str | None = None
    #: Provider reasoning effort (`low` / `medium` / `high` / …). Empty = default.
    reasoning: str = ""
    # Optional semantic-recall embedding route `provider[:model]`,
    # resolved by `agent.embeddings.resolve_embedder`. Empty = no embeddings:
    # recall stays keyword-only, identical to a harness without this feature.
    embeddings: str = ""
    auth_ref: str | None = None
    avatar: str = "robot"
    title: str = ""
    # Optional explicit "#RRGGBB" swatch. Empty = hash the name (colors.py).
    color: str = ""
    # Opt-in dreaming (harness/dreaming.py): the harness schedules
    # self-directed consolidate/reflect/aspire turns while nobody is
    # messaging this bot. Off by default — an unattended bot spending
    # tokens is a choice. Stored as `dreaming`; the first release spelled
    # it `idle_think` and that key is still read.
    dreaming: bool = False
    # Opt-in browser isolation (agent/browser.py): this bot's Chrome gets its
    # own empty cookie/login jar instead of the shared one, so an injected
    # web page in its browser has no ambient logins to spend. The cost is
    # logging in per bot. Applies to the process/shared-display backend;
    # machine-mode login sync exclusion is tracked as future work.
    private_browser: bool = False
    # Caveman mode override (agent/caveman.py): True / False, or None to
    # follow the account default in settings.json. Either side can override
    # the other, so this is a tri-state, not a bool.
    caveman: bool | None = None
    voice_provider: str | None = None
    elevenlabs_voice_id: str | None = None

    def display_name(self) -> str:
        label = (self.title or "").strip()
        # A title that is just the slug is not a display name.
        if label and label != self.name:
            return label
        role = (self.role or "").strip()
        if role and bot_slug(role) == self.name:
            return role
        if "-" not in self.name:
            return self.name
        return " ".join(part[:1].upper() + part[1:] for part in self.name.split("-") if part)

    def system_prompt(self) -> str:
        parts = []
        who = self.display_name()
        if self.role:
            parts.append(f"You are {who}, {self.role}.")
        else:
            parts.append(f"You are {who}.")
        persona = (self.personality or "").strip()
        if persona:
            parts.append(persona)
        parts.append(
            "You can hand off to another bot by starting a message with "
            "'@<botname> <request>'. Always keep your persona."
        )
        return "\n\n".join(parts)

    def secret_ref(self) -> str:
        return self.auth_ref or self.provider

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "personality": self.personality,
            "provider": self.provider,
            "model": self.model,
            "reasoning": self.reasoning or "",
            "embeddings": self.embeddings or "",
            "auth_ref": self.auth_ref,
            "avatar": self.avatar,
            "title": self.title,
            "color": self.color,
            "dreaming": self.dreaming,
            "private_browser": self.private_browser,
            "caveman": self.caveman,
            "voice_provider": self.voice_provider,
            "elevenlabs_voice_id": self.elevenlabs_voice_id,
        }


@dataclass
class Roster:
    bots: list[Bot] = field(default_factory=list)

    def names(self) -> list[str]:
        return [b.name for b in self.bots]

    def get(self, name: str) -> Bot:
        for b in self.bots:
            if b.name == name:
                return b
        raise RosterError(f"No bot named {name!r}. Known: {', '.join(self.names())}")

    def match(self, query: str, *, exclude: str | None = None) -> list[Bot]:
        """Bots matching a slug, display name, title, or role (case-insensitive).

        Exact identity wins; otherwise a unique substring. Used so a user can
        say "ask Cloud Engineer" without knowing the hyphenated slug.
        """
        raw = (query or "").strip()
        if not raw:
            return []
        pool = [b for b in self.bots if b.name != exclude]
        if not pool:
            return []
        slug = bot_slug(raw)
        low = raw.lower()

        def _keys(bot: Bot) -> list[str]:
            return [
                bot.name.lower(),
                bot.display_name().lower(),
                (bot.title or "").strip().lower(),
                (bot.role or "").strip().lower(),
                bot_slug(bot.display_name()),
            ]

        exact = [b for b in pool if low in _keys(b) or slug in _keys(b)]
        if exact:
            return exact
        fuzzy = [
            b
            for b in pool
            if low in b.name.lower() or low in b.display_name().lower() or slug in b.name.lower()
        ]
        return fuzzy

    def __iter__(self):
        return iter(self.bots)

    def __len__(self) -> int:
        return len(self.bots)


def _bot_from_entry(entry: dict, seen: set[str]) -> Bot:
    name = entry.get("name")
    if not name:
        raise RosterError(f"A bot entry is missing 'name': {entry!r}")
    if not valid_bot_name(name):
        raise RosterError(f"Bot name {name!r} is not a valid path component")
    if name in seen:
        raise RosterError(f"Duplicate bot name: {name!r}")
    seen.add(name)
    return Bot(
        name=name,
        role=entry.get("role", ""),
        personality=entry.get("personality", ""),
        provider=entry.get("provider", "echo"),
        model=entry.get("model"),
        reasoning=str(entry.get("reasoning") or "").strip(),
        embeddings=str(entry.get("embeddings") or "").strip(),
        auth_ref=entry.get("auth_ref"),
        avatar=entry.get("avatar", "robot"),
        title=entry.get("title", "") or "",
        color=entry.get("color", "") or "",
        dreaming=bool(entry.get("dreaming", entry.get("idle_think", False))),
        private_browser=bool(entry.get("private_browser", False)),
        caveman=caveman_flag(entry.get("caveman")),
        voice_provider=voice_override(entry.get("voice_provider")),
        elevenlabs_voice_id=voice_override(entry.get("elevenlabs_voice_id"), voice=True),
    )


def caveman_flag(value) -> bool | None:
    """Parse a Caveman override from the API or a roster file.

    JSON true/false/null and TOML booleans are the canonical forms; the
    strings on/off/true/false/yes/no (and "" / "inherit" / "default" for
    unset) are accepted so a hand-edited roster.toml reads naturally.
    """
    if value is None or isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("", "inherit", "default", "account", "null", "none"):
        return None
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise RosterError(f"caveman={value!r} is not on / off / unset")


def save_roster(path: Path | str, roster: Roster) -> None:
    """Persist the roster as JSON (the UI-managed source of truth)."""
    p = Path(path)
    write_atomic(
        p,
        json.dumps({"bots": [b.to_dict() for b in roster.bots]}, ensure_ascii=False, indent=2),
    )


def load_roster(path: Path | str) -> Roster:
    p = Path(path)
    if not p.is_file():
        raise RosterError(f"Roster file not found: {p}")

    if p.suffix == ".json":
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RosterError(f"Invalid JSON in {p}: {exc}") from exc
    else:
        try:
            data = tomllib.loads(p.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise RosterError(f"Invalid TOML in {p}: {exc}") from exc

    raw_bots = data.get("bots") or data.get("bot") or []
    if isinstance(raw_bots, dict):  # allow [bots.name] table style
        raw_bots = [{"name": k, **v} for k, v in raw_bots.items()]
    # Empty is valid: a new home has no bots until someone creates one.

    bots: list[Bot] = []
    seen: set[str] = set()
    for entry in raw_bots:
        bots.append(_bot_from_entry(entry, seen))
    return Roster(bots=bots)


def load_live_roster(home: Path | str) -> Roster | None:
    """Roster from a harness home (`roster.json` or `roster.toml`), if present."""
    root = Path(home)
    for name in ("roster.json", "roster.toml"):
        path = root / name
        if path.is_file():
            try:
                return load_roster(path)
            except RosterError:
                continue
    return None


def voice_override(value, *, voice=False) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RosterError("Voice override must be a string or null")
    if voice:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise RosterError("Invalid ElevenLabs voice ID")
    elif value not in {"auto", "grok", "elevenlabs"}:
        raise RosterError("Voice provider must be auto, grok, elevenlabs or null")
    return value
