"""UI-agnostic view-model shared by every channel (desktop app, tests, etc.).

It exposes exactly what a front-end needs — stream a reply, read/drive control
state — without importing any GUI toolkit, so it is fully unit-testable and so a
Telegram/Slack channel could reuse it unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator

from agent.commands import catalog as skill_catalog
from agent.mentions import resolve_mentions
from agent.streaming import StreamEvent, multiplex
from harness.control import ControlState
from harness.orchestrator import Orchestrator
from harness.rooms import Room


def _rank(query: str, items, *, key) -> list:
    """Items whose `key` contains `query` (case-insensitive), prefix hits first.

    Grok Bot's pickers match anywhere in the name; the Mac composer does the
    same (`ComposerBar`), and this keeps the Tk desktop honest with both.
    Order within each band is the catalog's own.
    """
    q = (query or "").lower()
    if not q:
        return list(items)
    prefix = [i for i in items if key(i).lower().startswith(q)]
    inner = [i for i in items if not key(i).lower().startswith(q) and q in key(i).lower()]
    return prefix + inner


class DesktopViewModel:
    def __init__(self, orch: Orchestrator, bot: str | None = None) -> None:
        self.orch = orch
        names = [b.name for b in orch.bots()]
        self.bot = bot or (names[0] if names else "")
        self.room: str | None = None

    def bots(self) -> list[str]:
        return [b.name for b in self.orch.bots()]

    def set_bot(self, name: str) -> None:
        self.bot = name
        self.room = None

    def set_room(self, room_id: str) -> None:
        self.room = room_id

    def rooms(self) -> list[Room]:
        return self.orch.rooms()

    def create_room(self, title: str, members: list[str]) -> Room:
        return self.orch.create_room(title, members)

    def slash_catalog(self) -> list[dict]:
        return skill_catalog(self.orch.paths, self.bot)

    def mention_candidates(self, query: str) -> list[str]:
        """Bots whose name contains `query` (Grok Bot matches anywhere, not
        just the prefix: `@lin` finds `berlin-office`). Prefix hits first."""
        return _rank(query, self.bots(), key=lambda n: n)

    def slash_candidates(self, query: str) -> list[dict]:
        """Catalog rows whose name contains `query`, prefix hits first."""
        return _rank(query, self.slash_catalog(), key=lambda i: str(i["name"]))

    def mentions_in(self, text: str) -> list[str]:
        return resolve_mentions(text, self.bots())

    # -- conversation -----------------------------------------------------
    def stream(self, text: str, *, timeout: float = 45.0) -> Iterator[StreamEvent]:
        """Send a message (1:1 or group) and yield live stream events."""
        turns = self.orch.dispatch_chat(
            text, bot=None if self.room else self.bot, room_id=self.room
        )
        readers = [(name, reader) for name, _rid, reader in turns]
        for name, ev in multiplex(readers, timeout=timeout):
            if ev.bot is None:
                ev.bot = name
            yield ev

    # -- interactive blocks (secure secret input, choice box) --------------
    def provide_secret(self, name: str, value: str) -> None:
        from harness.secrets import set_secret

        set_secret(name, value, self.orch.paths)

    def answer_choice(self, choice_id: str, value: str) -> bool:
        from agent.streaming import write_answer

        return write_answer(self.orch.paths, choice_id, value)

    # -- control ----------------------------------------------------------
    def control_state(self) -> ControlState:
        return self.orch.control.state(self.bot)

    def take_over(self) -> ControlState:
        return self.orch.control.take_over(self.bot)

    def return_control(self) -> ControlState:
        return self.orch.control.return_control(self.bot)

    def start_teach(self) -> ControlState:
        return self.orch.control.start_teach(self.bot)

    def record_step(self, step: str) -> ControlState:
        return self.orch.control.record_step(self.bot, step)

    def cancel_teach(self) -> ControlState:
        return self.orch.control.cancel_teach(self.bot)

    def save_teach(self, name: str, description: str = "") -> str:
        _state, path = self.orch.control.save_teach(self.bot, name, description)
        return path
