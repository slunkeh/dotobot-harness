"""Machines backend: pool state machine + hardened argv contract, no daemon."""

from __future__ import annotations

import json
import os
import stat
import threading
import time

import pytest

from harness.cli import build_parser
from harness.paths import HarnessPaths
from isolation import IsolationUnavailable, get_backend
from isolation.base import BotHandle, Status
from isolation.machines import (
    MACHINE_SCHEMA_VERSION,
    MACHINE_WORKSPACE,
    OWNER_LABEL,
    RUNTIME_LABEL,
    SCHEMA_LABEL,
    SECRET_BOT_LABEL,
    WORKSPACE_LABEL,
    MachineBackend,
    MachinePool,
    identity_decision,
    machine_prefix,
    runtime_fingerprint,
    workspace_volume,
)


def _owned_labels(**overrides) -> dict:
    labels = {
        OWNER_LABEL: "1",
        SCHEMA_LABEL: MACHINE_SCHEMA_VERSION,
        RUNTIME_LABEL: runtime_fingerprint(),
        SECRET_BOT_LABEL: "atlas",
    }
    labels.update(overrides)
    return labels


FAKE_DOCKER = """#!/bin/sh
printf '%s\\n' "$*" >> "{log}"
case "$1" in
  run) echo "abc123machineid" ;;
  inspect) case "$3" in
    "{{{{json .Config.Labels}}}}") echo '{labels}'; exit {inspect_rc} ;;
    *) echo "{inspect_state}"; exit {inspect_rc} ;;
  esac ;;
  exec) case "$3" in pgrep) exit 1 ;; esac ;;
  start|stop|cp) : ;;
esac
exit 0
"""

# python fake: cp round-trips a local dir standing in for /home/agent
FAKE_DOCKER_PY = """#!/usr/bin/env python3
import os, sys, tarfile
from pathlib import Path

root = Path(os.environ["FAKE_MACHINE_ROOT"])
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as fh:
    fh.write(" ".join(args) + "\\n")
cmd = args[0]
if cmd == "inspect":
    print("running"); sys.exit(0)
if cmd == "cp" and args[1] == "-":  # tar in -> extract into the machine home
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|*") as tar:
        for m in tar:
            if not m.isreg():
                continue
            rel = "/".join(p for p in m.name.split("/") if p not in ("", ".", ".."))
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(m).read())
            ns = int(m.mtime * 1_000_000_000)
            os.utime(target, ns=(ns, ns))
    sys.exit(0)
if cmd == "cp" or (cmd == "exec" and "tar" in args):
    # tar the machine home out (docker prefixes the dir name)
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        tar.add(root, arcname="agent")
    sys.exit(0)
if cmd == "exec" and "pgrep" in args:
    sys.exit(1)
sys.exit(0)
"""


def _install_fake(tmp_path, monkeypatch, script: str) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "docker"
    fake.write_text(script, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    for var in (
        "HARNESS_CONTAINER_ENGINE",
        "HARNESS_MACHINE_IMAGE",
        "HARNESS_MACHINE_POOL",
        "HARNESS_MACHINE_PREFIX",
        "HARNESS_MACHINE_SECRETS",
        "HARNESS_MACHINE_WORKSPACE",
    ):
        monkeypatch.delenv(var, raising=False)


def _fake_engine(tmp_path, monkeypatch, *, inspect_state="running", inspect_rc=0, labels=None):
    log = tmp_path / "docker-argv.log"
    _install_fake(
        tmp_path,
        monkeypatch,
        FAKE_DOCKER.format(
            log=log,
            inspect_state=inspect_state,
            inspect_rc=inspect_rc,
            labels=json.dumps(labels if labels is not None else _owned_labels()),
        ),
    )
    return log


def _paths(tmp_path) -> HarnessPaths:
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas", "nova"])
    return p


def _quiet_backend(paths, monkeypatch) -> tuple[MachineBackend, dict]:
    """Backend with sync + agent-process mechanics stubbed out for argv tests."""
    calls: dict = {"sync_start": [], "sync_up": [], "agents": [], "stopped": []}
    backend = MachineBackend(paths)
    monkeypatch.setattr(
        backend, "_sync_session_start", lambda m: calls["sync_start"].append(m.name)
    )
    monkeypatch.setattr(
        backend,
        "_sync_up",
        lambda name, receipt="up", live=False: calls["sync_up"].append(name) or {},
    )
    monkeypatch.setattr(
        backend,
        "_spawn_agent",
        lambda bot, argv, m: calls["agents"].append((bot, argv, m.name)) or (4242, "tok-test"),
    )
    monkeypatch.setattr(backend, "_stop_agent", lambda pid: calls["stopped"].append(pid))
    monkeypatch.setattr(backend, "_agent_identity_ok", lambda pid, token: True)
    monkeypatch.setattr("isolation.machines._pid_alive", lambda pid: True)
    return backend, calls


AGENT_ARGV = ["-m", "agent", "--bot", "atlas", "--roster", "R", "--home", "H"]


def test_spawn_builds_hardened_sandbox_argv(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_CHROME_CDP", raising=False)
    log = _fake_engine(tmp_path, monkeypatch, inspect_rc=1)  # no container yet
    paths = _paths(tmp_path)
    backend, calls = _quiet_backend(paths, monkeypatch)

    handle = backend.spawn("atlas", AGENT_ARGV)
    assert handle.status == Status.RUNNING
    assert handle.meta["machine"] == "harness-machine-0"

    run_line = next(
        line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("run ")
    )
    # Home and shared projects are writable; only explicitly granted credentials
    # get a separate read-only bind. The harness home never rides in.
    assert "-v harness-machine-0-home:/home/agent" in run_line
    assert f"-v harness-machine-workspace:{MACHINE_WORKSPACE}" in run_line
    assert run_line.count(" -v ") == 3
    assert f"{paths.run}/bot-secrets/atlas:/run/harness:ro" in run_line
    assert list((paths.run / "bot-secrets" / "atlas").iterdir()) == []
    home = str(paths.home)
    assert f"{home}:{home}" not in run_line
    assert str(paths.workspace) not in run_line, "host workspace/ is never bind-mounted"
    assert "HARNESS_HOME" not in run_line
    # sandbox hardening
    assert "--cap-drop ALL" in run_line
    assert "--security-opt no-new-privileges" in run_line
    assert "--security-opt seccomp=unconfined" in run_line
    assert "--pids-limit 512" in run_line
    assert run_line.rstrip().endswith("agent-harness-machine")
    # content-addressed identity labels
    assert f"--label {OWNER_LABEL}=1" in run_line
    assert f"--label {SCHEMA_LABEL}={MACHINE_SCHEMA_VERSION}" in run_line
    assert f"--label {RUNTIME_LABEL}={runtime_fingerprint()}" in run_line
    assert f"--label {WORKSPACE_LABEL}=1" in run_line
    # dock launch-chrome reads this at exec time
    assert "-e HARNESS_CHROME_CDP=0" in run_line

    # clone-down ran, then the agent spawned as a host process
    assert calls["sync_start"] == ["harness-machine-0"]
    assert calls["agents"] == [("atlas", AGENT_ARGV, "harness-machine-0")]

    data = json.loads(paths.run_file("atlas").read_text(encoding="utf-8"))
    assert data["backend"] == "machines"
    assert data["machine"] == "harness-machine-0"
    assert data["pid"] == 4242


def test_shared_workspace_can_be_left_out(tmp_path, monkeypatch):
    """HARNESS_MACHINE_WORKSPACE=0 spawns a machine with only its home volume."""
    log = _fake_engine(tmp_path, monkeypatch, inspect_rc=1)  # clears machine env
    monkeypatch.setenv("HARNESS_MACHINE_WORKSPACE", "0")
    paths = _paths(tmp_path)
    backend, _calls = _quiet_backend(paths, monkeypatch)
    backend.spawn("atlas", AGENT_ARGV)
    run_line = next(
        line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("run ")
    )
    assert "-v harness-machine-0-home:/home/agent" in run_line
    assert f":{MACHINE_WORKSPACE}" not in run_line
    assert run_line.count(" -v ") == 2
    # the supervisor is told, so its boot log says 'disabled' rather than 'ok'
    assert "-e HARNESS_MACHINE_WORKSPACE=0" in run_line
    assert f"--label {WORKSPACE_LABEL}=0" in run_line


def test_toggling_the_workspace_switch_recreates_the_container(monkeypatch):
    """The mount is part of the container's identity: a container created
    with the shared volume is recreated (home volume kept) when the host
    opts out, and vice versa — so an existing schema-2 machine can never be
    reused with a mount that disagrees with the bot's prompt."""
    monkeypatch.delenv("HARNESS_MACHINE_WORKSPACE", raising=False)
    mounted = _owned_labels(**{WORKSPACE_LABEL: "1"})
    private = _owned_labels(**{WORKSPACE_LABEL: "0"})
    assert identity_decision(mounted, runtime_fingerprint()) == "ok"
    assert identity_decision(private, runtime_fingerprint()) == "recreate"
    # a schema-2 container from before the label was created with the mount
    assert identity_decision(_owned_labels(), runtime_fingerprint()) == "ok"
    monkeypatch.setenv("HARNESS_MACHINE_WORKSPACE", "0")
    assert identity_decision(mounted, runtime_fingerprint()) == "recreate"
    assert identity_decision(private, runtime_fingerprint()) == "ok"
    assert identity_decision(_owned_labels(), runtime_fingerprint()) == "recreate"
    # explicit override, for callers that already know what they want
    assert identity_decision(mounted, runtime_fingerprint(), workspace=True) == "ok"


def test_opting_out_recreates_a_live_mounted_machine(tmp_path, monkeypatch):
    """End to end through spawn: a running container labelled as mounted is
    stopped, removed and re-run without the volume once the host opts out.
    The home volume is never touched."""
    log = _fake_engine(
        tmp_path,
        monkeypatch,
        inspect_state="running",
        labels=_owned_labels(**{WORKSPACE_LABEL: "1"}),
    )
    monkeypatch.setenv("HARNESS_MACHINE_WORKSPACE", "0")
    paths = _paths(tmp_path)
    backend, _calls = _quiet_backend(paths, monkeypatch)
    backend.spawn("atlas", AGENT_ARGV)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("stop ") for line in lines)
    assert any(line.startswith("rm harness-machine-0") for line in lines)
    run_line = next(line for line in lines if line.startswith("run "))
    assert f":{MACHINE_WORKSPACE}" not in run_line
    assert f"--label {WORKSPACE_LABEL}=0" in run_line
    assert not any("volume" in line for line in lines), "the machine's disk is never touched"


def test_agent_process_learns_whether_workspace_is_shared(tmp_path, monkeypatch):
    """agent.runtime gates _WORKSPACE_PROMPT on the env the backend sets from
    the same switch that decided the mount — never on the host's raw env."""
    import subprocess as sp

    from isolation.machines import Machine

    seen: list[dict] = []

    class _Proc:
        pid = 4242

    monkeypatch.setattr(sp, "Popen", lambda *a, **kw: seen.append(kw["env"]) or _Proc())
    paths = _paths(tmp_path)
    backend = MachineBackend(paths)
    machine = Machine(id=0, name="harness-machine-0")
    monkeypatch.delenv("HARNESS_MACHINE_WORKSPACE", raising=False)
    backend._spawn_agent("atlas", AGENT_ARGV, machine)
    monkeypatch.setenv("HARNESS_MACHINE_WORKSPACE", "0")
    backend._spawn_agent("atlas", AGENT_ARGV, machine)
    assert [e["HARNESS_MACHINE_WORKSPACE"] for e in seen] == ["1", "0"]
    assert all(e["HARNESS_MACHINE_NAME"] == "harness-machine-0" for e in seen)


def test_workspace_volume_is_one_per_harness_home(monkeypatch):
    """Every machine on a home shares one volume; it follows the prefix so two
    homes on one daemon keep separate workspaces."""
    monkeypatch.delenv("HARNESS_MACHINE_PREFIX", raising=False)
    assert workspace_volume() == "harness-machine-workspace"
    monkeypatch.setenv("HARNESS_MACHINE_PREFIX", "hm-alice")
    assert workspace_volume() == "hm-alice-workspace"


def test_machines_from_before_the_workspace_mount_are_recreated():
    """The mount is a container-contract change: a machine built under schema
    1 (home volume only) must be recreated — home volume kept — so it gets
    /workspace instead of running without it forever."""
    assert MACHINE_SCHEMA_VERSION != "1"
    stale = _owned_labels(**{SCHEMA_LABEL: "1"})
    assert identity_decision(stale, runtime_fingerprint()) == "recreate"
    assert identity_decision(_owned_labels(), runtime_fingerprint()) == "ok"


def test_spawn_returns_before_display_is_healthy(tmp_path, monkeypatch):
    """Restart/API must not wait on Xvfb. Record the agent and return; the
    display probe still runs, but off the caller thread."""
    _fake_engine(tmp_path, monkeypatch, inspect_rc=1)
    paths = _paths(tmp_path)
    backend, calls = _quiet_backend(paths, monkeypatch)
    release = threading.Event()
    probed = threading.Event()

    def health(_name):
        probed.set()
        release.wait(timeout=2)
        return "healthy"

    monkeypatch.setattr(backend, "_container_health", health)
    handle = backend.spawn("atlas", AGENT_ARGV)
    assert handle.status == Status.RUNNING
    assert handle.pid == 4242
    assert json.loads(paths.run_file("atlas").read_text(encoding="utf-8"))["pid"] == 4242
    assert not release.is_set(), "spawn blocked on display health"
    release.set()
    assert probed.wait(timeout=2)
    assert calls["agents"] == [("atlas", AGENT_ARGV, "harness-machine-0")]


def test_spawn_waits_for_healthy_display(tmp_path, monkeypatch):
    _fake_engine(tmp_path, monkeypatch, inspect_rc=1)
    paths = _paths(tmp_path)
    backend, calls = _quiet_backend(paths, monkeypatch)
    hits = {"n": 0}

    def health(_name):
        hits["n"] += 1
        return "starting" if hits["n"] < 3 else "healthy"

    monkeypatch.setattr(backend, "_container_health", health)
    monkeypatch.setattr("isolation.machines.time.sleep", lambda _s: None)
    backend.spawn("atlas", AGENT_ARGV)
    deadline = time.time() + 2
    while hits["n"] < 3 and time.time() < deadline:
        time.sleep(0.01)
    assert hits["n"] >= 3
    assert calls["agents"] == [("atlas", AGENT_ARGV, "harness-machine-0")]


def test_spawn_does_not_block_without_a_healthcheck(tmp_path, monkeypatch):
    _fake_engine(tmp_path, monkeypatch, inspect_rc=1)
    paths = _paths(tmp_path)
    backend, calls = _quiet_backend(paths, monkeypatch)
    hits: list[str] = []
    monkeypatch.setattr(backend, "_container_health", lambda n: hits.append(n) or "")
    backend.spawn("atlas", AGENT_ARGV)
    deadline = time.time() + 2
    while not hits and time.time() < deadline:
        time.sleep(0.01)
    assert hits == ["harness-machine-0"]
    assert calls["agents"]


def test_spawn_is_idempotent_for_a_running_bot(tmp_path, monkeypatch):
    log = _fake_engine(tmp_path, monkeypatch)
    paths = _paths(tmp_path)
    backend, calls = _quiet_backend(paths, monkeypatch)

    first = backend.spawn("atlas", AGENT_ARGV)
    second = backend.spawn("atlas", AGENT_ARGV)
    assert second.meta["machine"] == first.meta["machine"]
    assert len(calls["agents"]) == 1
    assert not any(
        line.startswith("run ") for line in log.read_text(encoding="utf-8").splitlines()
    ), "existing running container must be reused"


def test_each_bot_gets_its_own_machine_and_release_reuses(tmp_path, monkeypatch):
    _fake_engine(tmp_path, monkeypatch, inspect_rc=1)
    paths = _paths(tmp_path)
    backend, _calls = _quiet_backend(paths, monkeypatch)

    a = backend.spawn("atlas", AGENT_ARGV)
    b = backend.spawn("nova", AGENT_ARGV)
    assert a.meta["machine"] == "harness-machine-0"
    assert b.meta["machine"] == "harness-machine-1"

    backend.stop(a)
    c = backend.spawn("lyra", AGENT_ARGV)
    assert c.meta["machine"] == "harness-machine-0"  # lowest idle reused


def test_pool_cap_exhaustion_raises(tmp_path, monkeypatch):
    _fake_engine(tmp_path, monkeypatch, inspect_rc=1)
    monkeypatch.setenv("HARNESS_MACHINE_POOL", "1")
    paths = _paths(tmp_path)
    backend, _calls = _quiet_backend(paths, monkeypatch)

    backend.spawn("atlas", AGENT_ARGV)
    with pytest.raises(IsolationUnavailable, match="pool exhausted"):
        backend.spawn("nova", AGENT_ARGV)


def test_stale_busy_machine_is_reclaimed(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    pool = MachinePool(paths, cap=1)
    stale = {"id": 0, "name": "harness-machine-0", "state": "busy", "bot": "ghost", "since": 1.0}
    paths.machines_file().write_text(json.dumps({"machines": [stale]}), encoding="utf-8")

    machine = pool.acquire("atlas", lambda name: False)  # container is down
    assert machine.id == 0
    assert machine.bot == "atlas"


def test_fresh_busy_machine_is_not_stolen(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    pool = MachinePool(paths, cap=1)
    fresh = {
        "id": 0,
        "name": "harness-machine-0",
        "state": "busy",
        "bot": "ghost",
        "since": time.time(),
    }
    paths.machines_file().write_text(json.dumps({"machines": [fresh]}), encoding="utf-8")

    with pytest.raises(IsolationUnavailable, match="pool exhausted"):
        pool.acquire("atlas", lambda name: False)


def test_machine_prefix_defaults_and_custom(monkeypatch):
    monkeypatch.delenv("HARNESS_MACHINE_PREFIX", raising=False)
    assert machine_prefix() == "harness-machine"
    monkeypatch.setenv("HARNESS_MACHINE_PREFIX", "hm-alice")
    assert machine_prefix() == "hm-alice"
    monkeypatch.setenv("HARNESS_MACHINE_PREFIX", "  hm-bob  ")
    assert machine_prefix() == "hm-bob"


def test_machine_prefix_rejects_unsafe_docker_names(monkeypatch):
    monkeypatch.setenv("HARNESS_MACHINE_PREFIX", "-leading-dash")
    with pytest.raises(IsolationUnavailable, match="HARNESS_MACHINE_PREFIX"):
        machine_prefix()
    monkeypatch.setenv("HARNESS_MACHINE_PREFIX", "has space")
    with pytest.raises(IsolationUnavailable, match="HARNESS_MACHINE_PREFIX"):
        machine_prefix()


def test_acquire_uses_custom_prefix_for_new_machines(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_MACHINE_PREFIX", "hm-alice")
    paths = _paths(tmp_path)
    pool = MachinePool(paths)
    first = pool.acquire("atlas", lambda name: False)
    assert first.name == "hm-alice-0"
    assert first.volume == "hm-alice-0-home"
    second = pool.acquire("nova", lambda name: False)
    assert second.name == "hm-alice-1"


def test_acquire_keeps_existing_machine_names(tmp_path, monkeypatch):
    """A prefix change must not rename machines already in the pool file."""
    monkeypatch.setenv("HARNESS_MACHINE_PREFIX", "hm-alice")
    paths = _paths(tmp_path)
    existing = {
        "id": 0,
        "name": "harness-machine-0",
        "state": "idle",
        "bot": None,
        "since": 1.0,
    }
    paths.machines_file().write_text(json.dumps({"machines": [existing]}), encoding="utf-8")
    pool = MachinePool(paths)
    reused = pool.acquire("atlas", lambda name: False)
    assert reused.name == "harness-machine-0"
    grown = pool.acquire("nova", lambda name: False)
    assert grown.name == "hm-alice-1"


def test_corrupt_machines_json_treated_as_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_MACHINE_PREFIX", raising=False)
    paths = _paths(tmp_path)
    paths.machines_file().write_text("{not json", encoding="utf-8")
    pool = MachinePool(paths)
    assert pool.machines() == []
    assert pool.acquire("atlas", lambda name: False).name == "harness-machine-0"


def test_machines_json_with_unknown_fields_still_loads(tmp_path):
    """State written by a newer harness (extra keys) must not crash this one."""
    paths = _paths(tmp_path)
    entry = {
        "id": 0,
        "name": "harness-machine-0",
        "state": "idle",
        "bot": None,
        "since": 0.0,
        "added_by_future_version": True,
    }
    paths.machines_file().write_text(json.dumps({"machines": [entry]}), encoding="utf-8")
    pool = MachinePool(paths)
    machines = pool.machines()
    assert len(machines) == 1
    assert machines[0].name == "harness-machine-0"


def test_concurrent_acquire_hands_out_distinct_machines(tmp_path):
    paths = _paths(tmp_path)
    pool = MachinePool(paths)
    got: list[str] = []

    def grab(bot: str) -> None:
        got.append(pool.acquire(bot, lambda name: False).name)

    threads = [threading.Thread(target=grab, args=(f"bot{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(got)) == 4


def test_stop_syncs_then_stops_but_never_removes(tmp_path, monkeypatch):
    log = _fake_engine(tmp_path, monkeypatch)
    paths = _paths(tmp_path)
    backend, calls = _quiet_backend(paths, monkeypatch)
    handle = backend.spawn("atlas", AGENT_ARGV)
    paths.machine_dirty_file(0).write_text("1", encoding="utf-8")

    backend.stop(handle)

    assert calls["stopped"] == [4242]  # agent process stopped first
    assert calls["sync_up"] == ["harness-machine-0"]  # then merged up
    assert not paths.machine_dirty_file(0).exists()
    lines = log.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("stop -t ") for line in lines)
    assert not any(line.startswith("rm") for line in lines), "stop must not remove"
    assert not paths.run_file("atlas").is_file()
    # machine released back to the pool
    pool_state = json.loads(paths.machines_file().read_text(encoding="utf-8"))
    assert pool_state["machines"][0]["state"] == "idle"


FAKE_DOCKER_DRIFT = """#!/bin/sh
printf '%s\\n' "$*" >> "{log}"
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then echo "sha256:{image_id}"; exit 0; fi
if [ "$1" = "inspect" ]; then
  case "$3" in
    "{{{{.State.Status}}}}") echo "{state}"; exit 0 ;;
    "{{{{.Image}}}}") echo "sha256:{container_image}"; exit 0 ;;
    "{{{{json .Config.Labels}}}}") echo '{labels}'; exit 0 ;;
  esac
fi
case "$1" in exec) case "$3" in pgrep) exit 1 ;; esac ;; esac
exit 0
"""


def _drift_engine(
    tmp_path, monkeypatch, *, image_id: str, container_image: str, labels=None, state="exited"
):
    log = tmp_path / "docker-argv.log"
    _install_fake(
        tmp_path,
        monkeypatch,
        FAKE_DOCKER_DRIFT.format(
            log=log,
            image_id=image_id,
            container_image=container_image,
            state=state,
            labels=json.dumps(labels if labels is not None else _owned_labels()),
        ),
    )
    return log


def test_stopped_container_on_stale_image_is_recreated(tmp_path, monkeypatch):
    """`docker build` alone must be enough to update a machine: a stopped
    container from an older image is recreated (same name, same home volume)
    instead of restarting the old baked-in desktop forever."""
    log = _drift_engine(tmp_path, monkeypatch, image_id="new", container_image="old")
    paths = _paths(tmp_path)
    backend, _calls = _quiet_backend(paths, monkeypatch)

    backend.spawn("atlas", AGENT_ARGV)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert "rm harness-machine-0" in lines
    run_line = next(line for line in lines if line.startswith("run "))
    assert "-v harness-machine-0-home:/home/agent" in run_line
    assert not any(line.startswith("start ") for line in lines)


def test_stopped_container_on_current_image_is_started(tmp_path, monkeypatch):
    log = _drift_engine(tmp_path, monkeypatch, image_id="same", container_image="same")
    paths = _paths(tmp_path)
    backend, _calls = _quiet_backend(paths, monkeypatch)

    backend.spawn("atlas", AGENT_ARGV)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith("rm") for line in lines), "same image: never remove"
    assert "start harness-machine-0" in lines


def test_unowned_container_is_refused_and_never_removed(tmp_path, monkeypatch):
    """A same-named container without the owner label is NOT ours: loud
    refusal, and `rm` must never run against it."""
    log = _fake_engine(tmp_path, monkeypatch, labels={})
    paths = _paths(tmp_path)
    backend, calls = _quiet_backend(paths, monkeypatch)

    with pytest.raises(IsolationUnavailable, match="owner label"):
        backend.spawn("atlas", AGENT_ARGV)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith("rm") for line in lines), "never rm an unowned container"
    assert not any(line.startswith("run ") for line in lines)
    assert calls["agents"] == []


def test_runtime_hash_drift_recreates_but_keeps_volume(tmp_path, monkeypatch):
    """Injected-runtime drift (label hash != current fingerprint) recreates
    the container — stop + rm — while the named home volume survives."""
    log = _fake_engine(tmp_path, monkeypatch, labels=_owned_labels(**{RUNTIME_LABEL: "stale-hash"}))
    paths = _paths(tmp_path)
    backend, _calls = _quiet_backend(paths, monkeypatch)

    backend.spawn("atlas", AGENT_ARGV)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("stop -t ") for line in lines)  # running: stop first
    assert "rm harness-machine-0" in lines
    assert not any("volume" in line for line in lines), "the machine's disk is never touched"
    run_line = next(line for line in lines if line.startswith("run "))
    assert "-v harness-machine-0-home:/home/agent" in run_line  # same disk, new container
    assert f"--label {RUNTIME_LABEL}={runtime_fingerprint()}" in run_line


def test_schema_version_drift_recreates(tmp_path, monkeypatch):
    log = _fake_engine(tmp_path, monkeypatch, labels=_owned_labels(**{SCHEMA_LABEL: "0"}))
    paths = _paths(tmp_path)
    backend, _calls = _quiet_backend(paths, monkeypatch)

    backend.spawn("atlas", AGENT_ARGV)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert "rm harness-machine-0" in lines
    assert any(line.startswith("run ") for line in lines)


def test_machine_secrets_ride_as_files_not_env_values(tmp_path, monkeypatch):
    """A secret's VALUE never appears in the docker argv: it is staged as a
    0600 file in a per-machine dir mounted read-only, and the env var the
    machine sees carries only the file path."""
    log = _fake_engine(tmp_path, monkeypatch, inspect_rc=1)  # no container yet
    paths = _paths(tmp_path)
    backend, _calls = _quiet_backend(paths, monkeypatch)
    monkeypatch.setenv("HARNESS_MACHINE_SECRETS", "XAI_API_KEY")
    monkeypatch.setenv("XAI_API_KEY", "sk-super-secret")

    backend.spawn("atlas", AGENT_ARGV)

    run_line = next(
        line for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("run ")
    )
    assert "sk-super-secret" not in run_line
    host_dir = paths.run / "bot-secrets" / "atlas"
    assert f"-v {host_dir}:/run/harness:ro" in run_line
    assert "-e HARNESS_SECRET_XAI_API_KEY_FILE=/run/harness/XAI_API_KEY" in run_line
    secret_file = host_dir / "XAI_API_KEY"
    assert secret_file.read_text(encoding="utf-8") == "sk-super-secret"
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(host_dir.stat().st_mode) == 0o700


def test_stop_never_signals_a_recycled_pid(tmp_path, monkeypatch):
    """Recorded pid alive but wearing someone else's cmdline (pid reuse):
    stop must not SIGTERM it, yet still retires the record and the pool slot."""
    _fake_engine(tmp_path, monkeypatch)
    paths = _paths(tmp_path)
    backend, calls = _quiet_backend(paths, monkeypatch)
    handle = backend.spawn("atlas", AGENT_ARGV)

    monkeypatch.setattr(backend, "_agent_identity_ok", lambda pid, token: False)
    backend.stop(handle)

    assert calls["stopped"] == [], "an innocent recycled pid must never be signaled"
    assert not paths.run_file("atlas").is_file()
    pool_state = json.loads(paths.machines_file().read_text(encoding="utf-8"))
    assert pool_state["machines"][0]["state"] == "idle"


def test_spawn_adopts_verified_agent_but_replaces_recycled_pid(tmp_path, monkeypatch):
    """Harness restart: a token-verified running agent is adopted untouched;
    a pid whose cmdline lost our token reads as dead and is respawned."""
    _fake_engine(tmp_path, monkeypatch)
    paths = _paths(tmp_path)
    backend, calls = _quiet_backend(paths, monkeypatch)

    first = backend.spawn("atlas", AGENT_ARGV)
    adopted = backend.spawn("atlas", AGENT_ARGV)  # identity ok -> adopt
    assert adopted.pid == first.pid
    assert len(calls["agents"]) == 1

    monkeypatch.setattr(backend, "_agent_identity_ok", lambda pid, token: False)
    replaced = backend.spawn("atlas", AGENT_ARGV)
    assert len(calls["agents"]) == 2
    assert replaced.meta["machine"] == first.meta["machine"]


def test_status_dead_when_cmdline_loses_generation_token(tmp_path, monkeypatch):
    _fake_engine(tmp_path, monkeypatch)
    paths = _paths(tmp_path)
    backend = MachineBackend(paths)
    handle = BotHandle(
        bot="atlas",
        backend="machines",
        pid=4242,
        meta={"machine": "harness-machine-0", "token": "tok-test"},
    )
    monkeypatch.setattr("isolation.machines._pid_alive", lambda pid: True)
    monkeypatch.setattr(backend, "_agent_identity_ok", lambda pid, token: False)
    assert backend.status(handle) == Status.DEAD


def test_stop_releases_pool_even_when_engine_wedges(tmp_path, monkeypatch):
    """A quiesce/sync timeout must not leave a stale run file routing the
    user's screen and clicks at a machine this bot no longer owns."""
    import subprocess as sp

    _fake_engine(tmp_path, monkeypatch)
    paths = _paths(tmp_path)
    backend, _calls = _quiet_backend(paths, monkeypatch)
    handle = backend.spawn("atlas", AGENT_ARGV)

    def _wedged(name):
        raise sp.TimeoutExpired(cmd="docker exec", timeout=1)

    monkeypatch.setattr(backend, "_quiesce_chrome", _wedged)
    backend.stop(handle)

    assert not paths.run_file("atlas").is_file()
    pool_state = json.loads(paths.machines_file().read_text(encoding="utf-8"))
    assert pool_state["machines"][0]["state"] == "idle"


def test_spawn_failure_names_both_remedies(tmp_path, monkeypatch):
    bad = '#!/bin/sh\ncase "$1" in inspect) exit 1 ;; run) echo boom >&2; exit 1 ;; esac\nexit 0\n'
    _install_fake(tmp_path, monkeypatch, bad)
    paths = _paths(tmp_path)
    backend, _calls = _quiet_backend(paths, monkeypatch)
    with pytest.raises(IsolationUnavailable) as err:
        backend.spawn("atlas", AGENT_ARGV)
    assert "Dockerfile.machine" in str(err.value)
    assert "--backend process" in str(err.value)


def test_sync_round_trip_through_fake_cp(tmp_path, monkeypatch):
    """clone-down and merge-up both flow through docker cp tar streams."""
    machine_root = tmp_path / "machine-home"
    machine_root.mkdir()
    log = tmp_path / "docker-argv.log"
    monkeypatch.setenv("FAKE_MACHINE_ROOT", str(machine_root))
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    _install_fake(tmp_path, monkeypatch, FAKE_DOCKER_PY)

    paths = _paths(tmp_path)
    backend = MachineBackend(paths)
    canonical = paths.canonical_home()
    (canonical / "Desktop").mkdir(parents=True)
    (canonical / "Desktop" / "note.txt").write_text("from-canonical", encoding="utf-8")
    (machine_root / "machine-made.txt").write_text("from-machine", encoding="utf-8")

    from isolation.machines import Machine

    backend._sync_session_start(Machine(id=0, name="harness-machine-0"))
    # clone-down delivered the canonical file into the machine home
    assert (machine_root / "Desktop" / "note.txt").read_text(encoding="utf-8") == "from-canonical"
    assert paths.machine_dirty_file(0).is_file()

    (machine_root / "Desktop" / "login.txt").write_text("cookie", encoding="utf-8")
    backend._sync_up("harness-machine-0")
    # merge-up brought machine writes into the canonical store, no deletes
    assert (canonical / "Desktop" / "login.txt").read_text(encoding="utf-8") == "cookie"
    assert (canonical / "machine-made.txt").read_text(encoding="utf-8") == "from-machine"
    assert (canonical / "Desktop" / "note.txt").is_file()


def _fake_cp_backend(tmp_path, monkeypatch):
    """Backend wired to the python cp fake with a real machine-home dir."""
    machine_root = tmp_path / "machine-home"
    machine_root.mkdir()
    monkeypatch.setenv("FAKE_MACHINE_ROOT", str(machine_root))
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(tmp_path / "docker-argv.log"))
    _install_fake(tmp_path, monkeypatch, FAKE_DOCKER_PY)
    paths = _paths(tmp_path)
    return MachineBackend(paths), paths, machine_root


def _sqlite_db(path, rows):
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (v TEXT)")
    con.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    con.commit()
    con.close()


CHROME_DB = ".config/harness-chrome/Default/Cookies"


def test_live_flush_backs_up_sqlite_group_instead_of_file_copying(tmp_path, monkeypatch):
    """Interval flush with Chrome live: the DB group is excluded from the
    per-file merge and lands as a sqlite3-backup-verified copy."""
    import sqlite3

    backend, paths, machine_root = _fake_cp_backend(tmp_path, monkeypatch)
    db = machine_root / CHROME_DB
    _sqlite_db(db, ["login"])
    os.utime(db, ns=(4_200 * 1_000_000_000, 4_200 * 1_000_000_000))
    (db.parent / "Cookies-journal").write_bytes(b"")  # settled (empty) journal
    (machine_root / "Desktop").mkdir()
    (machine_root / "Desktop" / "note.txt").write_text("plain", encoding="utf-8")

    result = backend._sync_up("harness-machine-0", receipt="flush", live=True)

    canonical = paths.canonical_home()
    assert (canonical / "Desktop" / "note.txt").read_text(encoding="utf-8") == "plain"
    snap = canonical / CHROME_DB
    con = sqlite3.connect(snap)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("SELECT v FROM t").fetchall() == [("login",)]
    con.close()
    # sidecars never install live; newest-wins mtime tracks the machine base
    assert not (canonical / CHROME_DB).with_name("Cookies-journal").exists()
    assert snap.stat().st_mtime_ns == db.stat().st_mtime_ns
    assert result["sqlite"] == {"groups": 1, "backed_up": 1, "skipped": []}
    assert result["walk_complete"] is True
    assert result["walk_failed"] == 0
    receipt = paths.machine_meta() / "harness-machine-0.last-sync.json"
    assert '"walk_complete"' in receipt.read_text(encoding="utf-8")


def test_live_flush_keeps_richer_canonical_cookies(tmp_path, monkeypatch):
    """Idle Chrome rewrites Cookies with a later mtime but fewer logins.
    Newest-mtime-wins would poison the store; row-count rank keeps the
    signed-in copy."""
    import sqlite3

    backend, paths, machine_root = _fake_cp_backend(tmp_path, monkeypatch)
    good = paths.canonical_home() / CHROME_DB
    _sqlite_db(good, ["google", "youtube", "accounts"])
    os.utime(good, ns=(1_000 * 1_000_000_000, 1_000 * 1_000_000_000))

    db = machine_root / CHROME_DB
    _sqlite_db(db, ["idle"])
    os.utime(db, ns=(9_000 * 1_000_000_000, 9_000 * 1_000_000_000))
    (db.parent / "Cookies-journal").write_bytes(b"")

    result = backend._sync_up("harness-machine-0", receipt="flush", live=True)

    assert result["sqlite"]["backed_up"] == 0
    assert any("fewer rows" in s for s in result["sqlite"]["skipped"])
    con = sqlite3.connect(good)
    assert [r[0] for r in con.execute("SELECT v FROM t")] == [
        "google",
        "youtube",
        "accounts",
    ]
    con.close()


def test_live_flush_installs_older_richer_cookies(tmp_path, monkeypatch):
    """The signed-in profile is older but larger: plan_merge must still
    select it and the installer must replace the poorer canonical copy."""
    import sqlite3

    backend, paths, machine_root = _fake_cp_backend(tmp_path, monkeypatch)
    poor = paths.canonical_home() / CHROME_DB
    _sqlite_db(poor, ["idle"])
    os.utime(poor, ns=(9_000 * 1_000_000_000, 9_000 * 1_000_000_000))

    db = machine_root / CHROME_DB
    _sqlite_db(db, ["google", "youtube", "accounts"])
    os.utime(db, ns=(1_000 * 1_000_000_000, 1_000 * 1_000_000_000))
    (db.parent / "Cookies-journal").write_bytes(b"")

    result = backend._sync_up("harness-machine-0", receipt="flush", live=True)

    assert result["sqlite"]["backed_up"] == 1
    assert result["sqlite"]["skipped"] == []
    con = sqlite3.connect(poor)
    assert [r[0] for r in con.execute("SELECT v FROM t")] == [
        "google",
        "youtube",
        "accounts",
    ]
    con.close()


def test_stop_sync_does_not_clobber_larger_canonical_sqlite(tmp_path, monkeypatch):
    import sqlite3

    backend, paths, machine_root = _fake_cp_backend(tmp_path, monkeypatch)
    good = paths.canonical_home() / CHROME_DB
    _sqlite_db(good, ["google", "youtube", "accounts"])

    db = machine_root / CHROME_DB
    _sqlite_db(db, ["idle"])
    (db.parent / "Cookies-journal").write_bytes(b"hot")

    result = backend._sync_up("harness-machine-0")  # quiesced stop

    assert any("fewer rows" in s for s in result["sqlite"]["skipped"])
    con = sqlite3.connect(good)
    assert [r[0] for r in con.execute("SELECT v FROM t")] == [
        "google",
        "youtube",
        "accounts",
    ]
    con.close()
    journal = (paths.canonical_home() / CHROME_DB).with_name("Cookies-journal")
    assert not journal.exists() or journal.read_bytes() != b"hot"


def test_live_flush_skips_torn_sqlite_group_and_keeps_canonical_intact(tmp_path, monkeypatch):
    import sqlite3

    from isolation.state_sync import SQLITE_MAGIC

    backend, paths, machine_root = _fake_cp_backend(tmp_path, monkeypatch)
    torn = machine_root / CHROME_DB
    torn.parent.mkdir(parents=True)
    torn.write_bytes(SQLITE_MAGIC + b"\xff" * 4096)  # mid-write capture
    (torn.parent / "Cookies-journal").write_bytes(b"garbage")
    good = paths.canonical_home() / CHROME_DB
    _sqlite_db(good, ["old-but-sound"])
    os.utime(good, ns=(1_000_000_000, 1_000_000_000))  # machine copy is newer

    result = backend._sync_up("harness-machine-0", receipt="flush", live=True)

    assert result["sqlite"]["groups"] == 1
    assert result["sqlite"]["backed_up"] == 0
    assert len(result["sqlite"]["skipped"]) == 1
    assert result["sqlite"]["skipped"][0].startswith(CHROME_DB)
    con = sqlite3.connect(good)  # the torn capture never reached canonical
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("SELECT v FROM t").fetchall() == [("old-but-sound",)]
    con.close()


def test_stop_sync_copies_quiesced_sqlite_group_as_a_unit(tmp_path, monkeypatch):
    backend, paths, machine_root = _fake_cp_backend(tmp_path, monkeypatch)
    db = machine_root / CHROME_DB
    _sqlite_db(db, ["quiesced"])
    (db.parent / "Cookies-journal").write_bytes(b"hot journal")

    result = backend._sync_up("harness-machine-0")  # stop path: live=False

    canonical = paths.canonical_home()
    assert (canonical / CHROME_DB).read_bytes() == db.read_bytes()
    journal = (canonical / CHROME_DB).with_name("Cookies-journal")
    assert journal.read_bytes() == b"hot journal"  # same-snapshot group copy
    assert result["sqlite"] == {"groups": 1, "skipped": []}


def test_spawn_flushes_live_peers_before_clone_down(tmp_path, monkeypatch):
    """A new bot's clone-down must see the live fleet's latest logins: every
    dirty, running peer machine merges up before the new machine syncs down."""
    _fake_engine(tmp_path, monkeypatch)  # every container inspects as running
    paths = _paths(tmp_path)
    backend, _calls = _quiet_backend(paths, monkeypatch)

    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        backend,
        "_sync_up",
        lambda name, receipt="up", live=False: events.append(("up", name)) or {},
    )
    monkeypatch.setattr(backend, "_sync_session_start", lambda m: events.append(("down", m.name)))

    backend.spawn("atlas", AGENT_ARGV)
    paths.machine_dirty_file(0).write_text("1", encoding="utf-8")  # atlas is live
    events.clear()

    backend.spawn("nova", AGENT_ARGV)
    assert events == [("up", "harness-machine-0"), ("down", "harness-machine-1")]


def test_spawn_skips_clean_or_down_peers(tmp_path, monkeypatch):
    """Peers without un-synced state, or whose container is down, are not
    flushed — a crashed peer's dirty state salvages on its own next start."""
    _fake_engine(tmp_path, monkeypatch)
    paths = _paths(tmp_path)
    backend, calls = _quiet_backend(paths, monkeypatch)

    backend.spawn("atlas", AGENT_ARGV)  # no dirty marker (sync is stubbed)
    backend.spawn("nova", AGENT_ARGV)
    assert calls["sync_up"] == []  # clean peer: nothing to pull

    paths.machine_dirty_file(0).write_text("1", encoding="utf-8")
    monkeypatch.setattr(backend, "_container_running", lambda name: False)
    backend._flush_running_peers("nova")
    assert calls["sync_up"] == []  # dirty but down: left for salvage


def test_peer_flush_failure_never_blocks_spawn(tmp_path, monkeypatch):
    _fake_engine(tmp_path, monkeypatch)
    paths = _paths(tmp_path)
    backend, calls = _quiet_backend(paths, monkeypatch)
    backend.spawn("atlas", AGENT_ARGV)
    paths.machine_dirty_file(0).write_text("1", encoding="utf-8")

    def _boom(name, receipt="up", live=False):
        raise IsolationUnavailable("peer will not snapshot")

    monkeypatch.setattr(backend, "_sync_up", _boom)
    handle = backend.spawn("nova", AGENT_ARGV)
    assert handle.status == Status.RUNNING
    assert calls["sync_start"] == ["harness-machine-0", "harness-machine-1"]


def test_registry_and_cli_default_is_machines(tmp_path):
    assert isinstance(get_backend("machines", _paths(tmp_path)), MachineBackend)
    args = build_parser().parse_args(["status"])
    assert args.backend == "machines"


def test_status_reads_agent_and_container(tmp_path, monkeypatch):
    _fake_engine(tmp_path, monkeypatch, inspect_state="exited")
    paths = _paths(tmp_path)
    backend = MachineBackend(paths)
    handle = BotHandle(
        bot="atlas", backend="machines", pid=4242, meta={"machine": "harness-machine-0"}
    )
    monkeypatch.setattr("isolation.machines._pid_alive", lambda pid: True)
    assert backend.status(handle) == Status.DEAD  # agent alive, computer gone
    monkeypatch.setattr("isolation.machines._pid_alive", lambda pid: False)
    assert backend.status(handle) == Status.DEAD
    handle.pid = None
    assert backend.status(handle) == Status.STOPPED


# python fake for close_browser: chrome "runs" until pkill drops a marker
FAKE_DOCKER_CHROME = """#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as fh:
    fh.write(" ".join(args) + "\\n")
marker = Path(os.environ["FAKE_CHROME_DEAD"])
if args[0] == "inspect":
    if "Labels" in " ".join(args):
        print(os.environ["FAKE_LABELS"])
    else:
        print("running")
    sys.exit(0)
if args[0] == "exec" and "pgrep" in args:
    sys.exit(1 if marker.exists() else 0)
if args[0] == "exec" and "pkill" in args:
    marker.touch(); sys.exit(0)
sys.exit(0)
"""


def _spawned_quiet(tmp_path, monkeypatch, script):
    log = tmp_path / "docker-argv.log"
    dead = tmp_path / "chrome-dead"
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.setenv("FAKE_CHROME_DEAD", str(dead))
    monkeypatch.setenv("FAKE_LABELS", json.dumps(_owned_labels()))
    _install_fake(tmp_path, monkeypatch, script)
    paths = _paths(tmp_path)
    backend, calls = _quiet_backend(paths, monkeypatch)
    backend.spawn("atlas", AGENT_ARGV)
    return backend, calls, log, dead


def test_close_browser_syncs_then_quiesces_a_running_chrome(tmp_path, monkeypatch):
    """Idle close: merge the machine up FIRST (Chrome's logins reach the
    canonical store), then pkill; the machine itself stays running."""
    backend, calls, log, dead = _spawned_quiet(tmp_path, monkeypatch, FAKE_DOCKER_CHROME)
    assert backend.close_browser("atlas") is True
    assert calls["sync_up"] == ["harness-machine-0"]
    lines = log.read_text(encoding="utf-8").splitlines()
    pkill = next(i for i, ln in enumerate(lines) if "pkill" in ln)
    assert "harness-machine-0" in lines[pkill]
    assert dead.exists()
    assert not any(ln.startswith("stop ") for ln in lines), "the machine is not stopped"
    # nothing running now: a second pass is a no-op with no sync
    assert backend.close_browser("atlas") is False
    assert calls["sync_up"] == ["harness-machine-0"]


def test_close_browser_is_a_noop_for_an_unknown_or_stopped_bot(tmp_path, monkeypatch):
    backend, _calls, _log, _dead = _spawned_quiet(tmp_path, monkeypatch, FAKE_DOCKER_CHROME)
    assert backend.close_browser("nobody") is False
    monkeypatch.setattr("isolation.machines._pid_alive", lambda pid: False)
    assert backend.close_browser("atlas") is False


def test_machine_reassignment_replaces_previous_bot_secret_mount(tmp_path, monkeypatch):
    from isolation.machines import Machine

    log = _fake_engine(tmp_path, monkeypatch, labels=_owned_labels())
    paths = _paths(tmp_path)
    backend, _ = _quiet_backend(paths, monkeypatch)
    backend._ensure_container(Machine(0, "harness-machine-0", bot="other"))
    lines = log.read_text().splitlines()
    assert any(line.startswith("rm harness-machine-0") for line in lines)
    created = next(line for line in lines if line.startswith("run "))
    assert f"{paths.run}/bot-secrets/other:/run/harness:ro" in created
    assert f"{paths.run}/bot-secrets/atlas:" not in created


def test_restart_preserves_grants_without_requiring_server_configuration(tmp_path, monkeypatch):
    from harness import machine_secrets
    from isolation.machines import Machine

    paths = _paths(tmp_path)
    backend, _ = _quiet_backend(paths, monkeypatch)
    monkeypatch.delenv("HARNESS_MACHINE_SECRETS", raising=False)
    machine_secrets.stage(paths, "atlas", "AMAZON_KEY", "stored-value")
    args = backend._secret_run_args(Machine(0, "harness-machine-0", bot="atlas"))
    assert f"{paths.run}/bot-secrets/atlas:/run/harness:ro" in args
    assert "stored-value" not in str(args)
    assert (paths.run / "bot-secrets/atlas/AMAZON_KEY").read_text() == "stored-value"


def test_failed_replacement_cannot_reuse_another_bots_secret_mount(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from isolation import engine
    from isolation.machines import Machine

    log = _fake_engine(tmp_path, monkeypatch, labels=_owned_labels())
    backend, _ = _quiet_backend(_paths(tmp_path), monkeypatch)
    original = engine.run

    def run(*args, **kwargs):
        if args[0] == "rm":
            return SimpleNamespace(returncode=1)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "run", run)
    with pytest.raises(IsolationUnavailable, match="replace"):
        backend._ensure_container(Machine(0, "harness-machine-0", bot="other"))
    assert not any(line.startswith(("start ", "run ")) for line in log.read_text().splitlines())
