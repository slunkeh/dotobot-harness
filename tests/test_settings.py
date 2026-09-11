"""Settings are validated at startup, and the registry cannot drift from the code.

The defect: every reader in the tree swallowed a bad value back to its default,
so `HARNESS_MACHINE_PIDS=51x` and an unset variable were indistinguishable. An
operator who set a limit and watched it not apply had nothing to look at.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from harness.settings import BY_NAME, SETTINGS, SettingError, describe, validate_environment

ROOT = Path(__file__).resolve().parent.parent

#: Names that are not tunables and are deliberately not in the registry.
#: `HARNESS_SECRET_*` are credential references with operator-chosen suffixes;
#: enumerating them would mean printing secret names in `harness settings`.
#: `HARNESS_MCP_OAUTH_*` is the same shape for per-connector OAuth client
#: ids (`HARNESS_MCP_OAUTH_SLACK_CLIENT_ID`), read via an f-string prefix.
NOT_TUNABLES = {"HARNESS_SECRET_", "HARNESS_MCP_OAUTH_"}


def _names_in_source() -> set[str]:
    found: set[str] = set()
    # The private account backend has a separate configuration contract.
    runtime_dirs = (
        "harness",
        "agent",
        "providers",
        "connectors",
        "channels",
        "isolation",
        "deploy",
    )
    for path in (p for name in runtime_dirs for p in (ROOT / name).rglob("*.py")):
        if "/tests/" in str(path) or path.name == "settings.py":
            continue
        # Anchored left: `DEV_HARNESS_URL` in the cloud worker is its own
        # variable, and matching the tail of it invented `HARNESS_URL` and
        # `HARNESS_KEY_PARAM` — names nothing reads and nothing can document.
        for name in re.findall(r"(?<![A-Z0-9_])HARNESS_[A-Z_]+", path.read_text(encoding="utf-8")):
            found.add(name)
    return found


# -- the registry is the reference -----------------------------------------


def test_chrome_debugging_defaults_off_in_runtime_and_settings(monkeypatch):
    from harness import cdp

    monkeypatch.delenv("HARNESS_CHROME_CDP", raising=False)
    assert cdp.enabled() is False
    assert BY_NAME["HARNESS_CHROME_CDP"].default is False
    monkeypatch.setenv("HARNESS_CHROME_CDP", "1")
    assert cdp.enabled() is True


def test_every_setting_the_code_reads_is_registered():
    """The registry is also the documentation, so a tunable missing from it is
    both unvalidated and undocumented. Adding one means describing it."""
    missing = sorted(
        name
        for name in _names_in_source()
        if name not in BY_NAME and not any(name.startswith(p) for p in NOT_TUNABLES)
    )
    assert not missing, (
        f"HARNESS_* names read by the code but absent from harness/settings.py: {missing}"
    )


def test_no_setting_is_registered_that_nothing_reads():
    """The other direction: a registry entry nobody reads is documentation for
    a feature that does not exist."""
    in_source = _names_in_source()
    stale = sorted(s.name for s in SETTINGS if s.name not in in_source)
    assert not stale, f"registered but never read: {stale}"


def test_every_setting_has_help():
    assert [s.name for s in SETTINGS if not s.help.strip()] == []


def test_describe_lists_them_all():
    text = describe()
    for setting in SETTINGS:
        assert setting.name in text


# -- validation ------------------------------------------------------------


def test_unset_is_always_fine():
    validate_environment({})


def test_a_bad_int_is_refused_and_names_itself():
    with pytest.raises(SettingError) as exc:
        validate_environment({"HARNESS_MACHINE_PIDS": "51x"})
    assert "HARNESS_MACHINE_PIDS" in str(exc.value)
    assert "51x" in str(exc.value)
    assert "512" in str(exc.value)  # tells you the default it would have used


def test_a_bad_float_is_refused():
    with pytest.raises(SettingError):
        validate_environment({"HARNESS_STREAM_DELAY": "fast"})


def test_a_bad_bool_is_refused_and_lists_the_accepted_words():
    with pytest.raises(SettingError) as exc:
        validate_environment({"HARNESS_AUTO_ROLL": "maybe"})
    assert "true" in str(exc.value)


@pytest.mark.parametrize("word", ["1", "true", "TRUE", "yes", "on", "0", "false", "no", "off"])
def test_the_conventional_bool_spellings_all_work(word):
    validate_environment({"HARNESS_AUTO_ROLL": word})


def test_a_negative_timeout_is_refused_rather_than_puzzling_at_runtime():
    with pytest.raises(SettingError):
        validate_environment({"HARNESS_RUN_WATCHDOG_SECS": "-5"})


def test_a_zero_pid_limit_is_refused():
    with pytest.raises(SettingError):
        validate_environment({"HARNESS_MACHINE_PIDS": "0"})


def test_an_empty_string_setting_is_refused_not_treated_as_unset():
    """Set-but-empty is almost always a broken shell expansion, and silently
    treating it as unset is how that goes unnoticed."""
    with pytest.raises(SettingError):
        validate_environment({"HARNESS_MACHINE_IMAGE": ""})


def test_a_good_value_passes():
    validate_environment(
        {
            "HARNESS_MACHINE_PIDS": "256",
            "HARNESS_STREAM_DELAY": "0.05",
            "HARNESS_AUTO_ROLL": "yes",
            "HARNESS_HOME": "/tmp/x",
        }
    )


# -- the CLI actually refuses ----------------------------------------------


def _cli(env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, **env_extra}
    return subprocess.run(
        [sys.executable, "-m", "harness", "roster"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_cli_refuses_to_start_on_a_bad_value():
    proc = _cli({"HARNESS_MACHINE_PIDS": "51x"})
    assert proc.returncode == 2
    assert "HARNESS_MACHINE_PIDS" in proc.stderr


def test_the_cli_starts_on_a_good_value():
    assert _cli({"HARNESS_MACHINE_PIDS": "256"}).returncode == 0
