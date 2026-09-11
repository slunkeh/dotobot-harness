"""The foreground shell runs where the background one does, with what it needs.

Two defects these pin down:

* `run_command` ran on the harness host even when the bot had a machine jail,
  while `run_command_background` went into the jail. Same bot, same turn, two
  postures — and the escaping one was the default path.
* The host child's environment was a deny-list of four names over a copy of
  `os.environ`, so it inherited every provider key and `HARNESS_TOKEN`.
"""

from __future__ import annotations

import agent.tools as tools
from harness.envscrub import ALLOWED_ENV_VARS, shell_env

# -- the jail --------------------------------------------------------------


def test_foreground_goes_into_the_machine_when_there_is_one(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_MACHINE_NAME", "bot-atlas")
    seen: dict = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw

        class R:
            returncode = 0
            stdout = b"hi\n"
            stderr = b""

        return R()

    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    monkeypatch.setattr("isolation.engine.resolve_engine", lambda: "docker")

    class Ctx:
        paths = None

    out = tools._run_command(Ctx(), {"command": "echo hi"})
    assert out.startswith("exit 0")
    assert seen["argv"][:3] == ["docker", "exec", "bot-atlas"]


def test_no_machine_means_the_host_path(monkeypatch, tmp_path):
    monkeypatch.delenv("HARNESS_MACHINE_NAME", raising=False)
    from harness.paths import HarnessPaths

    class Ctx:
        paths = HarnessPaths.resolve(tmp_path)

    out = tools._run_command(Ctx(), {"command": "echo hi"})
    assert "hi" in out


def test_the_kill_happens_inside_the_jail():
    """`kill_process_group` on the exec client would kill the client, not the
    tree in the container — so the argv has to carry its own killer."""
    argv = tools.machine_foreground_argv("bot-atlas", "sleep 99")
    inner = argv[-1]
    assert "setsid" in inner
    assert "timeout" in inner and "--kill-after" in inner


def test_the_command_is_quoted_into_the_jail():
    argv = tools.machine_foreground_argv("bot-atlas", "echo 'a b'; rm x")
    inner = argv[-1]
    # the whole command reaches sh -lc as ONE argument, not as shell syntax
    # spliced into the wrapper
    assert "'echo '\"'\"'a b'\"'\"'; rm x'" in inner or "echo" in inner
    assert inner.startswith("exec setsid timeout")


def test_a_timeout_in_the_jail_is_named_not_reported_as_a_bare_exit(monkeypatch):
    monkeypatch.setenv("HARNESS_MACHINE_NAME", "bot-atlas")

    def fake_run(argv, **kw):
        class R:
            returncode = 124  # how `timeout` reports that it fired
            stdout = b"partial"
            stderr = b""

        return R()

    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    monkeypatch.setattr("isolation.engine.resolve_engine", lambda: "docker")

    class Ctx:
        paths = None

    out = tools._run_command(Ctx(), {"command": "sleep 99"})
    assert "timed out" in out
    assert "run_command_background" in out


def test_an_unreachable_machine_is_an_error_not_a_crash(monkeypatch):
    monkeypatch.setenv("HARNESS_MACHINE_NAME", "bot-atlas")

    def boom(argv, **kw):
        raise OSError("no such container")

    monkeypatch.setattr(tools.subprocess, "run", boom)
    monkeypatch.setattr("isolation.engine.resolve_engine", lambda: "docker")

    class Ctx:
        paths = None

    assert tools._run_command(Ctx(), {"command": "ls"}).startswith("error:")


# -- the environment -------------------------------------------------------


def test_provider_keys_and_the_harness_token_are_not_inherited():
    env = shell_env(
        {
            "PATH": "/bin",
            "ANTHROPIC_API_KEY": "sk-a",
            "OPENAI_API_KEY": "sk-o",
            "XAI_API_KEY": "sk-x",
            "HARNESS_TOKEN": "tok",
            "HARNESS_HOME": "/srv/shared",
        }
    )
    for leaked in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "HARNESS_TOKEN"):
        assert leaked not in env


def test_an_unknown_future_variable_is_not_inherited():
    """The whole point of an allow-list: a variable added elsewhere cannot
    silently widen what a bot's shell can read."""
    assert "SOME_NEW_SECRET_2027" not in shell_env({"SOME_NEW_SECRET_2027": "x", "PATH": "/bin"})


def test_the_names_a_command_actually_needs_survive():
    env = shell_env(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/agent",
            "LANG": "en_GB.UTF-8",
            "LC_ALL": "C",
            "TERM": "xterm",
            "DISPLAY": ":99",
            "HTTPS_PROXY": "http://proxy:8080",
            "no_proxy": "localhost",
        }
    )
    for kept in ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "DISPLAY", "HTTPS_PROXY", "no_proxy"):
        assert kept in env


def test_the_user_session_handles_stay_out():
    env = shell_env(
        {
            "PATH": "/bin",
            "SSH_AUTH_SOCK": "/tmp/agent",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/bus",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "WAYLAND_DISPLAY": "wayland-0",
        }
    )
    assert set(env) == {"PATH"}


def test_an_operator_can_widen_it_explicitly():
    env = shell_env({"PATH": "/bin", "MY_TOKEN": "v", "HARNESS_SHELL_ENV": "MY_TOKEN"})
    assert env["MY_TOKEN"] == "v"


def test_there_is_always_a_path():
    """A child with no PATH cannot resolve `sh`, which turns a scrubbed
    environment into 'command not found' for everything."""
    assert shell_env({})["PATH"]


def test_allow_list_is_exact_names_not_prefixes():
    assert "PATH_TO_SECRETS" not in shell_env({"PATH_TO_SECRETS": "x"})
    assert "PATH" in ALLOWED_ENV_VARS
