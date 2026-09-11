"""A run that goes quiet is ended, even when nobody is waiting behind it.

The existing wedge watchdog only fires on *demand*: a second user message
starving behind the run, or a takeover. A provider that accepts a connection
and then never writes another byte, with an empty queue, tripped nothing — the
channel stayed busy and the composer stayed locked over a bot that appeared to
be thinking and was not.

The subtle half is which side keeps the clock. `Run.last_output` is stamped
where the bot *emits*, not where a client reads: timing the read measures how
promptly somebody's app collected the bytes, so a paused viewer — or no viewer
at all — would be reported as a dead bot on a healthy run.
"""

from __future__ import annotations

import time

from agent.scheduler import (
    SILENCE_DEFAULT_SECS,
    GuardedStreamWriter,
    Run,
    TurnScheduler,
    _silence_secs_from_env,
)


class _Writer:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def status(self, text: str) -> str:
        self.sent.append(text)
        return "ok"

    def delta(self, text: str) -> None:
        self.sent.append(text)

    not_callable = "an attribute, not a method"


def _run(**kw) -> Run:
    return Run(msg_id="m1", lane="user", generation=0, **kw)


# -- the clock is kept by the producer -------------------------------------


def test_emitting_stamps_last_output():
    sched = TurnScheduler(silence_secs=10)
    run = _run()
    sched.active, sched._generation = run, run.generation
    run.last_output = 0.0

    GuardedStreamWriter(_Writer(), sched, run).status("working")
    assert run.last_output > 0.0


def test_the_stamp_survives_the_writer_returning_none():
    sched = TurnScheduler(silence_secs=10)
    run = _run()
    sched.active, sched._generation = run, run.generation
    run.last_output = 0.0

    GuardedStreamWriter(_Writer(), sched, run).delta("hello")  # returns None
    assert run.last_output > 0.0


def test_a_superseded_run_does_not_stamp():
    """A zombie must not keep its own silence clock alive — it is exactly the
    run that should look stalled."""
    sched = TurnScheduler(silence_secs=10)
    run = _run()
    sched.active = None  # superseded
    run.last_output = 0.0

    GuardedStreamWriter(_Writer(), sched, run).status("working")
    assert run.last_output == 0.0


def test_non_callable_attributes_pass_straight_through():
    sched = TurnScheduler(silence_secs=10)
    run = _run()
    sched.active, sched._generation = run, run.generation
    assert GuardedStreamWriter(_Writer(), sched, run).not_callable == "an attribute, not a method"


def test_a_fresh_run_starts_its_clock_now():
    """Not at epoch zero — a run that has said nothing yet is not instantly
    stalled."""
    assert time.time() - _run().last_output < 5


# -- the setting -----------------------------------------------------------


def test_the_default_is_generous():
    """A false trip throws away a working answer, which costs more than a
    stalled turn taking a few minutes to notice."""
    assert SILENCE_DEFAULT_SECS >= 120


def test_zero_disables_it(monkeypatch):
    monkeypatch.setenv("HARNESS_RUN_SILENCE_SECS", "0")
    assert _silence_secs_from_env() == 0.0
    assert TurnScheduler().silence_secs == 0.0


def test_unset_uses_the_default(monkeypatch):
    monkeypatch.delenv("HARNESS_RUN_SILENCE_SECS", raising=False)
    assert _silence_secs_from_env() == SILENCE_DEFAULT_SECS


def test_a_garbage_value_falls_back_rather_than_crashing_the_bot(monkeypatch):
    """Startup validation refuses a bad value before this is reached; a bot
    process that somehow sees one must still start."""
    monkeypatch.setenv("HARNESS_RUN_SILENCE_SECS", "soon")
    assert _silence_secs_from_env() == SILENCE_DEFAULT_SECS


def test_a_negative_value_is_clamped_to_disabled(monkeypatch):
    monkeypatch.setenv("HARNESS_RUN_SILENCE_SECS", "-30")
    assert _silence_secs_from_env() == 0.0


def test_an_explicit_argument_beats_the_environment(monkeypatch):
    monkeypatch.setenv("HARNESS_RUN_SILENCE_SECS", "999")
    assert TurnScheduler(silence_secs=42).silence_secs == 42


def test_the_silence_watchdog_is_separate_from_the_wedge_watchdog():
    """Two different questions — 'is anyone starving behind this' and 'is this
    producing anything' — so two thresholds."""
    sched = TurnScheduler(watchdog_secs=120, silence_secs=300)
    assert sched.watchdog_secs == 120
    assert sched.silence_secs == 300


# -- the trip itself -------------------------------------------------------


def _agent(tmp_path, **sched_kw):
    from agent.memory import Memory
    from agent.runtime import Agent
    from harness.control import Control
    from harness.paths import HarnessPaths
    from harness.roster import Bot
    from providers.echo import EchoProvider

    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    agent = Agent(
        paths=paths,
        bot=Bot(name="atlas", role="an assistant", provider="echo"),
        provider=EchoProvider(),
        memory=Memory(paths=paths, bot="atlas"),
        control=Control(paths),
        stream_delay=0.0,
    )
    if sched_kw:
        agent._scheduler = TurnScheduler(**sched_kw)
    return agent


def test_a_silent_run_trips_with_an_empty_queue(tmp_path):
    """The whole point. Nothing is waiting behind this run, so the wedge
    watchdog has nothing to measure — and it used to spin forever."""
    agent = _agent(tmp_path, silence_secs=10, watchdog_secs=120)
    run = _run()
    run.last_output = time.time() - 60  # quiet for a minute
    reason, quiet = agent._trip_reason(run, time.time())
    assert reason == "silence"
    assert quiet >= 60


def test_a_run_that_just_spoke_does_not_trip(tmp_path):
    agent = _agent(tmp_path, silence_secs=10, watchdog_secs=120)
    run = _run()
    run.last_output = time.time()
    assert agent._trip_reason(run, time.time())[0] is None


def test_silence_zero_restores_the_old_behaviour(tmp_path):
    agent = _agent(tmp_path, silence_secs=0, watchdog_secs=120)
    run = _run()
    run.last_output = time.time() - 100_000
    assert agent._trip_reason(run, time.time())[0] is None


def test_a_takeover_no_longer_interrupts_a_run(tmp_path):
    """Shared computer: the human and the bot drive at the same time.

    Taking control used to trip the active run with reason "takeover". That
    check went away when the desktop became shared, so a held takeover now
    decides nothing on its own — a healthy run keeps going, and a quiet one
    is still tripped by silence, on silence's own terms.
    """
    agent = _agent(tmp_path, silence_secs=10, watchdog_secs=120)
    agent.control.take_over(agent.bot.name)

    live = _run()
    live.last_output = time.time()
    assert agent._trip_reason(live, time.time())[0] is None

    quiet = _run()
    quiet.last_output = time.time() - 60
    assert agent._trip_reason(quiet, time.time())[0] == "silence"


def test_the_user_is_told_what_happened(tmp_path):
    agent = _agent(tmp_path, silence_secs=10)
    run = _run()
    run.reason = "silence"
    agent._turn_local.run = run
    text = agent._interrupt_text()
    assert "quiet" in text or "stopped producing" in text
    assert "ask again" in text
