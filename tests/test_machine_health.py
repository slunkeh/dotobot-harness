"""A machine whose desktop has died must not read as healthy.

`_container_running` was `state == "running"` and nothing else, so the only
failure the backend could see was the whole container exiting. If Xvfb died
while the supervisor kept reaping children, every GUI action failed against a
machine the pool believed was fine.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import harness  # noqa: F401  (import order: harness before isolation)
from isolation.machines import MachineBackend

ROOT = Path(__file__).resolve().parent.parent


class _Proc:
    def __init__(self, out: str = "", code: int = 0) -> None:
        self.stdout = out
        self.stderr = ""
        self.returncode = code


def _backend(monkeypatch, *, state: str, health: str, code: int = 0):
    from isolation import engine

    def fake_run(*args, **kw):
        joined = " ".join(str(a) for a in args)
        if "Health" in joined:
            return _Proc(health, code)
        return _Proc(state)

    monkeypatch.setattr(engine, "run", fake_run)
    monkeypatch.setattr("isolation.machines.engine.run", fake_run)
    return MachineBackend.__new__(MachineBackend)


def test_a_running_healthy_machine_is_running(monkeypatch):
    b = _backend(monkeypatch, state="running", health="healthy")
    assert b._container_running("bot-atlas") is True


def test_a_running_but_unhealthy_machine_is_not(monkeypatch):
    """The whole point: the process is alive and the desktop is gone."""
    b = _backend(monkeypatch, state="running", health="unhealthy")
    assert b._container_running("bot-atlas") is False


def test_a_starting_machine_is_still_running(monkeypatch):
    """A cold machine takes a while to bring up Xvfb and the desktop. Declaring
    it dead while it boots would be worse than the bug being fixed."""
    b = _backend(monkeypatch, state="running", health="starting")
    assert b._container_running("bot-atlas") is True


def test_an_image_with_no_healthcheck_is_still_running(monkeypatch):
    """Older machine images report an empty health string. They must keep
    working exactly as before."""
    b = _backend(monkeypatch, state="running", health="")
    assert b._container_running("bot-atlas") is True


def test_a_stopped_machine_is_not_running(monkeypatch):
    b = _backend(monkeypatch, state="exited", health="")
    assert b._container_running("bot-atlas") is False


def test_a_failed_inspect_does_not_declare_a_machine_dead(monkeypatch):
    b = _backend(monkeypatch, state="running", health="", code=1)
    assert b._container_running("bot-atlas") is True


# -- the image declares one ------------------------------------------------


def test_the_machine_image_wraps_rm():
    """Desktop xterm never sees govern(), so the image must replace /bin/rm."""
    text = (ROOT / "deploy" / "Dockerfile.machine").read_text(encoding="utf-8")
    assert "machine_rm.py" in text
    assert "/usr/lib/harness/rm.real" in text
    wrapper = (ROOT / "deploy" / "machine_rm.py").read_text(encoding="utf-8")
    assert "shellguard" in wrapper
    assert wrapper.startswith("#!/usr/bin/env python3")


def test_the_machine_image_owns_the_shared_workspace():
    """A fresh named volume copies the image directory's ownership, and the
    jail has no CAP_CHOWN to fix a root-owned mount afterwards — so the image
    must create /workspace as the agent uid before USER agent."""
    text = (ROOT / "deploy" / "Dockerfile.machine").read_text(encoding="utf-8")
    lines = text.splitlines()
    chown = next(i for i, ln in enumerate(lines) if "chown 1000:1000 /workspace" in ln)
    user = next(i for i, ln in enumerate(lines) if ln.strip() == "USER agent")
    assert chown < user
    volume = next(ln for ln in lines if ln.startswith("VOLUME"))
    assert "/workspace" in volume and "/home/agent" in volume


def test_the_machine_image_declares_a_healthcheck():
    text = (ROOT / "deploy" / "Dockerfile.machine").read_text(encoding="utf-8")
    assert "HEALTHCHECK" in text
    assert "display_bound" in text


def test_the_healthcheck_start_period_covers_a_cold_desktop():
    text = (ROOT / "deploy" / "Dockerfile.machine").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if "HEALTHCHECK" in ln)
    assert "--start-period=" in line
    seconds = int(line.split("--start-period=")[1].split("s")[0])
    assert seconds >= 60, "Xvfb plus openbox/tint2/Chrome needs longer than a minute"


def test_the_machine_cmd_imports_harness_before_the_supervisor():
    """PID 1 used to be `python -m isolation.machine_supervisor`, which loads
    isolation cold and died on the circular import. CMD must import harness
    first, matching HEALTHCHECK."""
    text = (ROOT / "deploy" / "Dockerfile.machine").read_text(encoding="utf-8")
    cmd = next(ln for ln in text.splitlines() if ln.startswith("CMD "))
    assert cmd.startswith("CMD ")
    assert "import harness" in cmd
    assert "machine_supervisor" in cmd
    assert "-m" not in cmd


def test_supervisor_import_does_not_require_harness_first():
    """A fresh interpreter loading isolation.machine_supervisor (the container
    entry) must not trip isolation → harness → isolation."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import isolation.machine_supervisor as s; assert callable(s.main)",
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Traceback" not in proc.stderr


def test_the_probe_command_actually_runs():
    """The HEALTHCHECK body is a string in a Dockerfile, which nothing type
    checks. Run the same import here so a rename of `display_bound` fails the
    suite rather than every machine's health check."""
    proc = subprocess.run(
        [
            "python3",
            "-c",
            "import harness, sys; from isolation.machine_supervisor import display_bound; "
            "sys.exit(0 if display_bound() else 1)",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # There is no X display in CI, so it should exit 1 — cleanly, not with a
    # traceback, which is what would happen if the import were wrong.
    assert proc.returncode == 1
    assert "Traceback" not in proc.stderr
