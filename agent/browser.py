"""Browser profile split: shared logins, isolated sessions.

Locked product model: a browser lives *inside each bot environment*. The
cookie/login jar is SHARED (a Google login on bot A is visible to bot B), while
each bot's session/cache/open tabs stay PRIVATE. We must never point two live
Chromium instances at the same `user-data-dir` — that fights over SingletonLock
and leaks tabs.

The split:
* Per-identity `user-data-dir` (private): SingletonLock, cache, sessions,
  window state, open tabs — `browser/sessions/<bot>` (the human desktop
  browser uses the `desktop` identity).
* Only the login-bearing files are shared, symlinked from the shared jar
  (`browser/cookies`) into each identity's `Default/` profile:
      Cookies, Login Data, Web Data (and their -journal siblings)
* A write on any identity updates the shared jar; a new login propagates.

`plan_profile_split()` computes the layout (pure); `ensure_profile_split()`
creates it on disk and is called by the desktop bringup and by bots opening a
browser. Concurrent jar writers (two live Chromiums) can race with one another — not yet
handled.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from harness.paths import HarnessPaths

# Chromium profile dir inside a bot machine's home (machines backend). It is
# a REAL profile — no symlink jar: login sharing rides the machine-state
# clone/merge sync instead, so no two live Chromiums ever touch one file
# (which is what finally resolves the shared-profile race in machine mode).
MACHINE_CHROME_DIR = ".config/harness-chrome"


def machine_mode() -> bool:
    """True in a machine context: inside a bot machine (HARNESS_MACHINE=1,
    baked into the image) or in an agent host process driving one
    (HARNESS_MACHINE_NAME, set by the machines backend)."""
    return bool(os.environ.get("HARNESS_MACHINE") or os.environ.get("HARNESS_MACHINE_NAME"))


def machine_profile_dir(home: Path | None = None) -> Path:
    """The in-machine Chrome profile dir (part of the synced home)."""
    return (home or Path.home()) / MACHINE_CHROME_DIR


# Chromium profile files that carry logins/cookies — these are the SHARED ones.
# "Login Data For Account" holds passwords saved while signed into Chrome
# itself (account-bound store, split from "Login Data" since M79) — without it
# a signed-in profile's remembered logins don't travel.
SHARED_PROFILE_FILES = [
    "Cookies",
    "Cookies-journal",
    "Login Data",
    "Login Data-journal",
    "Login Data For Account",
    "Login Data For Account-journal",
    "Web Data",
    "Web Data-journal",
]


@dataclass
class BrowserPlan:
    bot: str
    private_user_data_dir: Path
    shared_cookie_jar: Path
    shared_files: list[str] = field(default_factory=lambda: list(SHARED_PROFILE_FILES))

    def symlink_map(self) -> dict[Path, Path]:
        """link_path -> target_in_shared_jar for each shared login file."""
        default_profile = self.private_user_data_dir / "Default"
        return {default_profile / name: self.shared_cookie_jar / name for name in self.shared_files}

    def describe(self) -> str:
        lines = [
            f"bot: {self.bot}",
            f"private user-data-dir (isolated): {self.private_user_data_dir}",
        ]
        if self.shared_files:
            lines += [
                f"shared cookie/login jar:          {self.shared_cookie_jar}",
                "shared (symlinked into Default/):",
                *(f"  {name}" for name in self.shared_files),
            ]
        else:
            lines.append("cookie/login jar: PRIVATE (not shared — private_browser)")
        lines.append("isolated: SingletonLock, Cache, Sessions, tabs, window state")
        return "\n".join(lines)


def plan_profile_split(paths: HarnessPaths, bot: str, *, shared_logins: bool = True) -> BrowserPlan:
    """Compute the shared-vs-private Chromium layout for a bot (no side effects).

    `shared_logins=False` (the roster's `private_browser` flag) opts this bot
    out of the shared jar entirely: no login files are symlinked, Chrome
    creates fresh private DBs on first write, and a web page injected into
    this bot's browser has no ambient logins to spend.
    """
    return BrowserPlan(
        bot=bot,
        private_user_data_dir=paths.bot_session(bot),
        shared_cookie_jar=paths.browser_cookies,
        shared_files=list(SHARED_PROFILE_FILES) if shared_logins else [],
    )


def ensure_profile_split(plan: BrowserPlan) -> None:
    """Materialize the split on disk: private profile + jar symlinks.

    Idempotent, run before every launch. An existing regular login file whose
    jar counterpart does not exist yet is adopted into the jar (that identity's
    logins become the shared ones); a divergent regular file is never
    clobbered. Dangling symlinks are fine — Chromium/SQLite create the jar file
    through the link on first write. Note SQLite unlinks `-journal` files,
    which removes the symlink itself, so journal sharing is best-effort and
    re-linked here on the next launch. Concurrent writers to the shared jar
    can race with one another.
    """
    default_profile = plan.private_user_data_dir / "Default"
    default_profile.mkdir(parents=True, exist_ok=True)
    if not plan.shared_files:
        # private_browser: nothing shared. A bot that shared before the flag
        # flipped still has jar symlinks in Default/ from earlier launches —
        # remove those links (never the jar's own files) so private means
        # private from this launch on, not only for bots that never shared.
        for name in SHARED_PROFILE_FILES:
            link = default_profile / name
            try:
                if (
                    link.is_symlink()
                    and link.resolve() == (plan.shared_cookie_jar / name).resolve()
                ):
                    link.unlink()
            except OSError:
                pass
        return
    plan.shared_cookie_jar.mkdir(parents=True, exist_ok=True)
    for link, target in plan.symlink_map().items():
        if link.is_symlink():
            continue
        if link.exists():
            if target.exists():
                continue  # divergent real file: leave it alone
            link.replace(target)  # adopt this profile's file into the jar
        link.symlink_to(target)


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401

        return True
    except Exception:
        return False


def launch_isolated_session(plan: BrowserPlan, *, headless: bool = True):  # pragma: no cover
    """Optional: launch an isolated Chromium session for `plan`.

    Guarded behind Playwright availability so the core harness has no browser
    dependency. This proves *session isolation*; cookie *sharing* uses a separate profile.
    """
    if not playwright_available():
        raise RuntimeError(
            "Playwright not installed. `pip install playwright && playwright install chromium` "
            "to run the optional browser session spike."
        )
    from playwright.sync_api import sync_playwright

    plan.private_user_data_dir.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        user_data_dir=str(plan.private_user_data_dir),
        headless=headless,
    )
    return pw, context
