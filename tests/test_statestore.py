"""SQLite state store + session JSONL migration.

Contract tests: schema discipline (user_version + meta, WAL, a newer schema
refused with the distinct exit code), the one-way JSONL migration (idempotent,
resumable, torn lines counted, sources archived never deleted), and parity —
history rebuild, `user_thread`, `recall` and compaction read identically
before and after migration, including over the HTTP API.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.request

import pytest

from agent.compaction import maybe_compact
from agent.history import build_history, user_thread
from agent.memory import Memory
from agent.streaming import resolve_prompt, write_prompt
from harness import cli
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.server import make_server
from harness.statestore import (
    DEFAULT_BUDGET,
    SCHEMA_TOO_NEW_EXIT,
    SCHEMA_VERSION,
    SchemaTooNew,
    StateStore,
    migrate_sessions,
    network_home_warning,
    store_for,
)
from providers.base import Provider


def _paths(tmp_path) -> HarnessPaths:
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    return paths


def _write_jsonl(paths, bot, session, records, *, torn_tail=False) -> None:
    d = paths.bot_memory(bot) / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    if torn_tail:
        text += '{"ts": 99.0, "role": "out", "te'  # crash mid-write, no newline
    (d / f"{session}.jsonl").write_text(text, encoding="utf-8")


def _legacy_thread(base_ts=1000.0):
    return [
        {"ts": base_ts + 0, "role": "in:user", "text": "plan the deploy", "peer": "user"},
        {"ts": base_ts + 1, "role": "out", "text": "deploy planned", "peer": "user"},
        {"ts": base_ts + 2, "role": "in:user", "text": "ship the feature flag", "peer": "user"},
        {"ts": base_ts + 3, "role": "out", "text": "flag shipped", "peer": "user"},
    ]


# -- schema discipline -------------------------------------------------------
def test_schema_version_meta_row_and_wal(tmp_path):
    paths = _paths(tmp_path)
    store = store_for(paths)
    store.append_transcript("atlas", "s1", {"ts": 1.0, "role": "out", "text": "hi"})
    conn = sqlite3.connect(paths.state_db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    finally:
        conn.close()
    assert meta["schema_version"] == str(SCHEMA_VERSION)
    assert meta["app_version"]  # the writing app's version rides beside it


def test_schema_too_new_is_refused(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    store = StateStore(db)
    try:
        store.transcript_stamp("atlas")
        raise AssertionError("a newer schema must be refused, not migrated")
    except SchemaTooNew as exc:
        assert exc.exit_code == SCHEMA_TOO_NEW_EXIT


def test_cli_maps_schema_too_new_to_distinct_exit_code(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    conn = sqlite3.connect(home / "state.sqlite")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    code = cli.main(["--home", str(home), "init"])
    # Distinct from a generic error's 1/2, so systemd's
    # RestartPreventExitStatus can stop a restart loop into the same refusal.
    assert code == SCHEMA_TOO_NEW_EXIT


# -- migration ---------------------------------------------------------------
def test_migration_round_trips_history_and_archives_sources(tmp_path):
    paths = _paths(tmp_path)
    _write_jsonl(paths, "atlas", "20240101-000000", _legacy_thread())
    memory = Memory(paths=paths, bot="atlas")
    before_rows = user_thread(memory, peer="user")
    before_hist, before_cutoff = build_history(memory, peer="user", provider=Provider(model="m"))
    assert before_rows, "fixture must produce a legacy-read thread"

    report = migrate_sessions(paths)
    assert report.bots == 1 and report.files == 1
    assert report.records == len(_legacy_thread())

    sessions = paths.bot_memory("atlas") / "sessions"
    assert not list(sessions.glob("*.jsonl")), "source files must move away"
    archived = sessions / "archive" / "20240101-000000.jsonl"
    assert archived.is_file(), "archived, never deleted (downgrade path)"
    assert len(archived.read_text(encoding="utf-8").splitlines()) == len(_legacy_thread())

    after = Memory(paths=paths, bot="atlas")
    assert user_thread(after, peer="user") == before_rows
    after_hist, after_cutoff = build_history(after, peer="user", provider=Provider(model="m"))
    assert [(m.role, m.content) for m in after_hist] == [(m.role, m.content) for m in before_hist]
    assert after_cutoff == before_cutoff


def test_migration_is_idempotent_on_rerun(tmp_path):
    paths = _paths(tmp_path)
    _write_jsonl(paths, "atlas", "20240101-000000", _legacy_thread())
    first = migrate_sessions(paths)
    assert first.records == len(_legacy_thread())
    second = migrate_sessions(paths)
    assert second.files == 0 and second.records == 0
    assert store_for(paths).transcript_stamp("atlas")[0] == len(_legacy_thread())


def test_migration_resumes_after_crash_between_commit_and_archive(tmp_path):
    """The per-file marker row is the resume point: a file whose import
    committed but whose rename never happened is only re-archived."""
    paths = _paths(tmp_path)
    _write_jsonl(paths, "atlas", "20240101-000000", _legacy_thread())
    migrate_sessions(paths)
    sessions = paths.bot_memory("atlas") / "sessions"
    archived = sessions / "archive" / "20240101-000000.jsonl"
    # put the source back where the crash would have left it
    (sessions / archived.name).write_text(archived.read_text(encoding="utf-8"), encoding="utf-8")
    report = migrate_sessions(paths)
    assert report.records == 0, "marker row must prevent a duplicate import"
    assert not list(sessions.glob("*.jsonl")), "the leftover file is re-archived"
    rows = user_thread(Memory(paths=paths, bot="atlas"), peer="user")
    assert [r["text"] for r in rows] == [r["text"] for r in _legacy_thread()]


def test_torn_trailing_line_is_skipped_and_counted(tmp_path):
    paths = _paths(tmp_path)
    _write_jsonl(paths, "atlas", "20240101-000000", _legacy_thread(), torn_tail=True)
    report = migrate_sessions(paths)
    assert report.torn == 1
    assert report.records == len(_legacy_thread())
    rows = user_thread(Memory(paths=paths, bot="atlas"), peer="user")
    assert len(rows) == len(_legacy_thread())


def test_migration_covers_every_bot_in_the_home(tmp_path):
    paths = _paths(tmp_path)
    _write_jsonl(paths, "atlas", "s1", _legacy_thread())
    _write_jsonl(paths, "nova", "s1", _legacy_thread(base_ts=2000.0))
    report = migrate_sessions(paths)
    assert report.bots == 2 and report.files == 2
    for bot in ("atlas", "nova"):
        assert user_thread(Memory(paths=paths, bot=bot), peer="user")


def test_history_identical_over_http_after_migration(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text('[[bots]]\nname = "atlas"\nprovider = "echo"\n', encoding="utf-8")
    home = tmp_path / "home"
    paths = HarnessPaths.resolve(home)
    _write_jsonl(paths, "atlas", "20240101-000000", _legacy_thread())
    expected = user_thread(Memory(paths=paths, bot="atlas"), peer="user")

    orch = Orchestrator.create(home=home, roster_path=rp, backend="process")
    orch.init()  # runs the migration
    assert not list((paths.bot_memory("atlas") / "sessions").glob("*.jsonl"))
    httpd = make_server(orch, "127.0.0.1", 0)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/bots/atlas/history", timeout=10
        ) as r:
            rows = json.loads(r.read().decode())
    finally:
        httpd.shutdown()
    assert rows == expected


# -- the store as the live write path ---------------------------------------
def test_log_turn_writes_the_store_not_a_jsonl_file(tmp_path):
    paths = _paths(tmp_path)
    memory = Memory(paths=paths, bot="atlas")
    memory.log_turn("s1", "in:user", "hello", peer="user")
    memory.log_turn("s1", "out", "hi there", peer="user")
    assert not list(memory.sessions_dir.glob("*.jsonl"))
    rows = user_thread(memory, peer="user")
    assert [r["text"] for r in rows] == ["hello", "hi there"]
    assert memory.latest_session_id() == "s1"


def test_unmigrated_file_merges_before_new_store_rows(tmp_path):
    """A home that never ran serve/up keeps reading its legacy files, and new
    turns (which go to the store) sort after them within the same session."""
    paths = _paths(tmp_path)
    _write_jsonl(paths, "atlas", "s1", _legacy_thread())
    memory = Memory(paths=paths, bot="atlas")
    memory.log_turn("s1", "in:user", "and the rollback plan?", peer="user")
    memory.log_turn("s1", "out", "rollback documented", peer="user")
    rows = user_thread(memory, peer="user")
    assert [r["text"] for r in rows] == [
        *[r["text"] for r in _legacy_thread()],
        "and the rollback plan?",
        "rollback documented",
    ]


def test_migration_keeps_mixed_mode_thread_order(tmp_path):
    """A session with un-migrated JSONL plus newer store rows must migrate
    with the file's (older) turns still first — the import takes seqs below
    the store's, never after them."""
    paths = _paths(tmp_path)
    _write_jsonl(paths, "atlas", "s1", _legacy_thread())
    memory = Memory(paths=paths, bot="atlas")
    memory.log_turn("s1", "in:user", "and the rollback plan?", peer="user")
    memory.log_turn("s1", "out", "rollback documented", peer="user")
    expected = [r["text"] for r in user_thread(memory, peer="user")]
    migrate_sessions(paths)
    after = Memory(paths=paths, bot="atlas")
    assert [r["text"] for r in user_thread(after, peer="user")] == expected
    assert not list((paths.bot_memory("atlas") / "sessions").glob("*.jsonl"))


def test_recreated_jsonl_after_import_is_not_dropped(tmp_path):
    """An old-version agent still on the JSONL writer can recreate a session
    file after its import (the marker exists). Its turns must stay readable
    live and the next migration must fold exactly the new lines in."""
    paths = _paths(tmp_path)
    _write_jsonl(paths, "atlas", "s1", _legacy_thread())
    migrate_sessions(paths)
    late = {"ts": 2000.0, "role": "in:user", "text": "late jsonl turn", "peer": "user"}
    _write_jsonl(paths, "atlas", "s1", [late])
    live = user_thread(Memory(paths=paths, bot="atlas"), peer="user")
    assert [r["text"] for r in live][-1] == "late jsonl turn"
    report = migrate_sessions(paths)
    assert report.records == 1, "only the novel line imports"
    after = user_thread(Memory(paths=paths, bot="atlas"), peer="user")
    assert [r["text"] for r in after] == [r["text"] for r in live]
    sessions = paths.bot_memory("atlas") / "sessions"
    assert not list(sessions.glob("*.jsonl"))
    assert len(list((sessions / "archive").glob("s1*.jsonl"))) == 2, "both archives kept"


def test_append_landing_during_archive_is_swept_in(tmp_path, monkeypatch):
    """A still-running old-version writer can append between the import's
    snapshot read and the archive rename; those lines land in the renamed
    inode. The post-rename sweep must import exactly that delta."""
    import harness.statestore as statestore

    paths = _paths(tmp_path)
    _write_jsonl(paths, "atlas", "s1", _legacy_thread())
    late = {"ts": 3000.0, "role": "in:user", "text": "landed during archive", "peer": "user"}
    real_archive = statestore._archive

    def racing_archive(path):
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(late, ensure_ascii=False) + "\n")
        return real_archive(path)

    monkeypatch.setattr(statestore, "_archive", racing_archive)
    report = migrate_sessions(paths)
    assert report.records == len(_legacy_thread()) + 1
    rows = user_thread(Memory(paths=paths, bot="atlas"), peer="user")
    assert [r["text"] for r in rows][-1] == "landed during archive"


def test_history_read_survives_concurrent_archive_rename(tmp_path):
    """Migration renames session files from another thread/process; a path
    globbed a moment ago may be gone by the read. That is an empty file to
    the reader, never a FileNotFoundError out of history/recall."""
    paths = _paths(tmp_path)
    memory = Memory(paths=paths, bot="atlas")
    gone = memory.sessions_dir / "gone.jsonl"
    assert memory._file_records(gone, "gone") == []


def test_recall_parity_across_migration_and_summary_skip(tmp_path):
    paths = _paths(tmp_path)
    records = _legacy_thread() + [
        {
            "ts": 1010.0,
            "role": "summary",
            "text": "flag summary",
            "peer": "user",
            "is_summary": True,
            "covers_until": 1004.0,
        }
    ]
    _write_jsonl(paths, "atlas", "s1", records)
    memory = Memory(paths=paths, bot="atlas")
    before = memory.recall("flag")
    assert before and all(not r.get("is_summary") for r in before)
    migrate_sessions(paths)
    after = Memory(paths=paths, bot="atlas")
    assert after.recall("flag") == before


def test_compaction_summary_lands_in_the_store_after_migration(tmp_path):
    paths = _paths(tmp_path)
    _write_jsonl(
        paths,
        "atlas",
        "s1",
        [
            {"ts": 1.0, "role": "in:user", "text": "old ask " + "x" * 400, "peer": "user"},
            {"ts": 2.0, "role": "out", "text": "old answer " + "y" * 400, "peer": "user"},
            {"ts": 3.0, "role": "in:user", "text": "current ask", "peer": "user"},
            {"ts": 4.0, "role": "out", "text": "current answer", "peer": "user"},
        ],
    )
    migrate_sessions(paths)
    memory = Memory(paths=paths, bot="atlas")
    from providers.echo import EchoProvider

    assert maybe_compact(
        memory,
        peer="user",
        provider=EchoProvider(),
        budget=40,
        session_id="s1",
        paths=paths,
        bot="atlas",
    )
    summaries = [r for r in memory._session_records() if r.get("is_summary")]
    assert summaries, "the summary record must persist in the store"
    assert not list(memory.sessions_dir.glob("*.jsonl"))
    assert all(
        r.get("type") != "card" and not r.get("is_summary")
        for r in user_thread(memory, peer="user")
    )


def test_prompt_resolution_mirrored_into_the_store(tmp_path):
    paths = _paths(tmp_path)
    pid = write_prompt(
        paths, {"id": "p1", "bot": "atlas", "type": "choice", "question": "pick one"}
    )
    assert pid == "p1"
    row = resolve_prompt(paths, "p1", {"state": "answered", "responded_value": "a"})
    assert row is not None
    mirrored = store_for(paths).prompt_resolution("p1")
    assert mirrored is not None
    assert mirrored["state"] == "answered"
    assert mirrored["responded_value"] == "a"
    assert mirrored["ts"] > 0


# -- startup -----------------------------------------------------------------
def test_network_home_warning_flags_network_filesystems(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    resolved = str(home.resolve())
    nfs = f"fs:/export {resolved} nfs4 rw 0 0\n/dev/sda1 / ext4 rw 0 0\n"
    warning = network_home_warning(home, mounts_text=nfs)
    assert warning and "network filesystem" in warning
    local = f"/dev/sda1 / ext4 rw 0 0\n/dev/sdb1 {resolved} ext4 rw 0 0\n"
    assert network_home_warning(home, mounts_text=local) is None
    # the longest matching mountpoint wins, not the root fallback
    nested = f"fs:/export / nfs4 rw 0 0\n/dev/sdb1 {resolved} ext4 rw 0 0\n"
    assert network_home_warning(home, mounts_text=nested) is None


def test_appends_are_safe_across_threads(tmp_path):
    """WAL + BEGIN IMMEDIATE serialize the seq allocation — concurrent server
    and agent writers must never collide or drop a record."""
    paths = _paths(tmp_path)
    store = store_for(paths)
    errors: list[Exception] = []

    def _write(n):
        try:
            for i in range(10):
                store.append_transcript(
                    "atlas", "s1", {"ts": time.time(), "role": "out", "text": f"{n}-{i}"}
                )
        except Exception as exc:  # pragma: no cover - the assertion is below
            errors.append(exc)

    threads = [threading.Thread(target=_write, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    records = store.transcript_records("atlas")
    assert len(records) == 40
    assert len({r["text"] for r in records}) == 40


def test_append_retries_past_a_writer_outliving_the_busy_timeout(tmp_path):
    """A writer that holds the lock for longer than one whole busy timeout
    must not surface "database is locked" to the caller: the write is retried
    as a fresh transaction once the lock frees (a loaded CI box parks threads
    for longer than any reasonable single timeout)."""
    paths = _paths(tmp_path)
    store = store_for(paths)
    store.append_transcript("atlas", "s1", {"ts": 1.0, "role": "out", "text": "seed"})
    store._busy_timeout_ms = 5  # one attempt's wait is smaller than the hold
    blocker = sqlite3.connect(str(store.path), isolation_level=None, check_same_thread=False)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        release = threading.Timer(0.2, lambda: blocker.execute("COMMIT"))
        release.start()
        try:
            store.append_transcript(
                "atlas", "s1", {"ts": 2.0, "role": "out", "text": "after-lock"}
            )
        finally:
            release.join()
    finally:
        blocker.close()
    texts = {r["text"] for r in store.transcript_records("atlas")}
    assert "after-lock" in texts


# -- claims / admission / budget -----------------------------------
def _store(tmp_path) -> StateStore:
    return StateStore(HarnessPaths.resolve(tmp_path / "home"))


def test_admit_records_session_running_and_claim_together(tmp_path):
    store = _store(tmp_path)
    claim = store.admit("atlas", "s1", "req1", input_ref="f.json", input_json="[]", pid=123)
    assert claim["state"] == "running"
    assert claim["budget"] == DEFAULT_BUDGET
    assert claim["input_ref"] == "f.json"
    session = store.session_state("atlas", "s1")
    assert session["state"] == "running"
    assert session["request_id"] == "req1"
    assert session["owner_pid"] == 123


def test_settle_releases_claim_and_marks_session_idle(tmp_path):
    store = _store(tmp_path)
    store.admit("atlas", "s1", "req1")
    store.settle("req1")
    assert store.claim("req1") is None
    assert store.session_state("atlas", "s1")["state"] == "idle"


def test_settle_of_unknown_request_is_a_noop(tmp_path):
    _store(tmp_path).settle("nope")  # must not raise


def test_readmission_keeps_the_remaining_budget(tmp_path):
    """The retry allowance is durable across restarts: re-admitting a
    redispatched claim must not reset its budget to full."""
    store = _store(tmp_path)
    store.admit("atlas", "s1", "req1", input_json='[{"a": 1}]')
    assert store.charge("req1") == DEFAULT_BUDGET - 1
    # a NEW process (fresh store handle on the same file) re-admits the turn
    again = StateStore(store.db_path)
    claim = again.admit("atlas", "s2", "req1")
    assert claim["budget"] == DEFAULT_BUDGET - 1
    assert claim["state"] == "running"
    # the stored input survives a re-admit that carries none
    assert claim["input_json"] == '[{"a": 1}]'


def test_charge_decrements_and_exhausts(tmp_path):
    store = _store(tmp_path)
    store.admit("atlas", "s1", "req1")
    charges = [store.charge("req1") for _ in range(DEFAULT_BUDGET)]
    assert charges == [2, 1, 0]
    assert store.charge("req1") is None  # exhausted: no fourth attempt, ever
    assert store.charge("ghost") is None  # unknown claim never dispatches


def test_refund_restores_a_charge_and_caps_at_full(tmp_path):
    store = _store(tmp_path)
    store.admit("atlas", "s1", "req1")
    store.charge("req1")
    assert store.refund("req1") == DEFAULT_BUDGET
    assert store.refund("req1") == DEFAULT_BUDGET  # never above the allowance


def test_mark_interrupted_stamps_only_live_claims(tmp_path):
    store = _store(tmp_path)
    store.admit("atlas", "s1", "r1")
    store.admit("atlas", "s1", "r2")
    store.tombstone("r2")
    store.admit("nova", "s9", "r3")
    assert store.mark_interrupted("atlas", reason="shutdown") == 1
    assert store.claim("r1")["state"] == "interrupted"
    assert store.claim("r1")["reason"] == "shutdown"
    assert store.claim("r2")["state"] == "tombstone"
    assert store.claim("r3")["state"] == "running"  # other bot untouched


def test_orphaned_finds_dead_owners_and_stamped_claims(tmp_path):
    store = _store(tmp_path)
    store.admit("atlas", "s1", "dead", pid=1111)
    store.admit("atlas", "s1", "alive", pid=2222)
    store.admit("atlas", "s1", "stamped", pid=2222)
    store.mark_interrupted("atlas", reason="shutdown")  # stamps all three
    store.admit("atlas", "s1", "alive", pid=2222)  # re-admitted by the live owner
    rows = store.orphaned("atlas", alive=lambda pid: pid == 2222)
    assert {r["request_id"] for r in rows} == {"dead", "stamped"}


def test_a_recycled_pid_is_not_ownership(tmp_path):
    """A container restart hands the next agent PID 1 again: a live pid whose
    boot identity no longer matches the claim's is orphaned work, not an
    owner — skipping it silently drops steer-only inputs."""
    import os

    from harness.fsutil import pid_alive

    store = _store(tmp_path)
    store.admit(
        "atlas",
        "s1",
        "req1",
        pid=os.getpid(),  # alive — but recorded under a previous boot's identity
        identity="python -m agent --bot atlas --generation-token=dead-boot",
    )
    rows = store.orphaned("atlas", alive=pid_alive)
    assert [r["request_id"] for r in rows] == ["req1"]


def test_a_live_same_boot_owner_is_kept(tmp_path):
    import os

    from harness.fsutil import pid_alive

    store = _store(tmp_path)
    store.admit("atlas", "s1", "req1", pid=os.getpid())  # identity recorded automatically
    assert store.orphaned("atlas", alive=pid_alive) == []


def test_session_lock_held_by_a_recycled_pid_is_cleared(tmp_path):
    import os

    from harness.fsutil import pid_alive

    store = _store(tmp_path)
    store.admit("atlas", "old-boot", "r1", pid=os.getpid(), identity="not this boot")
    store.admit("atlas", "this-boot", "r2", pid=os.getpid())
    assert store.sweep_stale_sessions("atlas", alive=pid_alive) == 1
    assert store.session_state("atlas", "old-boot")["state"] == "idle"
    assert store.session_state("atlas", "this-boot")["state"] == "running"


def test_a_v1_database_migrates_forward_to_v2(tmp_path):
    """v1 predates owner_identity; opening it must add the bare nullable
    columns and bump user_version, keeping existing rows readable."""
    db = tmp_path / "home" / "state.sqlite"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(str(db)) as conn:
        conn.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "CREATE TABLE sessions (bot TEXT NOT NULL, session TEXT NOT NULL,"
            " state TEXT NOT NULL, request_id TEXT, owner_pid INTEGER,"
            " updated_ts REAL NOT NULL, PRIMARY KEY (bot, session));"
            "CREATE TABLE turn_claims (request_id TEXT PRIMARY KEY, bot TEXT NOT NULL,"
            " session TEXT NOT NULL, ts REAL NOT NULL,"
            " input_ref TEXT NOT NULL DEFAULT '', input_json TEXT NOT NULL DEFAULT '',"
            " state TEXT NOT NULL, reason TEXT, owner_pid INTEGER,"
            " budget INTEGER NOT NULL, updated_ts REAL NOT NULL);"
        )
        conn.execute(
            "INSERT INTO turn_claims(request_id, bot, session, ts, state, budget, updated_ts) "
            "VALUES('old', 'atlas', 's1', 1.0, 'running', 3, 1.0)"
        )
        conn.execute("PRAGMA user_version=1")
    store = StateStore(db)
    assert store.claim("old")["owner_identity"] is None  # column added, row kept
    store.admit("atlas", "s1", "new", pid=1234)
    with sqlite3.connect(str(db)) as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION


def test_v1_migration_replays_after_crash_between_alter_and_version_bump(tmp_path):
    """A crash after ALTER TABLE but before the user_version bump leaves a v1
    database that already carries owner_identity; re-opening must replay the
    migration cleanly instead of dying on a duplicate-column ALTER."""
    db = tmp_path / "home" / "state.sqlite"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(str(db)) as conn:
        conn.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "CREATE TABLE sessions (bot TEXT NOT NULL, session TEXT NOT NULL,"
            " state TEXT NOT NULL, request_id TEXT, owner_pid INTEGER,"
            " updated_ts REAL NOT NULL, PRIMARY KEY (bot, session));"
            "CREATE TABLE turn_claims (request_id TEXT PRIMARY KEY, bot TEXT NOT NULL,"
            " session TEXT NOT NULL, ts REAL NOT NULL,"
            " input_ref TEXT NOT NULL DEFAULT '', input_json TEXT NOT NULL DEFAULT '',"
            " state TEXT NOT NULL, reason TEXT, owner_pid INTEGER,"
            " budget INTEGER NOT NULL, updated_ts REAL NOT NULL);"
        )
        # the crashed migrator got exactly this far: columns added, version not bumped
        conn.execute("ALTER TABLE turn_claims ADD COLUMN owner_identity TEXT")
        conn.execute("ALTER TABLE sessions ADD COLUMN owner_identity TEXT")
        conn.execute("PRAGMA user_version=1")
    store = StateStore(db)
    store.admit("atlas", "s1", "new", pid=1234)  # opens + migrates
    with sqlite3.connect(str(db)) as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION


def test_sweep_stale_sessions_clears_dead_running_locks(tmp_path):
    store = _store(tmp_path)
    store.admit("atlas", "old", "r1", pid=1111)
    store.admit("atlas", "live", "r2", pid=2222)
    cleared = store.sweep_stale_sessions("atlas", alive=lambda pid: pid == 2222)
    assert cleared == 1
    assert store.session_state("atlas", "old")["state"] == "idle"
    assert store.session_state("atlas", "live")["state"] == "running"


def test_record_steer_appends_to_the_live_claim(tmp_path):
    store = _store(tmp_path)
    store.admit("atlas", "s1", "req1", input_json='[{"id": "req1"}]')
    assert store.record_steer("req1", [{"id": "extra"}]) == 1
    assert store.record_steer("ghost", [{"id": "x"}]) == 0
    claim = store.claim("req1")
    assert '"extra"' in claim["input_json"] and '"req1"' in claim["input_json"]


def test_budget_survives_reopening_the_store(tmp_path):
    """The whole point: the budget is durable across process restarts."""
    store = _store(tmp_path)
    store.admit("atlas", "s1", "req1")
    store.charge("req1")
    store.charge("req1")
    reopened = StateStore(store.db_path)
    assert reopened.claim("req1")["budget"] == DEFAULT_BUDGET - 2


def test_newer_schema_is_refused(tmp_path):
    store = _store(tmp_path)
    store.admit("atlas", "s1", "req1")  # creates the db at SCHEMA_VERSION
    with sqlite3.connect(str(store.db_path)) as conn:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    with pytest.raises(SchemaTooNew):
        StateStore(store.db_path).claim("req1")


def test_store_uses_wal_journal_mode(tmp_path):
    store = _store(tmp_path)
    store.admit("atlas", "s1", "req1")
    with sqlite3.connect(str(store.db_path)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_meta_records_the_app_version(tmp_path):
    from harness.version import __version__

    store = _store(tmp_path)
    store.admit("atlas", "s1", "req1")
    with sqlite3.connect(str(store.db_path)) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='app_version'").fetchone()
    assert row[0] == __version__


def test_write_retries_when_connect_itself_reports_locked(tmp_path):
    """The journal-mode pragma inside _connect needs locks too: a busy/locked
    error raised while OPENING the connection must retry like a failed BEGIN,
    not escape past the whole retry loop."""
    paths = _paths(tmp_path)
    store = store_for(paths)
    real_connect = type(store)._connect
    calls = {"n": 0}

    def flaky_connect(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_connect(self)

    store_cls_connect = store._connect
    try:
        store._connect = flaky_connect.__get__(store)
        store.append_transcript("atlas", "s1", {"ts": 1.0, "role": "out", "text": "x"})
    finally:
        store._connect = store_cls_connect
    assert calls["n"] >= 2
    assert [r["text"] for r in store.transcript_records("atlas")] == ["x"]
