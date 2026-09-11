"""The account owner's avatar: a "persona" — a hand-drawn head-and-shoulders
mark in the spirit of DiceBear's Personas style (a remix of Draftbit's
Personas, CC BY 4.0 — https://www.dicebear.com/styles/personas/). The
clients redraw it from scratch in SwiftUI; this module owns the *vocabulary*
so every client and the harness agree on what a persona string means.

A persona is one string on the account prefs (`user_avatar`)::

    persona:skin=2;hair=bobCut;hairColor=1;eyes=open;mouth=smile;
            facialHair=none;nose=smallRound;body=rounded;clothing=3

Colours are palette *indexes* (the palettes below are shared with the Swift
`Persona` model), parts are named after their DiceBear counterparts. An
account gets a random persona the first time its prefs are read
(`harness/prefs.py`), and the owner can pick every part in the apps.
"""

from __future__ import annotations

import random
import re
from collections.abc import Mapping

PREFIX = "persona:"
#: The owner uploaded a photo instead: `photo:<stored upload basename>`
#: (a file in `workspace/uploads`, served by `GET /api/uploads/<name>`).
PHOTO_PREFIX = "photo:"
_PHOTO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")

#: Palettes, in the DiceBear order. Index into these; never store hex.
SKIN_COLORS = ("EEB4A4", "E7A391", "E5A07E", "D78774", "B16A5B", "92594B", "623D36")
HAIR_COLORS = ("362C47", "6C4545", "E15C66", "E16381", "F27D65", "F29C65", "DEE1F5")
CLOTHING_COLORS = ("456DFF", "54D7C7", "7555CA", "6DBB58", "E24553", "F3B63A", "F55D81")

#: Named parts, in the order the apps list them.
HAIR = (
    "bald",
    "balding",
    "buzzcut",
    "shortCombover",
    "bobCut",
    "bobBangs",
    "long",
    "curly",
    "straightBun",
    "mohawk",
    "pigtails",
    "cap",
)
EYES = ("open", "happy", "wink", "sleep", "glasses", "sunglasses")
MOUTH = ("smile", "bigSmile", "smirk", "lips", "surprise", "frown")
FACIAL_HAIR = ("none", "shadow", "goatee", "beardMustache", "walrus", "soulPatch")
NOSE = ("smallRound", "mediumRound")
BODY = ("rounded", "squared", "small", "checkered")

#: field -> allowed values. Colour fields take an index into their palette.
FIELDS: dict[str, tuple[str, ...]] = {
    "skin": tuple(str(i) for i in range(len(SKIN_COLORS))),
    "hair": HAIR,
    "hairColor": tuple(str(i) for i in range(len(HAIR_COLORS))),
    "eyes": EYES,
    "mouth": MOUTH,
    "facialHair": FACIAL_HAIR,
    "nose": NOSE,
    "body": BODY,
    "clothing": tuple(str(i) for i in range(len(CLOTHING_COLORS))),
}

#: Chance a random persona gets facial hair (DiceBear's default is 10%).
FACIAL_HAIR_PROBABILITY = 0.15


class PersonaError(ValueError):
    """Not a persona string this harness understands."""


def parse(text: str) -> dict[str, str]:
    """`persona:k=v;…` -> {field: value}. Every field present, every value
    allowed, nothing unknown — or `PersonaError` naming the problem."""
    raw = (text or "").strip()
    if not raw.startswith(PREFIX):
        raise PersonaError("a persona string starts with 'persona:'")
    out: dict[str, str] = {}
    for pair in raw[len(PREFIX) :].split(";"):
        pair = pair.strip()
        if not pair:
            continue
        key, sep, value = pair.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in FIELDS:
            raise PersonaError(f"unknown persona field {key!r}")
        if value not in FIELDS[key]:
            raise PersonaError(f"persona {key} cannot be {value!r}")
        out[key] = value
    missing = [k for k in FIELDS if k not in out]
    if missing:
        raise PersonaError(f"persona is missing {', '.join(missing)}")
    return out


def encode(parts: Mapping[str, str]) -> str:
    """{field: value} -> canonical `persona:` string (validated)."""
    text = PREFIX + ";".join(f"{k}={parts[k]}" for k in FIELDS if k in parts)
    parse(text)
    return text


def normalize(text: str) -> str:
    """Canonical field order and spacing for a valid persona string."""
    return encode(parse(text))


def random_persona(rng: random.Random | None = None) -> str:
    """A fresh persona — what a new account gets before its owner picks."""
    r = rng or random.SystemRandom()
    parts = {field: r.choice(values) for field, values in FIELDS.items()}
    if r.random() >= FACIAL_HAIR_PROBABILITY:
        parts["facialHair"] = "none"
    return encode(parts)


def is_persona(text: str | None) -> bool:
    try:
        parse(text or "")
    except PersonaError:
        return False
    return True


def photo_name(text: str | None) -> str | None:
    """The upload basename behind a `photo:` avatar, or None if it is not one.
    A basename only — no path separators, nothing hidden — so the value can
    never point outside `workspace/uploads`."""
    raw = (text or "").strip()
    if not raw.startswith(PHOTO_PREFIX):
        return None
    name = raw[len(PHOTO_PREFIX) :].strip()
    if not _PHOTO_NAME.match(name) or ".." in name:
        return None
    return name


def normalize_avatar(text: str | None) -> str:
    """Canonical form of either kind of owner avatar, or `PersonaError`."""
    if (name := photo_name(text)) is not None:
        return PHOTO_PREFIX + name
    return normalize(text or "")


def is_avatar(text: str | None) -> bool:
    return photo_name(text) is not None or is_persona(text)
