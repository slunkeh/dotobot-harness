"""Per-bot turn scheduler: lanes, wedge watchdog, instant takeover."""

import threading
import time

from agent import messaging
from agent.runtime import build_agent
from agent.scheduler import GuardedStreamWriter, TurnScheduler
from agent.streaming import StreamWriter
from harness.paths import HarnessPaths
from harness.roster import Bot
from providers.base import Completion, Provider, ToolCall


def _setup(tmp_path, monkeypatch=None, *, watchdog=None, grace=None):
    if watchdog is not None:
        monkeypatch.setenv("HARNESS_RUN_WATCHDOG_SECS", str(watchdog))
    if grace is not None:
        monkeypatch.setenv("HARNESS_RUN_GRACE_SECS", str(grace))
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    agent = build_agent(paths, Bot(name="atlas", role="terse", provider="echo"), stream_delay=0.0)
    return agent, paths


def _events(agent, kind):
    return [e for e in agent.scheduler.events if e["event"] == kind]


def test_note_interrupt_counts_per_message():
    sched = TurnScheduler()
    assert sched.note_interrupt("a") == 1
    assert sched.note_interrupt("a") == 2
    assert sched.note_interrupt("b") == 1


class LoopTool(Provider):
    """Never finishes on its own: every completion asks for another tool call."""

    def __init__(self, model="m"):
        super().__init__(model)
        self.n = 0

    def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
        self.n += 1
        return Completion(
            tool_calls=[ToolCall(id=f"c{self.n}", name="remember", arguments={"text": "x"})],
            finish_reason="tool_use",
        )


class WedgedProvider(Provider):
    """Simulates a hung provider call: blocks until `release`, ignoring interrupts."""

    def __init__(self, release: threading.Event, model="m"):
        super().__init__(model)
        self.release = release

    def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
        self.release.wait(timeout=30)
        return Completion(text="zombie output", finish_reason="stop")


# -- lanes -----------------------------------------------------------------


def test_lane_classification():
    user = messaging.Msg(to="a", frm="user", text="hi")
    bot = messaging.Msg(to="a", frm="cloud-engineer", text="hi")
    routine = messaging.Msg(to="a", frm="user", text="[Routine: digest]\ngo", origin="routine")
    assert messaging.lane_of(user) == messaging.LANE_USER
    assert messaging.lane_of(bot) == messaging.LANE_AGENT
    assert messaging.lane_of(routine) == messaging.LANE_BACKGROUND


def test_lanes_drain_in_priority_order(tmp_path):
    agent, paths = _setup(tmp_path)
    # Arrival order background -> agent -> user; drain order must invert it.
    messaging.send(
        paths, messaging.Msg(to="atlas", frm="user", text="[Routine: digest]", origin="routine")
    )
    messaging.send(paths, messaging.Msg(to="atlas", frm="scout", text="bot question"))
    messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="user chat"))
    for _ in range(3):
        agent.process_inbox_once()
    assert [e["lane"] for e in _events(agent, "dequeued")] == ["user", "agent", "background"]
    assert messaging.pending(paths, "atlas") == []


# -- wedge watchdog --------------------------------------------------------


def test_watchdog_trips_hung_turn_when_user_chat_waits(tmp_path, monkeypatch):
    from agent import tools as toolsmod

    agent, paths = _setup(tmp_path, monkeypatch, watchdog="0.3", grace="5")
    agent.provider = LoopTool()

    injected = {"id": None}

    def inject_and_wait(ctx, args):
        if injected["id"] is None:
            # Send-now: a plain follow-up steers into the live turn
            # and must not trip the watchdog — only now=True can starve a run.
            waiting = messaging.Msg(to="atlas", frm="user", text="hurry up", now=True)
            injected["id"] = waiting.id
            messaging.send(paths, waiting)
            # Block PAST the watchdog window: at a tool boundary the loop
            # would yield to Send-now cooperatively (_preempted) before any
            # trip; a hung turn is one stuck inside a call, which is what
            # trips. Only this first call blocks — the second turn (for the
            # Send-now chat) loops up to max_tool_iterations, and a long
            # sleep every round would outlast the per-test timeout.
            time.sleep(0.6)
        else:
            time.sleep(0.05)
        return "ok: remembered"

    monkeypatch.setattr(toolsmod, "_remember", inject_and_wait)
    toolsmod._DEFAULT_TOOLS = None
    stuck = messaging.Msg(to="atlas", frm="user", text="long job")
    messaging.send(paths, stuck)

    agent.process_inbox_once()

    trips = _events(agent, "trip")
    assert trips and trips[0]["reason"] == "watchdog"
    # The interrupt took cooperatively: no escape, the turn wound itself down.
    assert _events(agent, "escape") == []
    replies = [m.text for _p, m in messaging.read_inbox(paths, "user")]
    assert any("pausing" in t for t in replies)
    # The interrupted message stays queued but yields to the fresh user chat.
    assert {m.text for m in messaging.pending(paths, "atlas")} == {"long job", "hurry up"}
    agent.process_inbox_once()
    assert _events(agent, "dequeued")[1]["id"] == injected["id"]
    toolsmod._DEFAULT_TOOLS = None


def test_watchdog_ignores_backlog_already_queued(tmp_path, monkeypatch):
    """Messages already waiting when a turn starts are not starvation — they
    are the rest of the queue. Watchdog only trips for chats that arrive
    after the run began."""
    from agent import tools as toolsmod

    agent, paths = _setup(tmp_path, monkeypatch, watchdog="0.2", grace="5")

    def slow_remember(ctx, args):
        time.sleep(0.05)
        return "ok: remembered"

    monkeypatch.setattr(toolsmod, "_remember", slow_remember)
    toolsmod._DEFAULT_TOOLS = None
    messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="first"))
    messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="already queued"))

    class Finite(LoopTool):
        def complete(self, *a, **k):
            if self.n >= 4:
                from providers.base import Completion

                return Completion(text="done first", finish_reason="stop")
            return super().complete(*a, **k)

    agent.provider = Finite()
    agent.process_inbox_once()
    assert _events(agent, "trip") == []
    pending = [m.text for m in messaging.pending(paths, "atlas")]
    assert pending == ["already queued"]
    toolsmod._DEFAULT_TOOLS = None


def test_escape_after_grace_marks_zombie_and_pumps_queue(tmp_path, monkeypatch):
    agent, paths = _setup(tmp_path, monkeypatch, watchdog="0.15", grace="0.15")
    release = threading.Event()

    class WedgeWithFollowup(WedgedProvider):
        def complete(self, *args, **kwargs):
            # Inject only once the original turn has entered its provider call.
            # A timed sender can run before admission on a busy CI runner,
            # causing Send-now to be dequeued first with no later chat to trip
            # the watchdog.
            messaging.send(
                paths, messaging.Msg(to="atlas", frm="user", text="still here", now=True)
            )
            return super().complete(*args, **kwargs)

    agent.provider = WedgeWithFollowup(release)
    stuck = messaging.Msg(to="atlas", frm="user", text="wedge me")
    messaging.send(paths, stuck)

    try:
        agent.process_inbox_once()  # returns once the wedged run escapes

        assert [e["reason"] for e in _events(agent, "trip")] == ["watchdog"]
        assert len(_events(agent, "escape")) == 1
        # The wedged message was settled with an error so the queue can move on.
        assert [m.text for m in messaging.pending(paths, "atlas")] == ["still here"]
        replies = [m.text for _p, m in messaging.read_inbox(paths, "user")]
        assert any("abandoned" in t for t in replies)
        assert len(agent.scheduler.zombies) == 1

        # Let the zombie settle late and check it is recorded, not resurrected.
        zombie = agent.scheduler.zombies[0]
        release.set()
        zombie.thread.join(timeout=5)
        assert not zombie.thread.is_alive()
        assert len(_events(agent, "late_settle")) == 1
        assert agent.scheduler.zombies == []
        # Generation guard: the zombie's late output never reached the stream.
        stream = paths.stream_file(stuck.id).read_text(encoding="utf-8")
        assert "zombie output" not in stream
        assert "abandoned" in stream
    finally:
        release.set()


def test_generation_guard_blocks_superseded_writer(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    sched = TurnScheduler(watchdog_secs=1, grace_secs=1)
    run = sched.begin("m1", messaging.LANE_USER)
    writer = GuardedStreamWriter(StreamWriter(paths, "m1"), sched, run)
    writer.delta("live")
    assert sched.escape(run)
    writer.delta("zombie-delta")
    writer.final("zombie-final", "atlas")
    content = paths.stream_file("m1").read_text(encoding="utf-8")
    assert "live" in content
    assert "zombie" not in content
    # A newer generation also supersedes a still-active older run.
    newer = sched.begin("m2", messaging.LANE_USER)
    assert sched.is_current(newer)
    assert not sched.is_current(run)


# -- takeover hook ---------------------------------------------------------


def test_takeover_does_not_interrupt_active_run(tmp_path, monkeypatch):
    from agent import tools as toolsmod

    agent, paths = _setup(tmp_path, monkeypatch, watchdog="60", grace="60")
    agent.provider = LoopTool()
    calls = {"n": 0}

    def grab(ctx, args):
        calls["n"] += 1
        if calls["n"] == 1:
            agent.control.take_over("atlas")
        time.sleep(0.02)
        return "ok: remembered"

    monkeypatch.setattr(toolsmod, "_remember", grab)
    toolsmod._DEFAULT_TOOLS = None
    messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="drive the computer"))

    agent.process_inbox_once()

    assert _events(agent, "trip") == []
    assert messaging.pending(paths, "atlas") == []
    replies = [m.text for _p, m in messaging.read_inbox(paths, "user")]
    assert replies
    toolsmod._DEFAULT_TOOLS = None


def test_env_knobs_configure_thresholds(tmp_path, monkeypatch):
    agent, _paths = _setup(tmp_path, monkeypatch, watchdog="0.25", grace="0.5")
    assert agent.scheduler.watchdog_secs == 0.25
    assert agent.scheduler.grace_secs == 0.5
