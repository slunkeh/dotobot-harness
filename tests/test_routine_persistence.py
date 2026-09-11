"""Concurrent editor, API and scheduler writes must preserve routine data."""

import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from harness import routines
from harness.paths import HarnessPaths


@pytest.fixture
def paths(tmp_path):
    return HarnessPaths.resolve(tmp_path)


def test_delete_during_autosave_preserves_other_routines(paths, monkeypatch):
    kept = routines.add_routine(paths, "atlas", title="Keep", prompt="Keep this")
    draft = routines.add_routine(paths, "atlas")
    saving = threading.Event()
    release = threading.Event()
    deleting = threading.Event()
    save = routines._save

    def delayed_save(paths, bot, rows):
        if any(row.get("title") == "Edited" for row in rows):
            saving.set()
            assert release.wait(3)
        save(paths, bot, rows)

    def delete():
        deleting.set()
        return routines.remove_routine(paths, "atlas", draft["id"])

    monkeypatch.setattr(routines, "_save", delayed_save)
    with ThreadPoolExecutor(2) as pool:
        edit = pool.submit(routines.update_routine, paths, "atlas", draft["id"], title="Edited")
        assert saving.wait(3)
        removal = pool.submit(delete)
        assert deleting.wait(3)
        # The old implementation commits the delete, then resurrects it from
        # the autosave's stale list. A transaction must block the delete.
        try:
            removal.result(timeout=0.2)
        except TimeoutError:
            pass
        finally:
            release.set()
        edit.result(timeout=3)
        assert removal.result(timeout=3)
    remaining = routines.list_routines(paths, "atlas")
    assert [row["id"] for row in remaining] == [kept["id"]]
    assert remaining[0]["prompt"] == "Keep this"
    assert not routines.remove_routine(paths, "atlas", draft["id"])


def test_atomic_writes_never_share_a_temporary_file(paths, monkeypatch):
    """Hold both writers after writing, before replacing the destination."""
    replace = routines.os.replace
    ready = threading.Barrier(2)

    def delayed_replace(source, destination):
        ready.wait(timeout=3)
        replace(source, destination)

    monkeypatch.setattr(routines.os, "replace", delayed_replace)
    rows = [[{"id": "long", "prompt": "x" * 10000}], [{"id": "short"}]]
    with ThreadPoolExecutor(2) as pool:
        writes = [pool.submit(routines._save, paths, "atlas", row) for row in rows]
        for write in writes:
            write.result(timeout=3)
    assert routines.list_routines(paths, "atlas") in rows


@pytest.mark.parametrize("mutation", ["add", "update", "delete", "run"])
@pytest.mark.parametrize("damage", ["trailing", "shape", "row", "duplicate", "encoding"])
def test_corruption_never_becomes_an_empty_store(paths, mutation, damage):
    row = routines.add_routine(paths, "atlas", prompt="Keep", when="8am", enabled=True)
    path = paths.bot_routines("atlas")
    if damage == "trailing":
        path.write_text(path.read_text() + ' "enabled": true }')
    elif damage == "shape":
        path.write_text('{"routines": {}}')
    elif damage == "row":
        path.write_text(json.dumps({"routines": [{"title": "Missing ID"}]}))
    elif damage == "duplicate":
        path.write_text(json.dumps({"routines": [row, row]}))
    else:
        path.write_bytes(b"\xff")
    original = path.read_bytes()
    calls = {
        "add": lambda: routines.add_routine(paths, "atlas"),
        "update": lambda: routines.update_routine(paths, "atlas", row["id"], title="New"),
        "delete": lambda: routines.remove_routine(paths, "atlas", row["id"]),
        "run": lambda: routines.run_now(paths, "atlas", row["id"]),
    }
    with pytest.raises(routines.RoutineError, match="cannot read routines"):
        calls[mutation]()
    assert path.read_bytes() == original


def test_scheduler_preserves_corrupt_bot_and_runs_healthy_bot(paths, caplog):
    routines.add_routine(paths, "broken")
    path = paths.bot_routines("broken")
    path.write_text('{"routines": [')
    original = path.read_bytes()
    row = routines.add_routine(paths, "healthy", prompt="Run", when="8am", enabled=True)
    sent = []
    fired = routines.fire_due(
        paths,
        ["broken", "healthy"],
        now=datetime(2026, 9, 7, 8),
        send=lambda bot, text: sent.append(bot),
    )
    assert [r["id"] for r in fired] == [row["id"]]
    assert sent == ["healthy"]
    assert path.read_bytes() == original
    assert "cannot read routines" in caplog.text


def test_agent_process_cannot_overwrite_an_http_transaction(paths):
    script = """
import sys
from harness.paths import HarnessPaths
from harness.routines import add_routine
print('ready', flush=True)
add_routine(HarnessPaths.resolve(sys.argv[1]), 'atlas', title='Agent')
"""
    with routines._lock(paths, "atlas"):
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(paths.home)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout.readline().strip() == "ready"
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.2)
            routines._save(paths, "atlas", [{"id": "http", "title": "HTTP"}])
        except BaseException:
            process.kill()
            process.wait()
            raise
    stdout, stderr = process.communicate(timeout=3)
    assert process.returncode == 0, stderr
    assert [r["title"] for r in routines.list_routines(paths, "atlas")] == ["HTTP", "Agent"]
