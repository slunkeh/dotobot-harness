"""Channel abstraction.

A *channel* is a user-facing interface to the harness. The bots, bus, streaming,
and control protocol are channel-agnostic, so new front-ends are adapters:

* ``desktop`` — a local Tkinter desktop app (the initial interface). Implemented.
* ``cli``     — the existing terminal commands. Implemented in ``harness`` CLI.
* ``telegram`` / ``slack`` / ``discord`` — planned; each would translate its
  events to the same ``chat_stream`` + ``Control`` calls the desktop app uses.

Keeping the desktop app thin over ``DesktopViewModel`` means those chat-platform
channels reuse the same core without a rewrite.
"""

from __future__ import annotations

#: name -> short status, surfaced by `harness channels`.
CHANNELS: dict[str, str] = {
    "desktop": "implemented (Tkinter local app)",
    "cli": "implemented (harness chat / --stream)",
    "telegram": "planned",
    "slack": "planned",
    "discord": "planned",
}


def available_channels() -> dict[str, str]:
    return dict(CHANNELS)
