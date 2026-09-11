"""Background terminal sessions: terminal files as the primitive.

A background command's combined stdout+stderr lives in a *terminal file* the
agent polls with offset/limit reads instead of holding a blocking wait:

* machines backend — ``~/terminals/<shell_id>.txt`` inside the bot's machine,
  run through the existing docker-exec doorway. The file is on the machine's
  home volume, so it survives brain restarts and the human can ``tail -f`` it
  from the machine's own terminal.
* process backend — ``<workspace>/terminals/<bot>/<shell_id>.txt`` on the
  host, same mechanics.

Terminal file format (grokbot-style):

* a fixed-width frontmatter header rewritten IN PLACE at offset 0 on a ~5s
  cadence while the command runs. ``pid``, ``status``, and ``running_for_ms``
  are padded to constant widths so a rewrite never shifts the body;
* the command's combined output appended below (stdin echoes as
  ``[stdin] ...`` lines);
* an exit footer with the code on completion.

Process hygiene: every command runs in its own process group
(``start_new_session=True`` / ``setsid``); cancellation kills the group with
SIGTERM, then SIGKILL after a grace period.

Shell ids increment per bot and are derived from the terminal files on disk,
so they stay stable across agent restarts.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.envscrub import scrub_ambient_authority
from harness.paths import HarnessPaths

#: fixed field widths — the in-place header rewrite must never change the
#: header's byte length, or it would overwrite the start of the body.
PID_WIDTH = 10
STATUS_WIDTH = 9  # longest status: "succeeded"
RUNNING_MS_WIDTH = 12
#: how often the running header (running_for_ms) is rewritten in place.
HEADER_UPDATE_INTERVAL = 5.0
#: SIGTERM -> SIGKILL escalation grace for cancelled commands.
STOP_GRACE = 5.0

_TERMINAL_NAME = re.compile(r"(\d+)\.txt")


class TerminalError(RuntimeError):
    """A terminal operation failed in a way the model should be told about."""


def _iso_utc(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def build_header(
    *,
    pid: int | None,
    cwd: str,
    command: str,
    status: str,
    started_at: str,
    running_ms: int,
) -> str:
    """The frontmatter header. Same inputs (minus the padded fields) always
    produce the same byte length, so it can be rewritten at offset 0."""
    pid_field = "" if pid is None else str(pid)
    return (
        "---\n"
        f"pid: {pid_field:<{PID_WIDTH}}\n"
        f"cwd: {json.dumps(cwd)}\n"
        f"command: {json.dumps(command)}\n"
        f"status: {status:<{STATUS_WIDTH}}\n"
        f"started_at: {started_at}\n"
        f"running_for_ms: {running_ms:<{RUNNING_MS_WIDTH}}\n"
        "---\n"
    )


def build_footer(*, exit_code: int, elapsed_ms: int, ended_at: str) -> str:
    return f"\n---\nexit_code: {exit_code}\nelapsed_ms: {elapsed_ms}\nended_at: {ended_at}\n---\n"


def _signal_group(pid: int, sig: int) -> None:
    """Signal the command's whole process group; fall back to the pid alone."""
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def kill_process_group(proc: subprocess.Popen, *, grace: float = STOP_GRACE) -> None:
    """SIGTERM the group, wait out the grace, then SIGKILL what remains."""
    if proc.poll() is not None:
        return
    _signal_group(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        _signal_group(proc.pid, signal.SIGKILL)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - kernel wedge
            pass


@dataclass
class TerminalInfo:
    """What a spawn returns: the id plus where the output lives."""

    shell_id: int
    path: str
    pid: int | None = None


def _next_id_from_names(names: list[str]) -> int:
    ids = [int(m.group(1)) for n in names if (m := _TERMINAL_NAME.fullmatch(n.strip()))]
    return max(ids, default=0) + 1


# -- process backend -------------------------------------------------------


class _Session:
    """One background command on the host: header keeper + group lifecycle.

    The command's stdout/stderr append straight to the terminal file through
    an inherited fd; a second fd (``r+b``) rewrites the fixed-width header at
    offset 0 without ever touching the body — the grokbot two-fd trick.
    """

    def __init__(
        self,
        shell_id: int,
        path: Path,
        command: str,
        cwd: str,
        *,
        update_interval: float = HEADER_UPDATE_INTERVAL,
        grace: float = STOP_GRACE,
    ) -> None:
        self.shell_id = shell_id
        self.path = path
        self.command = command
        self.cwd = cwd
        self.update_interval = update_interval
        self.grace = grace
        self.status = "running"
        self.cancelled = False
        self._t0 = time.time()
        self.started_at = _iso_utc(self._t0)
        self._finalized = False
        self._lock = threading.Lock()

        path.parent.mkdir(parents=True, exist_ok=True)
        # header first, then spawn: the body can only ever append below it
        path.write_text(self._header(pid=None), encoding="utf-8")
        out = open(path, "ab")  # noqa: SIM115 - child inherits its own dup
        try:
            self.proc = subprocess.Popen(
                ["/bin/sh", "-c", command],
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # own group: cancellation kills all of it
                # the same allow-listed environment foreground run_command
                # gets: a background `env` used to print the harness's own
                # provider keys and HARNESS_TOKEN (harness/envscrub.py)
                env=scrub_ambient_authority(),
            )
        finally:
            out.close()
        self._header_fh = open(path, "r+b")  # noqa: SIM115 - closed in _finalize
        self._rewrite_header()
        self._watcher = threading.Thread(target=self._watch, daemon=True)
        self._watcher.start()

    def _header(self, pid: int | None = -1) -> str:
        return build_header(
            pid=self.proc.pid if pid == -1 else pid,
            cwd=self.cwd,
            command=self.command,
            status=self.status,
            started_at=self.started_at,
            running_ms=int((time.time() - self._t0) * 1000),
        )

    def _rewrite_header(self) -> None:
        with self._lock:
            if self._header_fh.closed:
                return
            try:
                self._header_fh.seek(0)
                self._header_fh.write(self._header().encode("utf-8"))
                self._header_fh.flush()
            except OSError:  # pragma: no cover - disk gone
                pass

    def _watch(self) -> None:
        while True:
            try:
                self.proc.wait(timeout=self.update_interval)
            except subprocess.TimeoutExpired:
                self._rewrite_header()
                continue
            break
        self._finalize()

    def _finalize(self) -> None:
        with self._lock:
            if self._finalized:
                return
            self._finalized = True
        code = self.proc.returncode if self.proc.returncode is not None else 1
        try:
            with open(self.path, "ab") as fh:
                fh.write(
                    build_footer(
                        exit_code=code,
                        elapsed_ms=int((time.time() - self._t0) * 1000),
                        ended_at=_iso_utc(),
                    ).encode("utf-8")
                )
        except OSError:  # pragma: no cover - disk gone
            pass
        if self.cancelled:
            self.status = "aborted"
        else:
            self.status = "succeeded" if code == 0 else "failed"
        self._rewrite_header()
        with self._lock:
            try:
                self._header_fh.close()
            except OSError:  # pragma: no cover
                pass
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    def write_stdin(self, chars: str) -> int:
        """Write to the command's stdin; return the file length BEFORE the
        write so the caller can read exactly the output produced after it."""
        if not self.running or self.proc.stdin is None:
            raise TerminalError(f"shell {self.shell_id} is not running")
        before = self.path.stat().st_size
        data = chars.encode("utf-8")
        echo = b"[stdin] " + data + (b"" if data.endswith(b"\n") else b"\n")
        try:
            with open(self.path, "ab") as fh:
                fh.write(echo)
        except OSError:  # pragma: no cover - disk gone
            pass
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise TerminalError(f"shell {self.shell_id} closed its stdin: {exc}") from exc
        return before

    def cancel(self) -> None:
        """Kill the whole process group: SIGTERM, grace, then SIGKILL."""
        self.cancelled = True
        kill_process_group(self.proc, grace=self.grace)
        self._finalize()


class ProcessTerminals:
    """Per-bot background-shell manager for the process backend."""

    def __init__(
        self,
        root: Path,
        default_cwd: Path,
        *,
        update_interval: float = HEADER_UPDATE_INTERVAL,
        grace: float = STOP_GRACE,
    ) -> None:
        self.root = root
        self.default_cwd = default_cwd
        self.update_interval = update_interval
        self.grace = grace
        self._sessions: dict[int, _Session] = {}
        self._lock = threading.Lock()

    def terminal_path(self, shell_id: int) -> Path:
        return self.root / f"{shell_id}.txt"

    def _next_shell_id(self) -> int:
        names = [p.name for p in self.root.glob("*.txt")] if self.root.is_dir() else []
        floor = max(self._sessions, default=0)
        return max(_next_id_from_names(names), floor + 1)

    def spawn(self, command: str) -> TerminalInfo:
        with self._lock:
            shell_id = self._next_shell_id()
            self.default_cwd.mkdir(parents=True, exist_ok=True)
            try:
                session = _Session(
                    shell_id,
                    self.terminal_path(shell_id),
                    command,
                    str(self.default_cwd),
                    update_interval=self.update_interval,
                    grace=self.grace,
                )
            except OSError as exc:
                raise TerminalError(f"could not start the command: {exc}") from exc
            self._sessions[shell_id] = session
        return TerminalInfo(shell_id=shell_id, path=str(session.path), pid=session.proc.pid)

    def read(self, shell_id: int, offset: int, limit: int) -> tuple[bytes, int]:
        """Bytes ``[offset, offset+limit)`` of the terminal file + its size.

        Works for terminals from earlier sessions too — the file, not the
        in-memory session, is the source of truth.
        """
        path = self.terminal_path(shell_id)
        if not path.is_file():
            raise TerminalError(f"no terminal file for shell {shell_id} ({path})")
        size = path.stat().st_size
        with open(path, "rb") as fh:
            fh.seek(max(0, offset))
            data = fh.read(max(0, limit))
        return data, size

    def write_stdin(self, shell_id: int, chars: str) -> int:
        session = self._sessions.get(shell_id)
        if session is None:
            raise TerminalError(f"no running shell {shell_id} in this session")
        return session.write_stdin(chars)

    def stop(self, shell_id: int) -> str:
        session = self._sessions.get(shell_id)
        if session is None:
            raise TerminalError(f"no running shell {shell_id} in this session")
        if not session.running:
            return f"shell {shell_id} had already exited (see the terminal footer)"
        session.cancel()
        return (
            f"shell {shell_id} stopped (process group SIGTERM, SIGKILL after "
            f"{session.grace:.0f}s grace); footer written"
        )


# -- machines backend (docker-exec doorway) --------------------------------

#: terminals dir inside a bot machine (on the persistent home volume).
MACHINE_TERMINALS_DIR = "$HOME/terminals"


def _sh_header_fmt() -> str:
    """printf(1) format reproducing `build_header` byte-for-byte, so the
    in-machine runner's rewrites obey the same fixed widths."""
    return (
        "---\\n"
        f"pid: %-{PID_WIDTH}s\\n"
        "cwd: %s\\n"
        "command: %s\\n"
        f"status: %-{STATUS_WIDTH}s\\n"
        "started_at: %s\\n"
        f"running_for_ms: %-{RUNNING_MS_WIDTH}s\\n"
        "---\\n"
    )


def machine_runner_script(shell_id: int, command: str, cwd: str, *, interval: int = 5) -> str:
    """POSIX-sh runner executed detached inside the machine (`docker exec -d`).

    The machine is a jail with no harness code, so the whole terminal-file
    contract runs as plain sh: fixed-width header rewritten in place through
    `dd conv=notrunc`, body appended by the command itself, exit footer at
    the end. Stdin arrives through a fifo next to the terminal file, and the
    command runs under `setsid` so a group kill reaps everything. Because
    the runner lives in the container, the header/footer keep updating even
    if the host-side brain restarts.
    """
    qcmd = shlex.quote(command)
    qcwd = shlex.quote(cwd)
    jcwd = shlex.quote(json.dumps(cwd))
    jcmd = shlex.quote(json.dumps(command))
    fmt = _sh_header_fmt()
    return f"""set -u
dir="{MACHINE_TERMINALS_DIR}"
mkdir -p "$dir"
f="$dir/{shell_id}.txt"
fifo="$dir/.{shell_id}.stdin"
pidfile="$dir/.{shell_id}.pid"
rm -f "$fifo" "$pidfile"
mkfifo "$fifo"
started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
start_s=$(date +%s)
hdr() {{
  printf '{fmt}' "$1" {jcwd} {jcmd} "$2" "$started_at" "$3"
}}
hdr '' running 0 > "$f"
cd {qcwd} 2>> "$f" || {{
  printf '\\n---\\nexit_code: 127\\nelapsed_ms: 0\\nended_at: %s\\n---\\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$f"
  hdr '' failed 0 | dd of="$f" conv=notrunc 2>/dev/null
  rm -f "$fifo"
  exit 0
}}
if command -v setsid >/dev/null 2>&1; then
  setsid sh -c {qcmd} < "$fifo" >> "$f" 2>&1 &
else
  sh -c {qcmd} < "$fifo" >> "$f" 2>&1 &
fi
child=$!
printf '%s' "$child" > "$pidfile"
exec 3> "$fifo"
hdr "$child" running 0 | dd of="$f" conv=notrunc 2>/dev/null
while kill -0 "$child" 2>/dev/null; do
  sleep {interval}
  kill -0 "$child" 2>/dev/null || break
  now_s=$(date +%s)
  hdr "$child" running "$(( (now_s - start_s) * 1000 ))" | dd of="$f" conv=notrunc 2>/dev/null
done
wait "$child"
code=$?
now_s=$(date +%s)
printf '\\n---\\nexit_code: %s\\nelapsed_ms: %s\\nended_at: %s\\n---\\n' "$code" "$(( (now_s - start_s) * 1000 ))" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$f"
if [ "$code" -eq 0 ]; then status=succeeded; else status=failed; fi
hdr "$child" "$status" "$(( (now_s - start_s) * 1000 ))" | dd of="$f" conv=notrunc 2>/dev/null
rm -f "$fifo" "$pidfile"
"""


def machine_stop_script(shell_id: int, *, grace: int = int(STOP_GRACE)) -> str:
    """Group kill inside the machine: SIGTERM, wait the grace, SIGKILL."""
    return f"""pidfile="{MACHINE_TERMINALS_DIR}/.{shell_id}.pid"
pid=$(cat "$pidfile" 2>/dev/null) || exit 3
[ -n "$pid" ] || exit 3
kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || exit 3
n=0
while kill -0 "$pid" 2>/dev/null; do
  if [ "$n" -ge {grace} ]; then
    kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null
    break
  fi
  n=$((n + 1))
  sleep 1
done
exit 0
"""


class MachineTerminals:
    """Background shells on the bot's machine, over the docker-exec doorway.

    Stateless on the host on purpose: the pid file, stdin fifo, and terminal
    file inside the machine ARE the session state, so reads and kills keep
    working after a brain restart. `runner` receives the engine argv tail
    (everything after the docker/podman binary) — injectable for tests.
    """

    def __init__(self, machine: str, *, runner=None, grace: float = STOP_GRACE) -> None:
        self.machine = machine
        self.grace = grace
        self._runner = runner or self._engine_run

    @staticmethod
    def _engine_run(argv: list[str], *, input: bytes | None = None, timeout: int = 15):
        from isolation.engine import resolve_engine

        return subprocess.run(
            [resolve_engine(), *argv], input=input, capture_output=True, timeout=timeout
        )

    def _run(self, argv: list[str], *, input: bytes | None = None, timeout: int = 15):
        try:
            return self._runner(argv, input=input, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise TerminalError(f"machine {self.machine} is unreachable: {exc}") from exc

    def terminal_path(self, shell_id: int) -> str:
        return f"~/terminals/{shell_id}.txt"

    def _next_shell_id(self) -> int:
        proc = self._run(
            ["exec", self.machine, "sh", "-c", 'ls "$HOME/terminals" 2>/dev/null || true']
        )
        names = proc.stdout.decode("utf-8", "replace").split() if proc.stdout else []
        return _next_id_from_names(names)

    def spawn(self, command: str) -> TerminalInfo:
        from isolation.machines import MACHINE_HOME

        shell_id = self._next_shell_id()
        script = machine_runner_script(shell_id, command, MACHINE_HOME)
        proc = self._run(["exec", "-d", self.machine, "sh", "-c", script])
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            raise TerminalError(f"could not start the command on {self.machine}: {err}")
        return TerminalInfo(shell_id=shell_id, path=self.terminal_path(shell_id))

    def read(self, shell_id: int, offset: int, limit: int) -> tuple[bytes, int]:
        script = (
            f'f="{MACHINE_TERMINALS_DIR}/{shell_id}.txt"; [ -f "$f" ] || exit 3; '
            f'wc -c < "$f"; tail -c +{max(0, offset) + 1} "$f" | head -c {max(0, limit)}'
        )
        proc = self._run(["exec", self.machine, "sh", "-c", script])
        if proc.returncode != 0:
            raise TerminalError(
                f"no terminal file for shell {shell_id} on {self.machine} "
                f"({self.terminal_path(shell_id)})"
            )
        head, _, chunk = (proc.stdout or b"").partition(b"\n")
        try:
            size = int(head.strip())
        except ValueError as exc:
            raise TerminalError(f"unreadable terminal {shell_id} on {self.machine}") from exc
        return chunk, size

    def write_stdin(self, shell_id: int, chars: str) -> int:
        script = (
            f'f="{MACHINE_TERMINALS_DIR}/{shell_id}.txt"; '
            f'fifo="{MACHINE_TERMINALS_DIR}/.{shell_id}.stdin"; '
            '[ -p "$fifo" ] || exit 3; wc -c < "$f"; '
            'printf "[stdin] " >> "$f"; tee -a "$f" > "$fifo"; printf "\\n" >> "$f"'
        )
        proc = self._run(
            ["exec", "-i", self.machine, "sh", "-c", script],
            input=chars.encode("utf-8"),
        )
        if proc.returncode != 0:
            raise TerminalError(f"shell {shell_id} is not running on {self.machine}")
        try:
            return int((proc.stdout or b"").split(b"\n", 1)[0].strip())
        except ValueError as exc:
            raise TerminalError(f"unreadable terminal {shell_id} on {self.machine}") from exc

    def stop(self, shell_id: int) -> str:
        script = machine_stop_script(shell_id, grace=int(self.grace))
        proc = self._run(["exec", self.machine, "sh", "-c", script], timeout=int(self.grace) + 15)
        if proc.returncode != 0:
            raise TerminalError(
                f"shell {shell_id} is not running on {self.machine} (already exited?)"
            )
        return (
            f"shell {shell_id} stopped on {self.machine} (process group SIGTERM, "
            f"SIGKILL after {self.grace:.0f}s grace); footer written"
        )


# -- stdin tail: what a shell has been fed since its last newline ----------

#: bytes of unfinished line remembered per shell. A dangerous command typed
#: over several write_stdin calls (`rm -rf` then ` /\n`) is only a command
#: once the newline lands, so the gate's shell guard must see the whole line,
#: not the last chunk. Nothing here decides anything — `agent/govern.py`
#: reads it; the handlers only record what was actually written.
STDIN_TAIL_LIMIT = 4096

_stdin_tails: dict[tuple[str, str, int], str] = {}


def _tail_key(paths: Any, bot: str, shell_id: Any) -> tuple[str, str, int]:
    return (str(getattr(paths, "home", paths)), str(bot), int(shell_id))


def pending_stdin(paths: Any, bot: str, shell_id: Any) -> str:
    """The unfinished line already sitting in this shell's stdin ("" if none)."""
    try:
        return _stdin_tails.get(_tail_key(paths, bot, shell_id), "")
    except (TypeError, ValueError):
        return ""


def note_stdin(paths: Any, bot: str, shell_id: Any, chars: str) -> None:
    """Record bytes that were really written; keep only the last partial line."""
    try:
        key = _tail_key(paths, bot, shell_id)
    except (TypeError, ValueError):
        return
    text = _stdin_tails.get(key, "") + str(chars or "")
    # a newline submitted everything before it; only the remainder can still
    # be completed by a later write
    text = text.rsplit("\n", 1)[1] if "\n" in text else text
    text = text[-STDIN_TAIL_LIMIT:]
    if text:
        _stdin_tails[key] = text
    else:
        _stdin_tails.pop(key, None)


def forget_stdin(paths: Any, bot: str, shell_id: Any) -> None:
    try:
        _stdin_tails.pop(_tail_key(paths, bot, shell_id), None)
    except (TypeError, ValueError):
        pass


# -- per-bot resolution ----------------------------------------------------

#: process-backend managers live for the life of the agent process so
#: background shells outlive the tool turn that started them.
_process_managers: dict[tuple[str, str], ProcessTerminals] = {}


def terminals_for(paths: HarnessPaths, bot: str) -> ProcessTerminals | MachineTerminals:
    """The bot's terminal backend: its machine when it has one, else host."""
    machine = os.environ.get("HARNESS_MACHINE_NAME")
    if machine:
        return MachineTerminals(machine)
    key = (str(paths.home), bot)
    manager = _process_managers.get(key)
    if manager is None:
        manager = ProcessTerminals(paths.workspace / "terminals" / bot, paths.workspace)
        _process_managers[key] = manager
    return manager
