"""Human-in-the-loop control: takeover, teach, and stuck-handling.

Each bot has a control state that a channel (desktop app) and the bot itself
both read/write through the shared volume:

    shared/control/<bot>.json          current mode + takeover request
    shared/control/<bot>.events.jsonl  append-only audit for the UI

Modes:
    bot       the bot drives (normal)
    takeover  the human has taken control; the bot pauses and does not act
    teach     the human is demonstrating a task to be saved as a skill

Flows this enables:
* User takes over at any time ("take over"), does the work, then clicks
  "Return control" when done.
* Bot gets stuck -> it *requests* takeover with a reason; the UI surfaces a
  prompt so the human can step in.
* User teaches a task by demonstrating steps, which are saved as a SKILL.md the
  bot can reuse later (learning loop).
* Bot needs the desktop back after a takeover -> it *requests* a return with a
  reason; the UI shows an accept card and only the holder can hand it back.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .fsutil import pid_alive, write_atomic
from .paths import HarnessPaths

MODE_BOT = "bot"
MODE_TAKEOVER = "takeover"
MODE_TEACH = "teach"

#: Who holds control when nobody said. The harness is single-owner today (one
#: linking key), so every client that does not identify itself is the owner.
OWNER = "owner"


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ControlDenied(PermissionError):
    """Raised when someone other than the holder tries to hand control back."""


@dataclass
class ControlState:
    bot: str
    mode: str = MODE_BOT
    takeover_requested: bool = False
    reason: str | None = None
    teach_steps: list[str] = field(default_factory=list)
    #: True while Teach a task is capturing the bot's display.
    teach_recording: bool = False
    teach_started: float | None = None
    updated: float = field(default_factory=time.time)
    #: who took control; only they (or an unidentified owner) can hand it back
    holder: str | None = None
    #: the bot asked for the desktop back; one open request at a time
    return_requested: bool = False
    return_reason: str | None = None
    #: id of the accept card carrying that request (answers bus / prompts)
    return_request_id: str | None = None

    @property
    def paused(self) -> bool:
        """True when the bot should not act (human in control or teaching)."""
        return self.mode in (MODE_TAKEOVER, MODE_TEACH)


class Control:
    """Read/write control state for bots, with an event log for the UI."""

    def __init__(self, paths: HarnessPaths) -> None:
        self.paths = paths
        self._state_cache: dict[str, tuple[tuple[int, int] | None, dict[str, Any]]] = {}
        self._holds_cache: tuple[tuple, list[str]] | None = None

    def _file_stamp(self, path: Path) -> tuple[int, int] | None:
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _state_from_data(self, bot: str, data: dict[str, Any]) -> ControlState:
        return ControlState(
            bot=data.get("bot", bot),
            mode=data.get("mode", MODE_BOT),
            takeover_requested=data.get("takeover_requested", False),
            reason=data.get("reason"),
            teach_steps=list(data.get("teach_steps") or []),
            teach_recording=bool(data.get("teach_recording")),
            teach_started=_as_float(data.get("teach_started")),
            updated=data.get("updated", time.time()),
            holder=data.get("holder"),
            return_requested=data.get("return_requested", False),
            return_reason=data.get("return_reason"),
            return_request_id=data.get("return_request_id"),
        )

    # -- persistence ------------------------------------------------------
    def state(self, bot: str) -> ControlState:
        path = self.paths.control_file(bot)
        stamp = self._file_stamp(path)
        hit = self._state_cache.get(bot)
        if hit is not None and hit[0] == stamp:
            return self._state_from_data(bot, hit[1])
        if not path.is_file():
            data: dict[str, Any] = {"bot": bot}
            self._state_cache[bot] = (stamp, data)
            return ControlState(bot=bot)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {"bot": bot}
        self._state_cache[bot] = (stamp, data)
        return self._state_from_data(bot, data)

    def _control_dir_stamp(self) -> tuple:
        if not self.paths.control.is_dir():
            return ()
        marks = []
        for path in self.paths.control.glob("*.json"):
            stamp = self._file_stamp(path)
            if stamp is not None:
                marks.append((path.name, *stamp))
        return tuple(sorted(marks))

    def active_holds(self) -> list[str]:
        """Bots whose control state has a human driving (takeover or teach).

        The harness streams one physical desktop, so any hold means a human is
        at the shared mouse/keyboard regardless of which bot they grabbed.
        """
        stamp = self._control_dir_stamp()
        if self._holds_cache is not None and self._holds_cache[0] == stamp:
            return list(self._holds_cache[1])
        holds: list[str] = []
        if self.paths.control.is_dir():
            for path in sorted(self.paths.control.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if data.get("mode") in (MODE_TAKEOVER, MODE_TEACH):
                    holds.append(data.get("bot", path.stem))
        self._holds_cache = (stamp, holds)
        return list(holds)

    def _save(self, state: ControlState, event: str, **extra: Any) -> ControlState:
        state.updated = time.time()
        self.paths.control.mkdir(parents=True, exist_ok=True)
        payload = asdict(state)
        write_atomic(
            self.paths.control_file(state.bot),
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        path = self.paths.control_file(state.bot)
        self._state_cache[state.bot] = (self._file_stamp(path), payload)
        self._holds_cache = None
        record = {"ts": state.updated, "bot": state.bot, "event": event, **extra}
        with self.paths.control_events(state.bot).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return state

    def _flag(self, bot: str, kind: str) -> Path:
        self.paths.run.mkdir(parents=True, exist_ok=True)
        return self.paths.run / f"{bot}.{kind}"

    def request_stop(self, bot: str) -> None:
        """Hermes /stop: end the current turn; do not interrupt via the inbox."""
        self._flag(bot, "stop").write_text("1", encoding="utf-8")

    def stop_requested(self, bot: str) -> bool:
        return self._flag(bot, "stop").is_file()

    def consume_stop(self, bot: str) -> bool:
        path = self._flag(bot, "stop")
        if not path.is_file():
            return False
        try:
            path.unlink()
        except OSError:
            return True
        return True

    def set_busy(
        self,
        bot: str,
        request_id: str = "",
        *,
        frm: str = "",
        preview: str = "",
        origin: str = "",
        room: str = "",
        message_id: str = "",
        thread_id: str = "",
    ) -> None:
        payload = {"request_id": request_id, "pid": os.getpid(), "ts": time.time()}
        if frm:
            payload["frm"] = frm
        if preview:
            payload["preview"] = preview
        if origin:
            payload["origin"] = origin
        if message_id:
            payload["message_id"] = message_id
        if thread_id:
            payload["thread_id"] = thread_id
        if room:
            # The consult-relay poller announces this turn to clients; a
            # room turn must be announced into the room, not the 1:1.
            payload["room"] = room
        write_atomic(self._flag(bot, "busy"), json.dumps(payload))

    def clear_busy(self, bot: str) -> None:
        try:
            self._flag(bot, "busy").unlink()
        except OSError:
            pass

    def _busy_info(self, bot: str) -> dict[str, Any] | None:
        """The busy claim, or None. Drops the flag when its owner pid is dead."""
        try:
            raw = self._flag(bot, "busy").read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # pre-JSON busy files held only the request id; owner unknown
            data = {"request_id": raw}
        if not isinstance(data, dict):
            data = {"request_id": str(data)}
        pid = data.get("pid")
        if isinstance(pid, int) and not pid_alive(pid):
            self.clear_busy(bot)
            return None
        return data

    def is_busy(self, bot: str) -> bool:
        return self._busy_info(bot) is not None

    def busy_request(self, bot: str) -> str | None:
        """Request id of the turn currently claimed by a live agent, if any."""
        info = self._busy_info(bot)
        if info is None:
            return None
        return str(info.get("request_id") or "") or None

    def busy_state(self, bot: str) -> tuple[bool, str | None]:
        """(is_busy, busy_request) from ONE read of the busy flag — callers
        that need both were paying the file read (and the dead-pid check)
        twice per bot."""
        info = self._busy_info(bot)
        if info is None:
            return False, None
        return True, str(info.get("request_id") or "") or None

    # -- takeover ---------------------------------------------------------
    def request_takeover(self, bot: str, reason: str) -> ControlState:
        """Bot-initiated: 'I'm stuck, please take over.'"""
        state = self.state(bot)
        state.takeover_requested = True
        state.reason = reason
        return self._save(state, "request_takeover", reason=reason)

    def take_over(self, bot: str, holder: str | None = None) -> ControlState:
        """Human takes control (in response to a request, or unprompted)."""
        state = self.state(bot)
        state.mode = MODE_TAKEOVER
        state.takeover_requested = False
        state.holder = (holder or OWNER).strip() or OWNER
        state = self._clear_return(state)
        state = self._clear_teach(state)
        return self._save(state, "take_over", holder=state.holder)

    def request_return(self, bot: str, reason: str, request_id: str = "") -> ControlState:
        """Bot-initiated: 'I need the desktop back'.

        Only meaningful while a human holds control; the caller decides whether
        to show the accept card. One open request at a time — a second call
        replaces the first.
        """
        state = self.state(bot)
        state.return_requested = True
        state.return_reason = reason or None
        state.return_request_id = request_id or None
        return self._save(state, "request_return", reason=reason)

    def can_return(self, bot: str, by: str | None = None) -> bool:
        """True when `by` may hand control back.

        A caller that does not identify itself is the owner and is always
        allowed — that is every client today, and the CLI. Once a caller does
        name itself it must match the holder, so a second user cannot take the
        desktop out from under whoever grabbed it.
        """
        if by is None:
            return True
        holder = self.state(bot).holder
        # no holder recorded: state written before holders existed
        return holder is None or holder == by

    def decline_return(self, bot: str) -> ControlState:
        """Human dismissed the accept card; they keep control, bot stays blocked."""
        state = self._clear_return(self.state(bot))
        return self._save(state, "decline_return")

    def return_control(self, bot: str, by: str | None = None) -> ControlState:
        """Human is done; hand control back to the bot.

        `by` identifies who is accepting. Anyone other than the holder is
        refused with `ControlDenied`; `None` means the owner (CLI, unidentified
        client) and is always allowed.
        """
        if not self.can_return(bot, by):
            holder = self.state(bot).holder
            raise ControlDenied(f"{holder!r} holds control of {bot}; only they can return it")
        state = self.state(bot)
        state.mode = MODE_BOT
        state.takeover_requested = False
        state.reason = None
        state.holder = None
        state = self._clear_return(state)
        state = self._clear_teach(state)
        return self._save(state, "return_control")

    @staticmethod
    def _clear_return(state: ControlState) -> ControlState:
        state.return_requested = False
        state.return_reason = None
        state.return_request_id = None
        return state

    @staticmethod
    def _clear_teach(state: ControlState) -> ControlState:
        state.teach_recording = False
        state.teach_started = None
        return state

    # -- teach ------------------------------------------------------------
    def start_teach(
        self, bot: str, holder: str | None = None, *, recording: bool = False
    ) -> ControlState:
        state = self.state(bot)
        state.mode = MODE_TEACH
        state.teach_steps = []
        state.teach_recording = recording
        state.teach_started = time.time() if recording else None
        state.holder = (holder or OWNER).strip() or OWNER
        state = self._clear_return(state)
        return self._save(state, "start_teach", holder=state.holder, recording=recording)

    def record_step(self, bot: str, step: str) -> ControlState:
        state = self.state(bot)
        state.teach_steps.append(step)
        return self._save(state, "record_step", step=step)

    def cancel_teach(self, bot: str) -> ControlState:
        state = self.state(bot)
        state.mode = MODE_BOT
        state.teach_steps = []
        state.holder = None
        state = self._clear_return(state)
        state = self._clear_teach(state)
        return self._save(state, "cancel_teach")

    def save_teach(self, bot: str, name: str, description: str = "") -> tuple[ControlState, str]:
        """Persist demonstrated steps as a private SKILL.md and exit teach mode."""
        from agent.skills import propose_skill

        state = self.state(bot)
        steps = state.teach_steps or []
        body = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps)) or "(no steps recorded)"
        path = propose_skill(
            self.paths,
            bot,
            name=name,
            description=description or f"Taught by a human on {time.strftime('%Y-%m-%d')}",
            body=body,
            when_to_use="a task like the one demonstrated",
        )
        state.mode = MODE_BOT
        state.teach_steps = []
        state.holder = None
        state = self._clear_return(state)
        state = self._clear_teach(state)
        self._save(state, "save_teach", skill=name, path=str(path))
        return state, str(path)

    # -- events -----------------------------------------------------------
    def events(self, bot: str) -> list[dict[str, Any]]:
        path = self.paths.control_events(bot)
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out
