"""Wall-clock cap per test.

GitHub-hosted jobs default to 6 hours. A wedged pytest billed until a later
push cancelled it. Override with ``@pytest.mark.timeout(n)`` or
``HARNESS_TEST_TIMEOUT`` (seconds; 0 disables). Unix only — CI is Ubuntu.
"""

from __future__ import annotations

import os
import signal

import pytest

DEFAULT_TEST_TIMEOUT_SECS = 30
_TIMEOUT_ENV = "HARNESS_TEST_TIMEOUT"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "timeout(seconds): wall-clock cap for this test (see tests/conftest.py)",
    )


def _seconds_for(item: pytest.Item) -> float:
    raw = os.environ.get(_TIMEOUT_ENV)
    if raw is not None:
        try:
            env_secs = float(raw)
        except ValueError:
            env_secs = float(DEFAULT_TEST_TIMEOUT_SECS)
        if env_secs <= 0:
            return 0.0
    else:
        env_secs = float(DEFAULT_TEST_TIMEOUT_SECS)
    marker = item.get_closest_marker("timeout")
    if marker and marker.args:
        try:
            return max(0.0, float(marker.args[0]))
        except (TypeError, ValueError):
            return env_secs
    return env_secs


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    seconds = _seconds_for(item)
    if seconds <= 0 or not hasattr(signal, "setitimer"):
        yield
        return
    previous = signal.getsignal(signal.SIGALRM)

    def _alarm(_signum: int, _frame: object) -> None:
        raise TimeoutError(
            f"{item.nodeid} exceeded {seconds:g}s "
            f"(set {_TIMEOUT_ENV} or @pytest.mark.timeout)"
        )

    signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
