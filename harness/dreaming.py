"""Opt-in dreaming: bounded self-directed turns for quiet bots.

A bot with `dreaming = true` in the roster gets a dream turn when nobody has
messaged it for a while — the machine analogue of what a person does when
idle: consolidate what happened, reflect on what could have gone better, and
daydream a little about what would help next. The harness drops a
background-lane message in its inbox (origin="dream") so the existing turn
loop picks it up — the same delivery routines use, so nothing about the
runtime's scheduling, watchdogs, or takeover changes. Three bounds keep an
unattended bot cheap and safe:

* **Backoff** — the gap starts at $HARNESS_DREAM_MIN_SECS (default 15 min)
  and doubles after every dream up to $HARNESS_DREAM_MAX_SECS (default 6 h);
  any real (non-dream) message resets it to the minimum.
* **Budget** — dreams stop for the day once the bot's dream turns have spent
  $HARNESS_DREAM_TOKENS input+output tokens (default 150k) since local
  midnight, read from the usage ledger.
* **The gate** — dream turns carry origin="dream" through the tool loop, and
  `agent/govern.py` holds side-effect intents (connector writes, secrets,
  manage) back unless the operator wrote a policy rule for them.

This shipped one release earlier as "idle think" (`idle_think`, origin
"idle", the HARNESS_IDLE_MIN_SECS / HARNESS_IDLE_MAX_SECS /
HARNESS_IDLE_TOKENS names); every legacy spelling is still accepted — the
roster flag, the env vars, the wire origin, and the old state file.

Dreams fire only under `harness serve` (like routines), never while the bot
is paused by a takeover or already busy.
"""

from __future__ import annotations

import heapq
import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent import messaging
from harness.control import Control
from harness.paths import HarnessPaths
from harness.usage import tokens_today

_DEFAULT_MIN_SECS = 900.0  # 15 minutes
_DEFAULT_MAX_SECS = 21_600.0  # 6 hours
_DEFAULT_TOKENS = 150_000
#: how many newest inbox/archive files to inspect for the last real message
_ACTIVITY_SCAN = 50

DREAM_PROMPT = (
    "[Dreaming — nobody has messaged you for a while]\n"
    "This is a dream turn: unattended, reflective, low-stakes. Spend it in "
    "three moves.\n"
    "1. Consolidate: skim what happened recently (your memory, session "
    "recall) and distill anything durable — a fact, a preference, a lesson — "
    "into memory with `remember`.\n"
    "2. Reflect: what went well, what you would do differently next time. If "
    "it changes who you are, revise your soul in a small way; if a repeated "
    "pattern deserves a procedure, draft it with `propose_skill`.\n"
    "3. Aspire: think ahead for your user — what would genuinely help them "
    "next. Keep the idea as a `remember` note to surface when they next "
    "message you; do not message them now.\n"
    "Side-effect tools are held back on this unattended turn, so do not try "
    "to work around a refusal — note the intent instead. Do not open secret "
    "or choice boxes. If nothing comes, reply with one short line saying so."
)


def _env_number(name: str, legacy: str, default: float) -> float:
    """Read a tunable by its dream name, then its legacy idle-think name."""
    for key in (name, legacy):
        raw = os.environ.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except ValueError:
            continue
    return default


def intervals() -> tuple[float, float]:
    """(min, max) seconds between dreams; the gap doubles from min to max."""
    lo = max(
        60.0, _env_number("HARNESS_DREAM_MIN_SECS", "HARNESS_IDLE_MIN_SECS", _DEFAULT_MIN_SECS)
    )
    hi = max(lo, _env_number("HARNESS_DREAM_MAX_SECS", "HARNESS_IDLE_MAX_SECS", _DEFAULT_MAX_SECS))
    return lo, hi


def dream_budget() -> int:
    """Daily dream-turn token allowance per bot ($HARNESS_DREAM_TOKENS, 150k)."""
    return max(0, int(_env_number("HARNESS_DREAM_TOKENS", "HARNESS_IDLE_TOKENS", _DEFAULT_TOKENS)))


def wants_dreams(bot: Any) -> bool:
    """The roster opt-in, either spelling (`dreaming`, legacy `idle_think`)."""
    return bool(getattr(bot, "dreaming", False) or getattr(bot, "idle_think", False))


def _state_path(paths: HarnessPaths, bot: str) -> Path:
    return paths.home / "dreams" / f"{bot}.json"


def load_state(paths: HarnessPaths, bot: str) -> dict[str, Any]:
    path = _state_path(paths, bot)
    if not path.is_file():
        # the idle-think release kept its backoff state under idle/
        path = paths.home / "idle" / f"{bot}.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(paths: HarnessPaths, bot: str, state: dict[str, Any]) -> None:
    path = _state_path(paths, bot)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def last_activity(paths: HarnessPaths, bot: str) -> float:
    """ts of the newest non-dream message in the bot's inbox or its processed
    archive; 0.0 when there has never been one. Message filenames sort by
    send time (`messaging.send` prefixes the ts), so only the newest few
    files per folder are read."""
    newest = 0.0
    for folder in (paths.inbox(bot), paths.processed(bot)):
        if not folder.is_dir():
            continue
        # Top-N by name without sorting the whole (never-pruned) archive —
        # this runs once a minute per bot forever.
        for path in heapq.nlargest(_ACTIVITY_SCAN, folder.glob("*.json"), key=lambda p: p.name):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            if str(data.get("origin") or "") in (messaging.ORIGIN_DREAM, messaging.ORIGIN_IDLE):
                continue
            ts = float(data.get("ts") or 0.0)
            newest = max(newest, ts)
            break  # newest-first: the first non-dream file settles this folder
    return newest


def _spent_today(paths: HarnessPaths, bot: str) -> int:
    """Dream-turn tokens since midnight, counting the legacy origin too so an
    upgrade mid-day does not hand the bot a fresh allowance."""
    return tokens_today(paths, bot, origin=messaging.ORIGIN_DREAM) + tokens_today(
        paths, bot, origin=messaging.ORIGIN_IDLE
    )


def fire_due(
    paths: HarnessPaths,
    bots: list[Any],
    *,
    now: float | None = None,
    send: Callable[[str, str], None] | None = None,
    control: Control | None = None,
) -> list[str]:
    """Enqueue a dream for every opted-in bot whose quiet gap elapsed.

    Returns the bots that dreamed. `bots` is the live roster (objects with
    `.name` and `.dreaming`), read fresh each pass so an opt-in from the app
    takes effect without a restart.
    """
    now = time.time() if now is None else float(now)
    control = control or Control(paths)
    lo, hi = intervals()
    fired: list[str] = []
    for bot in bots:
        if not wants_dreams(bot):
            continue
        if getattr(bot, "blocked", False):
            # Blocked (guideline 1.2): no turn of any origin until the owner
            # unblocks it — the same rule `recipients_for` applies to chat.
            continue
        name = bot.name
        state = load_state(paths, name)
        last_tick = float(state.get("last_tick") or 0.0)
        interval = max(lo, min(float(state.get("interval") or lo), hi))
        if not last_tick:
            # First sighting: arm from now rather than firing immediately —
            # opting in should not instantly spend a turn.
            save_state(paths, name, {"last_tick": now, "interval": lo})
            continue
        base = last_tick
        activity = last_activity(paths, name)
        if activity > last_tick:
            interval = lo  # someone was here; the backoff starts over
            base = activity
        if now - base < interval:
            continue
        try:
            if control.state(name).paused or control.is_busy(name):
                continue  # a held or working bot is not idle; re-check next pass
        except Exception:
            continue
        next_state = {"last_tick": now, "interval": min(interval * 2, hi)}
        if _spent_today(paths, name) >= dream_budget():
            # Out of allowance for the day. Stamp the tick anyway so the
            # ledger is not re-read every scheduler pass; dreaming resumes on
            # the same backoff schedule after midnight.
            save_state(paths, name, next_state)
            continue
        if send is not None:
            send(name, DREAM_PROMPT)
        else:
            messaging.send(
                paths,
                messaging.Msg(
                    to=name, frm="user", text=DREAM_PROMPT, origin=messaging.ORIGIN_DREAM
                ),
            )
        save_state(paths, name, next_state)
        fired.append(name)
    return fired


def start_dream_scheduler(
    orch: Any,
    interval: float = 60.0,
    send: Callable[[str, str], None] | None = None,
) -> threading.Thread:
    """Background tick used by `harness serve`. Daemon so process exit is clean."""

    def loop() -> None:
        while True:
            try:
                fire_due(orch.paths, list(orch.roster), send=send)
            except Exception:
                pass
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="dreams", daemon=True)
    thread.start()
    return thread
