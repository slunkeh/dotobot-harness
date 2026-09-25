"""Per-bot scheduled routines, stored as JSON on the volume.

Recurring jobs are 5-field cron. One-shots store `once_at` (unix time), fire
once, then disable. A bot creates them in chat; the harness ticks due jobs
and drops a user message in that bot's inbox so the existing loop picks
them up — app open or not.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent import messaging
from harness.paths import HarnessPaths

_TIME = re.compile(
    r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$",
    re.IGNORECASE,
)
_CRON_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_DELAY = re.compile(
    r"""
    ^\s*in\s+
    (?:
        an?\s+(?P<an_unit>hours?|hrs?)
        |
        (?P<n>\d+(?:\.\d+)?)\s*(?P<unit>hours?|hrs?|h|minutes?|mins?|min|m)
    )
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)
_ISO = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")
_ONCE_CLOCK = re.compile(
    r"^\s*(?:once(?:\s+at)?|today(?:\s+at)?|at)\s+(.+?)\s*$",
    re.IGNORECASE,
)


class RoutineError(ValueError):
    """Bad routine payload or unknown id."""


DEFAULT_MAX_LATENESS_SECONDS = 3600


def _missed_run_settings(row: dict[str, Any]) -> tuple[str, int]:
    """Old recurring rows acquire a bounded grace; one-shots still run late."""
    policy = row.get("missed_run_policy") or ("run_late" if _once_at(row) is not None else "skip")
    grace = row.get("max_lateness_seconds", DEFAULT_MAX_LATENESS_SECONDS)
    if policy not in {"skip", "run_late"}:
        raise RoutineError("missed_run_policy must be skip or run_late")
    if isinstance(grace, bool) or not isinstance(grace, int) or not 0 <= grace <= 31_536_000:
        raise RoutineError("max_lateness_seconds must be an integer from 0 to 31536000")
    return policy, grace


def routine_revision(row: dict[str, Any]) -> str:
    """Configuration identity; queue history never changes an authorization scope."""
    config = {
        key: row.get(key)
        for key in (
            "id",
            "prompt",
            "cron",
            "once_at",
            "timezone",
            "task_scope",
            "missed_run_policy",
            "max_lateness_seconds",
        )
    }
    return hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def occurrence_expired(occurrence: dict | None, *, now: float | None = None) -> bool:
    if not occurrence or occurrence.get("expires_at") is None:
        return False
    return (time.time() if now is None else now) > float(occurrence["expires_at"])


def occurrence_stop_reason(paths: HarnessPaths, bot: str, occurrence: dict | None) -> str:
    if not occurrence:
        return ""
    if occurrence_expired(occurrence):
        return "expired"
    row = next(
        (row for row in list_routines(paths, bot) if row.get("id") == occurrence.get("id")), None
    )
    if row is None or routine_revision(row) != occurrence.get("revision"):
        return "cancelled"
    if not row.get("enabled", True) and occurrence.get("kind") not in {"once", "test"}:
        return "cancelled"
    return ""


def occurrence_context(occurrence: dict | None) -> str:
    if not occurrence:
        return ""
    scheduled = (
        datetime.fromisoformat(occurrence["scheduled_local"])
        if occurrence.get("scheduled_local")
        else datetime.fromtimestamp(float(occurrence["scheduled_at"])).astimezone()
    )
    zone = str(occurrence.get("timezone") or "")
    if zone:
        scheduled = scheduled.astimezone(ZoneInfo(zone))
    return (
        "[Scheduled occurrence recorded by the harness: "
        f"{scheduled.isoformat()}, timezone {zone or str(scheduled.tzinfo)}; "
        f"run {occurrence['run_id']}. This is the original due time, not the time "
        "the queue delivered it. Do not describe delayed work as an early future run.]"
    )


def record_run_state(paths: HarnessPaths, bot: str, occurrence: dict | None, state: str) -> None:
    """Update this occurrence, without overwriting the status of a newer run."""
    if not occurrence:
        return
    with _lock(paths, bot):
        rows = list_routines(paths, bot)
        row = next((row for row in rows if row.get("id") == occurrence.get("id")), None)
        if row is None:
            return  # deleted jobs stay deleted
        history = row.setdefault("history", [])
        entry = next(
            (entry for entry in history if entry.get("run_id") == occurrence["run_id"]), None
        )
        if entry is None:
            entry = {"run_id": occurrence["run_id"], "scheduled_at": occurrence["scheduled_at"]}
            history.append(entry)
        entry.update(status=state, updated_at=time.time())
        row["history"] = history[-20:]
        if row.get("last_run_id") == occurrence["run_id"]:
            row["last_run_status"] = state
        _save(paths, bot, rows)


def _timezone(value: Any) -> str:
    name = str(value or "").strip()
    if name:
        try:
            ZoneInfo(name)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise RoutineError(f"unknown timezone {name!r}") from exc
    return name


def _schedule_now(row: dict[str, Any], now: datetime) -> datetime:
    name = _timezone(row.get("timezone")) if _once_at(row) is None else ""
    return now.astimezone(ZoneInfo(name)) if name else now


def parse_schedule(raw: str) -> str:
    """Turn '8am' / '8:00' / '0 8 * * *' into a 5-field cron in local time."""
    text = (raw or "").strip()
    if not text:
        raise RoutineError("routine needs a time")
    if len(text.split()) == 5:
        _cron_fields(text)
        return " ".join(text.split())
    clock = _clock_parts(text)
    if clock is None:
        raise RoutineError(f"could not parse time {raw!r}")
    hour, minute = clock
    return f"{minute} {hour} * * *"


def parse_when(raw: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Delay / once / clock / cron → `{"once_at": unix}` or `{"cron": "..."}`.

    Bare `8am` stays daily cron. `in an hour`, `in 20 minutes`, `once at 17:30`,
    `at 5pm`, and `YYYY-MM-DD HH:MM` are one-shots in host local time.
    """
    text = (raw or "").strip()
    if not text:
        raise RoutineError("routine needs a time")
    now = now or datetime.now()
    delay = _parse_delay(text, now)
    if delay is not None:
        return {"once_at": delay}
    iso = _parse_iso(text)
    if iso is not None:
        return {"once_at": iso}
    once_clock = _parse_once_clock(text, now)
    if once_clock is not None:
        return {"once_at": once_clock}
    return {"cron": parse_schedule(text)}


def _clock_parts(raw: str) -> tuple[int, int] | None:
    match = _TIME.match(raw or "")
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    ampm = (match.group(3) or "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _parse_delay(text: str, now: datetime) -> float | None:
    match = _DELAY.match(text)
    if not match:
        return None
    if match.group("an_unit"):
        seconds = 3600.0
    else:
        n = float(match.group("n") or 0)
        unit = (match.group("unit") or "").lower()
        if unit.startswith("h"):
            seconds = n * 3600.0
        else:
            seconds = n * 60.0
    if seconds < 60:
        raise RoutineError("one-shot delay must be at least a minute")
    return now.timestamp() + seconds


def _parse_iso(text: str) -> float | None:
    match = _ISO.match(text)
    if not match:
        return None
    year, month, day = (int(p) for p in match.group(1).split("-"))
    hour = int(match.group(2))
    minute = int(match.group(3))
    second = int(match.group(4) or 0)
    try:
        when = datetime(year, month, day, hour, minute, second)
    except ValueError as exc:
        raise RoutineError(f"could not parse time {text!r}") from exc
    return when.timestamp()


def _parse_once_clock(text: str, now: datetime) -> float | None:
    match = _ONCE_CLOCK.match(text)
    if not match:
        return None
    parts = _clock_parts(match.group(1))
    if parts is None:
        raise RoutineError(f"could not parse time {text!r}")
    hour, minute = parts
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when.timestamp() + 30 < now.timestamp():
        when = when + timedelta(days=1)
    return when.timestamp()


def describe_once(ts: float, now: datetime | None = None) -> str:
    when = datetime.fromtimestamp(float(ts))
    now = now or datetime.now()
    if when.date() == now.date():
        return f"Once today at {when.strftime('%H:%M')}"
    return f"Once at {when.strftime('%Y-%m-%d %H:%M')}"


def describe(cron: str) -> str:
    parts = (cron or "").split()
    if len(parts) != 5:
        return cron
    minute, hour, dom, month, dow = parts
    if dom == month == dow == "*":
        try:
            h, m = int(hour), int(minute)
        except ValueError:
            return cron
        return f"Every day at {h:02d}:{m:02d}"
    return cron


def cron_match(cron: str, now: datetime) -> bool:
    try:
        minute, hour, dom, month, dow = _cron_fields(cron)
    except RoutineError:
        # Invalid legacy/manual rows cannot break the scheduler's whole tick.
        return False
    # cron DOW: 0/7 = Sunday. Python weekday(): Monday = 0.
    py_dow = (now.weekday() + 1) % 7
    day_matches = now.day in dom
    weekday_matches = py_dow in {day % 7 for day in dow}
    fields = cron.split()
    if fields[2] == "*" or fields[4] == "*":
        day_matches = day_matches and weekday_matches
    else:
        # Standard cron matches either day when both day fields are restricted.
        day_matches = day_matches or weekday_matches
    return now.minute in minute and now.hour in hour and now.month in month and day_matches


def _cron_fields(cron: str) -> list[set[int]]:
    """One parser for save-time validation and runtime matching.

    Support the existing numeric grammar: wildcard, list, and inclusive range.
    Steps, named weekdays/months and macros are rejected rather than saved idle.
    """
    fields = (cron or "").split()
    if len(fields) != 5:
        raise RoutineError("cron needs five fields: minute hour day month weekday")
    parsed = []
    for name, spec, (low, high) in zip(
        ("minute", "hour", "day", "month", "weekday"), fields, _CRON_BOUNDS, strict=True
    ):
        if spec == "*":
            parsed.append(set(range(low, high + 1)))
            continue
        values = set()
        for part in spec.split(","):
            match = re.fullmatch(r"([0-9]+)(?:-([0-9]+))?", part)
            try:
                start = int(match[1]) if match else -1
                end = int(match[2] or match[1]) if match else -1
            except ValueError:
                start = end = -1
            if not low <= start <= end <= high:
                raise RoutineError(
                    f"invalid cron {name} {spec!r}: use * or numbers, lists, or ascending "
                    f"ranges within {low}..{high}"
                )
            values.update(range(start, end + 1))
        parsed.append(values)
    return parsed


def _stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%d %H:%M")


@contextmanager
def _lock(paths: HarnessPaths, bot: str) -> Iterator[None]:
    # Lock a stable sidecar, not the JSON inode that _save replaces. Separate
    # opens serialize HTTP threads, the scheduler, and agent processes alike.
    path = paths.bot_routines(bot).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def list_routines(paths: HarnessPaths, bot: str) -> list[dict[str, Any]]:
    path = paths.bot_routines(bot)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except UnicodeError as exc:
        raise RoutineError(f"cannot read routines for {bot!r}: invalid UTF-8") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RoutineError(f"cannot read routines for {bot!r}: invalid JSON") from exc
    rows = data.get("routines") if isinstance(data, dict) else data
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]
        for row in rows
    ):
        raise RoutineError(f"cannot read routines for {bot!r}: invalid routine records")
    if len({row["id"] for row in rows}) != len(rows):
        raise RoutineError(f"cannot read routines for {bot!r}: duplicate routine ids")
    return rows


def _save(paths: HarnessPaths, bot: str, rows: list[dict[str, Any]]) -> None:
    path = paths.bot_routines(bot)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Never let another writer truncate an open staging file, including a
    # writer left running on an older version during a rolling update.
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"routines": rows}, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _once_at(row: dict[str, Any]) -> float | None:
    raw = row.get("once_at")
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _triggers(row: dict[str, Any]) -> list[dict[str, Any]]:
    raw = row.get("triggers")
    if isinstance(raw, list):
        out: list[dict[str, Any]] = []
        for i, t in enumerate(raw):
            if not isinstance(t, dict) or not t.get("type"):
                continue
            item = dict(t)
            item.setdefault("id", f"{item.get('type')}-{i}")
            out.append(item)
        if out:
            return out
    once = _once_at(row)
    if once is not None:
        return [{"id": "once", "type": "once", "at": once}]
    cron = str(row.get("cron") or "").strip()
    if cron:
        return [{"id": "schedule", "type": "schedule", "cron": cron}]
    return []


def public(row: dict[str, Any]) -> dict[str, Any]:
    cron = str(row.get("cron") or "")
    history = row.get("history") if isinstance(row.get("history"), list) else []
    once = _once_at(row)
    policy, grace = _missed_run_settings(row)
    if once is not None:
        schedule = describe_once(once)
    elif cron:
        schedule = describe(cron)
        if row.get("timezone"):
            schedule += f" ({row['timezone']})"
    else:
        schedule = ""
    return {
        "id": row.get("id"),
        "title": row.get("title") or "",
        "prompt": row.get("prompt") or "",
        "cron": cron,
        "timezone": row.get("timezone") or "",
        "once_at": once,
        "once_fired": bool(row.get("once_fired")),
        "schedule": schedule,
        "enabled": bool(row.get("enabled", True)),
        "last_run": row.get("last_run"),
        "last_run_id": row.get("last_run_id"),
        "last_run_status": row.get("last_run_status", "unknown"),
        "missed_run_policy": policy,
        "max_lateness_seconds": grace,
        "triggers": _triggers(row),
        "history": history[-20:],
    }


def add_routine(
    paths: HarnessPaths,
    bot: str,
    *,
    title: str = "",
    prompt: str = "",
    when: str = "",
    enabled: bool | None = None,
    once_at: float | None = None,
    timezone: str = "",
    missed_run_policy: str | None = None,
    max_lateness_seconds: int = DEFAULT_MAX_LATENESS_SECONDS,
) -> dict[str, Any]:
    with _lock(paths, bot):
        timezone = _timezone(timezone)
        title = (title or "").strip()
        prompt = (prompt or "").strip()
        when = (when or "").strip()
        cron = ""
        if once_at not in (None, ""):
            try:
                once_at = float(once_at)
            except (TypeError, ValueError) as exc:
                raise RoutineError(f"could not parse once_at {once_at!r}") from exc
        else:
            once_at = None
            if when:
                parsed = parse_when(when)
                once_at = parsed.get("once_at")
                cron = str(parsed.get("cron") or "")
        if enabled is None:
            enabled = once_at is not None
        if timezone and once_at is not None:
            raise RoutineError(
                "timezone applies to recurring schedules; use once_at for an absolute time"
            )
        triggers: list[dict[str, Any]] = []
        if once_at is not None:
            triggers.append({"id": uuid.uuid4().hex[:8], "type": "once", "at": once_at})
            cron = ""
        elif cron:
            triggers.append({"id": uuid.uuid4().hex[:8], "type": "schedule", "cron": cron})
        row = {
            "id": uuid.uuid4().hex[:12],
            "title": title,
            "prompt": prompt,
            "cron": cron,
            "once_at": once_at,
            "once_fired": False,
            "triggers": triggers,
            "enabled": bool(enabled),
            "last_run": None,
            "history": [],
            "created": time.time(),
            "timezone": timezone,
            "missed_run_policy": missed_run_policy,
            "max_lateness_seconds": max_lateness_seconds,
        }
        _missed_run_settings(row)
        rows = list_routines(paths, bot)
        rows.append(row)
        _save(paths, bot, rows)
        return public(row)


def update_routine(paths: HarnessPaths, bot: str, routine_id: str, **fields: Any) -> dict[str, Any]:
    with _lock(paths, bot):
        rows = list_routines(paths, bot)
        for row in rows:
            if row.get("id") != routine_id:
                continue
            if "timezone" in fields:
                timezone = _timezone(fields["timezone"])
                # last_run is a zone-less wall-clock stamp. A new zone must not
                # inherit it or the same clock time today is treated as already
                # fired (Europe/London 08:00 then America/New_York 08:00).
                if timezone != (row.get("timezone") or ""):
                    row["last_run"] = None
                row["timezone"] = timezone
            if "enabled" in fields and fields["enabled"] is not None:
                row["enabled"] = bool(fields["enabled"])
            if "title" in fields and fields["title"] is not None:
                row["title"] = str(fields["title"]).strip()
            if "prompt" in fields and fields["prompt"] is not None:
                row["prompt"] = str(fields["prompt"]).strip()
            for key in ("missed_run_policy", "max_lateness_seconds"):
                if key in fields:
                    row[key] = fields[key]
            when = fields.get("when") or fields.get("time") or fields.get("cron")
            if when:
                _apply_when(row, str(when))
            if "once_at" in fields and fields["once_at"] not in (None, ""):
                try:
                    _set_once(row, float(fields["once_at"]))
                except (TypeError, ValueError) as exc:
                    raise RoutineError(f"could not parse once_at {fields['once_at']!r}") from exc
            if "triggers" in fields and isinstance(fields["triggers"], list):
                row["triggers"] = [
                    t for t in fields["triggers"] if isinstance(t, dict) and t.get("type")
                ]
                _apply_triggers(row)
            if row.get("timezone") and _once_at(row) is not None:
                if fields.get("timezone"):
                    raise RoutineError(
                        "timezone applies to recurring schedules; use once_at for an absolute time"
                    )
                # A time-only edit may replace a zoned recurring schedule. Its
                # inherited timezone no longer applies to the absolute deadline.
                row["timezone"] = None
            _missed_run_settings(row)
            _save(paths, bot, rows)
            return public(row)
        raise RoutineError(f"no routine {routine_id!r}")


def remove_routine(paths: HarnessPaths, bot: str, routine_id: str) -> bool:
    with _lock(paths, bot):
        rows = list_routines(paths, bot)
        kept = [r for r in rows if r.get("id") != routine_id]
        if len(kept) == len(rows):
            return False
        _save(paths, bot, kept)
        return True


def bind_routine_scope(
    paths: HarnessPaths,
    bot: str,
    routine_id: str,
    *,
    conversation: str,
    task_id: str,
    revision: int,
) -> dict[str, Any]:
    """Bind a routine to a current human-derived task scope, never its prompt.

    Called with ToolContext metadata after the governed routine mutation.
    A stale task cannot overwrite a routine's bindings. Old routines stay
    unbound until a human explicitly names the connectors when updating them.
    """
    with _lock(paths, bot):
        from .taskscope import read_task

        scope = read_task(paths, bot, conversation)
        if (
            not scope
            or scope.get("task_id") != task_id
            or scope.get("revision") != revision
            or scope.get("status") != "active"
            or not scope.get("objective_source_id")
        ):
            raise RoutineError("routine connector scope no longer matches the current task")
        rows = list_routines(paths, bot)
        for row in rows:
            if row.get("id") == routine_id:
                row["task_scope"] = {
                    "connector_ids": list(scope.get("connector_ids") or []),
                    "provenance": list(scope.get("provenance") or []),
                    "source": {
                        "conversation": conversation,
                        "task_id": task_id,
                        "revision": revision,
                        "input_id": scope.get("input_id"),
                    },
                }
                _save(paths, bot, rows)
                return public(row)
        raise RoutineError(f"no routine {routine_id!r}")


def routine_scope_for_task(paths: HarnessPaths, bot: str, conversation: str,
                           task_id: str, revision: int) -> tuple[str, str] | None:
    """Verify scheduler-owned identity against the current task and routine."""
    from .taskscope import read_task

    task = read_task(paths, bot, conversation)
    if (not task or task.get("task_id") != task_id
            or task.get("revision") != revision
            or task.get("status") not in {"active", "waiting", "idle"}):
        return None
    rid, version = task.get("routine_id"), task.get("routine_revision")
    if not rid or not version:
        return None  # legacy prompt prose never migrates authority
    row = next((r for r in list_routines(paths, bot) if r.get("id") == rid), None)
    if row and row.get("enabled") and routine_revision(row) == version:
        return str(rid), str(version)
    return None


def _apply_when(row: dict[str, Any], when: str) -> None:
    parsed = parse_when(when)
    if parsed.get("once_at") is not None:
        _set_once(row, float(parsed["once_at"]))
        return
    cron = str(parsed.get("cron") or "")
    row["cron"] = cron
    row["once_at"] = None
    row["once_fired"] = False
    _ensure_schedule_trigger(row, cron)


def _set_once(row: dict[str, Any], ts: float) -> None:
    prev = _once_at(row)
    row["once_at"] = ts
    row["cron"] = ""
    if prev is None or abs(prev - ts) > 1:
        row["once_fired"] = False
    _ensure_once_trigger(row, ts)


def _apply_triggers(row: dict[str, Any]) -> None:
    triggers = row.get("triggers") if isinstance(row.get("triggers"), list) else []
    once = next((t for t in triggers if isinstance(t, dict) and t.get("type") == "once"), None)
    sched = next((t for t in triggers if isinstance(t, dict) and t.get("type") == "schedule"), None)
    if once is not None:
        at = None
        delay_text = str(once.get("time") or "").strip()
        existing = _once_at(row)
        # Inspector autosave re-sends "in 1 hour"; re-parsing would keep
        # pushing a live one-shot. Keep the stored deadline.
        if (
            delay_text
            and _DELAY.match(delay_text)
            and existing is not None
            and existing > time.time()
            and not row.get("once_fired")
        ):
            once["at"] = existing
            _set_once(row, existing)
            return
        if once.get("time"):
            parsed = parse_when(str(once["time"]))
            if parsed.get("once_at") is not None:
                at = float(parsed["once_at"])
            else:
                clock = _clock_parts(str(once["time"]))
                if clock:
                    hour, minute = clock
                    now = datetime.now()
                    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if when.timestamp() + 30 < now.timestamp():
                        when = when + timedelta(days=1)
                    at = when.timestamp()
        if at is None and once.get("at") not in (None, ""):
            try:
                at = float(once["at"])
            except (TypeError, ValueError) as exc:
                raise RoutineError(f"could not parse once_at {once['at']!r}") from exc
        if at is None:
            raise RoutineError("once trigger needs a time")
        once["at"] = at
        _set_once(row, at)
        return
    row["once_at"] = None
    if sched:
        cron = str(sched.get("cron") or "").strip()
        if cron:
            cron = parse_schedule(cron)
            sched["cron"] = cron
        if not cron and sched.get("time"):
            cron = parse_schedule(str(sched.get("time")))
            sched["cron"] = cron
        row["cron"] = cron
        if cron:
            _ensure_schedule_trigger(row, cron)
    else:
        row["cron"] = ""


def _ensure_schedule_trigger(row: dict[str, Any], cron: str) -> None:
    triggers = row.setdefault("triggers", [])
    if not isinstance(triggers, list):
        row["triggers"] = triggers = []
    for trig in triggers:
        if isinstance(trig, dict) and trig.get("type") == "schedule":
            trig["cron"] = cron
            return
    triggers.append({"id": uuid.uuid4().hex[:8], "type": "schedule", "cron": cron})


def _ensure_once_trigger(row: dict[str, Any], ts: float) -> None:
    triggers = row.setdefault("triggers", [])
    if not isinstance(triggers, list):
        row["triggers"] = triggers = []
    for trig in triggers:
        if isinstance(trig, dict) and trig.get("type") == "once":
            trig["at"] = ts
            return
    triggers.append({"id": uuid.uuid4().hex[:8], "type": "once", "at": ts})


def _enqueue(
    paths: HarnessPaths,
    bot: str,
    row: dict[str, Any],
    *,
    now: datetime,
    kind: str,
    send: Callable[..., None] | None,
) -> None:
    stamp = _stamp(_schedule_now(row, now))
    title = row.get("title") or "scheduled"
    prompt = (row.get("prompt") or "").strip()
    text = f"[Routine: {title}]\n{prompt}".strip()
    policy, grace = _missed_run_settings(row)
    scheduled_at = (
        float(_once_at(row))
        if kind == "once"
        else now.replace(second=0, microsecond=0).timestamp()
        if kind == "schedule"
        else now.timestamp()
    )
    run_id = (
        uuid.uuid4().hex
        if kind == "test"
        else uuid.uuid5(uuid.NAMESPACE_URL, f"routine:{bot}:{row['id']}:{scheduled_at}").hex
    )
    scheduled_local = datetime.fromtimestamp(scheduled_at).astimezone()
    if row.get("timezone"):
        scheduled_local = scheduled_local.astimezone(ZoneInfo(row["timezone"]))
    occurrence = {
        "id": row["id"],
        "run_id": run_id,
        "scheduled_at": scheduled_at,
        "timezone": row.get("timezone") or "",
        "prompt": text,
        "scheduled_local": scheduled_local.isoformat(),
        "kind": kind,
        "revision": routine_revision(row),
        "expires_at": scheduled_at + grace if policy == "skip" else None,
    }
    stored_scope = row.get("task_scope")
    scope = {
        **(stored_scope if isinstance(stored_scope, dict) else {}),
        "conversation": f"routine:{row['id']}:{run_id}",
        "routine_id": row["id"],
        "routine_revision": occurrence["revision"],
    }
    processed = (paths.processed(bot) / f"{scheduled_at:.6f}-{run_id}.json").exists()
    if send is not None:
        if not processed:
            # Server relay preserves scope before publishing its chosen input
            # id. A legacy callback cannot silently discard a bound scope.
            send(bot, text, task_scope=scope, routine=occurrence)
    else:
        # origin="routine" puts scheduled work in the background lane
        msg = messaging.Msg(
            to=bot,
            frm="user",
            text=text,
            origin="routine",
            id=run_id,
            ts=scheduled_at,
            routine=occurrence,
        )
        if not processed:
            from .taskscope import begin_task

            begin_task(
                paths,
                bot,
                scope["conversation"],
                text="",
                input_id=msg.id,
                trusted_user=False,
                inherited_scope=scope,
            )
            messaging.send(paths, msg)
    row["last_run"] = stamp
    row["last_run_id"] = run_id
    history = row.setdefault("history", [])
    if not isinstance(history, list):
        row["history"] = history = []
    previous = next((entry for entry in history if entry.get("run_id") == run_id), None)
    row["last_run_status"] = (
        (previous.get("status", "unknown") if previous else "unknown") if processed else "queued"
    )
    if previous is None:
        history.append(
            {
                "ts": stamp,
                "kind": kind,
                "run_id": run_id,
                "scheduled_at": scheduled_at,
                "status": row["last_run_status"],
            }
        )
    row["history"] = history[-20:]


def is_due(row: dict[str, Any], now: datetime) -> bool:
    if not row.get("enabled", True):
        return False
    if not (row.get("prompt") or "").strip():
        return False
    once = _once_at(row)
    if once is not None:
        if row.get("once_fired"):
            return False
        return now.timestamp() >= once
    cron = str(row.get("cron") or "")
    try:
        now = _schedule_now(row, now)
    except RoutineError:
        return False
    if not cron or not cron_match(cron, now):
        return False
    return row.get("last_run") != _stamp(now)


def fire_due(
    paths: HarnessPaths,
    bots: list[str],
    *,
    now: datetime | None = None,
    send: Callable[..., None] | None = None,
) -> list[dict[str, Any]]:
    """Enqueue a user message for each due routine. Returns the fired rows."""
    now = now or datetime.now()
    fired: list[dict[str, Any]] = []
    for bot in bots:
        with _lock(paths, bot):
            try:
                rows = list_routines(paths, bot)
            except RoutineError as exc:
                logging.getLogger(__name__).error("%s; leaving file untouched", exc)
                continue
            changed = False
            for row in rows:
                if not is_due(row, now):
                    continue
                once = _once_at(row) is not None
                _enqueue(
                    paths,
                    bot,
                    row,
                    now=now,
                    kind="once" if once else "schedule",
                    send=send,
                )
                if once:
                    row["once_fired"] = True
                    row["enabled"] = False
                changed = True
                fired.append(public(row))
            if changed:
                _save(paths, bot, rows)
    return fired


def scheduled_bots(roster: Any) -> list[str]:
    """Roster names the tick may fire for. A blocked bot (guideline 1.2) gets
    no routine turn until it is unblocked, exactly as it gets no chat turn;
    its routines stay stored and resume afterwards."""
    return [b.name for b in roster.bots if not getattr(b, "blocked", False)]


def start_scheduler(
    orch: Any,
    interval: float = 20.0,
    send: Callable[..., None] | None = None,
) -> threading.Thread:
    """Background tick used by `harness serve`. Daemon so process exit is clean."""

    def loop() -> None:
        while True:
            try:
                names = scheduled_bots(orch.roster)
                fire_due(orch.paths, names, send=send)
            except Exception:
                pass
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="routines", daemon=True)
    thread.start()
    return thread


def run_now(
    paths: HarnessPaths,
    bot: str,
    routine_id: str,
    *,
    send: Callable[..., None] | None = None,
) -> dict[str, Any]:
    with _lock(paths, bot):
        rows = list_routines(paths, bot)
        for row in rows:
            if row.get("id") != routine_id:
                continue
            if not (row.get("prompt") or "").strip():
                raise RoutineError("routine needs an instruction before it can run")
            _enqueue(paths, bot, row, now=datetime.now(), kind="test", send=send)
            _save(paths, bot, rows)
            return public(row)
        raise RoutineError(f"no routine {routine_id!r}")
