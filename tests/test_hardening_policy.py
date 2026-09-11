"""Hardening of the policy parser and the shell interlock.

Two fail-open holes in controls that promise to fail closed:

* `agent/policy.py` stringified a list/bool/int rule field into a glob no
  real value ever equals, so `deny = [{ intent = ["run_command",
  "type_secret"] }]` parsed cleanly and never fired.
* `agent/shellguard.py` split commands only on `;` `|` `&` (and its
  `&&`/`||` branches were dead: shlex emits those as single tokens), so a
  newline, a subshell, a brace group, a reserved word or `sh -c "…"` hid
  `rm -rf /` from the verb check.

Every test here fails on the pre-fix code and pins the ordinary inputs
that must keep working.
"""

from __future__ import annotations

import pytest

import agent.govern as govern
import agent.policy as policy
import agent.shellguard as shellguard
from harness import audit
from harness.paths import HarnessPaths


class _Ctx:
    def __init__(self) -> None:
        self.tool_call_id = "call-1"


def _ctx(**kw):
    base = {"tool": "run_command", "intent": "run_command", "target": "ls", "bot": "atlas"}
    base.update(kw)
    return base


def _hit(command: str, **kw):
    return shellguard.inspect(command, home="/home/agent", cwd="/home/agent/work", **kw)


# -- policy: a non-string field refuses by name ----------------------------


@pytest.mark.parametrize(
    "raw, field",
    [
        ({"deny": [{"intent": ["run_command", "type_secret"]}]}, "intent"),
        ({"deny": [{"tool": True}]}, "tool"),
        ({"deny": [{"target": 7}]}, "target"),
        ({"ask": [{"intent": ["type_secret"]}]}, "intent"),
        ({"allow": [{"tool": "show_*", "exposure": []}]}, "exposure"),
        ({"allow": [{"tool": "show_*", "origin": False}]}, "origin"),
        ({"deny": [{"intent": "run_command", "bot": {"name": "nova"}}]}, "bot"),
        ({"deny": [{"intent": ""}]}, "intent"),
        ({"deny": [{"intent": "run_command", "exposure": "  "}]}, "exposure"),
    ],
)
def test_non_string_rule_field_is_refused_and_named(raw, field):
    """Pre-fix: `str(["run_command", "type_secret"])` became the glob and
    parse() succeeded, so the deny silently never matched."""
    with pytest.raises(policy.PolicyError) as exc:
        policy.parse(raw)
    assert field in str(exc.value)


def test_list_valued_deny_cannot_fail_open():
    """The exploit shape end to end: an operator writes one deny naming both
    intents as a list. Pre-fix the rule parsed and `run_command` was allowed
    with source=default; now the parse refuses."""
    with pytest.raises(policy.PolicyError):
        policy.parse({"deny": [{"intent": ["run_command", "type_secret"]}]})


def test_list_valued_field_in_a_real_file_yields_a_broken_policy(tmp_path):
    paths = HarnessPaths.resolve(tmp_path)
    paths.home.mkdir(parents=True, exist_ok=True)
    policy.policy_path(paths).write_text(
        'deny = [{ intent = ["run_command", "type_secret"] }]\n', encoding="utf-8"
    )
    pol = policy.load(paths)
    decision = pol.evaluate(_ctx(intent="run_command"))
    assert decision.allowed is False
    assert "intent" in pol.broken
    assert "policy.toml" in pol.broken


def test_falsy_field_on_an_allow_rule_does_not_widen_the_grant():
    """`exposure = []` coerced to "" and dropped the constraint, turning
    `allow show_* only on the clean start` into `allow show_* always`."""
    with pytest.raises(policy.PolicyError):
        policy.parse({"allow": [{"tool": "show_*", "exposure": []}]})


def test_string_rule_fields_parse_exactly_as_before():
    pol = policy.parse(
        {
            "mode": "enforce",
            "deny": [{"intent": "run_command", "exposure": "web", "origin": "routine"}],
            "ask": [{"intent": "type_secret"}, "create_bot"],
            "allow": [{"bot": "*"}],
        }
    )
    assert pol.evaluate(_ctx(exposure="web", origin="routine")).allowed is False
    assert pol.evaluate(_ctx(tool="computer_type_secret", intent="type_secret")).ask is True
    assert pol.evaluate(_ctx(tool="create_bot")).ask is True
    assert pol.evaluate(_ctx()).allowed is True


def test_suggested_policy_still_parses():
    import tomllib

    pol = policy.parse(tomllib.loads(policy.SUGGESTED_POLICY))
    assert pol.evaluate(_ctx(tool="computer_type_secret", intent="type_secret")).ask is True


def test_absent_field_is_still_a_wildcard():
    pol = policy.parse({"deny": [{"intent": "run_command"}]})
    assert pol.evaluate(_ctx(exposure="web")).allowed is False
    assert pol.evaluate(_ctx()).allowed is False


# -- shellguard: compound commands are split the way sh reads them ---------


@pytest.mark.parametrize(
    "cmd",
    [
        "true && rm -rf /",
        "true || rm -rf /",
        "true\nrm -rf /",
        "echo hi\nrm -rf ~",
        "true\r\nrm -rf /",
        "echo cleaning\nrm -rf $HOME",
        "echo ok\n\n  rm -rf /home/agent",
        "(rm -rf /)",
        "true && (rm -rf /)",
        "{ rm -rf /; }",
        "{rm -rf /;}",
        "if true; then rm -rf /; fi",
        "while true; do rm -rf /; done",
        "! rm -rf /",
        "exec rm -rf /",
        "exec -a cleanup rm -rf /",
        'sh -c "rm -rf /"',
        "bash -c 'rm -rf ~'",
        "bash -lc 'echo start; rm -rf /'",
        'busybox sh -c "rm -rf /"',
        "sh -c \"sh -c 'rm -rf /'\"",
        "rm -rf \\\n/",
        "echo a |& rm -rf /",
        "case x in *) rm -rf /;; esac",
        "rm -rf / > /dev/null 2>&1",
        "true\nfind / -delete",
        "true && find ~ -delete",
        "(find /home/agent -delete)",
    ],
)
def test_compound_rm_rf_root_is_blocked(cmd):
    hit = _hit(cmd)
    assert hit is not None, cmd
    assert hit.command in {"rm", "find"}


def test_newline_reaches_extra_sensitive_paths():
    """$HARNESS_HOME on the process backend is only protected by this
    scanner; a two-liner must not walk past it either."""
    hit = shellguard.inspect(
        "echo cleaning\nrm -rf /tmp/harness-home",
        home="/Users/op",
        cwd="/tmp/harness-home/workspace",
        extra_sensitive=("/tmp/harness-home",),
    )
    assert hit is not None
    assert hit.path == "/tmp/harness-home"


@pytest.mark.parametrize(
    "cmd",
    [
        "ls -la /",
        "git status && git log --oneline",
        "ls | grep x >> out.txt",
        "find . -name '*.pyc' -delete",
        "find /tmp -delete",
        "rm -rf ./build 2>/dev/null",
        "rm -rf ./build >/dev/null 2>&1",
        "rm -rf ~/Downloads/old\nls ~/Downloads",
        "cd /tmp && rm -rf build",
        "if [ -d build ]; then rm -rf build; fi",
        "(cd /tmp && rm -rf scratch)",
        "{ rm -rf /tmp/x; rm -rf /tmp/y; }",
        'sh -c "ls /"',
        "bash -c 'rm -rf /tmp/build'",
        "bash deploy.sh",
        'echo "hello\nworld"',
        "echo rm -rf /",
        'echo "(" foo',
        "grep -rn 'rm -rf /' .",
        "python3 -m pytest -q",
        "rm file.txt",
        "rm /home/agent/.bashrc",
        "rm -rf /home/agent/Downloads/old",
        "make clean && make",
    ],
)
def test_ordinary_compound_commands_still_pass(cmd):
    assert _hit(cmd) is None, cmd


def test_redirect_target_is_not_an_rm_operand():
    """`2>/dev/null` used to be read as `rm -rf /dev/null` and refused an
    ordinary silenced delete; the redirection word is not an operand."""
    assert _hit("rm -rf ./build 2>/dev/null") is None
    assert _hit("rm -rf /dev/null") is not None  # a real operand still is


def test_inspect_still_never_raises_on_garbage():
    for cmd in ("rm 'unterminated\nrm -rf /", "sh -c", "sh -c ''", "(((", "}{", "\n\n", "rm \\"):
        shellguard.inspect(cmd, home="/home/agent")


def test_nested_sh_c_unwrapping_is_bounded():
    deep = "rm -rf /"
    for _ in range(shellguard._MAX_SHELL_DEPTH + 2):
        deep = "sh -c " + repr(deep)
    assert shellguard.inspect(deep, home="/home/agent") is None  # documented residual
    shallow = "sh -c " + repr("sh -c " + repr("rm -rf /"))
    assert shellguard.inspect(shallow, home="/home/agent") is not None


# -- through the gate --------------------------------------------------------


def _paths(tmp_path) -> HarnessPaths:
    return HarnessPaths.resolve(tmp_path)


@pytest.mark.parametrize(
    "command",
    ["echo cleaning\nrm -rf ~", "true && rm -rf /", "(rm -rf /)", 'sh -c "rm -rf /"'],
)
def test_run_command_compound_rm_rf_is_refused_without_a_policy(tmp_path, command):
    paths = _paths(tmp_path)
    out = govern.govern(
        _Ctx(),
        "run_command",
        {"command": command},
        paths=paths,
        bot="atlas",
    )
    assert out is not None and out.startswith("error:")
    row = audit.read(paths, "atlas")[0]
    assert row["decision"] == "refuse"
    assert row["source"] == "shell-guard"


def test_stdin_two_liner_uses_the_same_door(tmp_path):
    paths = _paths(tmp_path)
    stdin = govern.govern(
        _Ctx(),
        "write_stdin",
        {"shell_id": "1", "chars": "echo\nrm -rf /\n"},
        paths=paths,
        bot="atlas",
    )
    assert stdin is not None


def test_run_command_ordinary_two_liner_still_runs(tmp_path):
    paths = _paths(tmp_path)
    out = govern.govern(
        _Ctx(),
        "run_command",
        {"command": "cd /tmp\nrm -rf /tmp/build && make"},
        paths=paths,
        bot="atlas",
    )
    assert out is None
    assert audit.read(paths, "atlas")[0]["decision"] == "allow"
