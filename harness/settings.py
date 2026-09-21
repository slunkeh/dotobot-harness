"""Every `HARNESS_*` tunable, in one place, checked before the harness starts.

Two problems, one fix.

**A typo used to win silently.** Every reader in the tree is shaped like
`isolation/machines.py`'s `_int_env`::

    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default

so `HARNESS_MACHINE_PIDS=51x` and `HARNESS_MACHINE_PIDS` unset are the same
thing: the default, no warning, nothing in a log. An operator who set a limit
and watched it not apply had nowhere to look. Copied from openbot, which
refuses to start on a bad value and **names it** — a malformed policy stops
startup, an allow-list entry containing `*` stops startup and says which entry.

**Nothing was written down.** Of the forty-odd `HARNESS_*` names the code
reads, most appeared in no document. A reference kept beside the code drifts
from it; the fix is for the reference and the validation to be the same object,
so a tunable that is not described here is not validated either, and adding one
means describing it.

`validate_environment()` runs at CLI startup (`harness/cli.py`). It checks
every name below that is actually set and raises `SettingError` naming the
variable, the value it could not use, and what it expected. Individual readers
keep their existing `except ValueError: return default` shape, which is now
genuinely unreachable for anything startup validated rather than a silent
swallow.

`HARNESS_SECRET_*` is deliberately absent: those are credential references
with operator-chosen suffixes, not tunables, and enumerating them here would
mean naming secrets in a file that gets printed. `HARNESS_MCP_OAUTH_*` is
the same shape — per-connector client id/secret
(`HARNESS_MCP_OAUTH_SLACK_CLIENT_ID`) — so it is also not listed.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

BOOL_TRUE = ("1", "true", "yes", "on")
BOOL_FALSE = ("0", "false", "no", "off")


class SettingError(ValueError):
    """A setting this harness will not start with. Names the variable."""


@dataclass(frozen=True)
class Setting:
    name: str
    kind: str  # "int" | "float" | "bool" | "str" | "path"
    default: Any
    help: str
    #: smallest accepted value, for the numeric kinds. A negative timeout or a
    #: zero pid limit parses fine and then behaves as a puzzle at runtime.
    minimum: float | None = None

    def parse(self, raw: str) -> Any:
        text = raw.strip()
        if self.kind == "bool":
            low = text.lower()
            if low in BOOL_TRUE:
                return True
            if low in BOOL_FALSE:
                return False
            raise SettingError(
                f"{self.name}={raw!r} is not a yes/no value; "
                f"use one of {', '.join(BOOL_TRUE)} or {', '.join(BOOL_FALSE)}"
            )
        if self.kind in ("int", "float"):
            try:
                value = int(text) if self.kind == "int" else float(text)
            except ValueError:
                raise SettingError(
                    f"{self.name}={raw!r} is not {'an integer' if self.kind == 'int' else 'a number'}"
                    f" ({self.help}). Unset it to use the default, {self.default!r}."
                ) from None
            if self.minimum is not None and value < self.minimum:
                raise SettingError(
                    f"{self.name}={raw!r} is below the minimum of {self.minimum} ({self.help})"
                )
            return value
        if not text:
            raise SettingError(f"{self.name} is set but empty ({self.help}). Unset it instead.")
        return text


#: The tunables. Adding a `HARNESS_*` read to the tree means adding it here —
#: `tests/test_settings.py` walks the source and fails on anything missing.
SETTINGS: tuple[Setting, ...] = (
    # -- where things live ------------------------------------------------
    Setting("HARNESS_HOME", "path", "./shared", "shared state directory"),
    Setting("HARNESS_ROOT", "path", "", "repo/install root, set by the installer"),
    Setting("HARNESS_TOKEN", "str", "", "API linking key; overrides $HARNESS_HOME/link-key"),
    Setting(
        "HARNESS_PUBLIC_URL", "str", "", "public HTTPS origin for link codes and OAuth callbacks"
    ),
    Setting("HARNESS_PUSH_RELAY_URL", "str", "", "optional HTTPS notification relay; overrides push-relay.json"),
    Setting("HARNESS_BOT", "str", "", "bot name, set by the orchestrator on each agent"),
    # -- the model turn ---------------------------------------------------
    Setting("HARNESS_HISTORY_TOKENS", "int", 8000, "per-turn history budget", minimum=0),
    Setting(
        "HARNESS_ROOM_TRANSCRIPT_TOKENS",
        "int",
        3000,
        "group-chat transcript budget replayed on a member's turn",
        minimum=0,
    ),
    Setting("HARNESS_COMPACTION_TURNS", "int", 120, "turns kept before compaction", minimum=1),
    Setting("HARNESS_SUMMARY_CHAIN", "int", 4, "summaries kept before an epoch fold", minimum=2),
    Setting("HARNESS_EMBED_TIMEOUT", "float", 10.0, "embedding HTTP timeout, s", minimum=1),
    Setting(
        "HARNESS_EXTERNAL_MAX_CHARS",
        "int",
        64000,
        "chars of untrusted external content per wrapped tool result",
        minimum=1,
    ),
    # -- dreaming ---------------------------------------------------------
    Setting("HARNESS_DREAM_MIN_SECS", "float", 900.0, "first dream gap, s", minimum=60),
    Setting("HARNESS_DREAM_MAX_SECS", "float", 21600.0, "dream backoff ceiling, s", minimum=60),
    Setting("HARNESS_DREAM_TOKENS", "int", 150000, "daily dream-turn token allowance", minimum=0),
    Setting(
        "HARNESS_IDLE_MIN_SECS",
        "float",
        900.0,
        "legacy alias of HARNESS_DREAM_MIN_SECS",
        minimum=60,
    ),
    Setting(
        "HARNESS_IDLE_MAX_SECS",
        "float",
        21600.0,
        "legacy alias of HARNESS_DREAM_MAX_SECS",
        minimum=60,
    ),
    Setting(
        "HARNESS_IDLE_TOKENS", "int", 150000, "legacy alias of HARNESS_DREAM_TOKENS", minimum=0
    ),
    Setting("HARNESS_STREAM_DELAY", "float", 0.02, "per-chunk streaming pause, s", minimum=0),
    Setting("HARNESS_ACK_IDLE", "float", 0, "idle seconds before an ack card", minimum=0),
    # -- the scheduler ----------------------------------------------------
    Setting("HARNESS_RUN_WATCHDOG_SECS", "float", 120.0, "wedged-turn watchdog, s", minimum=0),
    Setting("HARNESS_RUN_GRACE_SECS", "float", 30.0, "grace before a run is escaped, s", minimum=0),
    Setting(
        "HARNESS_RUN_SILENCE_SECS",
        "float",
        300.0,
        "silence before a run is treated as stalled, s (0 disables)",
        minimum=0,
    ),
    Setting("HARNESS_STOP_GRACE", "float", 10.0, "SIGTERM→SIGKILL grace, s", minimum=0),
    # -- isolation / machines ---------------------------------------------
    Setting("HARNESS_MACHINE", "bool", False, "set inside a bot machine image"),
    Setting("HARNESS_MACHINE_NAME", "str", "", "this bot's machine, set by the backend"),
    Setting("HARNESS_MACHINE_IMAGE", "str", "agent-harness-machine", "bot machine image"),
    Setting(
        "HARNESS_MACHINE_PREFIX",
        "str",
        "harness-machine",
        "docker name prefix so two homes on one daemon do not collide",
    ),
    Setting("HARNESS_MACHINE_POOL", "int", 0, "machines kept warm", minimum=0),
    Setting("HARNESS_MACHINE_PIDS", "int", 512, "per-machine pid limit", minimum=1),
    Setting("HARNESS_MACHINE_MEMORY", "str", "", "per-machine memory cap, e.g. 2g"),
    Setting("HARNESS_MACHINE_CPUS", "str", "", "per-machine cpu cap, e.g. 1.5"),
    Setting("HARNESS_MACHINE_WORKSPACE", "bool", True, "mount the shared /workspace volume"),
    Setting("HARNESS_MACHINE_GEOMETRY", "str", "1280x800x24", "machine Xvfb geometry"),
    Setting("HARNESS_MACHINE_SECRETS", "str", "", "comma-separated credentials to stage in"),
    Setting("HARNESS_MACHINE_SYNC_INTERVAL", "int", 300, "state-sync interval, s", minimum=1),
    Setting(
        "HARNESS_BROWSER_IDLE_MINUTES",
        "int",
        30,
        "close a machine's Chrome after this many idle minutes (0 = never)",
        minimum=0,
    ),
    Setting(
        "HARNESS_BROWSER_IDLE_SWEEP_INTERVAL",
        "int",
        120,
        "idle-browser sweep period, s (0 = off)",
        minimum=0,
    ),
    Setting(
        "HARNESS_MACHINE_SYNC_MAX_FILE",
        "int",
        1 << 30,
        "largest file the state sync carries, bytes (0 = no cap)",
        minimum=0,
    ),
    Setting(
        "HARNESS_MACHINE_SYNC_MAX_TAR",
        "int",
        8 << 30,
        "most bytes one machine snapshot may write to the host, bytes (0 = no cap)",
        minimum=0,
    ),
    Setting("HARNESS_MACHINE_STOP_TIMEOUT", "int", 30, "machine stop timeout, s", minimum=1),
    Setting(
        "HARNESS_FETCH_ALLOW_PRIVATE",
        "bool",
        False,
        "let preview_link / post_image fetch RFC1918 LAN addresses from the host",
    ),
    Setting(
        "HARNESS_HTTP_TIMEOUT",
        "float",
        30.0,
        "idle limit for one HTTP request's line/headers/body, s",
        minimum=1.0,
    ),
    Setting("HARNESS_MACHINE_QUIESCE_TIMEOUT", "int", 15, "quiesce timeout, s", minimum=1),
    Setting(
        "HARNESS_MACHINE_DISPLAY_WAIT",
        "float",
        20.0,
        "wait for a machine's X display on spawn, s",
        minimum=0,
    ),
    Setting("HARNESS_GENERATION_TOKEN", "str", "", "machine generation token, set by the backend"),
    Setting("HARNESS_CONTAINER_ENGINE", "str", "", "docker or podman; auto-detected when unset"),
    Setting("HARNESS_CONTAINER_IMAGE", "str", "dotobot", "container-backend image"),
    Setting("HARNESS_SHARED_DISPLAY", "bool", True, "this bot drives the shared host display"),
    # -- the desktop / computer -------------------------------------------
    Setting(
        "HARNESS_DESKTOP_STYLE",
        "str",
        "machine",
        "desktop look: machine (field wallpaper + dock) or host (full taskbar)",
    ),
    Setting("HARNESS_BROWSER", "str", "", "browser binary override"),
    Setting("HARNESS_BROWSER_MAXIMIZE", "bool", True, "maximize the browser on open"),
    Setting("HARNESS_BROWSER_NO_SANDBOX", "bool", False, "pass --no-sandbox to Chromium"),
    Setting(
        "HARNESS_CHROME_CDP",
        "bool",
        False,
        "opt-in Chrome AX tree + node clicks over loopback CDP; off for profile sign-in compatibility; relaunch Chrome after changing",
    ),
    Setting(
        "HARNESS_CHROME_CDP_BUDGET",
        "float",
        2.0,
        "seconds a screenshot waits for the Chrome AX snapshot before going without it",
        minimum=0,
    ),
    Setting(
        "HARNESS_LOOP_IMAGES",
        "int",
        3,
        "screenshot frames kept in the in-flight tool loop; older frames are dropped",
        minimum=1,
    ),
    Setting("HARNESS_TERMINAL", "str", "", "terminal emulator override"),
    Setting("HARNESS_FILE_MANAGER", "str", "", "file manager override"),
    Setting("HARNESS_WALLPAPER", "path", "", "desktop wallpaper image"),
    Setting("HARNESS_SCREENSHOT_CMD", "str", "", "shell command writing PNG bytes to stdout"),
    Setting("HARNESS_SCREENSHOT_TTL_HOURS", "float", 1.5, "staged screenshot TTL, h", minimum=0),
    Setting("HARNESS_SCREENSHOT_SWEEP_INTERVAL", "int", 0, "screenshot sweep, s", minimum=0),
    # -- per-server log stream (harness/logstream.py) ---------------------
    Setting(
        "HARNESS_SERVER_LOG_MAX_BYTES",
        "int",
        5 * 1024 * 1024,
        "run/server/serve.log rotates once past this size, bytes",
        minimum=64 * 1024,
    ),
    Setting("HARNESS_LOG_POLL_INTERVAL", "float", 0.25, "log follower poll, s", minimum=0.05),
    Setting("HARNESS_LOG_HEARTBEAT", "float", 15.0, "quiet log stream heartbeat, s", minimum=1),
    Setting("HARNESS_REPORTS_KEEP", "int", 200, "problem reports kept (oldest pruned)", minimum=1),
    Setting(
        "HARNESS_TEACH_SAMPLE_SECS",
        "float",
        1.0,
        "teach-recording still interval when ffmpeg cannot run, s",
        minimum=0.05,
    ),
    # -- updates ----------------------------------------------------------
    Setting("HARNESS_AUTO_ROLL", "bool", False, "roll agents automatically on update"),
    Setting("HARNESS_ROLL_SPACING", "float", 0.0, "seconds between agent rolls", minimum=0),
    Setting("HARNESS_RELEASE_ROOT", "path", "/opt/harness", "mounted immutable release directory for the persistent controller"),
    Setting("HARNESS_RELEASE_MANIFEST", "str", "", "release manifest URL"),
    Setting(
        "HARNESS_RELEASE_ALLOW_INSECURE",
        "bool",
        False,
        "let the fleet updater fetch the manifest/tarball over file:// or http://",
    ),
    # -- the shell --------------------------------------------------------
    Setting("HARNESS_SHELL_ENV", "str", "", "extra env names a bot's shell inherits"),
)

BY_NAME: dict[str, Setting] = {s.name: s for s in SETTINGS}


def validate_environment(env: Mapping[str, str] | None = None) -> None:
    """Check every known setting that is set. Raises `SettingError`, named.

    Unset is always fine — a default is a decision somebody already made.
    Only a value that IS present and cannot be used stops startup, because
    that is the case where the operator believes something is in effect and
    it is not.
    """
    source = os.environ if env is None else env
    for setting in SETTINGS:
        raw = source.get(setting.name)
        if raw is None:
            continue
        setting.parse(raw)


def describe() -> str:
    """The configuration reference, generated from the same objects that do the
    validating, so it cannot drift from them."""
    width = max(len(s.name) for s in SETTINGS)
    lines = ["Environment settings (unset = the default in brackets):", ""]
    for setting in SETTINGS:
        default = "" if setting.default in ("", None) else f"  [{setting.default}]"
        lines.append(f"  {setting.name:<{width}}  {setting.help}{default}")
    return "\n".join(lines)
