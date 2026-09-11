"""Regression tests for harness.fsutil — atomic writes and pid liveness.

State files under $HARNESS_HOME are read by other processes at any moment;
`write_atomic` is the contract that a reader sees either the old or the new
file, never a torn one, and `pid_alive` is what stop/status trust.
"""

import os
import time

import pytest

from harness.fsutil import pid_alive, write_atomic


def test_write_atomic_creates_parents_and_writes(tmp_path):
    target = tmp_path / "run" / "deep" / "state.json"
    write_atomic(target, '{"ok": true}')
    assert target.read_text(encoding="utf-8") == '{"ok": true}'


def test_write_atomic_replaces_existing_content(tmp_path):
    target = tmp_path / "state.txt"
    write_atomic(target, "old")
    write_atomic(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_write_atomic_leaves_no_tmp_files(tmp_path):
    target = tmp_path / "state.txt"
    write_atomic(target, "x")
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "state.txt"]
    assert leftovers == []


def test_write_atomic_failure_keeps_old_file_and_cleans_tmp(tmp_path, monkeypatch):
    target = tmp_path / "state.txt"
    write_atomic(target, "old")

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="simulated"):
        write_atomic(target, "new")
    monkeypatch.undo()
    # the reader still sees the old content and no tmp file litters the dir
    assert target.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["state.txt"]


def test_pid_alive_for_this_process():
    assert pid_alive(os.getpid()) is True


def test_pid_alive_reaps_a_finished_child():
    # A dead child lingers as a zombie until reaped; pid_alive must report it
    # dead immediately (via WNOHANG waitpid), not after the SIGKILL grace.
    pid = os.fork()
    if pid == 0:
        os._exit(0)  # pragma: no cover - child process
    deadline = time.monotonic() + 5
    while pid_alive(pid):
        assert time.monotonic() < deadline, "finished child still reads as alive"
        time.sleep(0.01)
    # once reaped, it stays dead
    assert pid_alive(pid) is False
