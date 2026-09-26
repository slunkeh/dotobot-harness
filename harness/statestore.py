"""SQLite state store + per-bot session JSONL migration.

State that needs transactions and indexed reads lives in one stdlib-`sqlite3`
database at `$HARNESS_HOME/state.sqlite`. Today that is session transcripts
(what `agent/history.py`, `Memory.recall`, compaction and
`GET /api/bots/<name>/history` read), authoritative task and prompt decisions,
turn admission claims, and delivery receipts. Everything else deliberately stays as
files — roster.json, policy.toml, credentials, connectors, bundles, skills,
streams JSONL (ephemeral) and the append-only usage ledger.

Schema discipline (adopted from OpenClaw's session-storage move):

- The schema version is `PRAGMA user_version`, mirrored by a `meta` row that
  also carries the app version that wrote it. Migrations are forward-only and
  run on open; a database NEWER than this code is refused with
  `SchemaTooNew`, which the CLI maps to the distinct exit code
  `SCHEMA_TOO_NEW_EXIT` so a systemd unit can `RestartPreventExitStatus=` it
  instead of restart-looping into the same refusal.
- A same-version additive change is allowed only as a bare nullable column;
  anything constrained (NOT NULL, UNIQUE, index changes, new table) bumps
  `SCHEMA_VERSION` with a migration step.
- WAL journal mode, single-writer discipline via the serve process (agent
  subprocesses on the same host coordinate through WAL + busy_timeout). A
  `$HARNESS_HOME` on a network filesystem gets a startup warning — WAL over
  NFS/CIFS risks split-brain corruption.

Migration of the legacy per-bot `memory/<bot>/sessions/*.jsonl` logs is
one-way, idempotent and resumable: each file imports in one transaction that
also writes a per-file marker row (`imported_sessions`), so a crash mid-way
re-runs safely, then the source file moves to `sessions/archive/` — archived,
never deleted, so a downgrade can still read history. Torn trailing JSONL
lines are skipped with a logged count, matching the old reader.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .paths import HarnessPaths
from .redaction import scrub as scrub_secrets
from .version import __version__

log = logging.getLogger("harness.statestore")

DB_FILENAME = "state.sqlite"

#: Bump only with a converging migration step in _ensure_schema (forward-only).
#: v1 was the transcripts seed, v2 added owner-identity columns to
#: the claims tables; v3 is the union of both stores; v4 adds a
#: nullable thread_id on transcripts; v5 adds authoritative task decisions.
SCHEMA_VERSION = 5

#: Redispatch attempts an interrupted turn gets before it is tombstoned
#: Charged before dispatch; refunded only on a proven
#: pre-acceptance rejection; kept when the outcome is uncertain.
DEFAULT_BUDGET = 3

#: claim states
RUNNING = "running"
QUEUED = "queued"
INTERRUPTED = "interrupted"
TOMBSTONE = "tombstone"

#: Exit code for "the database schema is newer than this code" — sysexits
#: EX_CONFIG. Distinct from a crash's 1 so systemd units can set
#: `RestartPreventExitStatus=78` and not restart-loop into the same refusal.
SCHEMA_TOO_NEW_EXIT = 78

_NETWORK_FSTYPES = {
    "nfs",
    "nfs4",
    "cifs",
    "smb3",
    "smbfs",
    "9p",
    "glusterfs",
    "ceph",
    "cephfs",
    "lustre",
    "afs",
}


class SchemaTooNew(RuntimeError):
    """The on-disk schema was written by a newer harness than this code."""

    exit_code = SCHEMA_TOO_NEW_EXIT


def _self_identity(pid: int | None) -> str | None:
    """This process's boot identity, recorded on rows it owns: a
    cmdline snapshot whose spawn generation token no two boots share, so a
    recycled pid can never make orphaned work look owned."""
    if pid is None or pid != os.getpid():
        return None
    from isolation.process_identity import read_cmdline

    return read_cmdline(pid)


def _owned(pid, stored_identity, *, alive, identity_of) -> bool:
    """Is this claim/session still held by a live owner process?

    The pid must be alive AND, when an identity was recorded, the live
    process wearing that pid must still be the same boot. A recorded
    identity that no longer matches is a recycled pid — orphaned.
    """
    if pid is None or not alive(int(pid)):
        return False
    if stored_identity is None:
        return True
    return identity_of(int(pid)) == stored_identity


def _converge_schema(conn: sqlite3.Connection) -> None:
    """One converging migration: bring ANY older shape (fresh, transcripts-v1,
    claims-v1/v2) to the current union schema. Individual execute() calls,
    never executescript(): executescript implicitly commits the caller's
    wrapping migration transaction. Every step is guarded, so an interrupted
    tail replays cleanly."""
    ddl = """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transcripts (
            id         INTEGER PRIMARY KEY,
            bot        TEXT NOT NULL,
            session    TEXT NOT NULL,
            seq        INTEGER NOT NULL,
            ts         REAL NOT NULL,
            role       TEXT NOT NULL,
            peer       TEXT,
            room       TEXT,
            is_summary INTEGER NOT NULL DEFAULT 0,
            thread_id  TEXT,
            payload    TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ix_transcripts_thread ON transcripts (bot, session, seq);
        CREATE INDEX IF NOT EXISTS ix_transcripts_bot_ts ON transcripts (bot, ts);
        CREATE TABLE IF NOT EXISTS imported_sessions (
            bot         TEXT NOT NULL,
            filename    TEXT NOT NULL,
            imported_at REAL NOT NULL,
            records     INTEGER NOT NULL,
            torn        INTEGER NOT NULL,
            PRIMARY KEY (bot, filename)
        );
        CREATE TABLE IF NOT EXISTS prompt_resolutions (
            prompt_id TEXT PRIMARY KEY,
            bot       TEXT NOT NULL,
            state     TEXT NOT NULL,
            ts        REAL NOT NULL,
            payload   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            bot            TEXT NOT NULL,
            session        TEXT NOT NULL,
            state          TEXT NOT NULL,
            request_id     TEXT,
            owner_pid      INTEGER,
            owner_identity TEXT,
            updated_ts     REAL NOT NULL,
            PRIMARY KEY (bot, session)
        );
        CREATE TABLE IF NOT EXISTS turn_claims (
            request_id     TEXT PRIMARY KEY,
            bot            TEXT NOT NULL,
            session        TEXT NOT NULL,
            ts             REAL NOT NULL,
            input_ref      TEXT NOT NULL DEFAULT '',
            input_json     TEXT NOT NULL DEFAULT '',
            state          TEXT NOT NULL,
            reason         TEXT,
            owner_pid      INTEGER,
            owner_identity TEXT,
            budget         INTEGER NOT NULL,
            updated_ts     REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS turn_claims_bot_state ON turn_claims (bot, state);
        """
    # The transcripts-v1 seed shipped empty placeholder claims tables with a
    # different shape (no `state` column). They were never written by any code
    # path, so an empty one is dropped and recreated in the real shape; a
    # non-empty one is unknown data and is refused rather than guessed at.
    for stub in ("turn_claims", "delivery_receipts"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({stub})").fetchall()}
        is_stub = bool(cols) and (stub == "delivery_receipts" or "state" not in cols)
        if is_stub:
            count = int(conn.execute(f"SELECT COUNT(*) FROM {stub}").fetchone()[0])
            if count:
                raise RuntimeError(
                    f"state store table {stub} has an unknown legacy shape with "
                    f"{count} rows; refusing to migrate it away"
                )
            conn.execute(f"DROP TABLE {stub}")
    for stmt in ddl.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    # claims-v1 predates the owner-identity columns; add them as bare
    # nullable columns (the only shape an additive change may take).
    for table in ("turn_claims", "sessions"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "owner_identity" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN owner_identity TEXT")
    # v4: side-thread replies hang off a main-line message_id. Nullable so
    # existing rows stay main-transcript; no index (same-version would
    # refuse one — this rides the v4 bump).
    tcols = {r[1] for r in conn.execute("PRAGMA table_info(transcripts)").fetchall()}
    if tcols and "thread_id" not in tcols:
        conn.execute("ALTER TABLE transcripts ADD COLUMN thread_id TEXT")


def _task_decision_schema(conn: sqlite3.Connection) -> None:
    """v5: authoritative task decisions and idempotent task admission."""
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_tasks (
        bot TEXT NOT NULL, conversation TEXT NOT NULL, task_id TEXT NOT NULL,
        revision INTEGER NOT NULL, data TEXT NOT NULL, updated_ts REAL NOT NULL,
        PRIMARY KEY(bot, conversation))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_task_inputs (
        bot TEXT NOT NULL, conversation TEXT NOT NULL, input_id TEXT NOT NULL,
        data TEXT NOT NULL, PRIMARY KEY(bot, conversation, input_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS agent_prompts (
        prompt_id TEXT PRIMARY KEY, bot TEXT NOT NULL, task_id TEXT NOT NULL DEFAULT '',
        task_revision INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL,
        updated_ts REAL NOT NULL, payload TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS agent_prompts_task ON agent_prompts(bot, task_id)")


class StateStore:
    """One state.sqlite database. Connections are short-lived (opened per
    operation in the calling thread), so one instance is safe to share across
    server request threads and cheap to hold per process."""

    #: Re-attempts of a whole write transaction after the busy timeout expired
    #: while another writer held the lock. The busy handler already waits
    #: inside each attempt; the retries cover a writer parked on the lock for
    #: longer than one whole timeout (a loaded box scheduling threads
    #: unfairly), where SQLite gives up with "database is locked" even though
    #: nothing is wrong with the database.
    WRITE_LOCK_RETRIES = 3

    def __init__(self, path: HarnessPaths | Path | str):
        if isinstance(path, HarnessPaths):
            path = path.state_db
        self.path = Path(path)
        self._migrate_lock = threading.Lock()
        self._busy_timeout_ms = 10000
        self._dir_tightened = False

    @property
    def db_path(self) -> Path:
        """Alias for callers written against the claims-era store."""
        return self.path

    # -- open / schema -----------------------------------------------------
    def _private_file(self) -> None:
        """Make sure the database is 0600 before SQLite ever touches it.

        Every transcript lives in this file; a bare `sqlite3.connect` creates
        it with the umask (0644 under 022), readable by any other local
        account. Pre-creating with an explicit mode means it is *born*
        private — no chmod window — and SQLite copies the main file's mode
        onto its `-wal` / `-shm` siblings. An existing file we own that is
        wider (created by an older harness) is tightened on open, and the
        containing directory once per store so the siblings cannot be listed
        either.
        """
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
            os.close(fd)
            st = os.stat(self.path)
            if st.st_mode & 0o077 and st.st_uid == os.getuid():
                os.chmod(self.path, 0o600)
        except (OSError, AttributeError):  # pragma: no cover - non-posix
            return
        if not self._dir_tightened:
            self._dir_tightened = True
            try:
                parent = self.path.parent
                pst = os.stat(parent)
                if pst.st_mode & 0o077 and pst.st_uid == os.getuid():
                    os.chmod(parent, 0o700)
            except OSError:  # pragma: no cover - not ours
                pass

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._private_file()
        conn = sqlite3.connect(
            self.path, timeout=self._busy_timeout_ms / 1000.0, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        # busy_timeout FIRST: journal_mode needs locks too, and running it
        # with the default zero timeout makes it SQLITE_BUSY-flaky against a
        # concurrent writer.
        conn.execute(f"PRAGMA busy_timeout={int(self._busy_timeout_ms)}")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            # The mode is a persistent property of the database file, set at
            # creation and re-asserted on every connect — so a busy writer
            # here is tolerable: reads work in either mode and the next
            # successful connect converges the property.
            if "locked" not in str(exc) and "busy" not in str(exc):
                conn.close()
                raise
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            self._ensure_schema(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise SchemaTooNew(
                f"{self.path} is schema v{version} but this harness only knows "
                f"v{SCHEMA_VERSION} — it was written by a newer version. "
                "Upgrade the harness (or restore the older database) instead "
                "of retrying; migrations are forward-only."
            )
        if version == SCHEMA_VERSION:
            return
        with self._migrate_lock:
            # Serialize across threads; BEGIN IMMEDIATE serializes across
            # processes. Re-read under the lock — someone may have won.
            conn.execute("BEGIN IMMEDIATE")
            try:
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if version > SCHEMA_VERSION:
                    raise SchemaTooNew(f"{self.path} is schema v{version}")
                if version < SCHEMA_VERSION:
                    _converge_schema(conn)
                if version < 5:
                    _task_decision_schema(conn)
                # PRAGMA takes no parameter binding; the value is our own int.
                conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
                self._set_meta(conn, "schema_version", str(SCHEMA_VERSION))
                self._set_meta(conn, "app_version", __version__)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def check(self) -> None:
        """Open the database once: creates/migrates the schema, or raises
        SchemaTooNew. Startup calls this so a refusal happens at boot, not on
        the first write mid-turn."""
        self._connect().close()

    def meta(self) -> dict[str, str]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT key, value FROM meta").fetchall()
            return {row["key"]: row["value"] for row in rows}
        finally:
            conn.close()

    # -- transcripts -------------------------------------------------------
    @contextmanager
    def _tx(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    # -- admission / settlement ----------------------------------
    def cleanup_references(self, bot: str) -> tuple[set[str], set[str]]:
        """References needed to remove retained files before deleting SQL rows."""
        conn = self._connect()
        try:
            requests = {
                r[0] for r in conn.execute("SELECT request_id FROM turn_claims WHERE bot=?", (bot,))
            }
            prompts = {
                r[0]
                for r in conn.execute(
                    "SELECT prompt_id FROM prompt_resolutions WHERE bot=?", (bot,)
                )
            }
            prompts.update(
                row[0]
                for row in conn.execute("SELECT prompt_id FROM agent_prompts WHERE bot=?", (bot,))
            )
            return requests, prompts
        finally:
            conn.close()

    def delete_bot(self, bot: str) -> None:
        """Purge only this bot's rows after its deletion grace period."""
        with self._tx() as conn:
            for table in (
                "transcripts",
                "imported_sessions",
                "prompt_resolutions",
                "sessions",
                "turn_claims",
                "delivery_intents",
                "agent_tasks",
                "agent_task_inputs",
                "agent_prompts",
            ):
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone():
                    conn.execute(f"DELETE FROM {table} WHERE bot=?", (bot,))

    def admit(
        self,
        bot: str,
        session: str,
        request_id: str,
        *,
        input_ref: str = "",
        input_json: str = "",
        pid: int | None = None,
        identity: str | None = None,
        ts: float | None = None,
    ) -> dict:
        """Admit a turn: ONE transaction records the input reference, the
        session marked running, and the recovery claim — before any provider
        call. Re-admitting a known request (a redispatched recovery) keeps its
        remaining budget, so the retry allowance is durable across restarts.
        """
        now = time.time() if ts is None else ts
        identity = _self_identity(pid) if identity is None else identity
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO sessions(bot, session, state, request_id, owner_pid, "
                "owner_identity, updated_ts) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(bot, session) DO UPDATE SET state=excluded.state, "
                "request_id=excluded.request_id, owner_pid=excluded.owner_pid, "
                "owner_identity=excluded.owner_identity, updated_ts=excluded.updated_ts",
                (bot, session, RUNNING, request_id, pid, identity, now),
            )
            conn.execute(
                "INSERT INTO turn_claims(request_id, bot, session, ts, input_ref, "
                "input_json, state, reason, owner_pid, owner_identity, budget, updated_ts) "
                "VALUES(?,?,?,?,?,?,?,NULL,?,?,?,?) "
                "ON CONFLICT(request_id) DO UPDATE SET bot=excluded.bot, "
                "session=excluded.session, state=excluded.state, reason=NULL, "
                "owner_pid=excluded.owner_pid, owner_identity=excluded.owner_identity, "
                "updated_ts=excluded.updated_ts, "
                "input_ref=excluded.input_ref, "
                "input_json=CASE WHEN excluded.input_json != '' "
                "THEN excluded.input_json ELSE turn_claims.input_json END",
                (
                    request_id,
                    bot,
                    session,
                    now,
                    input_ref,
                    input_json,
                    RUNNING,
                    pid,
                    identity,
                    DEFAULT_BUDGET,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM turn_claims WHERE request_id=?", (request_id,)
            ).fetchone()
        return dict(row)

    def settle(self, request_id: str) -> None:
        """The turn finished (reply sent or deliberately dropped): release the
        claim and mark its session idle, in one transaction."""
        now = time.time()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT bot, session FROM turn_claims WHERE request_id=?", (request_id,)
            ).fetchone()
            conn.execute("DELETE FROM turn_claims WHERE request_id=?", (request_id,))
            if row is not None:
                conn.execute(
                    "UPDATE sessions SET state='idle', request_id=NULL, updated_ts=? "
                    "WHERE bot=? AND session=? AND (request_id=? OR request_id IS NULL)",
                    (now, row["bot"], row["session"], request_id),
                )

    def record_steer(self, request_id: str, items: list[dict]) -> int:
        """Append steered follow-ups (consumed from the inbox mid-turn) to the
        live claim's input, so a crash after the steer still redispatches
        them. Returns how many were recorded (0 when no claim is live)."""
        if not items:
            return 0
        with self._tx() as conn:
            row = conn.execute(
                "SELECT input_json FROM turn_claims WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                return 0
            try:
                existing = json.loads(row["input_json"] or "[]")
            except json.JSONDecodeError:
                existing = []
            if not isinstance(existing, list):
                existing = [existing]
            existing.extend(items)
            conn.execute(
                "UPDATE turn_claims SET input_json=?, updated_ts=? WHERE request_id=?",
                (json.dumps(existing, ensure_ascii=False), time.time(), request_id),
            )
        return len(items)

    # -- interruption / recovery -------------------------------------------
    def mark_interrupted(self, bot: str, *, reason: str) -> int:
        """Stamp a recovery marker on every live claim for `bot` (graceful
        shutdown path). Tombstones and already-interrupted claims keep their
        state; returns how many claims were stamped."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE turn_claims SET state=?, reason=?, updated_ts=? "
                "WHERE bot=? AND state IN (?, ?)",
                (INTERRUPTED, reason, time.time(), bot, RUNNING, QUEUED),
            )
            return cur.rowcount

    def orphaned(self, bot: str, *, alive, identity_of=None) -> list[dict]:
        """Claims holding interrupted work: stamped `interrupted`, or still
        claiming running/queued with no live SAME-BOOT owner process —
        `alive` is pid -> bool, and a recorded owner identity must still
        match what wears that pid (a recycled pid is not ownership;
        SIGKILL/OOM leaves nothing to stamp the claim)."""
        identity_of = self._identity_of if identity_of is None else identity_of
        with self._tx() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM turn_claims WHERE bot=? AND state != ? ORDER BY ts",
                    (bot, TOMBSTONE),
                )
            ]
        return [
            row
            for row in rows
            if row["state"] == INTERRUPTED
            or not _owned(
                row["owner_pid"], row["owner_identity"], alive=alive, identity_of=identity_of
            )
        ]

    def sweep_stale_sessions(self, bot: str, *, alive, identity_of=None) -> int:
        """Clear stale running locks: sessions claiming running whose owner
        process (same pid AND same boot) is gone go back to idle. Returns
        how many were cleared."""
        identity_of = self._identity_of if identity_of is None else identity_of
        with self._tx() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT session, owner_pid, owner_identity FROM sessions "
                    "WHERE bot=? AND state='running'",
                    (bot,),
                )
            ]
            cleared = 0
            for row in rows:
                if _owned(
                    row["owner_pid"], row["owner_identity"], alive=alive, identity_of=identity_of
                ):
                    continue
                conn.execute(
                    "UPDATE sessions SET state='idle', request_id=NULL, updated_ts=? "
                    "WHERE bot=? AND session=?",
                    (time.time(), bot, row["session"]),
                )
                cleared += 1
        return cleared

    @staticmethod
    def _identity_of(pid: int) -> str | None:
        """What identity the live process wearing `pid` presents now."""
        from isolation.process_identity import read_cmdline

        return read_cmdline(pid)

    # -- redispatch budget --------------------------------------------------
    def charge(self, request_id: str) -> int | None:
        """Spend one redispatch attempt BEFORE dispatching. Returns the
        remaining budget after the charge, or None when it is exhausted (or
        the claim is gone) — exhausted means tombstone, never another loop."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT budget FROM turn_claims WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None or int(row["budget"]) <= 0:
                return None
            remaining = int(row["budget"]) - 1
            conn.execute(
                "UPDATE turn_claims SET budget=?, updated_ts=? WHERE request_id=?",
                (remaining, time.time(), request_id),
            )
        return remaining

    def refund(self, request_id: str) -> int | None:
        """Give a charge back — only for a PROVEN pre-acceptance rejection
        (the dispatch demonstrably never entered the queue). An uncertain
        outcome keeps the charge. Returns the new budget."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT budget FROM turn_claims WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                return None
            budget = min(DEFAULT_BUDGET, int(row["budget"]) + 1)
            conn.execute(
                "UPDATE turn_claims SET budget=?, updated_ts=? WHERE request_id=?",
                (budget, time.time(), request_id),
            )
        return budget

    def tombstone(self, request_id: str) -> None:
        """Budget exhausted: the turn is lost. The row stays as the record."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE turn_claims SET state=?, updated_ts=? WHERE request_id=?",
                (TOMBSTONE, time.time(), request_id),
            )

    def mark_queued(
        self, request_id: str, *, pid: int | None = None, identity: str | None = None
    ) -> None:
        """A redispatched claim is back in the queue awaiting re-admission."""
        identity = _self_identity(pid) if identity is None else identity
        with self._tx() as conn:
            conn.execute(
                "UPDATE turn_claims SET state=?, owner_pid=?, owner_identity=?, updated_ts=? "
                "WHERE request_id=?",
                (QUEUED, pid, identity, time.time(), request_id),
            )

    # -- reads ---------------------------------------------------------------
    def claim(self, request_id: str) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM turn_claims WHERE request_id=?", (request_id,)
            ).fetchone()
        return None if row is None else dict(row)

    def claims(self, bot: str | None = None, state: str | None = None) -> list[dict]:
        query = "SELECT * FROM turn_claims"
        clauses, args = [], []
        if bot is not None:
            clauses.append("bot=?")
            args.append(bot)
        if state is not None:
            clauses.append("state=?")
            args.append(state)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self._tx() as conn:
            return [dict(r) for r in conn.execute(query + " ORDER BY ts", args)]

    def session_state(self, bot: str, session: str) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE bot=? AND session=?", (bot, session)
            ).fetchone()
        return None if row is None else dict(row)

    def _write_tx(self, fn):
        """Run `fn(conn)` as one IMMEDIATE transaction, retrying the whole
        attempt when the write lock could not be had within the busy timeout.
        A retry only ever re-runs `fn` after a ROLLBACK (or a failed BEGIN),
        so a committed transaction is never repeated."""
        last: sqlite3.OperationalError | None = None
        for attempt in range(self.WRITE_LOCK_RETRIES + 1):
            if attempt:
                time.sleep(0.1 * attempt)
            try:
                # _connect is inside the retried region: its journal_mode
                # pragma needs locks of its own and can report busy/locked
                # under a concurrent writer, exactly like BEGIN IMMEDIATE.
                conn = self._connect()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) and "busy" not in str(exc):
                    raise
                last = exc
                continue
            try:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc) and "busy" not in str(exc):
                        raise
                    last = exc
                    continue
                try:
                    result = fn(conn)
                    conn.execute("COMMIT")
                    return result
                except sqlite3.OperationalError as exc:
                    conn.execute("ROLLBACK")
                    if "locked" not in str(exc) and "busy" not in str(exc):
                        raise
                    last = exc
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                conn.close()
        raise last

    def append_transcript(self, bot: str, session: str, record: dict) -> None:
        """Append one session record; seq is allocated under the write lock."""
        payload = json.dumps(record, ensure_ascii=False)
        room = record.get("room")
        thread_id = record.get("thread_id")
        thread_id = None if thread_id is None else str(thread_id)

        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO transcripts "
                "(bot, session, seq, ts, role, peer, room, is_summary, "
                " thread_id, payload) "
                "VALUES (?, ?, "
                " (SELECT COALESCE(MAX(seq) + 1, 0) FROM transcripts"
                "  WHERE bot = ? AND session = ?), "
                " ?, ?, ?, ?, ?, ?, ?)",
                (
                    bot,
                    session,
                    bot,
                    session,
                    float(record.get("ts", 0.0)),
                    str(record.get("role", "")),
                    record.get("peer"),
                    None if room is None else str(room),
                    1 if record.get("is_summary") else 0,
                    thread_id,
                    payload,
                ),
            )

        self._write_tx(_insert)

    def transcript_records(self, bot: str) -> list[dict]:
        """All of a bot's records in thread order (session name, then seq),
        each with the legacy `session` key the JSONL reader used to add."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT session, payload FROM transcripts WHERE bot = ? ORDER BY session, seq, id",
                (bot,),
            ).fetchall()
        finally:
            conn.close()
        records: list[dict] = []
        for row in rows:
            try:
                rec = json.loads(row["payload"])
            except json.JSONDecodeError:  # pragma: no cover - we wrote it
                continue
            rec["session"] = row["session"]
            records.append(rec)
        return records

    def transcript_stamp(self, bot: str) -> tuple[int, int]:
        """Cheap change stamp for Memory's record cache."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM transcripts WHERE bot = ?",
                (bot,),
            ).fetchone()
            return (int(row[0]), int(row[1]))
        finally:
            conn.close()

    def transcript_sessions(self, bot: str) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT session FROM transcripts WHERE bot = ?", (bot,)
            ).fetchall()
            return [row["session"] for row in rows]
        finally:
            conn.close()

    # -- JSONL import ------------------------------------------------------
    def imported_files(self, bot: str) -> set[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT filename FROM imported_sessions WHERE bot = ?", (bot,)
            ).fetchall()
            return {row["filename"] for row in rows}
        finally:
            conn.close()

    def import_session_file(
        self, bot: str, path: Path, *, session: str | None = None, filename: str | None = None
    ) -> tuple[int, int]:
        """Import one legacy session JSONL in a single transaction.

        Returns (records imported, torn lines skipped). Idempotent at the
        row level: a line whose exact payload the session already stores is
        skipped, so a crash-leftover file (marker committed, rename lost)
        re-runs to zero, and a file recreated by an old-version JSONL writer
        after its import contributes only its new lines.

        Seq placement keeps thread order: a file with no marker predates
        every store row for its session (a mixed-mode home where log_turn
        wrote the store while the file sat un-migrated), so its lines take
        seqs BELOW the existing minimum; novel lines under an existing
        marker are post-import appends and go after the maximum.

        `session`/`filename` override the path-derived identity — the
        post-archive sweep in `migrate_sessions` reads the archived copy
        (whose name may carry a collision suffix) on the original session's
        behalf.
        """
        session = session or path.stem
        filename = filename or path.name
        parsed: list[dict] = []
        torn = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError:
                torn += 1
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                marker = (
                    conn.execute(
                        "SELECT 1 FROM imported_sessions WHERE bot = ? AND filename = ?",
                        (bot, filename),
                    ).fetchone()
                    is not None
                )
                stored = {
                    row["payload"]
                    for row in conn.execute(
                        "SELECT payload FROM transcripts WHERE bot = ? AND session = ?",
                        (bot, session),
                    )
                }
                novel = [
                    (rec, payload)
                    for rec in parsed
                    if (payload := json.dumps(rec, ensure_ascii=False)) not in stored
                ]
                if marker and not novel:
                    conn.execute("ROLLBACK")
                    return (0, 0)
                low, high = conn.execute(
                    "SELECT MIN(seq), MAX(seq) FROM transcripts WHERE bot = ? AND session = ?",
                    (bot, session),
                ).fetchone()
                if low is None:
                    base = 0
                elif marker:
                    base = int(high) + 1
                else:
                    base = int(low) - len(novel)
                for i, (rec, payload) in enumerate(novel):
                    room = rec.get("room")
                    thread_id = rec.get("thread_id")
                    conn.execute(
                        "INSERT INTO transcripts "
                        "(bot, session, seq, ts, role, peer, room, is_summary, "
                        " thread_id, payload) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            bot,
                            session,
                            base + i,
                            float(rec.get("ts", 0.0)),
                            str(rec.get("role", "")),
                            rec.get("peer"),
                            None if room is None else str(room),
                            1 if rec.get("is_summary") else 0,
                            None if thread_id is None else str(thread_id),
                            payload,
                        ),
                    )
                conn.execute(
                    "INSERT INTO imported_sessions (bot, filename, imported_at, records, torn) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (bot, filename) DO UPDATE SET "
                    "imported_at = excluded.imported_at, "
                    "records = records + excluded.records, torn = excluded.torn",
                    (bot, filename, time.time(), len(novel), torn),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return (len(novel), torn)

    # -- authoritative prompts --------------------------------------------
    def open_prompt(self, payload: dict) -> tuple[dict, bool]:
        """Create once; replay never overwrites the subject or an answer."""
        row = json.loads(scrub_secrets(json.dumps(payload, ensure_ascii=False)))
        pid = str(row["id"])
        now = float(row.setdefault("ts", time.time()))
        resolution = row.get("resolution") or {}
        with self._tx() as conn:
            created = (
                conn.execute(
                    "INSERT OR IGNORE INTO agent_prompts "
                    "(prompt_id,bot,task_id,task_revision,state,updated_ts,payload) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        pid,
                        str(row.get("bot") or ""),
                        str(row.get("task_id") or ""),
                        int(row.get("task_revision") or 0),
                        resolution.get("state") or "open",
                        now,
                        json.dumps(row, ensure_ascii=False),
                    ),
                ).rowcount
                == 1
            )
            stored = conn.execute(
                "SELECT payload FROM agent_prompts WHERE prompt_id=?", (pid,)
            ).fetchone()
        return json.loads(stored[0]), created

    def prompt(self, prompt_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT payload FROM agent_prompts WHERE prompt_id=?", (prompt_id,)
            ).fetchone()
            return json.loads(row[0]) if row else None
        finally:
            conn.close()

    def read_prompt_answer(self, prompt_id: str, *, consume: bool) -> str | None:
        """The mailbox consumes once; the decision itself remains replayable."""
        with self._tx() as conn:
            raw = conn.execute(
                "SELECT payload FROM agent_prompts WHERE prompt_id=?", (prompt_id,)
            ).fetchone()
            if raw is None:
                return None
            row = json.loads(raw[0])
            resolution = row.get("resolution") or {}
            value = resolution.get("responded_value")
            if resolution.get("state") != "answered" or row.get("answer_consumed") or value is None:
                return None
            if consume:
                row["answer_consumed"] = True
                conn.execute(
                    "UPDATE agent_prompts SET payload=? WHERE prompt_id=?",
                    (json.dumps(row), prompt_id),
                )
            return str(value)

    def prompts(
        self,
        bot: str | None = None,
        *,
        task_id: str | None = None,
        task_revision: int | None = None,
    ) -> list[dict]:
        conn = self._connect()
        try:
            query = "SELECT payload FROM agent_prompts"
            clauses, values = [], []
            for column, value in (
                ("bot", bot),
                ("task_id", task_id),
                ("task_revision", task_revision),
            ):
                if value is not None:
                    clauses.append(f"{column}=?")
                    values.append(value)
            rows = conn.execute(
                query + (" WHERE " + " AND ".join(clauses) if clauses else ""), values
            )
            return [json.loads(row[0]) for row in rows]
        finally:
            conn.close()

    @staticmethod
    def _prompt_current(conn: sqlite3.Connection, row: dict) -> bool:
        if not row.get("task_id") or not row.get("task_conversation"):
            return True  # legacy prompts retain their original scope
        current = conn.execute(
            "SELECT task_id,revision,data FROM agent_tasks WHERE bot=? AND conversation=?",
            (row.get("bot"), row["task_conversation"]),
        ).fetchone()
        return bool(
            current
            and current[0] == row["task_id"]
            and current[1] == int(row.get("task_revision") or 0)
            and json.loads(current[2]).get("status") != "stopped"
        )

    def prompt_current(self, row: dict) -> bool:
        conn = self._connect()
        try:
            return self._prompt_current(conn, row)
        finally:
            conn.close()

    def hide_prompt(self, prompt_id: str) -> None:
        with self._tx() as conn:
            raw = conn.execute(
                "SELECT payload FROM agent_prompts WHERE prompt_id=?", (prompt_id,)
            ).fetchone()
            if raw is not None:
                row = json.loads(raw[0])
                row["cleared"] = True
                conn.execute(
                    "UPDATE agent_prompts SET payload=? WHERE prompt_id=?",
                    (json.dumps(row), prompt_id),
                )

    @staticmethod
    def _save_prompt_resolution(conn: sqlite3.Connection, row: dict, resolution: dict) -> dict:
        row = json.loads(scrub_secrets(json.dumps(row, ensure_ascii=False)))
        res = json.loads(scrub_secrets(json.dumps(resolution, ensure_ascii=False)))
        res.setdefault("ts", time.time())
        row["resolution"] = res
        if res.get("reason") == "task_changed":
            row["projection_pending"] = True
        conn.execute(
            "UPDATE agent_prompts SET state=?,updated_ts=?,payload=? WHERE prompt_id=?",
            (res["state"], res["ts"], json.dumps(row, ensure_ascii=False), row["id"]),
        )
        conn.execute(
            "INSERT INTO prompt_resolutions(prompt_id,bot,state,ts,payload) VALUES(?,?,?,?,?) "
            "ON CONFLICT(prompt_id) DO UPDATE SET state=excluded.state,ts=excluded.ts,payload=excluded.payload",
            (row["id"], row.get("bot") or "", res["state"], res["ts"], json.dumps(res)),
        )
        return row

    def supersede_prompts(self, bot: str | None = None) -> list[dict]:
        """Settle obsolete pending decisions atomically; retain every prior answer."""
        changed = []
        with self._tx() as conn:
            query = "SELECT payload FROM agent_prompts WHERE state IN ('open','skipped')"
            rows = conn.execute(
                query + " AND bot=?" if bot else query, (bot,) if bot else ()
            ).fetchall()
            for raw in rows:
                row = json.loads(raw[0])
                if row.get("resolution"):
                    if row.get("projection_pending"):
                        changed.append(row)
                    continue
                if (
                    row.get("cleared")
                    or row.get("type") == "secret_request"
                    or self._prompt_current(conn, row)
                ):
                    continue
                changed.append(
                    self._save_prompt_resolution(
                        conn,
                        row,
                        {
                            "state": "skipped",
                            "skipped": True,
                            "reason": "task_changed",
                        },
                    )
                )
        return changed

    def prompt_projected(self, prompt_id: str) -> None:
        with self._tx() as conn:
            raw = conn.execute(
                "SELECT payload FROM agent_prompts WHERE prompt_id=?", (prompt_id,)
            ).fetchone()
            if raw is not None:
                row = json.loads(raw[0])
                row.pop("projection_pending", None)
                conn.execute(
                    "UPDATE agent_prompts SET payload=? WHERE prompt_id=?",
                    (json.dumps(row), prompt_id),
                )

    def settle_prompt(
        self, prompt_id: str, resolution: dict, *, bot: str = ""
    ) -> tuple[str, dict | None]:
        """One transaction arbitrates concurrent answers and preserves the winner.

        Same answers replay; conflicting/stale answers never become new chat.
        The durable resolution is also the waiting tool's answer mailbox.
        """
        resolution = json.loads(scrub_secrets(json.dumps(resolution, ensure_ascii=False)))
        with self._tx() as conn:
            raw = conn.execute(
                "SELECT payload FROM agent_prompts WHERE prompt_id=?", (prompt_id,)
            ).fetchone()
            if raw is None:
                return "missing", None
            row = json.loads(raw[0])
            if bot and bot != str(row.get("bot") or ""):
                return "conflict", row
            prior = row.get("resolution")
            if prior:
                if prior.get("reason") == "task_changed":
                    return "stale", row
                same = all(
                    prior.get(k) == resolution.get(k)
                    for k in ("state", "responded_value", "skipped", "secret_provided")
                )
                return ("replayed" if same else "conflict"), row
            if row.get("cleared"):
                return "stale", row
            if resolution.get("state") == "answered" and not self._prompt_current(conn, row):
                return "stale", self._save_prompt_resolution(
                    conn,
                    row,
                    {
                        "state": "skipped",
                        "skipped": True,
                        "reason": "task_changed",
                    },
                )
            if row.get("type") == "secret_request" and "responded_value" in resolution:
                return "conflict", row  # values only enter the separate secret store
            return "applied", self._save_prompt_resolution(conn, row, resolution)

    def claim_prompt_execution(self, prompt_id: str, *, bot: str, task_id: str, revision: int) -> bool:
        """Consume one admitted outgoing action before dispatch, including across workers."""
        with self._tx() as conn:
            raw = conn.execute("SELECT payload FROM agent_prompts WHERE prompt_id=?", (prompt_id,)).fetchone()
            if raw is None:
                return False
            row = json.loads(raw[0])
            if (row.get("execution_started") or row.get("bot") != bot
                    or row.get("task_id") != task_id or row.get("task_revision") != revision
                    or not self._prompt_current(conn, row)
                    or (row.get("resolution") or {}).get("state") != "answered"
                    or (row.get("resolution") or {}).get("responded_value") != "confirm"):
                return False
            row["execution_started"] = time.time()
            conn.execute("UPDATE agent_prompts SET payload=? WHERE prompt_id=?", (json.dumps(row), prompt_id))
            return True

    # -- prompt resolutions ------------------------------------------------
    def record_prompt_resolution(self, prompt_id: str, *, bot: str, resolution: dict) -> None:
        """Durable mirror of a settled prompt (records expire from
        `prompts/`; this row outlives the TTL). Never a secret's value —
        callers pass the same secret-free resolution dict the record carries."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO prompt_resolutions (prompt_id, bot, state, ts, payload) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (prompt_id) DO UPDATE SET "
                    "bot = excluded.bot, state = excluded.state, "
                    "ts = excluded.ts, payload = excluded.payload",
                    (
                        prompt_id,
                        bot,
                        str(resolution.get("state") or ""),
                        float(resolution.get("ts") or time.time()),
                        json.dumps(resolution, ensure_ascii=False),
                    ),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def prompt_resolution(self, prompt_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT payload FROM prompt_resolutions WHERE prompt_id = ?",
                (prompt_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:  # pragma: no cover - we wrote it
            return None


# -- per-home store cache ---------------------------------------------------
_STORES: dict[Path, StateStore] = {}
_STORES_LOCK = threading.Lock()


def store_for(paths: HarnessPaths) -> StateStore:
    """The (cached) StateStore for one harness home."""
    path = paths.state_db
    with _STORES_LOCK:
        store = _STORES.get(path)
        if store is None:
            store = StateStore(path)
            _STORES[path] = store
        return store


# -- legacy JSONL migration -------------------------------------------------
@dataclass
class MigrationReport:
    bots: int = 0
    files: int = 0
    records: int = 0
    torn: int = 0

    def summary(self) -> str:
        return (
            f"migrated {self.records} session record(s) from {self.files} "
            f"JSONL file(s) across {self.bots} bot(s) into state.sqlite "
            f"({self.torn} torn line(s) skipped); sources archived under "
            "memory/<bot>/sessions/archive/"
        )


def _archive(path: Path) -> Path:
    """Move an imported JSONL under sessions/archive/ — never delete it, so a
    downgrade can still read history. A name collision keeps both files.
    Returns the archived path (the post-rename sweep re-reads it)."""
    archive = path.parent / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    target = archive / path.name
    if target.exists():
        target = archive / f"{path.stem}.{int(time.time() * 1000)}{path.suffix}"
    path.rename(target)
    return target


def migrate_sessions(paths: HarnessPaths) -> MigrationReport:
    """One-way import of every bot's `sessions/*.jsonl` into the store.

    Idempotent and resumable at the row level (see `import_session_file`): a
    crash-leftover file whose marker committed imports nothing and is only
    re-archived, while a file recreated by an old-version JSONL writer after
    its import contributes exactly its new lines before being archived.
    """
    report = MigrationReport()
    store = store_for(paths)
    if not paths.memory.is_dir():
        return report
    for bot_dir in sorted(p for p in paths.memory.iterdir() if p.is_dir()):
        sessions = bot_dir / "sessions"
        if not sessions.is_dir():
            continue
        files = sorted(sessions.glob("*.jsonl"))
        if not files:
            continue
        bot = bot_dir.name
        for path in files:
            try:
                records, torn = store.import_session_file(bot, path)
                target = _archive(path)
                # Sweep the archived copy: a still-running old-version writer
                # can append between the import's snapshot read and the
                # rename, and those lines land in the renamed inode.
                # Re-reading it after the rename imports exactly that delta
                # (row-level dedupe makes the common case a no-op) instead of
                # archiving it unread.
                late, late_torn = store.import_session_file(
                    bot, target, session=path.stem, filename=path.name
                )
            except OSError:
                continue  # a concurrent migrator archived this file first
            records += late
            torn = max(torn, late_torn)
            report.records += records
            report.torn += torn
            if torn:
                log.warning("statestore: skipped %d torn JSONL line(s) importing %s", torn, path)
            report.files += 1
        report.bots += 1
    return report


# -- startup ----------------------------------------------------------------
def network_home_warning(home: Path, mounts_text: str | None = None) -> str | None:
    """A warning line when `home` sits on a network filesystem, else None.

    WAL assumes the locking of a local filesystem; two hosts mounting the same
    export can split-brain the database. Best-effort: parsed from /proc/mounts
    (injectable for tests), silent anywhere that file doesn't exist.
    """
    if mounts_text is None:
        try:
            mounts_text = Path("/proc/mounts").read_text(encoding="utf-8")
        except OSError:
            return None
    try:
        resolved = str(Path(home).resolve())
    except OSError:
        resolved = str(home)
    best_len = -1
    best_type: str | None = None
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mountpoint = parts[1].replace("\\040", " ").replace("\\011", "\t")
        fstype = parts[2]
        if resolved == mountpoint or resolved.startswith(mountpoint.rstrip("/") + "/"):
            if len(mountpoint) > best_len:
                best_len = len(mountpoint)
                best_type = fstype
    if best_type is None:
        return None
    lowered = best_type.lower()
    networked = lowered in _NETWORK_FSTYPES or lowered.startswith(("nfs", "fuse.sshfs", "smb"))
    if not networked:
        return None
    return (
        f"harness home {resolved} is on a network filesystem ({best_type}); "
        "state.sqlite uses WAL, which risks split-brain corruption when two "
        "hosts share the mount — keep the home on local disk"
    )


def boot_state(paths: HarnessPaths) -> MigrationReport:
    """Startup hook (`harness serve` / `harness up` via Orchestrator.init):
    warn about a network home, then run the idempotent JSONL migration.
    Raises SchemaTooNew for a database written by a newer harness."""
    warning = network_home_warning(paths.home)
    if warning:
        print(f"warning: {warning}", file=sys.stderr, flush=True)
    store_for(paths).check()
    report = migrate_sessions(paths)
    if report.files:
        print(f"state: {report.summary()}", flush=True)
    return report
