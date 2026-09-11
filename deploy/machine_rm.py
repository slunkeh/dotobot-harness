#!/usr/bin/env python3
"""In-machine stand-in for /bin/rm.

The bot desktop (xterm, Thunar launchers that shell out, anything on PATH)
does not go through `agent/govern.py`. Replacing `/bin/rm` in the machine
image is the door that does: the same `shellguard` check run_command uses,
then exec of the real binary kept off PATH.

Installed by `deploy/Dockerfile.machine` as `/bin/rm`. The real Coreutils
binary lives at `/usr/lib/harness/rm.real`. Tests monkeypatch `REAL_RM`
rather than touching /bin.
"""

from __future__ import annotations

import os
import sys

REAL_RM = "/usr/lib/harness/rm.real"
_APP = "/app"


def _inspect(argv: list[str]):
    sys.path.insert(0, _APP)
    from agent.shellguard import inspect_argv

    return inspect_argv(
        ["rm", *argv[1:]],
        home="/home/agent",
        cwd=os.environ.get("PWD") or "/home/agent",
        extra_sensitive=("/app", "/home/agent/.harness-local"),
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    try:
        hit = _inspect(argv)
    except Exception as exc:
        # Fail closed on a broken scanner: a recursive wipe must not slip
        # through because /app failed to import. A plain `rm file` still
        # proceeds so the desktop keeps working if the harness tree is gone.
        sys.stderr.write(f"rm: safety check failed ({exc}); refusing recursive delete\n")
        flags = " ".join(argv[1:])
        if any(ch in flags for ch in ("-r", "-R", "--recursive", "--no-preserve-root")):
            return 1
        hit = None
    if hit:
        where = f" ({hit.path})" if hit.path else ""
        sys.stderr.write(
            f"rm: refused — {hit.reason}{where}. "
            "Destructive deletes of system directories and the home root "
            "are blocked.\n"
        )
        return 1
    real = REAL_RM
    try:
        os.execv(real, [real, *argv[1:]])
    except OSError as exc:
        sys.stderr.write(f"rm: {exc}\n")
        return 127
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
