"""OS packages the harness needs on the *host* for computer use.

The Python runtime stays stdlib-only. Screen streaming and Take-control input
are shell-outs (`ffmpeg`, `xdotool`, a screenshot tool). Those must be present
on Linux/X11 hosts (Pi, VM, Docker). `harness install` and the first
`harness serve` install them via apt when possible.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable

# (binary on PATH, apt package)
CORE = (
    ("ffmpeg", "ffmpeg"),  # live JPEG stream (x11grab)
    ("xdotool", "xdotool"),  # mouse/keyboard while Take control is held
    ("scrot", "scrot"),  # one-shot PNG fallback
    ("xclip", "xclip"),  # copy/paste between Mac clipboard and the host
)

DESKTOP = (
    ("openbox", "openbox"),
    ("tint2", "tint2"),
    ("thunar", "thunar"),
    ("xterm", "xterm"),
    ("feh", "feh"),
    ("Xvfb", "xvfb"),
    ("xcompmgr", "xcompmgr"),
)


class InstallError(RuntimeError):
    """Raised when host packages cannot be installed."""


def _which(name: str) -> str | None:
    return shutil.which(name)


def needed(desktop: bool = False) -> list[tuple[str, str]]:
    pkgs = list(CORE)
    if desktop:
        pkgs.extend(DESKTOP)
    return pkgs


def missing(desktop: bool = False) -> list[tuple[str, str]]:
    return [(binary, pkg) for binary, pkg in needed(desktop) if not _which(binary)]


def apt_available() -> bool:
    return bool(_which("apt-get"))


def _sudo() -> list[str]:
    if os.geteuid() == 0:
        return []
    if _which("sudo"):
        return ["sudo", "-n"]
    return []


def install(
    desktop: bool = False,
    *,
    run: Callable[..., subprocess.CompletedProcess] | None = None,
    log: Callable[[str], None] | None = None,
) -> int:
    """Install missing computer-use packages. Returns a process exit code."""
    say = log or (lambda m: print(m, flush=True))
    runner = run or subprocess.run
    absent = missing(desktop)
    if not absent:
        say("computer-use tools already installed: " + ", ".join(b for b, _ in needed(desktop)))
        return 0
    if not apt_available():
        pkgs = " ".join(p for _, p in absent)
        say(f"missing host tools ({', '.join(b for b, _ in absent)}). install: {pkgs}")
        return 1
    packages = sorted({p for _, p in absent})
    prefix = _sudo()
    if prefix == [] and os.geteuid() != 0 and not _which("sudo"):
        say("need root or sudo to install: " + " ".join(packages))
        return 1
    cmd = [
        *prefix,
        "apt-get",
        "install",
        "-y",
        "--no-install-recommends",
        *packages,
    ]
    say("installing " + " ".join(packages))
    try:
        proc = runner(cmd, check=False)
    except OSError as exc:
        raise InstallError(str(exc)) from exc
    if proc.returncode != 0:
        say(f"apt-get failed (exit {proc.returncode}). try: python3 -m harness install")
        return proc.returncode
    still = missing(desktop)
    if still:
        say("still missing: " + ", ".join(b for b, _ in still))
        return 1
    say("computer-use tools ready: " + ", ".join(b for b, _ in needed(desktop)))
    return 0


def ensure(desktop: bool = False) -> None:
    """Best-effort install on serve. Never raises; prints a warning on failure."""
    absent = missing(desktop)
    if not absent:
        return
    names = ", ".join(b for b, _ in absent)
    if not apt_available():
        if sys.platform.startswith("linux"):
            print(
                f"warning: computer-use tools missing ({names}). "
                "run: python3 -m harness install",
                file=sys.stderr,
                flush=True,
            )
        return
    print(f"computer-use tools missing ({names}); installing…", flush=True)
    code = install(desktop=desktop)
    if code != 0:
        print(
            "warning: live screen/control needs ffmpeg + xdotool + xclip. "
            "run: python3 -m harness install",
            file=sys.stderr,
            flush=True,
        )
