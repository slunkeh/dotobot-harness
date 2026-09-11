"""Built-in block on rm -rf of sensitive directories.

The regression these guard is an install with no policy.toml running
`rm -rf /` (or `rm -rf ~`) through run_command or a desktop terminal.
Policy can still forbid more; it cannot re-open these paths.
"""

from __future__ import annotations

import agent.govern as govern
import agent.policy as policy
import agent.shellguard as shellguard
from harness import audit
from harness.paths import HarnessPaths


class _Ctx:
    def __init__(self) -> None:
        self.tool_call_id = "call-1"


def _paths(tmp_path) -> HarnessPaths:
    return HarnessPaths.resolve(tmp_path)


def _hit(command: str, **kw):
    return shellguard.inspect(command, home="/home/agent", cwd="/home/agent", **kw)


# -- scanner ---------------------------------------------------------------


def test_rm_rf_root_is_blocked():
    for cmd in (
        "rm -rf /",
        "rm -fr /",
        "rm --recursive --force /",
        "rm -r /",
        "rm --no-preserve-root -rf /",
        "sudo rm -rf /",
        "/bin/rm -rf /",
        "rm -rf /*",
        "timeout 5 rm -rf /",
        "env FOO=bar rm -rf /",
        "rm -rf / && echo gone",
        "echo hi; rm -rf /",
        "RM -RF /",
        'rm -rf "/"',
    ):
        hit = _hit(cmd)
        assert hit is not None, cmd
        assert hit.command == "rm"


def test_rm_rf_system_and_home_root_are_blocked():
    for cmd in (
        "rm -rf /usr",
        "rm -rf /usr/bin",
        "rm -rf /etc",
        "rm -rf /home",
        "rm -rf /home/agent",
        "rm -rf ~",
        "rm -rf $HOME",
        "rm -rf ${HOME}",
        "rm -rf ~/.config",
        "rm -rf /home/agent/.config/harness-chrome",
        "rm -rf /app",
        "rm -rf /var/log",
        "find / -delete",
        "find /home/agent -delete",
    ):
        assert _hit(cmd) is not None, cmd


def test_rm_of_user_files_and_tmp_is_allowed():
    for cmd in (
        "ls -la /",
        "rm file.txt",
        "rm -rf /tmp/build",
        "rm -rf /tmp",
        "rm -rf /var/tmp/x",
        "rm -rf /home/agent/Downloads/old",
        "rm -rf ~/Desktop/draft",
        "rm -rf ./build",
        "rm -rf $HOME/Downloads",
        "rm /home/agent/.bashrc",
        "find /tmp -delete",
        "find /home/agent/Downloads -name '*.tmp' -delete",
        "echo rm -rf /",  # not an rm invocation
    ):
        assert _hit(cmd) is None, cmd


def test_relative_dotdot_from_home_child_resolves_to_home():
    hit = shellguard.inspect(
        "rm -rf ..",
        home="/home/agent",
        cwd="/home/agent/Downloads",
    )
    assert hit is not None
    assert hit.path == "/home/agent"


def test_relative_dotdot_from_tmp_is_allowed():
    assert shellguard.inspect("rm -rf ..", home="/home/agent", cwd="/tmp/work") is None


def test_harness_home_on_the_process_backend_is_extra_sensitive():
    hit = shellguard.inspect(
        "rm -rf /tmp/harness-home",
        home="/Users/op",
        cwd="/tmp/harness-home/workspace",
        extra_sensitive=("/tmp/harness-home",),
    )
    assert hit is not None


def test_inspect_never_raises_on_garbage():
    shellguard.inspect("rm 'unterminated")
    assert shellguard.inspect("") is None


def test_default_home_inside_a_machine(monkeypatch):
    monkeypatch.setenv("HARNESS_MACHINE_NAME", "harness-machine-0")
    assert shellguard.default_home() == "/home/agent"
    monkeypatch.delenv("HARNESS_MACHINE_NAME")
    monkeypatch.setenv("HARNESS_MACHINE", "1")
    assert shellguard.default_home() == "/home/agent"


# -- govern chokepoint -----------------------------------------------------


def test_run_command_rm_rf_root_is_refused_without_a_policy(tmp_path):
    """Silence permits everything *except* this. No policy.toml required."""
    paths = _paths(tmp_path)
    out = govern.govern(
        _Ctx(),
        "run_command",
        {"command": "rm -rf /"},
        paths=paths,
        bot="atlas",
    )
    assert out is not None and out.startswith("error:")
    assert "sensitive" in out.lower() or "destructive" in out.lower()
    row = audit.read(paths, "atlas")[0]
    assert row["decision"] == "refuse"
    assert row["source"] == "shell-guard"


def test_run_command_rm_of_tmp_still_runs_without_a_policy(tmp_path):
    paths = _paths(tmp_path)
    out = govern.govern(
        _Ctx(),
        "run_command",
        {"command": "rm -rf /tmp/x"},
        paths=paths,
        bot="atlas",
    )
    assert out is None
    assert audit.read(paths, "atlas")[0]["decision"] == "allow"


def test_background_shell_and_stdin_use_the_same_door(tmp_path):
    paths = _paths(tmp_path)
    bg = govern.govern(
        _Ctx(),
        "run_command_background",
        {"command": "rm -rf ~"},
        paths=paths,
        bot="atlas",
    )
    assert bg is not None
    stdin = govern.govern(
        _Ctx(),
        "write_stdin",
        {"shell_id": "1", "chars": "rm -rf /\n"},
        paths=paths,
        bot="atlas",
    )
    assert stdin is not None


def test_an_allow_rule_cannot_reopen_rm_rf_root(tmp_path):
    """Policy is the operator's preference. This is a safety interlock."""
    paths = _paths(tmp_path)
    pol = policy.parse({"allow": [{"intent": "run_command"}]})
    out = govern.govern(
        _Ctx(),
        "run_command",
        {"command": "rm -rf /"},
        paths=paths,
        bot="atlas",
        policy=pol,
    )
    assert out is not None
    assert audit.read(paths, "atlas")[0]["source"] == "shell-guard"


def test_ls_is_untouched(tmp_path):
    paths = _paths(tmp_path)
    assert govern.govern(_Ctx(), "run_command", {"command": "ls"}, paths=paths, bot="atlas") is None


# -- in-machine wrapper ----------------------------------------------------


def test_machine_rm_refuses_root_and_execs_otherwise(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "deploy" / "machine_rm.py"
    spec = importlib.util.spec_from_file_location("machine_rm", path)
    assert spec is not None and spec.loader is not None
    machine_rm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(machine_rm)

    real = tmp_path / "rm.real"
    real.write_text('#!/bin/sh\necho ran "$@"\n')
    real.chmod(0o755)
    monkeypatch.setattr(machine_rm, "REAL_RM", str(real))
    execs: list[list[str]] = []

    def fake_execv(path, argv):
        execs.append([path, *argv[1:]])
        raise OSError("stop here")

    monkeypatch.setattr(machine_rm.os, "execv", fake_execv)

    assert machine_rm.main(["rm", "-rf", "/"]) == 1
    assert execs == []

    rc = machine_rm.main(["rm", "-rf", "/tmp/x"])
    assert rc == 127  # execv raised
    assert execs[0][0] == str(real)
    assert execs[0][-1] == "/tmp/x"
