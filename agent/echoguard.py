"""Inbound echo suppression at message admission.

Port of OpenClaw v2's shared outbound-echo guard
(`src/channels/message/outbound-echo.ts`). Every outbound send — a
bot-to-bot bus write into `shared/messages/<name>/inbox/`, a room
transcript append — records its identity ``(sender, conversation,
message_id)`` in ONE process-wide bounded map. The shared inbound
admission path (`agent.runtime.Agent.process_inbox_once`) consults the
map for any fresh message claiming to come from the admitting bot itself
and silently archives a match before session recording or agent dispatch,
so a delayed copy of our own outbound message can never re-enter the bot
as fresh input and start a reply loop (rooms plus `@<botname>` handoffs
make A→B→A loops possible otherwise).

Invariants kept from OpenClaw:

* **One shared guard at admission** — this module's singleton is the only
  echo cache; connectors and channels must not grow per-connector or
  per-channel duplicate TTL caches for the same identity.
* **Reservation before the send completes** — `messaging.send` reserves
  the identity *before* the inbox file becomes visible, closing the race
  where the echo arrives faster than the send returns.
* **Bounded memory** — cap ~10k entries, 30s TTL, lazy expiry pruning on
  insert, LRU refresh on hit.

The map is deliberately in-memory and per-process: the guard suppresses a
bot's OWN outbound messages coming back at it, and the sender and the
admitter are then the same agent process. It must never be file-backed or
otherwise shared across processes — bot A's send record would suppress
the genuine delivery at bot B. For the same reason (tests and the
orchestrator run many participants in one process) admission consults the
guard only for messages whose `frm` is the admitting bot itself.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable

#: bounded-map defaults, from OpenClaw's shared outbound-echo guard
DEFAULT_CAP = 10_000
DEFAULT_TTL = 30.0


def conversation_of(*, room: str | None, to: str | None) -> str:
    """Canonical conversation id for a bus message.

    Both the send side and the admission side derive it from the same Msg
    fields (`room` for group chats, else the recipient), so an echoed copy
    of the file yields the identical tuple.
    """
    return str(room or to or "")


class EchoGuard:
    """Bounded TTL map of recently sent message identities."""

    def __init__(
        self,
        *,
        cap: int = DEFAULT_CAP,
        ttl: float = DEFAULT_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cap = int(cap)
        self.ttl = float(ttl)
        self._clock = clock
        self._lock = threading.Lock()
        #: identity -> expiry. Every write stamps `now + ttl` and moves the
        #: key to the end, so iteration order IS expiry order and lazy
        #: pruning only ever has to look at the front.
        self._entries: OrderedDict[tuple[str, str, str], float] = OrderedDict()

    @staticmethod
    def _key(sender: str, conversation: str, message_id: str) -> tuple[str, str, str]:
        return (str(sender or ""), str(conversation or ""), str(message_id or ""))

    def reserve(self, sender: str, conversation: str, message_id: str) -> None:
        """Record an identity, before or after the send completes.

        Call before the message becomes visible to close the race where
        the echo arrives faster than the send returns.
        """
        key = self._key(sender, conversation, message_id)
        if not key[0] or not key[2]:
            return  # no sender or id — nothing an echo could ever match
        with self._lock:
            now = self._clock()
            self._prune(now)
            self._entries.pop(key, None)
            self._entries[key] = now + self.ttl
            while len(self._entries) > self.cap:
                self._entries.popitem(last=False)

    #: recording a completed send is the same write as reserving one
    record = reserve

    def is_echo(self, sender: str, conversation: str, message_id: str) -> bool:
        """True when this identity was sent from this process within the TTL.

        A hit refreshes the entry (TTL and LRU position): redelivery can
        echo the same message more than once, and every copy inside the
        window must stay suppressed. An expired entry is dropped, so a
        genuine later message with the same identity is admitted.
        """
        key = self._key(sender, conversation, message_id)
        with self._lock:
            expiry = self._entries.get(key)
            if expiry is None:
                return False
            now = self._clock()
            if expiry <= now:
                del self._entries[key]
                return False
            self._entries.pop(key)
            self._entries[key] = now + self.ttl
            return True

    def _prune(self, now: float) -> None:
        while self._entries:
            key, expiry = next(iter(self._entries.items()))
            if expiry > now:
                break
            del self._entries[key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_GUARD = EchoGuard()


def guard() -> EchoGuard:
    """The one shared per-process guard (see the module docstring)."""
    return _GUARD
