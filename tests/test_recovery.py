"""Turn admission claims + restart recovery sweep.

A crash or `harness restart` must never silently drop an admitted turn: the
claim recorded at admission is found by the startup sweep and redispatched on
a charged budget; messages arriving while the process drains get an explicit
restart error; an exhausted budget tombstones the turn with a next-turn note.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from agent import messaging, recovery
from agent.runtime import build_agent
from agent.streaming import pop_turn_notes
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.roster import Bot
from harness.statestore import DEFAULT_BUDGET, StateStore
from isolation import BotHandle, Status
from providers.base import Message

ROSTER = """
[[bots]]
name = "atlas"
role = "a terse research assistant"
provider = "echo"
"""

DEAD = lambda pid: False  # noqa: E731 - "no owner process survives" in one word


def _setup(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    agent = build_agent(paths, Bot(name="atlas", role="terse", provider="echo"), stream_delay=0.0)
    return agent, paths


def _send(paths, text="hello", **kw):
    msg = messaging.Msg(to="atlas", frm="user", text=text, **kw)
    messaging.send(paths, msg)
    return msg


class _Boom(BaseException):
    """Simulates the process dying mid-turn (not caught by `except Exception`)."""


# -- admission ---------------------------------------------------------------
def test_claim_is_recorded_before_the_provider_call_and_settled_after(tmp_path, monkeypatch):
    agent, paths = _setup(tmp_path)
    msg = _send(paths)
    seen = {}
    orig = agent.provider.stream_completion

    def spying(messages, **kw):
        seen["claim"] = agent.statestore.claim(msg.id)
        return orig(messages, **kw)

    monkeypatch.setattr(agent.provider, "stream_completion", spying)
    agent.process_inbox_once()
    assert seen["claim"] is not None, "no claim existed when the provider was called"
    assert seen["claim"]["state"] == "running"
    assert seen["claim"]["bot"] == "atlas"
    assert seen["claim"]["session"] == agent.session_id
    assert agent.statestore.claim(msg.id) is None  # settled with the reply


def test_admission_failure_never_fails_the_turn(tmp_path, monkeypatch):
    agent, paths = _setup(tmp_path)

    def boom(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(agent.statestore, "admit", boom)
    monkeypatch.setattr(agent.statestore, "settle", boom)
    _send(paths, "hello there")
    assert agent.process_inbox_once() is True
    replies = [m for _p, m in messaging.read_inbox(paths, "user")]
    assert len(replies) == 1 and "hello there" in replies[0].text


# -- crash + startup sweep ---------------------------------------------------
def test_crash_mid_turn_is_found_by_the_sweep_and_redispatched(tmp_path, monkeypatch):
    agent, paths = _setup(tmp_path)
    msg = _send(paths, "important ask")

    def die(*a, **kw):
        raise _Boom

    monkeypatch.setattr(agent.provider, "stream_completion", die)
    monkeypatch.setattr(agent.provider, "complete", die)
    with pytest.raises(_Boom):
        agent.process_inbox_once()
    claim = agent.statestore.claim(msg.id)
    assert claim is not None and claim["state"] == "running"  # nothing settled it

    # "next boot": no recorded owner pid is alive
    summary = recovery.startup_sweep(agent.statestore, paths, "atlas", alive=DEAD)
    assert summary["redispatched"] == [msg.id]
    claim = agent.statestore.claim(msg.id)
    assert claim["state"] == "queued"
    assert claim["budget"] == DEFAULT_BUDGET - 1  # charged before dispatch, kept
    # exactly one copy queued: the surviving inbox file is the vehicle
    assert [m.id for m in messaging.pending(paths, "atlas")] == [msg.id]

    # the redispatched turn then completes and releases the claim
    fresh = build_agent(paths, Bot(name="atlas", role="terse", provider="echo"), stream_delay=0.0)
    assert fresh.process_inbox_once() is True
    assert fresh.statestore.claim(msg.id) is None
    replies = [m for _p, m in messaging.read_inbox(paths, "user")]
    assert any("important ask" in m.text for m in replies)


def test_sweep_resends_input_that_was_consumed_from_the_inbox(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    store = recovery.store_for(paths)
    msg = messaging.Msg(to="atlas", frm="user", text="lost work")
    recovery.admit_turn(store, bot="atlas", session="s1", msg=msg, pid=424242)
    # the inbox file is gone (steer consumed it / fs loss): only the claim knows
    summary = recovery.startup_sweep(store, paths, "atlas", alive=DEAD)
    assert summary["redispatched"] == [msg.id]
    pending = messaging.pending(paths, "atlas")
    assert [m.id for m in pending] == [msg.id]
    assert pending[0].text == "lost work"


def test_sweep_redispatches_work_owned_by_a_recycled_pid(tmp_path):
    """After a SIGKILL'd container restart the next agent often wears the dead
    owner's pid (PID 1 again). The claim's recorded boot identity no longer
    matches, so the sweep must treat it as orphaned — steer-only inputs that
    exist nowhere but the claim would otherwise be silently dropped."""
    import os

    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    store = recovery.store_for(paths)
    msg = messaging.Msg(to="atlas", frm="user", text="steer-only work")
    store.admit(
        "atlas",
        "s1",
        msg.id,
        input_json=f"[{msg.to_json()}]".replace("\n", " "),
        pid=os.getpid(),  # alive — but this claim belongs to a previous boot
        identity="python -m agent --bot atlas --generation-token=dead-boot",
    )
    summary = recovery.startup_sweep(store, paths, "atlas")  # real pid_alive
    assert summary["redispatched"] == [msg.id]
    assert [m.text for m in messaging.pending(paths, "atlas")] == ["steer-only work"]


def test_sweep_clears_stale_session_locks(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    store = recovery.store_for(paths)
    store.admit("atlas", "old-session", "r1", pid=424242)
    recovery.startup_sweep(store, paths, "atlas", alive=DEAD)
    assert store.session_state("atlas", "old-session")["state"] == "idle"


def test_sweep_leaves_live_owners_alone(tmp_path):
    agent, paths = _setup(tmp_path)
    msg = _send(paths)
    recovery.admit_turn(agent.statestore, bot="atlas", session="s1", msg=msg, pid=424242)
    summary = recovery.startup_sweep(
        agent.statestore, paths, "atlas", alive=lambda pid: pid == 424242
    )
    assert summary == {"redispatched": [], "tombstoned": [], "refunded": []}
    assert agent.statestore.claim(msg.id)["state"] == "running"
    assert agent.statestore.claim(msg.id)["budget"] == DEFAULT_BUDGET  # no charge


# -- steer -------------------------------------------------------------------
def test_steered_followup_joins_the_live_claim_and_survives_a_crash(tmp_path):
    agent, paths = _setup(tmp_path)
    first = messaging.Msg(to="atlas", frm="user", text="start")
    recovery.admit_turn(agent.statestore, bot="atlas", session=agent.session_id, msg=first)
    follow = _send(paths, "also this please")
    turn = [Message(role="user", content="start")]
    assert agent._inject_followups(turn, turn_id=first.id, thread_peer="user", room=None)
    assert messaging.pending(paths, "atlas") == []  # steer consumed the file
    assert "also this please" in agent.statestore.claim(first.id)["input_json"]

    # crash now: the sweep re-queues the original AND the consumed follow-up
    summary = recovery.startup_sweep(agent.statestore, paths, "atlas", alive=DEAD)
    assert summary["redispatched"] == [first.id]
    assert {m.id for m in messaging.pending(paths, "atlas")} == {first.id, follow.id}


# -- drain window ------------------------------------------------------------
def test_drain_rejects_new_arrivals_with_an_explicit_restart_error(tmp_path):
    agent, paths = _setup(tmp_path)
    early = _send(paths, "before drain", ts=time.time() - 5)
    agent.begin_drain()
    late = _send(paths, "after drain", ts=agent._drain_ts + 1)
    assert agent.process_inbox_once() is False  # no admissions while draining
    agent.drain_shutdown()
    replies = [m for _p, m in messaging.read_inbox(paths, "user")]
    assert [m.reply_to for m in replies] == [late.id]
    assert replies[0].text == recovery.RESTART_ERROR
    # the pre-drain queue survives for the next process; late is gone
    assert [m.id for m in messaging.pending(paths, "atlas")] == [early.id]


def test_restart_interrupt_gets_its_own_wind_down_text(tmp_path):
    """A drain-tripped run must say it is pausing for a restart, not fall
    through to the Send-now wording about a newer message that isn't there."""
    agent, _paths = _setup(tmp_path)
    run = agent.scheduler.begin("m1", "user")
    agent._turn_local.run = run  # what the worker thread would see
    agent.begin_drain()
    text = agent._interrupt_text()
    assert "restart" in text
    assert "message you sent now" not in text


def test_begin_drain_stamps_recovery_markers_and_trips_the_run(tmp_path):
    agent, paths = _setup(tmp_path)
    msg = _send(paths, "in flight")
    recovery.admit_turn(agent.statestore, bot="atlas", session=agent.session_id, msg=msg)
    run = agent.scheduler.begin(msg.id, "user")
    agent.begin_drain()
    claim = agent.statestore.claim(msg.id)
    assert claim["state"] == "interrupted" and claim["reason"] == "shutdown"
    assert run.interrupt.is_set() and run.reason == "restart"


# -- budget ------------------------------------------------------------------
def test_redispatch_charges_are_kept_on_uncertain_outcomes(tmp_path):
    """Two crashes = two charges; the budget never silently refills."""
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    store = recovery.store_for(paths)
    msg = _send(paths, "crashy")
    recovery.admit_turn(store, bot="atlas", session="s1", msg=msg, pid=424242)
    recovery.startup_sweep(store, paths, "atlas", alive=DEAD)
    assert store.claim(msg.id)["budget"] == DEFAULT_BUDGET - 1
    recovery.startup_sweep(store, paths, "atlas", alive=DEAD)
    assert store.claim(msg.id)["budget"] == DEFAULT_BUDGET - 2


def test_proven_preacceptance_rejection_refunds_the_charge(tmp_path, monkeypatch):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    store = recovery.store_for(paths)
    msg = messaging.Msg(to="atlas", frm="user", text="cannot enqueue")
    recovery.admit_turn(store, bot="atlas", session="s1", msg=msg, pid=424242)

    def refuse(*a, **kw):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(messaging, "send", refuse)
    summary = recovery.startup_sweep(store, paths, "atlas", alive=DEAD)
    assert summary["refunded"] == [msg.id]
    claim = store.claim(msg.id)
    assert claim["budget"] == DEFAULT_BUDGET  # the attempt was provably not consumed
    assert claim["state"] != "queued"  # still interrupted work for the next sweep


def test_exhausted_budget_tombstones_drops_the_input_and_notes(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    store = recovery.store_for(paths)
    msg = _send(paths, "doomed work")
    recovery.admit_turn(store, bot="atlas", session="s1", msg=msg, pid=424242)
    for _ in range(DEFAULT_BUDGET):
        assert store.charge(msg.id) is not None
    summary = recovery.startup_sweep(store, paths, "atlas", alive=DEAD)
    assert summary["tombstoned"] == [msg.id]
    assert store.claim(msg.id)["state"] == "tombstone"
    assert messaging.pending(paths, "atlas") == []  # never loop
    notes = pop_turn_notes(paths, "atlas")
    assert any("doomed work" in n for n in notes)
    # a second sweep does nothing: tombstones are terminal
    assert recovery.startup_sweep(store, paths, "atlas", alive=DEAD) == {
        "redispatched": [],
        "tombstoned": [],
        "refunded": [],
    }


def test_tombstone_note_reaches_the_next_turn_system_prompt(tmp_path, monkeypatch):
    agent, paths = _setup(tmp_path)
    msg = _send(paths, "doomed work")
    recovery.admit_turn(agent.statestore, bot="atlas", session="s1", msg=msg, pid=424242)
    for _ in range(DEFAULT_BUDGET):
        agent.statestore.charge(msg.id)
    recovery.startup_sweep(agent.statestore, paths, "atlas", alive=DEAD)

    seen = {}
    orig = agent.provider.stream_completion

    def spying(messages, **kw):
        seen["system"] = kw.get("system") or ""
        return orig(messages, **kw)

    monkeypatch.setattr(agent.provider, "stream_completion", spying)
    _send(paths, "hello again")
    agent.process_inbox_once()
    assert "doomed work" in seen["system"], "lost-turn note missing from the next-turn prompt"


def test_unrecoverable_claim_without_input_is_tombstoned_not_looped(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    store = recovery.store_for(paths)
    store.admit("atlas", "s1", "ghost", input_json="", pid=424242)
    summary = recovery.startup_sweep(store, paths, "atlas", alive=DEAD)
    assert summary["tombstoned"] == ["ghost"]
    assert store.claim("ghost")["state"] == "tombstone"


# -- restart endpoint contract ----------------------------------------------
class _FakeBackend:
    """Records spawn/stop; never touches processes or the state store."""

    id = "fake"

    def __init__(self):
        self.calls = []

    def load(self, bot):
        return None

    def spawn(self, bot, argv):
        self.calls.append(("spawn", bot))
        return BotHandle(bot=bot, backend=self.id, pid=None, status=Status.RUNNING)

    def stop(self, handle):
        self.calls.append(("stop", handle.bot))


def test_restart_does_not_wait_on_the_state_store(tmp_path):
    """The recovery sweep runs in the agent process at boot — spawn/restart
    must return even while another process holds the state store's write
    lock, keeping the restart endpoint's return-on-record contract."""
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    store = StateStore(orch.paths)
    store.admit("atlas", "s1", "r1")  # the db exists and has state
    lock = sqlite3.connect(str(store.db_path))
    try:
        lock.execute("BEGIN EXCLUSIVE")
        backend = _FakeBackend()
        orch._backend_cache = backend
        orch._backend_cache_key = orch.backend_name
        started = time.monotonic()
        handle = orch.restart("atlas")
        assert time.monotonic() - started < 1.0
        assert handle.bot == "atlas"
        assert ("spawn", "atlas") in backend.calls
    finally:
        lock.rollback()
        lock.close()
