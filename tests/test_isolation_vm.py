"""Regression tests for isolation.vm — the not-yet-implemented VM backend.

The documented contract (AGENTS.md, ADR) is that `vm` raises a CLEAN
`IsolationUnavailable` naming the escape hatch, rather than half-starting a
bot. If someone begins implementing it, these tests are the reminder to bring
real coverage with the feature.
"""

import pytest

from harness.paths import HarnessPaths
from isolation.base import BotHandle, IsolationUnavailable, Status
from isolation.vm import VMBackend


@pytest.fixture
def backend(tmp_path):
    return VMBackend(HarnessPaths(home=tmp_path / "home"))


def test_spawn_raises_a_clean_unavailable_naming_the_escape_hatch(backend):
    with pytest.raises(IsolationUnavailable) as exc:
        backend.spawn("atlas", ["python", "-m", "agent", "--bot", "atlas"])
    assert "--backend process" in str(exc.value)


def test_stop_raises_unavailable_too(backend):
    handle = BotHandle(bot="atlas", backend="vm")
    with pytest.raises(IsolationUnavailable):
        backend.stop(handle)


def test_status_is_unknown_not_a_crash(backend):
    handle = BotHandle(bot="atlas", backend="vm")
    assert backend.status(handle) is Status.UNKNOWN
