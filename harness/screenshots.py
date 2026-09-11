"""Screenshot staging store with a retention sweep.

Screenshots the harness produces land in one known temp folder,
``$HARNESS_HOME/tmp/screenshots``, instead of chat ``uploads/`` or a
short-lived public host. Files are eligible for purge once their mtime is
older than the retention window (``HARNESS_SCREENSHOT_TTL_HOURS``, default
72h); ``sweep()`` deletes them, and ``harness serve`` runs it on a timer
(``HARNESS_SCREENSHOT_SWEEP_INTERVAL`` seconds, default hourly, 0 disables).

Anything that sends a screenshot somewhere (chat, Linear, a public rehost
fallback) should read it from here via ``read()`` / ``touch()``: both refresh
the file's mtime, which restarts its retention clock, so an in-flight send
never races the sweeper. Public rehosts stay a delivery fallback — this
folder is the source of truth while the file lives.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from .paths import HarnessPaths

DEFAULT_TTL_HOURS = 72.0
TTL_ENV = "HARNESS_SCREENSHOT_TTL_HOURS"
DEFAULT_SWEEP_INTERVAL = 3600
SWEEP_INTERVAL_ENV = "HARNESS_SCREENSHOT_SWEEP_INTERVAL"


def ttl_seconds() -> float:
    """Retention window in seconds ($HARNESS_SCREENSHOT_TTL_HOURS, min 0)."""
    raw = os.environ.get(TTL_ENV, "")
    try:
        hours = float(raw) if raw else DEFAULT_TTL_HOURS
    except ValueError:
        hours = DEFAULT_TTL_HOURS
    return max(hours, 0.0) * 3600.0


def sweep_interval() -> int:
    """Seconds between sweeps for the serve timer; 0 disables the timer."""
    try:
        return int(os.environ.get(SWEEP_INTERVAL_ENV, str(DEFAULT_SWEEP_INTERVAL)))
    except ValueError:
        return DEFAULT_SWEEP_INTERVAL


class ScreenshotStore:
    """Stage, hand out, and expire screenshots under one temp directory."""

    def __init__(self, paths: HarnessPaths):
        self.root = paths.screenshots

    def stage(self, data: bytes, *, ext: str = "png", label: str = "") -> Path:
        """Write one screenshot atomically and return its path.

        Names sort chronologically (`20260824-104501-ab12cd34[-label].png`)
        so `ls` reads as a timeline and the sweeper needs no index file.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", label).strip("-")[:40]
        name = f"{stamp}-{uuid.uuid4().hex[:8]}" + (f"-{slug}" if slug else "")
        path = self.root / f"{name}.{ext.lstrip('.')}"
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path

    def touch(self, path: Path | str) -> None:
        """Restart a file's retention clock (call before a slow send)."""
        try:
            os.utime(path, None)
        except OSError:
            pass

    def read(self, path: Path | str) -> bytes:
        """Bytes of a staged screenshot; refreshes its mtime first so the
        sweeper cannot delete it mid-send."""
        self.touch(path)
        return Path(path).read_bytes()

    def sweep(self, now: float | None = None) -> int:
        """Delete staged screenshots older than the retention window.

        Only regular files directly in the store are touched; anything that
        vanishes concurrently is ignored. Returns how many were removed.
        """
        cutoff = (now if now is not None else time.time()) - ttl_seconds()
        removed = 0
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return 0
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                if entry.stat().st_mtime >= cutoff:
                    continue
                entry.unlink()
                removed += 1
            except OSError:
                continue
        return removed
