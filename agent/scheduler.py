"""Per-bot turn scheduler: lanes, wedge watchdog, instant takeover.

The runtime used to drain its inbox strictly serially: one wedged turn (a hung
provider call, a stuck tool) blocked every later message, and a human takeover
could not preempt the in-flight run. This module keeps the single-process,
file-inbox model but adds:

* **Lanes** — pending messages are classified ``user | agent | background``
  (human chat vs bot-to-bot vs routine work, see `messaging.lane_of`) and
  drained in that priority order, one active run per bot.
* **Wedge watchdog** — the active turn runs in a worker thread. When a user
  message has waited behind it longer than ``$HARNESS_RUN_WATCHDOG_SECS``
  (default 120s), the run is asked to stop via the cooperative interrupt flag
  the tool loop checks between tool calls and stream chunks.
* **Silence watchdog** — the wedge watchdog above only fires when somebody is
  *waiting*: a second user message queued behind the run, or a takeover. A run
  that goes quiet with an empty queue had nothing watching it at all, and a
  provider that accepts a connection and then never writes another byte left
  the channel busy and the composer locked with a bot that appeared to be
  thinking and was not. ``$HARNESS_RUN_SILENCE_SECS`` (default 300s, 0 to
  disable) trips the same interrupt path when a run has emitted nothing for
  that long.

  The clock is kept by the **producer**, not the consumer: ``Run.last_output``
  is stamped inside `GuardedStreamWriter`, where the bot emits, rather than
  where a client reads. Timing the read would measure how promptly somebody's
  app collected the bytes, so a viewer that paused — or no viewer at all —
  would be reported as a dead bot on a run that was streaming fine.
* **Grace-then-escape** — if the interrupt does not take within
  ``$HARNESS_RUN_GRACE_SECS`` (default 30s) the run is abandoned as a zombie
  and the queue pumps the next task. Generation counters guard the stream
  writer so a zombie that eventually settles cannot emit onto a newer run's
  stream; its completion is logged as ``late_settle``.
* **Takeover hook** — a human taking control trips the same interrupt path,
  so takeover feels instant instead of waiting out the turn.

Observable events (kept on `TurnScheduler.events` and mirrored to the log):
``accepted``, ``dequeued``, ``trip``, ``escape``, ``late_settle``.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

#: how long a run may emit nothing at all before it is treated as stalled.
#: Generous on purpose: a legitimate turn can be quiet through a slow provider
#: call, and the cost of a false trip (a working answer thrown away) is higher
#: than the cost of a stalled turn taking five minutes to notice.
SILENCE_DEFAULT_SECS = 300.0

#: how long a user chat may starve behind an active run before the watchdog
#: trips, and how long a tripped run gets to notice the interrupt flag.
WATCHDOG_DEFAULT_SECS = 120.0
GRACE_DEFAULT_SECS = 30.0

_EVENT_CAP = 200
_SEEN_CAP = 1024


class RunInterrupted(Exception):
    """Raised inside a turn (from a stream-chunk callback) to abort it now."""


def _silence_secs_from_env(default: float = SILENCE_DEFAULT_SECS) -> float:
    """Seconds of silence before a run is treated as stalled; 0 disables it."""
    raw = os.environ.get("HARNESS_RUN_SILENCE_SECS", "")
    if not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(0.0, value)


def _env_secs(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass
class Run:
    """One active (or escaped) turn: identity, generation, interrupt flag."""

    msg_id: str
    lane: str
    generation: int
    started: float = field(default_factory=time.time)
    #: when this run last emitted anything on its stream. Stamped by
    #: `GuardedStreamWriter` — on the producing side, deliberately.
    last_output: float = field(default_factory=time.time)
    #: cooperative preemption flag — the turn loop checks it between tool
    #: calls / stream chunks and winds down when set.
    interrupt: threading.Event = field(default_factory=threading.Event)
    #: why the interrupt was requested ("watchdog" | "takeover"), for the
    #: pause text. Set before `interrupt` so the worker reads it coherently.
    reason: str | None = None
    #: the turn noticed the interrupt flag and wound down cooperatively.
    interrupted: bool = False
    #: escaped by the watchdog: still running, but no longer the active run.
    zombie: bool = False
    #: the turn function returned (or raised) in the worker thread.
    settled: bool = False
    thread: threading.Thread | None = None
    #: True when this run began while a human already held the computer.
    #: Those turns must not be takeover-tripped — they ask for it back.
    began_paused: bool = False
    #: Room inputs joined to this run; their files stay queued until it settles.
    room_handoff_ids: set[str] = field(default_factory=set)


class TurnScheduler:
    """Per-bot run bookkeeping: one active run, watchdog state, zombies.

    The inbox files stay the queue; this tracks what is *running* so the
    drain loop can trip, escape, and late-settle wedged turns safely.
    """

    def __init__(
        self,
        *,
        watchdog_secs: float | None = None,
        grace_secs: float | None = None,
        silence_secs: float | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.watchdog_secs = (
            _env_secs("HARNESS_RUN_WATCHDOG_SECS", WATCHDOG_DEFAULT_SECS)
            if watchdog_secs is None
            else watchdog_secs
        )
        # `_env_secs` folds a non-positive value back to the default, which is
        # right for a threshold that must exist but wrong for one that can be
        # switched off, so 0 is read directly here.
        self.silence_secs = (
            _silence_secs_from_env() if silence_secs is None else max(0.0, silence_secs)
        )
        self.grace_secs = (
            _env_secs("HARNESS_RUN_GRACE_SECS", GRACE_DEFAULT_SECS)
            if grace_secs is None
            else grace_secs
        )
        self._log = log or (lambda _line: None)
        self._lock = threading.Lock()
        self._generation = 0
        self.active: Run | None = None
        self.zombies: list[Run] = []
        #: interrupted-run message ids: they stay queued for retry but yield
        #: to fresher work in their lane so the same turn cannot starve it.
        self.deferred: set[str] = set()
        #: consecutive cooperative interrupts per message id (loop breaker).
        self._interrupts: dict[str, int] = {}
        self.events: list[dict] = []
        self._seen: dict[str, None] = {}

    # -- observability -----------------------------------------------------
    def _emit(self, event: str, **fields) -> None:
        row = {"ts": time.time(), "event": event, **fields}
        with self._lock:
            self.events.append(row)
            if len(self.events) > _EVENT_CAP:
                del self.events[:-_EVENT_CAP]
        detail = " ".join(f"{k}={v}" for k, v in fields.items())
        self._log(f"run-scheduler {event} {detail}".rstrip())

    # -- lifecycle ---------------------------------------------------------
    def accept(self, msg_id: str, lane: str) -> None:
        """Log a newly seen pending message once (the inbox is the queue)."""
        with self._lock:
            if msg_id in self._seen:
                return
            self._seen[msg_id] = None
            while len(self._seen) > _SEEN_CAP:
                self._seen.pop(next(iter(self._seen)))
        self._emit("accepted", id=msg_id, lane=lane)

    def begin(self, msg_id: str, lane: str, *, enqueued_ts: float | None = None) -> Run:
        """Start a run for a dequeued message; bumps the generation counter."""
        with self._lock:
            self._generation += 1
            run = Run(msg_id=msg_id, lane=lane, generation=self._generation)
            self.active = run
            self.deferred.discard(msg_id)
        wait = None if enqueued_ts is None else round(max(0.0, run.started - enqueued_ts), 3)
        self._emit("dequeued", id=msg_id, lane=lane, wait=wait, zombies=len(self.zombies))
        return run

    def is_current(self, run: Run) -> bool:
        """Generation guard: may this run still touch its stream / state?"""
        with self._lock:
            return self.active is run and not run.zombie and run.generation == self._generation

    def trip(self, run: Run, reason: str, *, waited: float | None = None) -> None:
        """Watchdog fired (or takeover): ask the run to stop cooperatively."""
        run.reason = reason
        run.interrupt.set()
        self._emit(
            "trip",
            id=run.msg_id,
            lane=run.lane,
            reason=reason,
            runtime=round(time.time() - run.started, 3),
            waited=None if waited is None else round(waited, 3),
        )

    def escape(self, run: Run) -> bool:
        """Abandon a run that ignored its interrupt: mark it a zombie.

        False when the run settled in the meantime (no escape needed).
        """
        with self._lock:
            if run.settled or self.active is not run:
                return False
            run.zombie = True
            self.active = None
            self.zombies.append(run)
        self._emit(
            "escape",
            id=run.msg_id,
            lane=run.lane,
            reason=run.reason,
            runtime=round(time.time() - run.started, 3),
        )
        return True

    def settle(self, run: Run) -> None:
        """Worker-thread exit hook; a zombie finishing late is logged, not kept."""
        with self._lock:
            run.settled = True
            was_zombie = run.zombie
            if was_zombie and run in self.zombies:
                self.zombies.remove(run)
        if was_zombie:
            self._emit(
                "late_settle",
                id=run.msg_id,
                lane=run.lane,
                runtime=round(time.time() - run.started, 3),
            )

    def finish(self, run: Run) -> None:
        """Normal end of the active run (after the worker joined)."""
        with self._lock:
            if self.active is run:
                self.active = None

    def defer(self, msg_id: str) -> None:
        """An interrupted run's message stays queued but yields to fresh work."""
        with self._lock:
            self.deferred.add(msg_id)

    def note_interrupt(self, msg_id: str) -> int:
        """Count cooperative interrupts of this message; used as a stall cap."""
        with self._lock:
            n = self._interrupts.get(msg_id, 0) + 1
            self._interrupts[msg_id] = n
            return n


class GuardedStreamWriter:
    """Stream-writer proxy that goes dark once its run is superseded.

    A zombie turn keeps its `StreamWriter` reference; every call is gated on
    the generation check so late output cannot land on the stream after the
    watchdog already settled the request and moved on.
    """

    def __init__(self, writer, scheduler: TurnScheduler, run: Run) -> None:
        self._writer = writer
        self._scheduler = scheduler
        self._run = run

    def __getattr__(self, name: str):
        attr = getattr(self._writer, name)
        if not callable(attr):
            return attr

        def guarded(*args, **kwargs):
            if not self._scheduler.is_current(self._run):
                return None
            result = attr(*args, **kwargs)
            # Stamped AFTER the write, on the producing side. This is the whole
            # of the silence watchdog's clock: every path a run has to say
            # anything goes through this proxy, so anything that reaches a
            # client resets it, and nothing else does.
            self._run.last_output = time.time()
            return result

        return guarded
