"""Bring up the bot "computer": a desktop with a bottom taskbar.

Prepares the display the harness streams so the computer-use view shows a real
desktop — a window manager (openbox), a bottom panel/taskbar (tint2) with
launchers for **Chrome, Thunar, and xterm**, and a wallpaper. Apps are not
auto-opened on a bot machine; the bot or the human launches them from the
dock. Everything degrades gracefully if a tool is missing.

On a real host (Pi / home server / VM) install: openbox tint2 thunar xterm feh
and chromium (or google-chrome). Browsers on Ubuntu are snaps; a normal desktop
host or the container/VM images ship a real chromium.
"""

from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
from pathlib import Path

from .envscrub import scrub_ambient_authority
from .paths import HarnessPaths

BROWSER_CANDIDATES = [
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "epiphany-browser",
    "firefox",
    "surf",
    "netsurf-gtk",
    "dillo",
]
FILE_MANAGERS = ["thunar", "pcmanfm", "nautilus"]
TERMINALS = ["xterm", "xfce4-terminal", "x-terminal-emulator"]
DEFAULT_URL = "https://www.google.com"
# Bundled field photo used as the default bot-machine wallpaper.
_BUNDLED_WALLPAPER = Path(__file__).resolve().parent.parent / "deploy" / "machine-wallpaper.jpg"
# Solid terminal glyph — xterm's own XPM is line-art and vanishes on the field.
_BUNDLED_TERMINAL_ICON = Path(__file__).resolve().parent.parent / "deploy" / "terminal-icon.png"


def product_computer() -> bool:
    """Bot-computer look: field wallpaper + transparent centered dock.

    Default on every VM. Set HARNESS_DESKTOP_STYLE=host for the old
    full-width taskbar (local Pi / shared display only).
    """
    style = os.environ.get("HARNESS_DESKTOP_STYLE", "machine").strip().lower()
    return style not in ("host", "taskbar", "0", "false", "no")


def _which(names: list[str]) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def find_browser() -> str | None:
    return os.environ.get("HARNESS_BROWSER") or _which(BROWSER_CANDIDATES)


def find_file_manager() -> str | None:
    return os.environ.get("HARNESS_FILE_MANAGER") or _which(FILE_MANAGERS)


def find_terminal() -> str | None:
    return os.environ.get("HARNESS_TERMINAL") or _which(TERMINALS)


def _is_chromium(browser: str) -> bool:
    b = os.path.basename(browser)
    return b.startswith("google-chrome") or b.startswith("chromium")


# Google Chrome in the machine jail (cap-drop ALL). --no-sandbox is not
# enough: Chrome's own seccomp/namespace filters SIGTRAP, and crashpad
# aborts if it has no dump dir. Ozone must be X11 (no Wayland).
_CHROME_JAIL_FLAGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-namespace-sandbox",
    "--disable-seccomp-filter-sandbox",
    "--no-zygote",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--ozone-platform=x11",
    "--disable-crash-reporter",
    "--crash-dumps-dir=/tmp",
]
# Opt-in loopback CDP for AX snapshot / node click. Chrome profile
# sign-in uses the normal screenshot + xdotool path without debugging by default.
# 127.0.0.1 only —
# never the container's external interface. Port 0 lets each profile bind
# its own debugger — a shared 9222 loses the second Chrome and can attach
# snapshot/click to someone else's browser. DevToolsActivePort in the
# profile is the attach gate; no port file means computer_* stays
# screenshot+xdotool.
_CHROME_CDP_FLAGS = [
    "--remote-debugging-port=0",
    "--remote-debugging-address=127.0.0.1",
    "--force-renderer-accessibility",
]


def _cdp_flags() -> list[str]:
    """HARNESS_CHROME_CDP=0 keeps the debug port out of new launches too.

    cdp.enabled() already stops the client side; gating the flags here makes
    the kill switch complete for live argv (machine_browser_command /
    browser_command). The dock wrapper cannot bake these — see
    write_chrome_wrapper — or a flip plus relaunch would leave the port open.
    """
    from harness import cdp

    return list(_CHROME_CDP_FLAGS) if cdp.enabled() else []


def machine_browser() -> str:
    """The browser command to run *inside* a bot machine.

    This must resolve on the **machine's** PATH, not the host's. The argv is
    normally built host-side and handed to `docker exec`, so a host-resolved
    absolute path (/usr/bin/google-chrome, or a snap wrapper) need not exist
    in the machine — the bot's browser then fails to open on exactly those
    hosts that have Chrome installed themselves.

    Probing PATH is only correct when we are genuinely inside a machine, which
    `HARNESS_MACHINE` marks. `HARNESS_MACHINE_NAME` means the opposite: a host
    process driving one.
    """
    override = os.environ.get("HARNESS_BROWSER")
    if override:
        return override
    if os.environ.get("HARNESS_MACHINE"):
        return _which(BROWSER_CANDIDATES) or "google-chrome"
    # deploy/Dockerfile.machine installs Google Chrome, with Debian chromium
    # as its build-time fallback. A bare name lets the machine's PATH decide.
    return "google-chrome"


_CHROME_SINGLETON = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def clear_stale_chrome_singleton(profile_dir: str | Path) -> bool:
    """Drop a Chrome profile lock that does not belong to a live process here.

    Clone-down / named volumes keep `.config/harness-chrome` across container
    recreates. Chrome's SingletonLock is `hostname-pid` of the last run, so a
    new container (new hostname) treats the profile as in use on another
    computer, fails to show the dialog in the jail, and paints a grey square
    over the dock instead. Only the lock is removed; cookies stay.
    Returns True when it deleted a stale lock.
    """
    profile = Path(profile_dir)
    lock = profile / "SingletonLock"
    if not lock.exists() and not lock.is_symlink():
        return False
    target = ""
    try:
        target = os.readlink(lock)
    except OSError:
        target = ""
    host, _, pid_s = target.rpartition("-")
    try:
        pid = int(pid_s)
    except ValueError:
        pid = None
    if host == socket.gethostname() and pid is not None and _pid_alive(pid):
        return False
    for name in _CHROME_SINGLETON:
        path = profile / name
        try:
            path.unlink()
        except OSError:
            pass
    return True


def machine_browser_command(url: str, profile_dir: str | Path, *, cdp: bool = True) -> list[str]:
    """Chrome argv for INSIDE a bot machine (machines backend).

    Prefers Google Chrome; Debian Chromium is the fallback. The profile is a
    real dir in the synced home (no symlink jar), and the container is the
    sandbox boundary, hence --no-sandbox. `cdp=False` leaves the CDP flags
    out for callers that add them at run time (the launch-chrome wrapper).
    """
    browser = machine_browser()
    return [
        browser,
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile_dir}",
        "--start-maximized",
        *_CHROME_JAIL_FLAGS,
        *(_cdp_flags() if cdp else []),
        url,
    ]


def browser_command(url: str, profile_dir: Path, *, no_sandbox: bool = False) -> list[str] | None:
    browser = find_browser()
    if not browser:
        return None
    if _is_chromium(browser):
        args = [
            browser,
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={profile_dir}",
        ]
        if os.environ.get("HARNESS_BROWSER_MAXIMIZE", "1") not in ("0", "false", "no"):
            args.append("--start-maximized")
        args += _cdp_flags()
        if no_sandbox or os.environ.get("HARNESS_BROWSER_NO_SANDBOX"):
            args += list(_CHROME_JAIL_FLAGS)
        args.append(url)
        return args
    return [browser, url]


def _desktop_entry(path: Path, name: str, exec_: str, icon: str) -> None:
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={name}\n"
        f"Exec={exec_} %U\n"
        f"Icon={icon}\n"
        "Terminal=false\n",
        encoding="utf-8",
    )


def _icon(candidates: list[str]) -> str:
    """First existing pixmap, else the first name (tint2 Icon=).

    Adwaita in the machine image is SVG-only and librsvg is not installed, so
    theme names like web-browser render as empty diamonds. Prefer raster files
    the packages already ship under pixmaps / hicolor.
    """
    roots = (
        Path("/usr/share/pixmaps"),
        Path("/usr/share/icons/hicolor/48x48/apps"),
        Path("/usr/share/icons/hicolor/32x32/apps"),
    )
    for name in candidates:
        path = Path(name)
        if path.is_file():
            return str(path)
        for root in roots:
            for candidate in (root / name, root / f"{name}.png", root / f"{name}.xpm"):
                if candidate.is_file():
                    return str(candidate)
    return candidates[0]


def write_chrome_wrapper(cfg_dir: Path, chrome_argv: list[str], profile_dir: Path) -> Path:
    """Shell wrapper the dock (and bot open) run instead of Chrome directly.

    Clears a stale SingletonLock, then execs Chrome. Written inside the
    machine so a tint2 click does not have to go through the host Python.

    `chrome_argv` should not carry the CDP flags: the script is baked once
    at bringup, so the kill switch is decided when the wrapper RUNS.
    computer_open uses live argv for the same reason — a flip plus relaunch
    must close the port without rewriting this file.
    """
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "launch-chrome"
    # Strip in case the caller passed machine_browser_command() output.
    chrome_argv = [part for part in chrome_argv if part not in _CHROME_CDP_FLAGS]
    quoted = " ".join(shlex.quote(part) for part in chrome_argv)
    cdp_quoted = " ".join(shlex.quote(part) for part in _CHROME_CDP_FLAGS)
    path.write_text(
        "#!/bin/sh\n"
        f"PROFILE={shlex.quote(str(profile_dir))}\n"
        "LOCK=$PROFILE/SingletonLock\n"
        'if [ -L "$LOCK" ] || [ -e "$LOCK" ]; then\n'
        '  target=$(readlink "$LOCK" 2>/dev/null || true)\n'
        "  pid=${target##*-}\n"
        "  host=${target%-*}\n"
        '  if [ "$host" != "$(hostname)" ] || [ ! -d "/proc/$pid" ]; then\n'
        '    rm -f "$LOCK" "$PROFILE/SingletonSocket" "$PROFILE/SingletonCookie"\n'
        "  fi\n"
        "fi\n"
        f"CDP_FLAGS={shlex.quote(cdp_quoted)}\n"
        'case "${HARNESS_CHROME_CDP:-0}" in 0|false|no) CDP_FLAGS= ;; esac\n'
        f'exec {quoted} $CDP_FLAGS "$@"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def write_launchers(cfg_dir: Path) -> list[Path]:
    """Write .desktop launchers for Chrome, Thunar, and xterm."""
    launch_dir = cfg_dir / "launchers"
    launch_dir.mkdir(parents=True, exist_ok=True)
    browser = find_browser() or "google-chrome"
    fm = find_file_manager() or "thunar"
    term = find_terminal() or "xterm"
    chrome_exec = browser
    if _is_chromium(browser) and product_computer():
        from agent.browser import machine_profile_dir

        profile = machine_profile_dir()
        # cdp=False: the wrapper appends the CDP flags itself, gated on
        # HARNESS_CHROME_CDP at run time (the script outlives this bake).
        argv = machine_browser_command("", profile, cdp=False)[:-1]
        chrome_exec = str(write_chrome_wrapper(cfg_dir, argv, profile))

    chrome = launch_dir / "chrome.desktop"
    thunar = launch_dir / "thunar.desktop"
    xterm = launch_dir / "xterm.desktop"
    _desktop_entry(
        chrome,
        "Chrome",
        chrome_exec,
        _icon(["google-chrome", "google-chrome-stable", "chromium", "web-browser"]),
    )
    _desktop_entry(
        thunar, "Thunar", fm, _icon(["org.xfce.thunar", "Thunar", "system-file-manager"])
    )
    _desktop_entry(
        xterm,
        "xterm",
        term,
        _icon(
            [
                str(_BUNDLED_TERMINAL_ICON),
                "utilities-terminal",
                "org.gnome.Terminal",
                "gnome-terminal",
                "terminal",
                "xterm",
            ]
        ),
    )
    return [chrome, thunar, xterm]


def write_tint2rc(cfg_dir: Path, launchers: list[Path], *, dock: bool = False) -> Path:
    """Write a tint2 config.

    Default: the host desktop's full-width bottom taskbar (launchers + tasks
    + clock). `dock=True` is the bot-machine look: a small bottom-centered
    dock holding exactly the three launchers (Chrome, files, terminal), no
    taskbar, no clock — nothing else on the desktop.
    """
    if dock:
        # id 0 is tint2's special "no background" (not background 1 with
        # alpha 0 — that still paints a plate unless a compositor is up).
        # xcompmgr is started in bringup so the wallpaper shows through.
        lines = [
            "panel_items = L",
            "panel_position = bottom center horizontal",
            "panel_size = 210 62",
            "panel_margin = 0 14",
            "panel_padding = 0 0 0",
            "panel_background_id = 0",
            "launcher_background_id = 0",
            "launcher_padding = 14 8 16",
            "launcher_icon_size = 40",
            "launcher_icon_theme = Adwaita",
        ]
    else:
        lines = [
            "panel_items = LTC",
            "panel_position = bottom center horizontal",
            "panel_size = 100% 46",
            "panel_background_id = 1",
            "rounded = 0",
            "background_color = #1c1c1e 90",
            "launcher_padding = 8 6 8",
            "launcher_icon_size = 32",
            "launcher_icon_theme = Adwaita",
            "taskbar_name = 0",
            "task_text = 1",
            "task_maximum_size = 180 30",
            "time1_format = %H:%M",
        ]
    for item in launchers:
        lines.append(f"launcher_item_app = {item}")
    rc = cfg_dir / "tint2rc"
    rc.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rc


def _spawn(args: list[str], env: dict) -> bool:
    try:
        subprocess.Popen(
            args,
            # desktop app launched on behalf of a bot: drop the user's
            # session sockets (SSH agent, DBus, ...) — see harness.envscrub
            env=scrub_ambient_authority(env),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError:
        return False


def _running(name: str) -> bool:
    return subprocess.run(["pgrep", "-x", name], capture_output=True, check=False).returncode == 0


def bringup(
    paths: HarnessPaths,
    *,
    display: str | None = None,
    url: str = DEFAULT_URL,
    no_sandbox: bool = False,
) -> dict:
    """Start the desktop on `display` (or $DISPLAY). Returns a report."""
    env = dict(os.environ)
    if display:
        env["DISPLAY"] = display
    disp = env.get("DISPLAY")
    report: dict = {"display": disp, "started": [], "skipped": []}
    if not disp:
        report["error"] = "no DISPLAY set"
        return report

    look = product_computer()
    cfg_dir = paths.home / "desktop"
    cfg_dir.mkdir(parents=True, exist_ok=True)

    # Product computer (machines + tenant --desktop): field photo + dock.
    # Bare host desktop (no HARNESS_DESKTOP_STYLE): dark blue solid + taskbar.
    color = "#d4d4d4" if look else "#20364f"
    wallpaper = os.environ.get("HARNESS_WALLPAPER")
    if wallpaper and not Path(wallpaper).is_file():
        wallpaper = None
    if not wallpaper and look and _BUNDLED_WALLPAPER.is_file():
        wallpaper = str(_BUNDLED_WALLPAPER)
    if not wallpaper and shutil.which("convert"):
        paper = cfg_dir / "wallpaper.png"
        if not paper.is_file():
            subprocess.run(
                ["convert", "-size", "1280x800", f"xc:{color}", str(paper)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if paper.is_file():
            wallpaper = str(paper)
    if wallpaper and shutil.which("feh"):
        _spawn(["feh", "--bg-fill", wallpaper], env)
    elif shutil.which("xsetroot"):
        _spawn(["xsetroot", "-solid", color], env)

    # window manager
    if shutil.which("openbox") and not _running("openbox"):
        report["started"].append("openbox") if _spawn(["openbox"], env) else None
    elif not shutil.which("openbox"):
        report["skipped"].append("openbox (not installed)")

    # tint2 alpha needs a compositor; without one id=0 still shows a grey plate
    if shutil.which("xcompmgr") and not _running("xcompmgr"):
        report["started"].append("xcompmgr") if _spawn(["xcompmgr", "-n"], env) else None
    elif not shutil.which("xcompmgr"):
        report["skipped"].append("xcompmgr (not installed)")

    # panel: three-icon dock on the product computer, full taskbar otherwise
    if look:
        from agent.browser import machine_profile_dir

        clear_stale_chrome_singleton(machine_profile_dir())
    launchers = write_launchers(cfg_dir)
    rc = write_tint2rc(cfg_dir, launchers, dock=look)
    if shutil.which("tint2"):
        if _running("tint2"):
            subprocess.run(["pkill", "-x", "tint2"], check=False)
        report["started"].append("tint2") if _spawn(["tint2", "-c", str(rc)], env) else None
    else:
        report["skipped"].append("tint2 (not installed)")

    # Host shared desktop still opens the usual apps. The product computer
    # only ships the dock; Chrome / Thunar / xterm wait for the bot or the user.
    if not look:
        fm = find_file_manager()
        if fm:
            fm_target = paths.workspace
            fm_target.mkdir(parents=True, exist_ok=True)
            if _spawn([fm, str(fm_target)], env):
                report["started"].append(os.path.basename(fm))
        else:
            report["skipped"].append("thunar (not installed)")

        term = find_terminal()
        if term and _spawn([term], env):
            report["started"].append(os.path.basename(term))
        elif not term:
            report["skipped"].append("xterm (not installed)")

        from agent.browser import ensure_profile_split, plan_profile_split

        plan = plan_profile_split(paths, "desktop")
        ensure_profile_split(plan)
        cmd = browser_command(url, plan.private_user_data_dir, no_sandbox=no_sandbox)
        if cmd and _spawn(cmd, env):
            report["started"].append(f"{os.path.basename(cmd[0])} -> {url}")
        elif not cmd:
            report["skipped"].append("browser (none of chrome/chromium/... installed)")

    report["taskbar"] = ["Chrome", "Thunar", "xterm"]
    return report
