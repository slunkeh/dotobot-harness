"""Container backend: argv contract against a fake engine CLI (no daemon)."""

from __future__ import annotations

import json
import os
import stat

import pytest

from harness.cli import main as cli_main
from harness.paths import HarnessPaths
from isolation import IsolationUnavailable, get_backend
from isolation.base import BotHandle, Status
from isolation.container import ContainerBackend

FAKE_DOCKER = """#!/bin/sh
printf '%s\\n' "$*" >> "{log}"
case "$1" in
  run) echo "abc123containerid" ;;
  inspect) echo "{inspect_state}" ;;
  rm) : ;;
esac
exit 0
"""


def _fake_engine(tmp_path, monkeypatch, inspect_state="running"):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "docker-argv.log"
    script = bindir / "docker"
    script.write_text(
        FAKE_DOCKER.format(log=log, inspect_state=inspect_state), encoding="utf-8"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.delenv("HARNESS_CONTAINER_ENGINE", raising=False)
    monkeypatch.delenv("HARNESS_CONTAINER_IMAGE", raising=False)
    return log


def _paths(tmp_path) -> HarnessPaths:
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas"])
    return p


AGENT_ARGV = ["-m", "agent", "--bot", "atlas", "--roster", "ROSTER", "--home", "HOME"]


def _argv(paths, roster) -> list[str]:
    return [
        "-m",
        "agent",
        "--bot",
        "atlas",
        "--roster",
        str(roster),
        "--home",
        str(paths.home),
    ]


def test_spawn_builds_engine_argv_contract(tmp_path, monkeypatch):
    log = _fake_engine(tmp_path, monkeypatch)
    paths = _paths(tmp_path)
    roster = tmp_path / "roster.toml"  # outside the home -> read-only mount
    roster.write_text("", encoding="utf-8")

    backend = ContainerBackend(paths)
    handle = backend.spawn("atlas", _argv(paths, roster))

    assert handle.status == Status.RUNNING
    assert handle.meta["container"] == "abc123containerid"
    assert handle.meta["name"] == "harness-atlas"

    lines = log.read_text(encoding="utf-8").splitlines()
    run_line = next(line for line in lines if line.startswith("run "))
    home = str(paths.home)
    assert f"-v {home}:{home}" in run_line
    assert f"-e HARNESS_HOME={home}" in run_line
    assert "-e HARNESS_BOT=atlas" in run_line
    # per-bot environment: not driving the host display, so no global gate
    assert "-e HARNESS_SHARED_DISPLAY=0" in run_line
    assert f"-v {roster}:{roster}:ro" in run_line
    assert "--name harness-atlas" in run_line
    # image comes right before the in-container command
    assert "dotobot python -m agent --bot atlas" in run_line

    # run file recorded for load()
    data = json.loads(paths.run_file("atlas").read_text(encoding="utf-8"))
    assert data["backend"] == "container"
    assert data["container"] == "abc123containerid"


def test_roster_inside_home_gets_no_extra_mount(tmp_path, monkeypatch):
    log = _fake_engine(tmp_path, monkeypatch)
    paths = _paths(tmp_path)
    roster = paths.home / "roster.json"
    roster.write_text("{}", encoding="utf-8")

    ContainerBackend(paths).spawn("atlas", _argv(paths, roster))
    run_line = next(
        line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("run ")
    )
    assert f"{roster}:{roster}:ro" not in run_line


def test_status_maps_engine_states(tmp_path, monkeypatch):
    _fake_engine(tmp_path, monkeypatch, inspect_state="running")
    paths = _paths(tmp_path)
    backend = ContainerBackend(paths)
    handle = BotHandle(bot="atlas", backend="container", meta={"name": "harness-atlas"})
    assert backend.status(handle) == Status.RUNNING

    _fake_engine(tmp_path, monkeypatch, inspect_state="exited")
    assert backend.status(handle) == Status.DEAD


def test_spawn_reuses_running_container(tmp_path, monkeypatch):
    log = _fake_engine(tmp_path, monkeypatch)
    paths = _paths(tmp_path)
    backend = ContainerBackend(paths)
    first = backend.spawn("atlas", _argv(paths, paths.home / "roster.json"))
    second = backend.spawn("atlas", _argv(paths, paths.home / "roster.json"))
    assert second.meta["container"] == first.meta["container"]
    run_lines = [
        line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("run ")
    ]
    assert len(run_lines) == 1


def test_stop_removes_container_and_run_file(tmp_path, monkeypatch):
    log = _fake_engine(tmp_path, monkeypatch)
    paths = _paths(tmp_path)
    backend = ContainerBackend(paths)
    handle = backend.spawn("atlas", _argv(paths, paths.home / "roster.json"))
    backend.stop(handle)
    assert not paths.run_file("atlas").is_file()
    assert any(
        line.startswith("rm -f harness-atlas")
        for line in log.read_text(encoding="utf-8").splitlines()
    )


def test_missing_engine_raises_isolation_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    monkeypatch.delenv("HARNESS_CONTAINER_ENGINE", raising=False)
    paths = _paths(tmp_path)
    with pytest.raises(IsolationUnavailable, match="docker or podman"):
        ContainerBackend(paths).spawn("atlas", AGENT_ARGV)


def test_image_override_via_env(tmp_path, monkeypatch):
    log = _fake_engine(tmp_path, monkeypatch)
    monkeypatch.setenv("HARNESS_CONTAINER_IMAGE", "custom/bots:1")
    paths = _paths(tmp_path)
    ContainerBackend(paths).spawn("atlas", _argv(paths, paths.home / "roster.json"))
    run_line = next(
        line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("run ")
    )
    assert "custom/bots:1 python -m agent" in run_line


def test_registry_builds_container_backend(tmp_path):
    backend = get_backend("container", _paths(tmp_path))
    assert isinstance(backend, ContainerBackend)


def test_cli_up_with_vm_backend_fails_cleanly(tmp_path, capsys):
    roster = tmp_path / "roster.toml"
    roster.write_text(
        '[[bots]]\nname = "atlas"\nrole = "helper"\nprovider = "echo"\n', encoding="utf-8"
    )
    rc = cli_main(
        [
            "--home",
            str(tmp_path / "home"),
            "--roster",
            str(roster),
            "--backend",
            "vm",
            "up",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "vm backend is not implemented" in err
