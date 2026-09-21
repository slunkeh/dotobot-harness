"""Terminal files as the background-command primitive.

Process backend runs for real against tmp_path; the machines backend is
exercised through an injected fake exec runner (no docker daemon needed).
"""

from __future__ import annotations

import os
import subprocess
import time

from agent import tools as tools_mod
from agent.memory import Memory
from agent.terminals import (
    PID_WIDTH,
    RUNNING_MS_WIDTH,
    STATUS_WIDTH,
    MachineTerminals,
    ProcessTerminals,
    TerminalError,
    build_footer,
    build_header,
    machine_runner_script,
    machine_stop_script,
)
from agent.tools import ToolContext, default_tools
from harness.paths import HarnessPaths


def _manager(tmp_path, **kw) -> ProcessTerminals:
    kw.setdefault("update_interval", 0.05)
    kw.setdefault("grace", 1.0)
    return ProcessTerminals(tmp_path / "terminals", tmp_path / "work", **kw)


def _wait_for(cond, timeout=8.0, step=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


# -- header/footer format ---------------------------------------------------


def test_header_is_fixed_width_across_states():
    base = dict(cwd="/w", command="sleep 1", started_at="2026-08-24T00:00:00Z")
    lengths = {
        len(build_header(pid=None, status="running", running_ms=0, **base)),
        len(build_header(pid=1, status="running", running_ms=7, **base)),
        len(build_header(pid=4194304, status="succeeded", running_ms=10**9, **base)),
        len(build_header(pid=99, status="aborted", running_ms=123456, **base)),
    }
    assert len(lengths) == 1  # padded pid/status/running_for_ms never shift the body
    header = build_header(pid=42, status="running", running_ms=5, **base)
    assert f"pid: {'42':<{PID_WIDTH}}\n" in header
    assert f"status: {'running':<{STATUS_WIDTH}}\n" in header
    assert f"running_for_ms: {'5':<{RUNNING_MS_WIDTH}}\n" in header


def test_header_rewrite_in_place_does_not_shift_body(tmp_path):
    mgr = _manager(tmp_path)
    info = mgr.spawn("echo body-marker; sleep 0.6")
    path = mgr.terminal_path(info.shell_id)
    assert _wait_for(lambda: "body-marker" in _read(path))
    first = _read(path)
    marker_at = first.index("body-marker")
    assert first.startswith("---\n")
    assert "status: running" in first
    # several ~50ms header cadences pass while the command sleeps
    assert _wait_for(lambda: "exit_code: 0" in _read(path) and "status: succeeded" in _read(path))
    final = _read(path)
    assert final.index("body-marker") == marker_at
    assert "status: succeeded" in final
    assert "running_for_ms:" in final


def test_footer_written_on_exit_with_code(tmp_path):
    mgr = _manager(tmp_path)
    info = mgr.spawn("echo out; exit 3")
    path = mgr.terminal_path(info.shell_id)
    # Finalization appends the footer before rewriting the status header.
    assert _wait_for(lambda: "exit_code: 3" in _read(path) and "status: failed" in _read(path))
    text = _read(path)
    assert "status: failed" in text
    assert "\n---\nexit_code: 3\n" in text
    assert "elapsed_ms:" in text and "ended_at:" in text
    footer = build_footer(exit_code=3, elapsed_ms=1, ended_at="2026-08-24T00:00:00Z")
    assert footer.startswith("\n---\nexit_code: 3\n")


def test_shell_ids_increment_and_survive_restarts(tmp_path):
    mgr = _manager(tmp_path)
    a = mgr.spawn("true")
    b = mgr.spawn("true")
    assert (a.shell_id, b.shell_id) == (1, 2)
    # a terminal file left by an earlier agent process keeps ids moving forward
    (tmp_path / "terminals" / "7.txt").write_text("old", encoding="utf-8")
    fresh = _manager(tmp_path)
    assert fresh.spawn("true").shell_id == 8


def test_write_stdin_returns_length_before_write(tmp_path):
    mgr = _manager(tmp_path)
    info = mgr.spawn("cat")
    path = mgr.terminal_path(info.shell_id)
    header_len = path.stat().st_size
    before = mgr.write_stdin(info.shell_id, "hello\n")
    assert before == header_len  # nothing but the header had been written yet
    # everything after `before` is the new output: the [stdin] echo + cat's echo
    assert _wait_for(lambda: path.stat().st_size >= before + len("[stdin] hello\nhello\n"))
    data, size = mgr.read(info.shell_id, before, 4096)
    new = data.decode("utf-8")
    assert new.startswith("[stdin] hello\n")
    assert "hello\n" in new[len("[stdin] hello\n") :]
    mgr.stop(info.shell_id)


def test_write_stdin_rejects_exited_shell(tmp_path):
    mgr = _manager(tmp_path)
    info = mgr.spawn("true")
    assert _wait_for(lambda: "exit_code:" in _read(mgr.terminal_path(info.shell_id)))
    try:
        mgr.write_stdin(info.shell_id, "hi\n")
        raise AssertionError("expected TerminalError")
    except TerminalError as exc:
        assert "not running" in str(exc)


def test_stop_kills_the_whole_process_group(tmp_path):
    mgr = _manager(tmp_path)
    pid_file = tmp_path / "grandchild.pid"
    info = mgr.spawn(f"sleep 300 & echo $! > {pid_file}; wait")
    assert _wait_for(lambda: pid_file.is_file() and pid_file.read_text().strip())
    grandchild = int(pid_file.read_text().strip())
    leader = mgr._sessions[info.shell_id].proc.pid
    msg = mgr.stop(info.shell_id)
    assert "SIGTERM" in msg

    def dead(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        # still visible: only fine if it is a zombie awaiting reap
        try:
            out = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
            ).stdout.strip()
            return out == "" or out.startswith("Z")
        except OSError:
            return False

    assert _wait_for(lambda: dead(leader) and dead(grandchild))
    text = _read(mgr.terminal_path(info.shell_id))
    assert "status: aborted" in text
    assert "exit_code:" in text


def test_read_offset_limit_slices_bytes(tmp_path):
    mgr = _manager(tmp_path)
    info = mgr.spawn("printf 'abcdefghij'")
    path = mgr.terminal_path(info.shell_id)
    assert _wait_for(lambda: "exit_code: 0" in _read(path))
    whole = _read(path)
    at = whole.index("abcdefghij")
    data, size = mgr.read(info.shell_id, at + 2, 3)
    assert data == b"cde"
    assert size == path.stat().st_size


# -- tool surface (process backend) ----------------------------------------


def _ctx(tmp_path) -> ToolContext:
    paths = HarnessPaths(tmp_path)
    paths.ensure_layout(["atlas"])
    return ToolContext(paths=paths, bot="atlas", memory=Memory(paths=paths, bot="atlas"))


def test_background_tools_registered_with_polling_guidance():
    tools = default_tools()
    for name in ("run_command_background", "read_terminal", "write_stdin", "stop_terminal"):
        assert name in tools
    read_desc = tools["read_terminal"].spec.description
    assert "offset" in read_desc
    assert "not hold a long wait" in read_desc
    bg_desc = tools["run_command_background"].spec.description
    assert "read_terminal" in bg_desc and "never wait in a sleep loop" in bg_desc
    assert "BEFORE the write" in tools["write_stdin"].spec.description


def test_background_round_trip_through_tools(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_MACHINE_NAME", raising=False)
    ctx = _ctx(tmp_path)
    tools = default_tools()
    out = tools["run_command_background"].handler(ctx, {"command": "echo bg-done"})
    assert out.startswith("ok: background shell ")
    shell_id = int(out.split("background shell ")[1].split()[0])
    term_path = ctx.paths.workspace / "terminals" / "atlas" / f"{shell_id}.txt"
    assert str(term_path) in out
    assert _wait_for(lambda: "exit_code: 0" in _read(term_path))
    read = tools["read_terminal"].handler(ctx, {"shell_id": shell_id, "offset": 0})
    assert read.startswith(f"[terminal {shell_id}: bytes 0-")
    assert "bg-done" in read
    assert "status: succeeded" in read
    # polling past the end explains itself instead of erroring
    tail = tools["read_terminal"].handler(ctx, {"shell_id": shell_id, "offset": 10**9})
    assert tail.startswith("(no new output")
    assert tools["stop_terminal"].handler(ctx, {"shell_id": 999}).startswith("error:")


def test_write_stdin_tool_reports_before_length(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_MACHINE_NAME", raising=False)
    ctx = _ctx(tmp_path)
    tools = default_tools()
    out = tools["run_command_background"].handler(ctx, {"command": "cat"})
    shell_id = int(out.split("background shell ")[1].split()[0])
    reply = tools["write_stdin"].handler(ctx, {"shell_id": shell_id, "chars": "ping\n"})
    assert reply.startswith("ok: sent 5 bytes")
    assert "offset=" in reply
    stop = tools["stop_terminal"].handler(ctx, {"shell_id": shell_id})
    assert stop.startswith("ok:")


# -- foreground caps --------------------------------------------------------


def test_foreground_stream_cap_marks_trimmed_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(tools_mod, "_FG_STREAM_CAP", 64)
    ctx = _ctx(tmp_path)
    out = default_tools()["run_command"].handler(ctx, {"command": "seq 1 100"})
    assert "exit 0" in out
    assert "[trimmed:" in out and "bytes]" in out


def test_foreground_truncation_points_at_terminal_files(tmp_path, monkeypatch):
    monkeypatch.setattr(tools_mod, "_RUN_LIMIT", 50)
    ctx = _ctx(tmp_path)
    out = default_tools()["run_command"].handler(ctx, {"command": "seq 1 100"})
    assert "run_command_background" in out  # overflow points at the terminal file


def test_foreground_still_returns_stdout(tmp_path):
    ctx = _ctx(tmp_path)
    out = default_tools()["run_command"].handler(ctx, {"command": "echo hello-fg"})
    assert out.startswith("exit 0")
    assert "hello-fg" in out


# -- machines backend (fake exec runner) ------------------------------------


class FakeExec:
    """Records engine argv tails and plays back canned results."""

    def __init__(self, results=None):
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.results = list(results or [])

    def __call__(self, argv, *, input=None, timeout=15):
        self.calls.append((list(argv), input))
        stdout, rc = (self.results.pop(0) if self.results else (b"", 0))
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr=b"")


def test_machine_spawn_runs_detached_runner_in_machine():
    fake = FakeExec(results=[(b"1.txt\n3.txt\n", 0), (b"", 0)])
    term = MachineTerminals("harness-machine-0", runner=fake)
    info = term.spawn("npm run dev")
    assert info.shell_id == 4  # max existing id + 1
    assert info.path == "~/terminals/4.txt"
    ls_argv, _ = fake.calls[0]
    assert ls_argv[:4] == ["exec", "harness-machine-0", "sh", "-c"]
    spawn_argv, _ = fake.calls[1]
    assert spawn_argv[:5] == ["exec", "-d", "harness-machine-0", "sh", "-c"]
    script = spawn_argv[5]
    assert "mkfifo" in script
    assert "setsid sh -c 'npm run dev'" in script
    assert "dd of=\"$f\" conv=notrunc" in script  # in-place header rewrite
    assert "exit_code:" in script


def test_machine_runner_script_header_matches_python_widths():
    script = machine_runner_script(4, "sleep 5", "/home/agent")
    assert f"pid: %-{PID_WIDTH}s" in script
    assert f"status: %-{STATUS_WIDTH}s" in script
    assert f"running_for_ms: %-{RUNNING_MS_WIDTH}s" in script


def test_machine_read_parses_size_then_chunk():
    fake = FakeExec(results=[(b"120\nhello-machine", 0)])
    term = MachineTerminals("harness-machine-0", runner=fake)
    data, size = term.read(4, offset=100, limit=20)
    assert (data, size) == (b"hello-machine", 120)
    argv, _ = fake.calls[0]
    assert "tail -c +101" in argv[-1]
    assert "head -c 20" in argv[-1]


def test_machine_write_stdin_pipes_chars_and_returns_before_length():
    fake = FakeExec(results=[(b"88\n", 0)])
    term = MachineTerminals("harness-machine-0", runner=fake)
    assert term.write_stdin(4, "yes\n") == 88
    argv, sent = fake.calls[0]
    assert argv[:2] == ["exec", "-i"]
    assert sent == b"yes\n"
    assert ".4.stdin" in argv[-1]


def test_machine_stop_script_escalates_group_kill():
    script = machine_stop_script(4, grace=5)
    assert 'kill -TERM -- "-$pid"' in script
    assert 'kill -KILL -- "-$pid"' in script
    fake = FakeExec(results=[(b"", 3)])
    term = MachineTerminals("harness-machine-0", runner=fake)
    try:
        term.stop(4)
        raise AssertionError("expected TerminalError")
    except TerminalError as exc:
        assert "not running" in str(exc)
