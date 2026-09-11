"""Delivery intent → receipt ledger: never replay an uncertain send.

Port of OpenClaw v2's delivery intent/receipt mechanism. The at-least-once
inbox loop (`agent/runtime.py`) re-runs a whole turn after a crash, which is
right for the work but wrong for its outbound sends: a peer inbox write, a
connector side effect (a Linear comment), or the final chat reply may already
have been received, and rerunning them creates a duplicate the recipient can
see. The ledger records what this harness was *about to send* and what it
*knows arrived*, so recovery can tell the three cases apart:

* **pending** — the intent was recorded but the send was never begun (a crash
  during policy/approval). Nothing left the machine: cleared on recovery and
  the turn replays cleanly.
* **inflight** — the send was begun and its outcome never resolved (process
  death or timeout mid-request). This is the case that must never be
  replayed: recovery resumes the turn with send tools withheld and the bot
  reports the ambiguity instead of retrying.
* **sent** — a durable receipt. Recovery completes the send (terminal reply:
  the whole turn) without rerunning it.
* **uncertain** — the handler survived but could not prove the send did or
  did not arrive (a timeout error string). Treated like inflight on recovery.

Key invariant, adopted verbatim from OpenClaw: **an uncertain send is
preserved as uncertain** — warn on next contact rather than creating a likely
duplicate.

Rows are keyed `{bot, turn, target, seq}` — all persisted identifiers (the
roster name, the inbox message id, the declared send target, the occurrence
index within the turn), never a pid or boot stamp, so keys are stable across
process restarts. Each row also stores a content `digest` (`args_digest`):
on replay a receipt binds to a send by *content*, never by call order — a
replayed model can reorder, drop, or reword its calls, and a positional
match would hand the wrong receipt back (skipping a send that never
happened, or duplicating one that did). A same-target call whose content
matches no receipt while unconsumed receipts exist is refused with
`ambiguous_bind_notice` — the invariant again: report, don't likely-
duplicate. The originating `session` id is stored as an informational
column only: it changes per agent boot and a recovered turn runs in a new one.

Storage is the `$HARNESS_HOME/state.sqlite` database (stdlib `sqlite3`, WAL)
that the state store will grow into; this table is its first tenant and
will fold into that ticket's schema/migration discipline when it lands.
Legacy turn-recovery methods remain best-effort. Task action methods are
strict: protected sends require a durable intent before execution, and a
failed outcome write leaves the intent inflight rather than permitting a retry.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass

from .paths import HarnessPaths

STATUS_PENDING = "pending"
STATUS_INFLIGHT = "inflight"
STATUS_SENT = "sent"
STATUS_UNCERTAIN = "uncertain"

#: `govern` intents that are external sends and ride the ledger mid-turn.
SEND_INTENTS = frozenset({"message", "write_tool"})

#: outcome of classify_failure: proof the request never left the machine.
FAILURE_UNSENT = "unsent"
#: outcome of classify_failure: the send may or may not have arrived.
FAILURE_UNCERTAIN = "uncertain"

#: stored receipt detail is capped; it is a convenience echo, not the record
#: of the send itself (the recipient's inbox / the service holds that).
DETAIL_CAP = 4000

# Completed, inactive task receipts can be swept after seven days. Unknown
# outcomes never expire automatically: age does not prove a send failed.
RECEIPT_TTL = 7 * 24 * 60 * 60


class DeliveryUnavailable(RuntimeError):
    """Protected delivery state could not be read or durably written."""


#: Error-text markers proving a send never left: connect/DNS-stage failures
#: (the socket never carried the request) and this codebase's own pre-flight
#: validation shapes (missing key, bad arguments, unknown tool). Anything
#: not provably unsent is preserved as uncertain — that direction is the
#: invariant; the cost of a false "uncertain" is one warning, the cost of a
#: false "unsent" is a duplicate the recipient sees.
_UNSENT_MARKERS = (
    "connection refused",
    "name or service not known",
    "nodename nor servname",
    "temporary failure in name resolution",
    "getaddrinfo",
    "no route to host",
    "network is unreachable",
    "has no api key",
    "unknown tool",
    "needs '",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_intents (
    bot         TEXT NOT NULL,
    turn        TEXT NOT NULL,
    target      TEXT NOT NULL,
    seq         INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    digest      TEXT NOT NULL DEFAULT '',
    session     TEXT NOT NULL DEFAULT '',
    created_ts  REAL NOT NULL,
    resolved_ts REAL,
    PRIMARY KEY (bot, turn, target, seq)
)
"""


def chat_target(recipient: str | None) -> str:
    """Ledger target of a turn's terminal chat reply."""
    return f"chat:{(recipient or '').strip() or 'user'}"


def send_target(intent: str, tool: str, target: str) -> str:
    """Ledger target of a mid-turn send, from the gate's classification.

    A `message` intent names the peer (stable across replays given the same
    arguments); a connector write's classified target is the tool name itself
    (`agent/govern.py` explains why), so the prefix carries the kind.
    """
    if intent == "message":
        return f"peer:{target or tool}"
    return f"tool:{tool}"


def args_digest(tool: str, args: dict) -> str:
    """Content identity of one send: sha256 over the tool name and canonical
    JSON of its arguments. A receipt binds to the send's *content*, never to
    its position in the tool stream — a replayed turn can reorder, drop, or
    reword calls, and a positional match would hand the wrong receipt back."""
    if tool == "message_agent" and isinstance(args, dict):
        # Waiting changes collection, not what the colleague receives. Keep
        # old receipts valid and dedupe the same send across both modes.
        args = {k: v for k, v in args.items() if k != "wait"}
    try:
        canonical = json.dumps(
            {"tool": tool, "args": args if isinstance(args, dict) else {}},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        canonical = f"{tool}:{args!r}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def classify_failure(result: str, *, tool: str = "") -> str:
    """FAILURE_UNSENT when the error text proves nothing left the machine,
    else FAILURE_UNCERTAIN. Timeouts and connection resets are uncertain on
    purpose: the request may have been applied before the answer was lost."""
    text = (result or "").lower()
    # message_agent emits this only before messaging.send: a missing/ambiguous
    # recipient is a definite non-send, not an unknown external outcome.
    if tool == "message_agent" and text.startswith("error: message_agent not sent: "):
        return FAILURE_UNSENT
    if any(marker in text for marker in _UNSENT_MARKERS):
        return FAILURE_UNSENT
    return FAILURE_UNCERTAIN


def dedup_notice(target: str, row: dict) -> str:
    """Tool result standing in for a send a previous attempt already made."""
    echo = (row.get("detail") or "").strip()
    tail = f" Its result was: {echo}" if echo else ""
    return f"(already delivered: this task already sent {target}, so it was not sent again.{tail})"


def uncertain_notice(target: str, row: dict | None = None) -> str:
    """An unknown outcome is a hold, including within the current turn."""
    detail = str((row or {}).get("detail") or "").strip()
    tail = f" Recorded result: {detail}" if detail else ""
    return (
        f"error: delivery to {target} is uncertain or still in flight. "
        "Do not send it again. Check the destination for the existing action "
        f"and report what can be confirmed.{tail}"
    )


def ambiguous_bind_notice(target: str, row: dict) -> str:
    """Refusal for a same-target send whose content differs from the receipt.

    A previous attempt confirmed a send to this target; this call is not
    provably that send (reworded, reordered, or genuinely new). Sending it
    risks a duplicate of the confirmed one, and silently binding it to the
    receipt would claim it delivered something it did not — so the honest
    move is neither: refuse, and have the bot report both facts.
    """
    echo = (row.get("detail") or "").strip()
    tail = f" The delivered send's recorded result: {echo}" if echo else ""
    return (
        f"error: not sent — a previous attempt of this request already "
        f"delivered a send to {target}, and this call's content differs from "
        "it, so it cannot be proven to be the same send. Do not retry it; "
        "tell the user what was already delivered and what you additionally "
        f"wanted to send, and let them decide.{tail}"
    )


@dataclass(frozen=True)
class RecoveryState:
    """What the ledger knows about a crashed turn, bucketed for recovery."""

    #: status of the terminal chat-reply row, or None if never recorded
    terminal_status: str | None
    #: stored echo of the delivered terminal reply (may be capped/empty)
    terminal_detail: str
    #: mid-turn targets whose outcome is unknown (inflight / uncertain)
    unresolved: tuple[str, ...]


def recovery_state(rows: list[dict], terminal: str) -> RecoveryState:
    """Bucket a turn's rows. `terminal` is chat_target(recipient)."""
    terminal_status: str | None = None
    terminal_detail = ""
    unresolved: list[str] = []
    for row in rows:
        target = str(row.get("target") or "")
        status = str(row.get("status") or "")
        if target == terminal:
            terminal_status = status
            terminal_detail = str(row.get("detail") or "")
        elif status in (STATUS_INFLIGHT, STATUS_UNCERTAIN):
            unresolved.append(target)
    return RecoveryState(
        terminal_status=terminal_status,
        terminal_detail=terminal_detail,
        unresolved=tuple(unresolved),
    )


class Ledger:
    """Delivery intents/receipts for one harness home.

    A connection per call (WAL, 5s busy timeout): volumes are a handful of
    rows per turn, and short-lived connections keep every bot process a
    well-behaved concurrent writer.
    """

    def __init__(self, paths: HarnessPaths) -> None:
        self.path = paths.state_db

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=5.0)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(_SCHEMA)
        except (sqlite3.Error, OSError):
            con.close()
            raise
        return con

    # -- strict task action lifecycle ------------------------------------
    @staticmethod
    def _action_row(con: sqlite3.Connection, bot: str, task: str, target: str, digest: str):
        # Older versions could create several rows for the same action. An
        # unresolved attempt takes precedence even if another copy succeeded.
        return con.execute(
            "SELECT * FROM delivery_intents WHERE bot = ? AND turn = ? "
            "AND target = ? AND digest = ? ORDER BY "
            "CASE status WHEN 'uncertain' THEN 0 WHEN 'inflight' THEN 0 "
            "WHEN 'sent' THEN 1 ELSE 2 END, seq DESC LIMIT 1",
            (bot, task, target, digest),
        ).fetchone()

    def action_state(self, bot: str, task: str, target: str, *, digest: str) -> dict | None:
        """Read the logical action across retries and provider call IDs.

        Task identity is assigned by the harness. A deliberate new task may
        repeat the same payload; a model retry inside this task may not.
        Unlike legacy recovery reads, a broken store is never an empty one.
        """
        try:
            with contextlib.closing(self._connect()) as con:
                row = self._action_row(con, bot, task, target, digest)
                return dict(row) if row is not None else None
        except (sqlite3.Error, OSError) as exc:
            raise DeliveryUnavailable("delivery state could not be read") from exc

    def unresolved_actions(self, bot: str, task: str) -> list[dict]:
        """Unknown outcomes hold the whole task, including resumed attempts."""
        try:
            with contextlib.closing(self._connect()) as con:
                return [
                    dict(row)
                    for row in con.execute(
                        "SELECT * FROM delivery_intents WHERE bot = ? AND turn = ? "
                        "AND status IN ('inflight', 'uncertain') ORDER BY created_ts, target, seq",
                        (bot, task),
                    )
                ]
        except (sqlite3.Error, OSError) as exc:
            raise DeliveryUnavailable("unresolved delivery state could not be read") from exc

    def legacy_action(
        self,
        bot: str,
        turn: str,
        target: str,
        digest: str,
        consumed: set[tuple[str, int]],
        *,
        task_id: str = "",
    ) -> tuple[dict | None, dict | None]:
        """Bridge old turn receipts once: return (exact, ambiguous).

        Older versions replayed entire turns and consumed receipts in call
        order only after a content match. Preserve that conservative mismatch
        hold during upgrades. Exact receipts are also copied to the new task
        identity so consuming a legacy row cannot reopen a same-turn duplicate.
        """
        try:
            with contextlib.closing(self._connect()) as con, con:
                con.execute("BEGIN IMMEDIATE")
                rows = [
                    dict(row)
                    for row in con.execute(
                        "SELECT * FROM delivery_intents WHERE bot = ? AND turn = ? "
                        "AND target = ? AND status = 'sent' ORDER BY seq",
                        (bot, turn, target),
                    )
                    if (target, int(row["seq"])) not in consumed
                ]
                exact = next((row for row in rows if row["digest"] == digest), None)
                ambiguous = rows[0] if rows and exact is None else None
                if exact and task_id and task_id != turn:
                    existing = self._action_row(con, bot, task_id, target, digest)
                    if existing is None:
                        seq = con.execute(
                            "SELECT COALESCE(MAX(seq), -1) + 1 FROM delivery_intents "
                            "WHERE bot = ? AND turn = ? AND target = ?",
                            (bot, task_id, target),
                        ).fetchone()[0]
                        con.execute(
                            "INSERT INTO delivery_intents "
                            "(bot, turn, target, seq, status, detail, digest, session, "
                            "created_ts, resolved_ts) VALUES (?, ?, ?, ?, 'sent', ?, ?, ?, ?, ?)",
                            (
                                bot,
                                task_id,
                                target,
                                seq,
                                exact["detail"],
                                digest,
                                exact["session"],
                                exact["created_ts"],
                                exact["resolved_ts"],
                            ),
                        )
                    elif existing["status"] == STATUS_PENDING:
                        con.execute(
                            "UPDATE delivery_intents SET status = 'sent', detail = ?, "
                            "resolved_ts = ? WHERE bot = ? AND turn = ? AND target = ? AND seq = ?",
                            (
                                exact["detail"],
                                exact["resolved_ts"],
                                bot,
                                task_id,
                                target,
                                existing["seq"],
                            ),
                        )
            # Only a matched receipt is consumed. Forgetting an ambiguous
            # receipt after one refusal would let the next identical retry
            # bypass the hold without new user instructions.
            if exact:
                consumed.add((target, int(exact["seq"])))
            return exact, ambiguous
        except (sqlite3.Error, OSError) as exc:
            raise DeliveryUnavailable("legacy delivery receipt could not be recovered") from exc

    def prepare_action(
        self,
        bot: str,
        task: str,
        target: str,
        *,
        digest: str,
        session: str = "",
    ) -> dict:
        """Atomically reuse or persist a logical action before its gate.

        Preparing does not claim execution. The gate calls start_action only
        after policy and any required approval have allowed the actual call.
        """
        if not task or not digest:
            raise DeliveryUnavailable("delivery requires a task and exact action identity")
        try:
            with contextlib.closing(self._connect()) as con, con:
                con.execute("BEGIN IMMEDIATE")
                row = self._action_row(con, bot, task, target, digest)
                if row is None:
                    seq = con.execute(
                        "SELECT COALESCE(MAX(seq), -1) + 1 FROM delivery_intents "
                        "WHERE bot = ? AND turn = ? AND target = ?",
                        (bot, task, target),
                    ).fetchone()[0]
                    con.execute(
                        "INSERT INTO delivery_intents "
                        "(bot, turn, target, seq, status, detail, digest, session, created_ts) "
                        "VALUES (?, ?, ?, ?, 'pending', '', ?, ?, ?)",
                        (bot, task, target, seq, digest, session, time.time()),
                    )
                    row = self._action_row(con, bot, task, target, digest)
                return dict(row)
        except (sqlite3.Error, OSError) as exc:
            raise DeliveryUnavailable("delivery intent could not be saved") from exc

    def start_action(self, bot: str, task: str, target: str, seq: int = 0, *, digest: str) -> bool:
        """Claim a pending action once, immediately before its handler.

        False means another execution already claimed or settled the action.
        A missing/corrupt record never creates a fresh inflight action here.
        """
        try:
            with contextlib.closing(self._connect()) as con, con:
                con.execute("BEGIN IMMEDIATE")
                if con.execute(
                    "SELECT 1 FROM delivery_intents WHERE bot = ? AND turn = ? "
                    "AND status IN ('inflight', 'uncertain') LIMIT 1",
                    (bot, task),
                ).fetchone():
                    return False
                changed = con.execute(
                    "UPDATE delivery_intents SET status = 'inflight', resolved_ts = NULL "
                    "WHERE bot = ? AND turn = ? AND target = ? AND seq = ? "
                    "AND digest = ? AND status = 'pending'",
                    (bot, task, target, seq, digest),
                ).rowcount
                return changed == 1
        except (sqlite3.Error, OSError) as exc:
            raise DeliveryUnavailable("delivery execution claim could not be saved") from exc

    def finish_action(
        self,
        bot: str,
        task: str,
        target: str,
        seq: int = 0,
        *,
        digest: str,
        outcome: str,
        detail: str = "",
    ) -> None:
        """Persist sent/uncertain, or make a definitely-unsent action retryable.

        If saving fails the prior inflight row remains, so a retry cannot
        interpret a lost receipt as proof the action never happened.
        """
        if outcome not in {STATUS_SENT, STATUS_UNCERTAIN, FAILURE_UNSENT}:
            raise ValueError("delivery outcome must be sent, uncertain, or unsent")
        status = STATUS_PENDING if outcome == FAILURE_UNSENT else outcome
        try:
            with contextlib.closing(self._connect()) as con, con:
                changed = con.execute(
                    "UPDATE delivery_intents SET status = ?, detail = ?, resolved_ts = ? "
                    "WHERE bot = ? AND turn = ? AND target = ? AND seq = ? "
                    "AND digest = ? AND status = 'inflight'",
                    (
                        status,
                        (detail or "")[:DETAIL_CAP],
                        None if status == STATUS_PENDING else time.time(),
                        bot,
                        task,
                        target,
                        seq,
                        digest,
                    ),
                ).rowcount
                if changed != 1:
                    raise DeliveryUnavailable("delivery action was not an inflight execution")
        except (sqlite3.Error, OSError) as exc:
            raise DeliveryUnavailable("delivery outcome could not be saved") from exc

    def prune_expired(
        self, *, active_tasks: set[tuple[str, str]], retention_seconds: float = RECEIPT_TTL
    ) -> int:
        """Expire confirmed receipts and unstarted intents for inactive tasks.

        Active task receipts and unknown outcomes survive regardless of age.
        The caller supplies the active task identities from durable task state.
        """
        try:
            with contextlib.closing(self._connect()) as con, con:
                con.execute("BEGIN IMMEDIATE")
                cutoff = time.time() - retention_seconds
                rows = con.execute(
                    "SELECT bot, turn, target, seq FROM delivery_intents "
                    "WHERE (status = 'sent' AND resolved_ts < ?) "
                    "OR (status = 'pending' AND created_ts < ?)",
                    (cutoff, cutoff),
                ).fetchall()
                keys = [
                    tuple(row)
                    for row in rows
                    if (str(row["bot"]), str(row["turn"])) not in active_tasks
                ]
                con.executemany(
                    "DELETE FROM delivery_intents WHERE bot = ? AND turn = ? "
                    "AND target = ? AND seq = ? AND status IN ('sent', 'pending')",
                    keys,
                )
                return len(keys)
        except (sqlite3.Error, OSError) as exc:
            raise DeliveryUnavailable("expired delivery receipts could not be pruned") from exc

    # -- the intent lifecycle ---------------------------------------------
    def begin(
        self,
        bot: str,
        turn: str,
        target: str,
        seq: int = 0,
        *,
        session: str = "",
        digest: str = "",
    ) -> None:
        """Record an unresolved intent before anything is sent (pending).

        Never clobbers a resolved or inflight row: a key collision (a
        degraded read handed out a stale seq) must not erase what a previous
        attempt knew — only a still-pending row is refreshed.
        """
        self._write(
            "INSERT INTO delivery_intents "
            "(bot, turn, target, seq, status, detail, digest, session, created_ts) "
            "VALUES (?, ?, ?, ?, ?, '', ?, ?, ?) "
            "ON CONFLICT(bot, turn, target, seq) DO UPDATE SET "
            "digest = excluded.digest, session = excluded.session, "
            "created_ts = excluded.created_ts "
            "WHERE delivery_intents.status = 'pending'",
            (bot, turn, target, seq, STATUS_PENDING, digest, session, time.time()),
        )

    def inflight(self, bot: str, turn: str, target: str, seq: int = 0, *, digest: str = "") -> None:
        """The send is about to leave; from here a crash is an unknown outcome.
        Upserts for the same reason `_resolve` does: a lost begin() must not
        turn a real send into an unrecorded one — and it must not lose the
        content digest either, or replay could never exact-match the row.
        A caller passing no digest never erases a stored one."""
        now = time.time()
        self._write(
            "INSERT INTO delivery_intents "
            "(bot, turn, target, seq, status, detail, digest, session, created_ts) "
            "VALUES (?, ?, ?, ?, ?, '', ?, '', ?) "
            "ON CONFLICT(bot, turn, target, seq) DO UPDATE SET "
            "status = excluded.status, "
            "digest = CASE WHEN excluded.digest != '' THEN excluded.digest "
            "ELSE delivery_intents.digest END "
            "WHERE delivery_intents.status IN ('pending', 'inflight')",
            (bot, turn, target, seq, STATUS_INFLIGHT, digest, now),
        )

    def receipt(
        self, bot: str, turn: str, target: str, seq: int = 0, *, detail: str = "", digest: str = ""
    ) -> None:
        """Durable proof of a confirmed send."""
        self._resolve(bot, turn, target, seq, STATUS_SENT, detail, digest)

    def uncertain(
        self, bot: str, turn: str, target: str, seq: int = 0, *, detail: str = "", digest: str = ""
    ) -> None:
        """The handler survived but the outcome is unknowable (kept, on purpose)."""
        self._resolve(bot, turn, target, seq, STATUS_UNCERTAIN, detail, digest)

    def clear(self, bot: str, turn: str, target: str, seq: int = 0) -> None:
        """Confirmed failure with proof nothing was sent: replay is clean."""
        self._write(
            "DELETE FROM delivery_intents WHERE bot = ? AND turn = ? AND target = ? AND seq = ?",
            (bot, turn, target, seq),
        )

    def _resolve(
        self, bot: str, turn: str, target: str, seq: int, status: str, detail: str, digest: str = ""
    ) -> None:
        # Upsert on purpose: a receipt must stick even if the matching begin()
        # was swallowed by a transient storage error — losing a receipt is a
        # replayed send, the exact thing this ledger exists to prevent. The
        # digest rides too (a receipt without one can never exact-match on
        # replay); an empty caller digest never erases a stored one.
        now = time.time()
        self._write(
            "INSERT INTO delivery_intents "
            "(bot, turn, target, seq, status, detail, digest, session, created_ts, resolved_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?) "
            "ON CONFLICT(bot, turn, target, seq) DO UPDATE SET "
            "status = excluded.status, detail = excluded.detail, "
            "digest = CASE WHEN excluded.digest != '' THEN excluded.digest "
            "ELSE delivery_intents.digest END, "
            "resolved_ts = excluded.resolved_ts",
            (bot, turn, target, seq, status, (detail or "")[:DETAIL_CAP], digest, now, now),
        )

    # -- recovery reads ----------------------------------------------------
    def lookup(self, bot: str, turn: str, target: str, seq: int = 0) -> dict | None:
        rows = self._read(
            "SELECT * FROM delivery_intents WHERE bot = ? AND turn = ? AND target = ? AND seq = ?",
            (bot, turn, target, seq),
        )
        return rows[0] if rows else None

    def turn_rows(self, bot: str, turn: str) -> list[dict]:
        return self._read(
            "SELECT * FROM delivery_intents WHERE bot = ? AND turn = ? "
            "ORDER BY created_ts, target, seq",
            (bot, turn),
        )

    def receipts_for(self, bot: str, turn: str, target: str) -> list[dict]:
        """Confirmed (sent) rows for one target of one turn, oldest first."""
        return self._read(
            "SELECT * FROM delivery_intents "
            "WHERE bot = ? AND turn = ? AND target = ? AND status = ? ORDER BY seq",
            (bot, turn, target, STATUS_SENT),
        )

    def next_seq(self, bot: str, turn: str, target: str) -> int:
        """First unused occurrence index for a fresh send to this target —
        computed over ALL existing rows (any status, any attempt) so a new
        intent never lands on a key a previous attempt already used."""
        rows = self._read(
            "SELECT MAX(seq) AS top FROM delivery_intents "
            "WHERE bot = ? AND turn = ? AND target = ?",
            (bot, turn, target),
        )
        top = rows[0].get("top") if rows else None
        return 0 if top is None else int(top) + 1

    def clear_pending(self, bot: str, turn: str) -> None:
        """Drop pre-send intents from a crashed attempt so the replay is clean."""
        self._write(
            "DELETE FROM delivery_intents WHERE bot = ? AND turn = ? AND status = ?",
            (bot, turn, STATUS_PENDING),
        )

    def prune_turn(self, bot: str, turn: str) -> None:
        """The turn is settled (processed/dropped): its rows can never be
        consulted again — the inbox message they guard is gone."""
        self._write(
            "DELETE FROM delivery_intents WHERE bot = ? AND turn = ?",
            (bot, turn),
        )

    # -- plumbing (bookkeeping never fails a turn) -------------------------
    def _write(self, sql: str, params: tuple) -> None:
        try:
            with contextlib.closing(self._connect()) as con, con:
                con.execute(sql, params)
        except (sqlite3.Error, OSError):
            pass

    def _read(self, sql: str, params: tuple) -> list[dict]:
        try:
            with contextlib.closing(self._connect()) as con:
                return [dict(row) for row in con.execute(sql, params)]
        except (sqlite3.Error, OSError):
            return []
