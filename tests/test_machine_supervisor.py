"""In-machine supervisor: boot ordering, shutdown quiesce, and verified
display startup/shutdown — no Xvfb needed."""

from __future__ import annotations

import signal
import socket as socketlib

import pytest

from isolation import machine_supervisor


class _FakeXvfb:
    def __init__(self):
        self.terminated = False
        self.exit_code = None

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True


def test_run_session_boots_display_then_desktop_then_quiesces(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "local-home"))
    order: list[str] = []
    xvfb = _FakeXvfb()

    def fake_start_x():
        order.append("xvfb")
        return xvfb

    def fake_bringup(paths, *, display, no_sandbox):
        order.append(f"bringup:{display}:{no_sandbox}")
        assert str(paths.home).endswith("local-home")
        return {"display": display, "started": [], "skipped": []}

    def fake_quiesce():
        order.append("quiesce")

    def fake_idle(_seconds):
        order.append("idle")
        # simulate docker stop arriving during the wait loop
        signal.raise_signal(signal.SIGTERM)

    rc = machine_supervisor.run_session(
        bringup=fake_bringup,
        start_x=fake_start_x,
        quiesce=fake_quiesce,
        idle=fake_idle,
        reap=lambda: None,  # never waitpid inside the test process
        socket_path=tmp_path / "X0",  # never existed -> released instantly
    )
    assert rc == 0
    assert order == ["xvfb", "bringup::0:True", "idle", "quiesce"]
    assert xvfb.terminated


def test_run_session_restarts_xvfb_when_it_dies(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "local-home"))
    first = _FakeXvfb()
    second = _FakeXvfb()
    starts = [first, second]
    bringups: list[str] = []
    ticks = {"n": 0}

    def fake_start_x():
        return starts.pop(0)

    def fake_bringup(paths, *, display, no_sandbox):
        bringups.append(display)
        return {"display": display}

    def fake_idle(_seconds):
        ticks["n"] += 1
        if ticks["n"] == 1:
            first.exit_code = 1
        elif ticks["n"] >= 2:
            signal.raise_signal(signal.SIGTERM)

    rc = machine_supervisor.run_session(
        bringup=fake_bringup,
        start_x=fake_start_x,
        quiesce=lambda: None,
        idle=fake_idle,
        reap=lambda: None,
    )
    assert rc == 0
    assert bringups == [":0", ":0"]
    assert starts == []
    assert second.terminated
    assert not first.terminated


def test_run_session_fails_cleanly_without_display(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "local-home"))
    rc = machine_supervisor.run_session(
        bringup=lambda *a, **k: {},
        start_x=lambda: None,
        quiesce=lambda: None,
        socket_path=tmp_path / "X0",
    )
    assert rc == 1
    assert "Xvfb" in capsys.readouterr().err


def _bind_display_socket(path):
    server = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    return server


def test_start_xvfb_refuses_contaminated_display(tmp_path):
    """A live server already answering on the display socket means another
    generation owns this machine's one display: refuse, loudly, pre-spawn."""
    sock_path = tmp_path / "X0"
    server = _bind_display_socket(sock_path)
    try:
        with pytest.raises(RuntimeError, match="refusing contaminated startup"):
            machine_supervisor.start_xvfb(
                spawn=lambda *a, **k: pytest.fail("must not spawn onto a bound display"),
                socket_path=sock_path,
            )
    finally:
        server.close()


def test_start_xvfb_clears_stale_socket_then_boots(tmp_path):
    """A leftover socket FILE with nothing answering is an unclean-death
    relic, not contamination: clear it and boot."""
    sock_path = tmp_path / "X0"
    sock_path.touch()  # stale: exists but never accepts connections

    class _Proc:
        def poll(self):
            return None

    def fake_spawn(argv, **_kwargs):
        assert argv[0] == "Xvfb"
        assert not sock_path.exists(), "stale socket must be cleared before spawn"
        sock_path.touch()  # the new server publishing its socket
        return _Proc()

    proc = machine_supervisor.start_xvfb(spawn=fake_spawn, socket_path=sock_path)
    assert proc is not None


def test_run_session_reports_contaminated_startup(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "local-home"))

    def refused():
        raise RuntimeError("refusing contaminated startup: display :0 is already bound")

    rc = machine_supervisor.run_session(
        bringup=lambda *a, **k: pytest.fail("desktop must not come up"),
        start_x=refused,
        quiesce=lambda: None,
        socket_path=tmp_path / "X0",
    )
    assert rc == 1
    assert "refusing contaminated startup" in capsys.readouterr().err


def test_run_session_raises_when_display_stays_bound(tmp_path, monkeypatch):
    """Verified shutdown: a display still accepting connections after Xvfb
    terminate is a failed release and must raise, not report a clean stop."""
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "local-home"))
    sock_path = tmp_path / "X0"
    server = _bind_display_socket(sock_path)  # stays bound through "shutdown"
    xvfb = _FakeXvfb()

    def fake_idle(_seconds):
        signal.raise_signal(signal.SIGTERM)

    try:
        with pytest.raises(RuntimeError, match="left display"):
            machine_supervisor.run_session(
                bringup=lambda *a, **k: {},
                start_x=lambda: xvfb,
                quiesce=lambda: None,
                idle=fake_idle,
                reap=lambda: None,
                socket_path=sock_path,
                release_timeout=0.3,
            )
    finally:
        server.close()
    assert xvfb.terminated


def test_quiesce_chrome_pkills_then_waits(monkeypatch):
    calls: list[list[str]] = []

    class _Done:
        returncode = 1  # pgrep: nothing left

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _Done()

    machine_supervisor.quiesce_chrome(run=fake_run, timeout=1)
    assert calls[0][:2] == ["pkill", "-f"]
    assert calls[1][:2] == ["pgrep", "-f"]


def test_workspace_status_reads_the_shared_mount(tmp_path, monkeypatch):
    import os

    assert machine_supervisor.workspace_status(str(tmp_path / "nope")) == "missing"
    ws = tmp_path / "workspace"
    ws.mkdir()
    assert machine_supervisor.workspace_status(str(ws)) == "ok"
    # a root-owned volume from an image without the chown step: present, not
    # writable by the jail uid (os.access is faked — tests may run as root)
    monkeypatch.setattr(os, "access", lambda path, mode: False)
    assert machine_supervisor.workspace_status(str(ws)) == "read-only"
    # opted out: the image's private dir exists and is writable, but it is
    # not shared — the log must not call it ok
    monkeypatch.setattr(os, "access", lambda path, mode: True)
    monkeypatch.setenv("HARNESS_MACHINE_WORKSPACE", "0")
    assert machine_supervisor.workspace_status(str(ws)) == "disabled"


def test_run_session_reports_a_missing_shared_workspace(tmp_path, monkeypatch, capsys):
    """The failure lands in the machine log at boot, not as a mystery inside
    a bot's shell later."""
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "local-home"))
    monkeypatch.setattr(machine_supervisor, "WORKSPACE", str(tmp_path / "absent"))

    def fake_idle(_seconds):
        signal.raise_signal(signal.SIGTERM)

    rc = machine_supervisor.run_session(
        bringup=lambda paths, *, display, no_sandbox: {"display": display},
        start_x=_FakeXvfb,
        quiesce=lambda: None,
        idle=fake_idle,
        reap=lambda: None,
        socket_path=tmp_path / "X0",
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "shared workspace" in err and "is missing" in err
