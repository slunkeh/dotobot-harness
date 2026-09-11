"""Delayed deletion of bot-owned state, never account/shared data.

The DELETE path writes a job and removes the roster entry before background
shutdown. MachinePool uses that job to reserve its disk, and the server
retries shutdown immediately and permanent cleanup after 24 hours. A
job is removed only after every step succeeds. Recreating a name during the
grace period cancels its job; creation and sweeping share the lifecycle lock.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

from .fsutil import pid_alive, write_private
from .roster import RosterError, load_roster, valid_bot_name

RETENTION_SECONDS = 24 * 60 * 60
SWEEP_SECONDS = 60
log = logging.getLogger(__name__)


def job_path(paths, bot: str) -> Path:
    if not valid_bot_name(bot):
        raise RosterError("invalid bot name")
    return paths.deleted_bots / f"{bot}.json"


def schedule(paths, bot: str, handle, *, now: float | None = None) -> dict:
    path = job_path(paths, bot)
    if path.exists():
        return json.loads(path.read_text())
    stamp = time.time() if now is None else now
    pid = handle.pid if handle else None
    identity = _process_identity(pid) if pid else None
    job = {
        "bot": bot,
        "deleted_at": stamp,
        "purge_after": stamp + RETENTION_SECONDS,
        "pid": pid,
        "identity": hashlib.sha256(identity.encode()).hexdigest() if identity else None,
    }
    write_private(path, json.dumps(job))
    return job


def _process_identity(pid: int) -> str | None:
    from isolation.process_identity import read_cmdline

    command = read_cmdline(pid)
    if command or sys.platform != "darwin":
        return command
    # Process-backend Mac hosts have no /proc. Include the start time so a
    # reused PID running the same command still cannot inherit a shutdown.
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart=,command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def cancel(paths, bot: str, *, check_only: bool = False) -> None:
    path = job_path(paths, bot)
    if path.exists():
        job = json.loads(path.read_text())
        if job.get("stopping") or job.get("purging"):
            raise RosterError("Bot cleanup is in progress; retry creation after cleanup completes")
    if not check_only:
        path.unlink(missing_ok=True)


def schedule_shutdown(orch, bot: str) -> dict:
    """Record shutdown without probing Docker or waiting for a process to exit."""
    from isolation import BotHandle

    run_file = orch.paths.run_file(bot)
    row = _record(run_file) if run_file.is_file() else {}
    handle = BotHandle(
        bot=bot, backend=row.get("backend", orch.backend_name), pid=row.get("pid"), meta=row
    )
    job = schedule(orch.paths, bot, handle)
    job.update(stopping=True, handle=asdict(handle))
    write_private(job_path(orch.paths, bot), json.dumps(job))
    return job


def _shutdown_locked(orch, bot: str, job: dict) -> None:
    from agent import messaging
    from isolation import BotHandle, Status, get_backend

    if not job.get("stopping"):
        return
    handle = BotHandle(**job["handle"])
    backend = (
        orch.backend
        if handle.backend == orch.backend_name
        else get_backend(handle.backend, orch.paths)
    )
    if handle.backend == "machines":
        backend.reserve_deleted(bot)
    if handle.pid and pid_alive(handle.pid):
        identity = _process_identity(handle.pid)
        if not identity or not job.get("identity"):
            raise RuntimeError("Cannot verify deleted agent identity; retry shutdown")
        if hashlib.sha256(identity.encode()).hexdigest() != job["identity"]:
            handle.pid = None  # recycled PID: never signal its new owner
    if handle.pid or handle.meta.get("machine") or handle.meta.get("container"):
        backend.stop(handle)
        if handle.pid and pid_alive(handle.pid):
            raise RuntimeError("Deleted agent has not stopped; retry shutdown")
        if machine := handle.meta.get("machine"):
            state = backend._container_state(machine)
            if state is None:
                from isolation import engine

                listing = engine.run("ps", "-a", "--format", "{{.Names}}")
                if listing.returncode != 0 or machine in listing.stdout.splitlines():
                    raise RuntimeError("Cannot verify deleted computer shutdown; retry")
            elif state not in ("exited", "dead"):
                raise RuntimeError("Deleted computer has not stopped; retry shutdown")
        if handle.backend == "container" and backend.status(handle) not in (
            Status.DEAD,
            Status.STOPPED,
        ):
            raise RuntimeError("Deleted container has not stopped; retry shutdown")
    orch._clear_startup_error(bot)
    for path, msg in messaging.read_inbox(orch.paths, bot):
        if msg.origin == messaging.ORIGIN_WELCOME and msg.frm == "user" and msg.reply_to is None:
            messaging.mark_processed(orch.paths, bot, path)
    job["stopping"] = False
    job.pop("handle", None)
    write_private(job_path(orch.paths, bot), json.dumps(job))


def shutdown(orch, bot: str) -> None:
    """Finish a durable shutdown once; the regular sweeper retries interruptions."""
    try:
        with orch._lifecycle_lock(bot):
            path = job_path(orch.paths, bot)
            if not path.exists() or bot in load_roster(orch.roster_path).names():
                return
            _shutdown_locked(orch, bot, json.loads(path.read_text()))
    except Exception:
        log.exception("Deleted bot shutdown deferred: %s", bot)


def start_shutdown(orch, bot: str) -> None:
    try:
        threading.Thread(
            target=shutdown, args=(orch, bot), daemon=True, name=f"delete-{bot}"
        ).start()
    except RuntimeError:
        # Deletion is already accepted; the durable sweeper will pick it up.
        log.exception("Could not start deleted bot shutdown; queued for retry: %s", bot)


def _remove(paths, path: Path) -> None:
    """Unlink leaf symlinks, refuse symlink parents, never follow them outside home."""
    relative = path.relative_to(paths.home)
    parent = paths.home
    for part in relative.parts[:-1]:
        parent = parent / part
        if parent.is_symlink():
            raise RuntimeError(f"Unsafe cleanup parent: {parent}")
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _record(path: Path) -> dict:
    try:
        row = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return row if isinstance(row, dict) else {}


def _purge_files(paths, bot: str) -> None:
    from .statestore import StateStore

    # Capture request and prompt IDs before removing their owning rows/files.
    requests: set[str] = set()
    if paths.state_db.exists():
        requests, prompts = StateStore(paths).cleanup_references(bot)
        for prompt in prompts:
            if valid_bot_name(prompt):
                _remove(paths, paths.answers / f"{prompt}.json")
    message_root = paths.messages / bot
    for path in message_root.glob("*/*.json"):
        if bot == "user":
            break  # the human inbox is shared even if a roster uses this name
        row = _record(path)
        requests.update(str(row[k]) for k in ("id", "reply_to") if row.get(k))
    for path in (paths.messages / "user").glob("*/*.json"):
        row = _record(path)
        if bot != "user" and row.get("frm") == bot and not row.get("room"):
            requests.update(str(row[k]) for k in ("id", "reply_to") if row.get(k))
            # Keep this ownership record until its stream is removed successfully.
            # The path is deleted after stream cleanup below.
    for root in (paths.prompts, paths.blocks_state):
        for path in root.glob("*.json"):
            row = _record(path)
            if row.get("bot") == bot:
                if row.get("request_id"):
                    requests.add(str(row["request_id"]))
                # Answers first, so a crash cannot orphan a secret response.
                _remove(paths, paths.answers / path.name)
                _remove(paths, path)
    for request in requests:
        if not valid_bot_name(request):
            continue
        # Room streams are shared and must survive deletion of a participant.
        path = paths.stream_file(request)
        shared = False
        if path.is_file():
            with path.open() as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                        shared = shared or (isinstance(event, dict) and bool(event.get("room")))
                    except json.JSONDecodeError:
                        continue  # a crash may leave a torn final event
        if not shared:
            _remove(paths, path)
    for path in (paths.messages / "user").glob("*/*.json"):
        row = _record(path)
        if bot != "user" and row.get("frm") == bot and not row.get("room"):
            _remove(paths, path)
    for path in (
        paths.bot_memory(bot),
        paths.bot_session(bot),
        *([message_root] if bot != "user" else []),
        paths.run / "bot-secrets" / bot,
        paths.home / "teach-sessions" / bot,
        paths.bot_routines(bot),
        paths.obligation_file(bot),
        paths.control_file(bot),
        paths.control_events(bot),
        paths.control / f"approvals-{bot}.json",
        paths.home / "notes" / f"{bot}.jsonl",
        paths.home / "dreams" / f"{bot}.json",
        paths.audit / f"{bot}.jsonl",
        *(
            paths.run / f"{bot}.{suffix}"
            for suffix in (
                "json",
                "log",
                "busy",
                "stop",
                "now",
                "computer-active",
                "startup-error",
            )
        ),
    ):
        if path == paths.machines_file():
            continue  # shared pool metadata, even if a bot is named "machines"
        _remove(paths, path)
    # Files first: a restart must not reimport legacy transcripts after SQL deletion.
    if paths.state_db.exists():
        StateStore(paths).delete_bot(bot)


def sweep(orch, *, now: float | None = None) -> list[str]:
    from isolation.machines import MachineBackend

    stamp = time.time() if now is None else now
    removed = []
    for path in sorted(orch.paths.deleted_bots.glob("*.json")):
        bot = path.stem
        try:
            with orch._lifecycle_lock(bot):
                if not path.exists():
                    continue
                job = json.loads(path.read_text())
                if job.get("bot") != bot:
                    continue
                # The disk roster catches another process recreating the bot.
                if bot in orch.roster.names() or bot in load_roster(orch.roster_path).names():
                    continue
                _shutdown_locked(orch, bot, job)
                if stamp < float(job["purge_after"]):
                    continue
                pid = job.get("pid")
                if pid and pid_alive(pid):
                    identity = _process_identity(pid)
                    digest = hashlib.sha256(identity.encode()).hexdigest() if identity else None
                    if not digest or not job.get("identity") or digest == job["identity"]:
                        raise RuntimeError("Deleted bot's agent is still running")
                job["purging"] = True
                write_private(path, json.dumps(job))
                # The pool, not a stored container name, is the ownership authority.
                MachineBackend(orch.paths).purge_deleted(bot)
                _purge_files(orch.paths, bot)
                path.unlink()
                removed.append(bot)
                log.info("Deleted bot cleanup completed: %s", bot)
        except Exception:
            log.exception("Deleted bot cleanup deferred: %s", bot)
    return removed


def start_sweeper(orch) -> None:
    """Catch up after downtime off the startup path; retry failures every minute."""

    def run():
        while True:
            try:
                sweep(orch)
            except Exception:
                log.exception("Deleted bot cleanup scan failed; will retry")
            time.sleep(SWEEP_SECONDS)

    threading.Thread(target=run, daemon=True, name="deleted-bot-cleanup").start()
