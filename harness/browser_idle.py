"""Close a bot's browser once nobody has driven its computer for a while.

Chrome is the cost that decides how many bots fit on a host: an idle instance
holds 300-500 MB, and on the machines backend one lives inside every machine
whose bot ever called `computer_open`. Bringup never starts Chrome (the dock
waits for the bot or the user), so launch is already lazy; this module is the
other half — a serve-loop sweep that quiesces Chrome inside a machine whose
bot has not touched its computer for `HARNESS_BROWSER_IDLE_MINUTES` (default
30; 0 disables).

The clock is one file per bot, `run/<bot>.computer-active`, touched by every
computer action the bot takes (`agent.computer.HostComputer`) and by every
human input event on its screen (`harness.server` WS `input`). Absence means
"never used": nothing to close. A bot whose control a human holds (takeover /
teach) or with an open user decision is never swept — the browser may hold
a login, context or draft needed after the person answers. Closing goes through
`MachineBackend.close_browser`, which merges the machine's state up first so
the logins Chrome wrote are in the canonical store before the process dies;
the next `computer_open` relaunches on the same profile.

Process backend: not swept. Its per-bot host Chromes share one display and
one host; the tenant path off it is cloud/host/migrate-to-machines.sh.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable

from .paths import HarnessPaths

DEFAULT_IDLE_MINUTES = 30
DEFAULT_SWEEP_INTERVAL = 120


def idle_minutes() -> int:
    try:
        return max(0, int(os.environ.get("HARNESS_BROWSER_IDLE_MINUTES", DEFAULT_IDLE_MINUTES)))
    except ValueError:
        return DEFAULT_IDLE_MINUTES


def sweep_interval() -> int:
    try:
        return max(
            0, int(os.environ.get("HARNESS_BROWSER_IDLE_SWEEP_INTERVAL", DEFAULT_SWEEP_INTERVAL))
        )
    except ValueError:
        return DEFAULT_SWEEP_INTERVAL


def touch(paths: HarnessPaths, bot: str, now: float | None = None) -> None:
    """Record that someone drove `bot`'s computer just now. Never raises —
    a bookkeeping failure must not fail a computer action."""
    if not bot:
        return
    path = paths.computer_activity_file(bot)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8"):
            pass
        if now is not None:
            os.utime(path, (now, now))
        else:
            os.utime(path, None)
    except OSError:
        return


def last_activity(paths: HarnessPaths, bot: str) -> float | None:
    """When the computer was last driven, or None if never."""
    try:
        return paths.computer_activity_file(bot).stat().st_mtime
    except OSError:
        return None


def is_idle(paths: HarnessPaths, bot: str, *, limit_s: float, now: float | None = None) -> bool:
    """True when the bot HAS used its computer and has not for `limit_s`."""
    last = last_activity(paths, bot)
    if last is None:
        return False
    return ((time.time() if now is None else now) - last) >= limit_s


def sweep(
    paths: HarnessPaths,
    bots: Iterable[str],
    *,
    close_browser: Callable[[str], bool],
    control_held: Callable[[str], bool],
    limit_s: float,
    now: float | None = None,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    """One pass: close the browser of every idle, un-held bot. Returns the
    bots whose browser was actually closed. A failure on one bot never stops
    the pass."""
    closed: list[str] = []
    from agent.streaming import list_prompts

    for bot in bots:
        try:
            if not is_idle(paths, bot, limit_s=limit_s, now=now):
                continue
            if control_held(bot):
                continue
            # A decision can take hours; the browser still contains the
            # context or draft the bot must use once the person answers.
            if list_prompts(paths, bot):
                continue
            if close_browser(bot):
                closed.append(bot)
                # the closed browser is not "activity", but resetting the clock
                # keeps the sweep from re-probing an idle machine every tick
                touch(paths, bot, now=now)
                if log:
                    log(f"browser-idle: closed {bot}'s browser after {int(limit_s // 60)}m idle")
        except Exception as exc:  # noqa: BLE001 - a sick machine must not stop the pass
            if log:
                log(f"browser-idle: {bot}: {exc}")
    return closed
