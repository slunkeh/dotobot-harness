"""A wedged test must die in seconds, not hold GitHub-hosted runners."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

CONFTEST = Path(__file__).with_name("conftest.py")


@pytest.mark.skipif(not hasattr(__import__("signal"), "setitimer"), reason="no SIGALRM")
def test_a_sleeping_test_is_killed_before_it_can_hold_a_runner(tmp_path):
    (tmp_path / "conftest.py").write_text(CONFTEST.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "test_wedge.py").write_text(
        "import time\n\n\ndef test_wedge():\n    time.sleep(60)\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HARNESS_TEST_TIMEOUT"] = "1"
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(tmp_path / "test_wedge.py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    elapsed = time.monotonic() - t0
    assert proc.returncode != 0
    assert elapsed < 8
    combined = proc.stdout + proc.stderr
    assert "exceeded" in combined or "TimeoutError" in combined or "Failed" in combined
