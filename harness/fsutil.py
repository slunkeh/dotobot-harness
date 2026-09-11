"""Small shared filesystem helpers.

State files under $HARNESS_HOME are read by other processes (server, agents,
CLI) at any moment, so writers that can race a reader — or die mid-write —
must go through `write_atomic` rather than `Path.write_text`.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path


def write_atomic(path: Path, text: str) -> None:
    """Write `text` to `path` so readers see either the old or the new file.

    A plain write_text leaves a torn file if the process dies mid-write;
    tmp-file + os.replace is atomic on POSIX.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_private(path: Path, text: str) -> None:
    """Write `text` to `path` as a private (0600) file, atomically.

    A credential written with `Path.write_text` and chmod'ed afterwards is
    born with the process umask (0644 under the usual 022) and holds the
    plaintext until the chmod lands — a window, and a torn file if the
    process dies in between. Here the temp file is opened O_CREAT|O_EXCL at
    0600 in the target's own directory and renamed over the destination, so
    no reader ever sees a wide or half-written credential. An existing
    file's mode is forced back to 0600 as well.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - non-posix
        pass


def pid_alive(pid: int) -> bool:
    """True when the process is actually running (EPERM counts as alive).

    A dead child of THIS process lingers as a zombie until reaped — and a
    zombie still answers os.kill(pid, 0). Backends spawn agents and drop the
    Popen object, so nothing else reaps them; try a non-blocking waitpid
    first so a finished agent reads as dead immediately instead of stalling
    stop/status for the whole SIGKILL grace.
    """
    try:
        done, _status = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return False
    except ChildProcessError:
        pass  # not our child; fall through to the liveness probe
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
