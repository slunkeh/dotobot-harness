"""Computer control interface.

The bot drives a "computer" through a small `Computer` interface.
`GatedComputer` still fail-closes if control state cannot be read, but
takeover/teach no longer pause GUI tools — the human and the bot share
the desktop.

`HostComputer` drives the real X11 desktop via `xdotool` and launched apps.
The test double that records intended actions lives in `tests/fakes.py`.
"""

from __future__ import annotations

import math
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from harness import computer_env, hostinput
from harness.control import Control
from harness.envscrub import scrub_ambient_authority

from . import gate
from .gate import GateRefusal


class TakeoverActive(GateRefusal):
    """Raised when the bot tries to act while the human holds control.

    str(exc) is the model-facing refusal (see agent/gate.py vocabulary).
    """


@runtime_checkable
class Computer(Protocol):
    def act(self, action: str, **params) -> str: ...


#: A machine display that answered the probe stays trusted this long, so a
#: click/type/screenshot burst costs one `docker exec` probe, not one per
#: action. A miss is never cached: Xvfb boots asynchronously after spawn
#: and the next call must see it come up.
_DISPLAY_READY_TTL = 30.0
_display_ready_until: dict[str, float] = {}


def forget_display_ready(machine: str | None = None) -> None:
    """Drop the cached probe for one machine (None: all) — tests, relaunch."""
    if machine is None:
        _display_ready_until.clear()
    else:
        _display_ready_until.pop(machine, None)


_APP_ALIASES = {
    "files": "files",
    "file": "files",
    "file browser": "files",
    "file manager": "files",
    "folders": "files",
    "thunar": "files",
    "nautilus": "files",
    "browser": "browser",
    "chrome": "browser",
    "chromium": "browser",
    "web": "browser",
    "google": "browser",
    "terminal": "terminal",
    "xterm": "terminal",
    "console": "terminal",
    "shell": "terminal",
}


def _launch(argv: list[str]) -> str:
    try:
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            # app launch on behalf of a bot: no user session sockets
            env=scrub_ambient_authority(),
        )
    except OSError as exc:
        return f"error: could not launch {argv[0]}: {exc}"
    return f"ok: launched {' '.join(argv)}"


def _point(params: dict) -> tuple[float, float] | None:
    """Validate a model coordinate before any desktop input is emitted."""
    try:
        x, y = float(params["x"]), float(params["y"])
        if math.isfinite(x) and math.isfinite(y) and x >= 0 and y >= 0:
            return x, y
    except (KeyError, TypeError, ValueError):
        pass
    return None


class HostComputer:
    """Drive the bot's computer (xdotool + launched apps).

    Default target is the harness host desktop. With `machine` set (or
    $HARNESS_MACHINE_NAME, exported by the machines backend), every action
    routes into that bot-machine container over `docker exec` instead — same
    tool surface, own display, nothing shared with other bots.

    With `paths` and `bot` set, host-mode browser launches use the per-bot
    profile split (private session dir, shared login jar) instead of the host
    user's default profile; machine mode uses the machine's real profile.
    """

    def __init__(
        self,
        paths=None,
        bot: str | None = None,
        machine: str | None = None,
        private_browser: bool = False,
    ) -> None:
        self.paths = paths
        self.bot = bot
        self.machine = machine or os.environ.get("HARNESS_MACHINE_NAME") or None
        #: roster `private_browser`: this bot's Chrome skips the shared login
        #: jar (host mode only; machine profiles ride the state sync).
        self.private_browser = private_browser

    def display_ready(self) -> bool:
        """True when this computer's X display is accepting connections.

        A newly spawned machine starts Xvfb asynchronously. GUI
        tools must fail closed on a missing display instead of looping on
        xdotool errors that look like a working desktop.
        """
        if self.machine:
            if _display_ready_until.get(self.machine, 0.0) > time.monotonic():
                return True
            try:
                from harness.machine_view import exec_prefix

                proc = subprocess.run(
                    [*exec_prefix(self.machine), "sh", "-c", "test -S /tmp/.X11-unix/X0"],
                    capture_output=True,
                    timeout=3,
                )
            except (OSError, subprocess.SubprocessError, Exception):
                return False
            ready = proc.returncode == 0
            if ready:
                _display_ready_until[self.machine] = time.monotonic() + _DISPLAY_READY_TTL
            return ready
        # Process-backend host desktop: do not infer unreadiness from a
        # missing $DISPLAY (CI, macOS Aqua). Machine jails are the case
        # where Xvfb boots after spawn.
        return True

    def _note_activity(self) -> None:
        """Every action resets the idle-browser clock (harness/browser_idle.py)."""
        if self.paths is not None and self.bot:
            from harness import browser_idle

            browser_idle.touch(self.paths, self.bot)

    def act(self, action: str, **params) -> str:
        action = (action or "").strip().lower()
        self._note_activity()
        if action == "open":
            return self._open(str(params.get("app") or params.get("name") or ""))
        if action == "click":
            node = str(params.get("node") or "").strip()
            has_xy = params.get("x") is not None and params.get("y") is not None
            button, clicks = params.get("button", 1), params.get("clicks", 1)
            if (
                type(button) is not int
                or type(clicks) is not int
                or button not in (1, 2, 3)
                or clicks not in (1, 2)
            ):
                return "error: computer_click needs button=1|2|3 and clicks=1|2"
            if node and (button != 1 or clicks != 1) and not has_xy:
                return "error: double, middle, and right clicks need x,y; AX node clicks are single left clicks"
            if node and button == 1 and clicks == 1:
                from harness import cdp

                # CDP does not share xdotool's command queue. Finish a
                # preceding type/key before a node click can submit it.
                if not hostinput.flush(self.machine):
                    return "error: earlier computer input has not completed; inspect a screenshot before retrying"
                if cdp.click_node(node, machine=self.machine, user_data_dir=self._chrome_profile()):
                    return f"ok: click node {node.lstrip('#')}"
                if not has_xy:
                    return "error: chrome node click failed; call computer_screenshot and click x,y"
            elif not has_xy:
                # No node and no coordinates must never fall through to the
                # 0.5,0.5 default — a blind center click on whatever is open.
                return "error: computer_click needs x,y (or node=<id> from the AX tree)"
            point = _point(params)
            if point is None:
                return "error: computer_click needs finite, nonnegative x,y coordinates"
            nx, ny = point
            pos = hostinput._pos(nx, ny, machine=self.machine)
            ok = hostinput.click(nx, ny, button, machine=self.machine, clicks=clicks)
            if not ok:
                return "error: click failed (is xdotool installed and DISPLAY set?)"
            if pos:
                return f"ok: click at {pos[0]},{pos[1]}"
            return "ok: click"
        if action == "move":
            point = _point(params)
            if point is None:
                return "error: computer_move needs finite, nonnegative x,y coordinates"
            ok = hostinput.move(*point, machine=self.machine)
            return "ok: moved pointer" if ok else "error: move failed"
        if action == "drag":
            path, button = params.get("path"), params.get("button", 1)
            if not isinstance(path, list) or not 2 <= len(path) <= hostinput.MAX_DRAG_POINTS:
                return f"error: computer_drag needs a path of 2..{hostinput.MAX_DRAG_POINTS} x,y points"
            if type(button) is not int or button not in (1, 2, 3):
                return "error: computer_drag needs button=1|2|3"
            points = [_point(p) if isinstance(p, dict) else None for p in path]
            if any(p is None for p in points):
                return (
                    "error: computer_drag needs finite, nonnegative x,y coordinates at every point"
                )
            ok = hostinput.drag(points, button=button, machine=self.machine)
            return "ok: dragged" if ok else "error: drag failed"
        if action == "type":
            text = str(params.get("text") or "")
            # A previous type may still have Ctrl+V queued. Finish it before
            # replacing the clipboard with this call's text; the same drain
            # also lets preceding clicks reach their intended field first.
            if not hostinput.flush(self.machine):
                return "error: earlier computer input has not completed; inspect a screenshot before retrying"
            ok = hostinput.type_text(text, machine=self.machine)
            return "ok: typed" if ok else "error: type failed"
        if action in {"key", "hotkey"}:
            name = str(params.get("key") or params.get("keys") or "")
            ok = hostinput.key(name, machine=self.machine)
            return f"ok: key {name}" if ok else f"error: key {name} failed"
        if action == "scroll":
            amount, axis = params.get("amount", 1), params.get("axis", "vertical")
            limit = hostinput.MAX_SCROLL_TICKS
            if type(amount) is not int or not -limit <= amount <= limit:
                return f"error: computer_scroll amount must be an integer between -{limit} and {limit} wheel ticks"
            if axis not in ("vertical", "horizontal"):
                return "error: computer_scroll axis must be vertical or horizontal"
            target = None
            if params.get("x") is not None or params.get("y") is not None:
                target = _point(params)
                if target is None:
                    return "error: computer_scroll needs both finite, nonnegative x,y coordinates"
            nx, ny = target if target else (None, None)
            ok = hostinput.scroll(amount, machine=self.machine, nx=nx, ny=ny, axis=axis, wait=True)
            return f"ok: scroll {amount} {axis} wheel ticks" if ok else "error: scroll failed"
        if action == "screenshot":
            frame = self.screenshot()
            return "ok: screenshot" if frame else "error: screenshot failed"
        return f"error: unknown computer action {action!r}"

    def screenshot(self) -> tuple[bytes, str] | None:
        """One frame of this computer, sized for a vision model."""
        self._note_activity()
        if not hostinput.flush(self.machine):
            return None
        from harness import screen

        if self.machine:
            from harness import machine_view

            frame = machine_view.capture_for_model(self.machine)
        else:
            png = screen.capture_png()
            frame = screen.compress_for_model(png) if png else None
        if frame:
            dim = screen.image_size(frame[0])
            if dim:
                hostinput.remember_shot(dim, self.machine)
        return frame

    def chrome_snapshot(self) -> str | None:
        """AX dump from the bot's Chrome, or None when CDP is down.

        Never raises: screenshot text must stay identical to today if attach
        fails.
        """
        try:
            # AX capture runs alongside the image capture; both must wait
            # for preceding pointer/keyboard input before observing.
            if not hostinput.flush(self.machine):
                return None
            from harness import cdp

            return cdp.snapshot(machine=self.machine, user_data_dir=self._chrome_profile())
        except Exception:
            return None

    def browser_session(self, *, guard=lambda: None):
        from harness.browser_dom import Browser

        self._note_activity()
        return Browser(machine=self.machine, user_data_dir=self._chrome_profile(), guard=guard)

    def _chrome_profile(self) -> str | None:
        if self.machine:
            from isolation.machines import MACHINE_HOME

            from .browser import MACHINE_CHROME_DIR

            return f"{MACHINE_HOME}/{MACHINE_CHROME_DIR}"
        if self.paths is not None and self.bot:
            from .browser import plan_profile_split

            return str(
                plan_profile_split(
                    self.paths, self.bot, shared_logins=not self.private_browser
                ).private_user_data_dir
            )
        return None

    def _open_in_machine(self, kind: str, name: str) -> str:
        """Launch one of the machine's apps inside its container.

        The image guarantees chromium/thunar/xterm, so there is no host
        `which()` here; unknown names run as a binary inside the jail.
        """
        from harness import machine_view
        from isolation.machines import MACHINE_HOME

        from .browser import MACHINE_CHROME_DIR

        if kind == "files":
            argv = ["thunar", MACHINE_HOME]
        elif kind == "terminal":
            argv = ["xterm"]
        elif kind == "browser":
            # Live argv so HARNESS_CHROME_CDP=0 is honoured on relaunch.
            # launch-chrome is written once at bringup; using it first left
            # a stale --remote-debugging-port=0 after the kill switch flipped.
            argv = computer_env.machine_browser_command(
                computer_env.DEFAULT_URL, f"{MACHINE_HOME}/{MACHINE_CHROME_DIR}"
            )
            if machine_view.launch(self.machine, argv):
                return f"ok: launched chrome on {self.machine}"
            from harness import cdp

            if cdp.enabled():
                wrapper = f"{MACHINE_HOME}/.harness-local/desktop/launch-chrome"
                argv = [wrapper, computer_env.DEFAULT_URL]
        elif name.strip():
            argv = name.strip().split()
        else:
            return "error: computer_open needs app=files|browser|terminal"
        if machine_view.launch(self.machine, argv):
            return f"ok: launched {' '.join(argv)} on {self.machine}"
        return f"error: could not launch {argv[0]} on {self.machine}"

    def _open(self, name: str) -> str:
        if self.machine:
            kind = _APP_ALIASES.get(name.strip().lower(), "")
            return self._open_in_machine(kind, name)
        kind = _APP_ALIASES.get(name.strip().lower(), "")
        if kind == "files":
            path = computer_env.find_file_manager()
            if not path:
                return "error: no file manager installed (thunar/nautilus)"
            return _launch([path])
        if kind == "browser":
            path = computer_env.find_browser()
            if not path:
                return "error: no browser installed"
            if self.paths is not None and self.bot:
                # per-bot session dir + shared login jar, never the host's
                # real default profile
                from .browser import ensure_profile_split, plan_profile_split

                plan = plan_profile_split(
                    self.paths, self.bot, shared_logins=not self.private_browser
                )
                ensure_profile_split(plan)
                cmd = computer_env.browser_command(
                    computer_env.DEFAULT_URL, plan.private_user_data_dir
                )
                if cmd:
                    return _launch(cmd)
            return _launch([path])
        if kind == "terminal":
            path = computer_env.find_terminal()
            if not path:
                return "error: no terminal installed"
            return _launch([path])
        if name.strip():
            # Treat unknown names as a binary on PATH (e.g. "gedit").
            import shutil

            binary = name.strip().split()[0]
            path = shutil.which(binary)
            if not path:
                return f"error: unknown app {name!r}. try files, browser, or terminal"
            extra = name.strip().split()[1:]
            return _launch([path, *extra])
        return "error: computer_open needs app=files|browser|terminal"


@dataclass
class GatedComputer:
    """Wrap a Computer with the takeover gate and teach recording."""

    inner: Computer
    control: Control
    bot: str
    #: whether this bot drives the same physical display as every other bot.
    #: True for the process backend today (one host X display); per-bot
    #: environments (container/VM, the target architecture) set
    #: HARNESS_SHARED_DISPLAY=0 so a takeover of one bot does not pause the
    #: others. None = resolve from that env var.
    shared_display: bool | None = None

    def _shares_display(self) -> bool:
        if self.shared_display is not None:
            return self.shared_display
        return os.environ.get("HARNESS_SHARED_DISPLAY", "1") not in ("0", "false", "no")

    @gate.fail_closed("control state")
    def _gate(self, action: str, params: dict | None = None) -> str | None:
        """Shared computer: human and bot may drive at the same time.

        Takeover/teach no longer pause GUI tools. The decorator still fails
        closed if control state cannot be read.
        """
        self.control.state(self.bot)  # keep the fail-closed read
        return None

    def act(self, action: str, **params) -> str:
        recorded = self._gate(action, params)
        if recorded is not None:
            return recorded
        return self.inner.act(action, **params)

    def display_ready(self) -> bool:
        fn = getattr(self.inner, "display_ready", None)
        return True if not callable(fn) else bool(fn())

    def screenshot(self) -> tuple[bytes, str] | None:
        self._gate("screenshot")
        fn = getattr(self.inner, "screenshot", None)
        if not callable(fn):
            return None
        return fn()

    def browser_session(self):
        return self.inner.browser_session(guard=lambda: self._gate("browser"))

    def chrome_snapshot(self) -> str | None:
        self._gate("screenshot")
        fn = getattr(self.inner, "chrome_snapshot", None)
        if not callable(fn):
            return None
        return fn()
