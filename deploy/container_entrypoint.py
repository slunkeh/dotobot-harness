"""Container PID 1: stop agents through the lifecycle before exiting."""

from __future__ import annotations

import os
import signal
import subprocess
import sys


def main() -> int:
    command = [sys.executable, "-m", "harness", "--backend", "machines"]
    child = subprocess.Popen(command + ["serve", "--up", "--host", "0.0.0.0", "--port", "8765"])
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        # Keep the server alive until down has quiesced/synced each machine.
        # Docker's stop timeout is longer than this bounded lifecycle operation.
        try:
            subprocess.run(command + ["down"], timeout=180, check=False)
        except subprocess.TimeoutExpired:
            print("Bot shutdown timed out; persistent state was retained.", file=sys.stderr)
        child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return child.wait()


if __name__ == "__main__":
    if not os.environ.get("HARNESS_HOME"):
        raise SystemExit("HARNESS_HOME must be an explicitly mounted state directory.")
    raise SystemExit(main())
