"""Overlapping settings/restart requests must never create duplicate agents."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent import messaging
from harness.orchestrator import Orchestrator
from isolation import BotHandle, Status


class Backend:
    """A slow stop exposes the same stop/spawn window as a machine backend."""

    def __init__(self):
        self.current = {"atlas": BotHandle("atlas", "process", pid=1, status=Status.RUNNING)}
        self.live = {1}
        self.next_pid = 2
        self.entered = threading.Event()
        self.release = threading.Event()
        self.overlap = threading.Event()
        self.guard = threading.Lock()
        self.stopping = False

    def load(self, name):
        return self.current.get(name)

    def stop(self, handle):
        with self.guard:
            first = not self.entered.is_set()
            if self.stopping:
                self.overlap.set()
            self.stopping = True
        if first:
            self.entered.set()
            assert self.release.wait(5), "test did not release slow stop"
        with self.guard:
            self.live.discard(handle.pid)
            self.current.pop(handle.bot, None)
            self.stopping = False

    def spawn(self, name, argv):
        with self.guard:
            if self.stopping and name == "atlas":
                self.overlap.set()
            if name in self.current:
                return self.current[name]
            handle = BotHandle(name, "process", pid=self.next_pid, status=Status.RUNNING)
            self.next_pid += 1
            self.live.add(handle.pid)
            self.paths.run_file(name).write_text(
                json.dumps({"bot": name, "backend": "process", "pid": handle.pid})
            )
            self.current[name] = handle
            return handle


def make_orch(tmp_path, backend):
    rp = tmp_path / "roster.json"
    if not rp.exists():
        rp.write_text('{"bots":[{"name":"atlas","provider":"echo"}]}')
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp)
    orch.init()
    backend.paths = orch.paths
    orch._backend_cache = backend
    orch._backend_cache_key = orch.backend_name
    return orch


@pytest.mark.parametrize("operation", ["restart", "update", "up", "down", "remove"])
@pytest.mark.parametrize("separate_orchestrator", [False, True])
def test_lifecycle_waits_for_inflight_restart(
    tmp_path, operation, separate_orchestrator, monkeypatch
):
    # Backend PIDs are counters, not host processes (PID 2 can be live on Linux).
    monkeypatch.setattr("harness.bot_cleanup.pid_alive", lambda pid: False)
    backend = Backend()
    orch = make_orch(tmp_path, backend)
    other = make_orch(tmp_path, backend) if separate_orchestrator else orch
    queued = messaging.send(orch.paths, messaging.Msg(to="atlas", frm="user", text="hello"))
    begun = threading.Event()

    def competing_request():
        begun.set()
        if operation == "update":
            return other.update_bot("atlas", private_browser=True)
        if operation == "restart":
            return other.restart("atlas")
        if operation == "remove":
            return other.remove_bot("atlas")
        return getattr(other, operation)()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(orch.restart, "atlas")
        try:
            assert backend.entered.wait(2)
            second = pool.submit(competing_request)
            assert begun.wait(2)
            assert not backend.overlap.wait(0.2), "two lifecycle operations entered the backend"
            assert not second.done(), "request returned while the earlier restart was incomplete"
        finally:
            backend.release.set()
        first.result(timeout=2)
        second.result(timeout=2)
    if operation == "remove":
        from harness.bot_cleanup import shutdown

        shutdown(other, "atlas")
    assert len(backend.live) == (0 if operation in ("down", "remove") else 1)
    assert queued.exists(), "lifecycle operations must preserve queued messages"


def test_other_bot_can_restart_while_one_is_stopping(tmp_path):
    backend = Backend()
    orch = make_orch(tmp_path, backend)
    orch.add_bot(name="nova", provider="echo", start=False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(orch.restart, "atlas")
        try:
            assert backend.entered.wait(2)
            handle = pool.submit(orch.restart, "nova").result(timeout=1)
            assert handle.bot == "nova"
        finally:
            backend.release.set()
        first.result(timeout=2)


def test_failed_spawn_releases_lifecycle_lock(tmp_path, monkeypatch):
    backend = Backend()
    backend.release.set()
    orch = make_orch(tmp_path, backend)
    spawn = backend.spawn

    def fail(*args):
        raise RuntimeError("machine unavailable")

    monkeypatch.setattr(backend, "spawn", fail)
    with pytest.raises(RuntimeError, match="machine unavailable"):
        orch.restart("atlas")
    monkeypatch.setattr(backend, "spawn", spawn)
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(orch.restart, "atlas").result(timeout=2).status == Status.RUNNING


def _restart_from_cli(root, ready, begin, stopped, proceed, result):
    """A separate CLI process sharing only the deployment's on-disk state."""
    import json
    import os
    from pathlib import Path

    root = Path(root)
    record = root / "current.json"

    class DiskBackend:
        def load(self, name):
            return BotHandle(name, "process", pid=json.loads(record.read_text())["pid"])

        def stop(self, handle):
            stopped.put(handle.pid)
            assert proceed.wait(5)
            record.unlink()

        def spawn(self, name, argv):
            record.write_text(json.dumps({"pid": os.getpid()}))
            return BotHandle(name, "process", pid=os.getpid(), status=Status.RUNNING)

    orch = Orchestrator.create(home=root / "home", roster_path=root / "roster.json")
    orch._backend_cache = DiskBackend()
    orch._backend_cache_key = orch.backend_name
    ready.set()
    assert begin.wait(15), "test did not begin the prepared CLI restart"
    result.put(orch.restart("atlas").pid)


def test_separate_cli_processes_restart_the_current_generation(tmp_path):
    import json
    import multiprocessing
    import queue

    make_orch(tmp_path, Backend())
    (tmp_path / "current.json").write_text('{"pid":1}')
    ctx = multiprocessing.get_context("spawn")
    stopped, result = ctx.Queue(), ctx.Queue()
    proceed = ctx.Event()
    ready = [ctx.Event(), ctx.Event()]
    begin = [ctx.Event(), ctx.Event()]
    children = [
        ctx.Process(
            target=_restart_from_cli,
            args=(str(tmp_path), signal, start, stopped, proceed, result),
        )
        for signal, start in zip(ready, begin, strict=True)
    ]
    try:
        # Interpreter imports on a loaded runner are not part of the stop/spawn
        # race. Prepare both processes before starting the five-second stop gate.
        deadline = time.monotonic() + 10
        for child in children:
            child.start()
        for signal in ready:
            assert signal.wait(max(0, deadline - time.monotonic())), "CLI did not initialize"
        begin[0].set()
        assert stopped.get(timeout=5) == 1
        begin[1].set()
        with pytest.raises(queue.Empty):
            stopped.get(timeout=0.2)
        proceed.set()
        assert stopped.get(timeout=5) == children[0].pid
        assert {result.get(timeout=5), result.get(timeout=5)} == {p.pid for p in children}
        for child in children:
            child.join(timeout=5)
            assert child.exitcode == 0
        assert json.loads((tmp_path / "current.json").read_text())["pid"] == children[1].pid
    finally:
        proceed.set()
        for signal in begin:
            signal.set()
        for child in children:
            if child.pid is not None:
                child.join(timeout=1)
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=2)
        stopped.close()
        result.close()
