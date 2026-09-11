"""Idle-browser close: the per-bot activity clock and the serve-loop sweep."""

from __future__ import annotations

import threading

from agent.computer import HostComputer
from harness import browser_idle, server
from harness.paths import HarnessPaths


def _paths(tmp_path) -> HarnessPaths:
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas", "nova"])
    return p


def test_touch_and_idle_clock(tmp_path):
    paths = _paths(tmp_path)
    assert browser_idle.last_activity(paths, "atlas") is None
    # never used: never idle, nothing to close
    assert not browser_idle.is_idle(paths, "atlas", limit_s=60, now=10_000)
    browser_idle.touch(paths, "atlas", now=1_000)
    assert browser_idle.last_activity(paths, "atlas") == 1_000
    assert not browser_idle.is_idle(paths, "atlas", limit_s=600, now=1_500)
    assert browser_idle.is_idle(paths, "atlas", limit_s=600, now=1_600)


def test_computer_actions_reset_the_clock(tmp_path):
    """Every computer_* action the bot takes counts as activity, even one
    that fails — the bot is clearly at the keyboard."""
    paths = _paths(tmp_path)
    comp = HostComputer(paths=paths, bot="atlas", machine=None)
    assert browser_idle.last_activity(paths, "atlas") is None
    out = comp.act("no-such-action")
    assert out.startswith("error:")
    assert browser_idle.last_activity(paths, "atlas") is not None
    # a driver without a bot identity records nothing
    HostComputer(paths=paths, bot=None).act("no-such-action")
    assert browser_idle.last_activity(paths, "nova") is None


def test_sweep_closes_only_idle_unheld_bots(tmp_path):
    paths = _paths(tmp_path)
    paths.ensure_layout(["atlas", "nova", "held", "fresh"])
    now = 100_000.0
    for bot in ("atlas", "nova", "held"):
        browser_idle.touch(paths, bot, now=now - 3600)
    browser_idle.touch(paths, "fresh", now=now - 60)
    closed_calls: list[str] = []

    def close(bot: str) -> bool:
        closed_calls.append(bot)
        return bot != "nova"  # nova had no browser running

    logs: list[str] = []
    closed = browser_idle.sweep(
        paths,
        ["atlas", "nova", "held", "fresh", "never"],
        close_browser=close,
        control_held=lambda b: b == "held",
        limit_s=1800,
        now=now,
        log=logs.append,
    )
    assert closed == ["atlas"]
    assert closed_calls == ["atlas", "nova"], "held and fresh bots are never probed"
    assert any("atlas" in m and "closed" in m for m in logs)
    # closing reset atlas's clock so the next tick does not re-probe it
    assert browser_idle.last_activity(paths, "atlas") == now
    # nova's did not move: nothing was closed
    assert browser_idle.last_activity(paths, "nova") == now - 3600


def test_sweep_isolates_a_failing_machine(tmp_path):
    paths = _paths(tmp_path)
    now = 5_000.0
    browser_idle.touch(paths, "atlas", now=now - 7200)
    browser_idle.touch(paths, "nova", now=now - 7200)

    def close(bot: str) -> bool:
        if bot == "atlas":
            raise RuntimeError("engine wedged")
        return True

    logs: list[str] = []
    closed = browser_idle.sweep(
        paths,
        ["atlas", "nova"],
        close_browser=close,
        control_held=lambda b: False,
        limit_s=60,
        now=now,
        log=logs.append,
    )
    assert closed == ["nova"]
    assert any("atlas" in m and "wedged" in m for m in logs)


def test_settings_defaults_and_disable(monkeypatch):
    monkeypatch.delenv("HARNESS_BROWSER_IDLE_MINUTES", raising=False)
    monkeypatch.delenv("HARNESS_BROWSER_IDLE_SWEEP_INTERVAL", raising=False)
    assert browser_idle.idle_minutes() == 30
    assert browser_idle.sweep_interval() == 120
    monkeypatch.setenv("HARNESS_BROWSER_IDLE_MINUTES", "0")
    assert browser_idle.idle_minutes() == 0
    monkeypatch.setenv("HARNESS_BROWSER_IDLE_MINUTES", "junk")
    assert browser_idle.idle_minutes() == 30


class _Orch:
    def __init__(self, backend_name: str):
        self.backend_name = backend_name
        self.backend = type("B", (), {"close_browser": staticmethod(lambda bot: False)})()


def _threads() -> set[str]:
    return {t.name for t in threading.enumerate()}


def test_sweep_timer_only_runs_on_the_machines_backend(monkeypatch):
    monkeypatch.setenv("HARNESS_BROWSER_IDLE_MINUTES", "30")
    monkeypatch.setenv("HARNESS_BROWSER_IDLE_SWEEP_INTERVAL", "3600")
    server.start_browser_idle_sweep(_Orch("process"))
    assert "browser-idle" not in _threads()
    monkeypatch.setenv("HARNESS_BROWSER_IDLE_MINUTES", "0")
    server.start_browser_idle_sweep(_Orch("machines"))
    assert "browser-idle" not in _threads(), "0 minutes disables the sweep"
    monkeypatch.setenv("HARNESS_BROWSER_IDLE_MINUTES", "30")
    server.start_browser_idle_sweep(_Orch("machines"))
    assert "browser-idle" in _threads()
