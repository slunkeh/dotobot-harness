"""Generation-token process identity + ambient-authority env scrub."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness.envscrub import AMBIENT_AUTHORITY_ENV_VARS, scrub_ambient_authority
from isolation.process_identity import (
    GENERATION_TOKEN_ENV,
    boot_identity_error,
    command_carries_token,
    mint_generation_token,
    read_cmdline,
    token_argument,
    verify_process_token,
)

TOK = "0123456789abcdef0123456789abcdef"


# -- cmdline token matching -----------------------------------------------
def test_command_carries_token_exact_whitespace_bounded():
    flag = token_argument(TOK)
    assert command_carries_token(f"python -m agent --bot atlas {flag}", TOK)
    assert command_carries_token(flag, TOK)  # whole command
    assert command_carries_token(f"{flag} --after", TOK)  # at start


def test_command_carries_token_rejects_substring_hits():
    flag = token_argument(TOK)
    # token embedded in a longer argument must not count
    assert not command_carries_token(f"python {flag}extra", TOK)
    assert not command_carries_token(f"python x{flag}", TOK)
    # a different process quoting our flag inside another arg
    assert not command_carries_token(f"sh -c 'echo{flag}done'", TOK)
    # prefix of a longer token
    assert not command_carries_token(f"python {token_argument(TOK + 'ff')}", TOK)
    assert not command_carries_token("python -m agent --bot atlas", TOK)
    assert not command_carries_token("", TOK)
    assert not command_carries_token(f"python {flag}", "")


def test_verify_process_token_needs_alive_and_matching_cmdline():
    live = {"alive": True, "cmd": f"python -m agent {token_argument(TOK)}"}
    check = lambda: verify_process_token(  # noqa: E731
        4242, TOK, alive=lambda pid: live["alive"], read=lambda pid: live["cmd"]
    )
    assert check()
    live["alive"] = False
    assert not check()
    live["alive"] = True
    live["cmd"] = "some-other-binary --that-reused --the-pid"
    assert not check()
    live["cmd"] = None  # /proc gone between checks
    assert not check()
    assert not verify_process_token(None, TOK)


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs /proc")
def test_verify_process_token_reads_real_proc_cmdline():
    token = mint_generation_token()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", token_argument(token)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # `Popen` returns once the fork is under way, not once `execve` has
        # replaced the image, so /proc/<pid>/cmdline can still be empty for a
        # moment. Unnoticeable on an idle machine and reliably racy on a loaded
        # CI runner, which is where it matters. Poll rather than sleep a fixed
        # amount: the wait is microseconds when it is not needed.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if token_argument(token) in (read_cmdline(child.pid) or ""):
                break
            time.sleep(0.01)
        assert token_argument(token) in (read_cmdline(child.pid) or "")
        assert verify_process_token(child.pid, token)
        assert not verify_process_token(child.pid, mint_generation_token())
    finally:
        child.kill()
        child.wait()


# -- child-side boot check ------------------------------------------------
def test_boot_identity_ok_when_both_halves_agree_or_absent():
    assert boot_identity_error(TOK, {GENERATION_TOKEN_ENV: TOK}) is None
    assert boot_identity_error(None, {}) is None  # manual run: no identity


def test_boot_identity_refuses_lone_or_mismatched_halves():
    assert "refusing to boot" in boot_identity_error(None, {GENERATION_TOKEN_ENV: TOK})
    assert "refusing to boot" in boot_identity_error(TOK, {})
    assert "mismatch" in boot_identity_error(TOK, {GENERATION_TOKEN_ENV: "not-" + TOK})


def test_agent_entrypoint_refuses_token_mismatch(monkeypatch, capsys):
    from agent.__main__ import main

    monkeypatch.setenv(GENERATION_TOKEN_ENV, TOK)
    rc = main(["--bot", "atlas", f"--generation-token=not-{TOK}"])
    assert rc == 2
    assert "refusing to boot" in capsys.readouterr().err


# -- ambient-authority scrub ----------------------------------------------
def test_scrub_drops_session_sockets_and_keeps_the_rest():
    env = {
        "PATH": "/usr/bin",
        "DISPLAY": ":0",
        "SSH_AUTH_SOCK": "/run/user/1000/ssh.sock",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "WAYLAND_DISPLAY": "wayland-0",
    }
    scrubbed = scrub_ambient_authority(env)
    for name in AMBIENT_AUTHORITY_ENV_VARS:
        assert name not in scrubbed
    assert scrubbed["PATH"] == "/usr/bin"
    assert scrubbed["DISPLAY"] == ":0"  # bots still need their X display
    assert "SSH_AUTH_SOCK" in env, "input mapping must not be mutated"


def test_scrub_defaults_to_os_environ_without_mutating_it(monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    scrubbed = scrub_ambient_authority()
    assert "SSH_AUTH_SOCK" not in scrubbed
    assert os.environ["SSH_AUTH_SOCK"] == "/tmp/agent.sock"


def test_bot_spawn_sites_use_the_shared_scrub():
    """The scrub is one shared helper: every host-side spawn on behalf of a
    bot goes through it (command exec, app launch, desktop spawn)."""
    import inspect

    from agent import computer, tools
    from harness import computer_env

    assert "scrub_ambient_authority" in inspect.getsource(tools._run_command)
    assert "scrub_ambient_authority" in inspect.getsource(computer._launch)
    assert "scrub_ambient_authority" in inspect.getsource(computer_env._spawn)
