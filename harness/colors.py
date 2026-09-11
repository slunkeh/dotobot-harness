"""Stable per-bot colors shared by the API and clients.

The Mac avatar palette is hashed from the bot name. Swift's `hashValue` is
not stable across launches, so we use djb2 on the lowercased name and keep
the same hex swatches on both sides.
"""

from __future__ import annotations

PALETTE = (
    "#EF476F",  # Bubblegum Pink
    "#FFD166",  # Royal Gold
    "#06D6A0",  # Emerald
    "#118AB2",  # Ocean Blue
    "#073B4C",  # Dark Teal
)

#: The full picker palette (`BotCharacterEditor.swatches` on the clients,
#: kept in lockstep by tests/test_recipes.py): the five hashed defaults,
#: four more brights, then neutrals. Bots keep hashing over PALETTE so no
#: existing bot changes colour; recipes hash over all twelve so the gallery
#: shows the whole range instead of five.
SWATCHES = PALETTE + (
    "#9B5DE5",  # Violet
    "#F15BB5",  # Orchid
    "#FF8C42",  # Tangerine
    "#4CC9F0",  # Sky
    "#1C1C1E",  # Ink
    "#8D6E63",  # Cocoa
    "#8E8E93",  # Slate
)


def _djb2(name: str) -> int:
    h = 5381
    for byte in name.lower().encode("utf-8"):
        h = ((h * 33) + byte) & 0xFFFFFFFF
    return h


def bot_color(name: str) -> str:
    """Return a `#RRGGBB` swatch for `name` (case-insensitive, stable)."""
    return PALETTE[_djb2(name) % len(PALETTE)]


def recipe_color(recipe_id: str) -> str:
    """A recipe's swatch, stable per id, drawn from the full picker palette.
    Install copies it onto the bot, so the bot keeps the card's colour."""
    return SWATCHES[_djb2(recipe_id) % len(SWATCHES)]
