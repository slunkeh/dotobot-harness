"""Operator-writable policy: deny before allow, fail closed, dry-run.

The design note these enforce is that a policy must never fail *open*. Every
malformed thing here has one correct answer and it is "refuse", except an
absent policy, which is a deliberate exception: not configuring a policy is a
choice, mistyping one is an accident, and they must not behave the same.
"""

from __future__ import annotations

import pytest

import agent.policy as policy
from harness.paths import HarnessPaths


def _ctx(**kw):
    base = {"tool": "run_command", "intent": "run_command", "target": "ls", "bot": "atlas"}
    base.update(kw)
    return base


# -- precedence ------------------------------------------------------------


def test_no_policy_permits_everything():
    """An existing install with no policy.toml behaves exactly as before."""
    assert policy.Policy().evaluate(_ctx()).allowed is True


def test_deny_beats_allow():
    pol = policy.parse({"deny": ["run_command"], "allow": ["*"]})
    decision = pol.evaluate(_ctx())
    assert decision.allowed is False
    assert decision.source == "deny"


def test_deny_only_leaves_everything_else_alone():
    pol = policy.parse({"deny": ["create_bot"]})
    assert pol.evaluate(_ctx(tool="run_command")).allowed is True
    assert pol.evaluate(_ctx(tool="create_bot")).allowed is False


def test_an_allow_list_makes_everything_else_refuse():
    pol = policy.parse({"allow": ["show_*"]})
    assert pol.evaluate(_ctx(tool="show_table")).allowed is True
    assert pol.evaluate(_ctx(tool="run_command")).allowed is False


# -- intent, which is the point --------------------------------------------


def test_one_intent_rule_closes_all_three_activation_doors():
    """A rule naming `computer_click` covers one of three activation paths;
    a rule naming the intent covers the door."""
    pol = policy.parse({"deny": [{"intent": "activate"}]})
    for tool in ("computer_click", "computer_key"):
        assert pol.evaluate(_ctx(tool=tool, intent="activate")).allowed is False
    assert pol.evaluate(_ctx(tool="computer_type", intent="type")).allowed is True


def test_target_globs_match_the_command():
    pol = policy.parse({"deny": [{"intent": "run_command", "target": "*rm -rf*"}]})
    assert pol.evaluate(_ctx(target="rm -rf /")).allowed is False
    assert pol.evaluate(_ctx(target="ls -la")).allowed is True


def test_matching_is_case_insensitive():
    pol = policy.parse({"deny": [{"target": "*RM *"}]})
    assert pol.evaluate(_ctx(target="rm foo")).allowed is False


def test_a_rule_can_scope_to_one_bot():
    pol = policy.parse({"deny": [{"tool": "run_command", "bot": "nova"}]})
    assert pol.evaluate(_ctx(bot="nova")).allowed is False
    assert pol.evaluate(_ctx(bot="atlas")).allowed is True


def test_a_rule_can_scope_to_exposed_turns():
    """`exposure = "web"` matches only after web content entered the turn, so
    an operator can rule on the tainted tail separately from the clean start."""
    pol = policy.parse({"deny": [{"intent": "run_command", "exposure": "web"}]})
    assert pol.evaluate(_ctx(exposure="web")).allowed is False
    assert pol.evaluate(_ctx(exposure="")).allowed is True
    assert pol.evaluate(_ctx()).allowed is True  # absent field, same as empty


# -- failing closed --------------------------------------------------------


def test_a_broken_policy_refuses_everything():
    assert policy.broken("bad file").evaluate(_ctx()).allowed is False


def test_a_deny_rule_that_raises_denies():
    class Exploding(policy.Rule):
        def matches(self, ctx):
            raise RuntimeError("boom")

    pol = policy.Policy(deny=(Exploding(source="x"),))
    decision = pol.evaluate(_ctx())
    assert decision.allowed is False
    assert decision.source == "deny"


def test_an_allow_rule_that_raises_does_not_permit():
    class Exploding(policy.Rule):
        def matches(self, ctx):
            raise RuntimeError("boom")

    pol = policy.Policy(allow=(Exploding(source="x"),))
    assert pol.evaluate(_ctx()).allowed is False


@pytest.mark.parametrize(
    "raw",
    [
        {"mode": "audit"},
        {"deny": [{"nope": "x"}]},
        {"deny": [{}]},
        {"deny": [123]},
        {"deny": [""]},
        {"unknown_key": 1},
        "not a table",
    ],
)
def test_bad_policy_is_refused_by_name(raw):
    """Refused at parse time, named — not accepted and silently never matched,
    which is the failure mode `harness/routines.py` shipped with."""
    with pytest.raises(policy.PolicyError):
        policy.parse(raw)


# -- dry-run ---------------------------------------------------------------


def test_dry_run_permits_what_it_would_have_refused_and_says_so():
    pol = policy.parse({"mode": "dry-run", "deny": ["run_command"]})
    decision = pol.evaluate(_ctx())
    assert decision.allowed is True
    assert decision.decision == "dry-run"
    assert decision.rule == "run_command"


def test_dry_run_also_covers_the_default_refusal():
    pol = policy.parse({"mode": "dry-run", "allow": ["show_*"]})
    assert pol.evaluate(_ctx(tool="run_command")).allowed is True


# -- loading ---------------------------------------------------------------


def test_absent_file_is_an_empty_policy(tmp_path):
    assert policy.load(HarnessPaths.resolve(tmp_path)).evaluate(_ctx()).allowed is True


def test_unparseable_file_refuses_everything(tmp_path):
    paths = HarnessPaths.resolve(tmp_path)
    paths.home.mkdir(parents=True, exist_ok=True)
    policy.policy_path(paths).write_text("this is not = = toml")
    pol = policy.load(paths)
    assert pol.evaluate(_ctx()).allowed is False
    assert "policy.toml" in pol.broken


def test_a_bad_rule_in_a_real_file_refuses_and_names_the_file(tmp_path):
    paths = HarnessPaths.resolve(tmp_path)
    paths.home.mkdir(parents=True, exist_ok=True)
    policy.policy_path(paths).write_text('mode = "shout"\n')
    pol = policy.load(paths)
    assert pol.evaluate(_ctx()).allowed is False
    assert "mode" in pol.broken


def test_per_bot_table_replaces_the_global_policy(tmp_path):
    paths = HarnessPaths.resolve(tmp_path)
    paths.home.mkdir(parents=True, exist_ok=True)
    policy.policy_path(paths).write_text(
        'deny = ["run_command"]\n\n[bots.nova]\ndeny = ["create_bot"]\n'
    )
    atlas = policy.load(paths, "atlas")
    nova = policy.load(paths, "nova")
    assert atlas.evaluate(_ctx(tool="run_command")).allowed is False
    # nova's table replaces rather than merges: run_command is not in it
    assert nova.evaluate(_ctx(tool="run_command")).allowed is True
    assert nova.evaluate(_ctx(tool="create_bot")).allowed is False


def test_ensure_suggested_policy_writes_once_and_asks(tmp_path):
    paths = HarnessPaths.resolve(tmp_path)
    paths.home.mkdir(parents=True, exist_ok=True)
    written = policy.ensure_suggested_policy(paths)
    assert written is not None and written.is_file()
    assert policy.ensure_suggested_policy(paths) is None
    pol = policy.load(paths)
    secret = pol.evaluate(_ctx(tool="computer_type_secret", intent="type_secret"))
    assert secret.ask is True
    web_shell = pol.evaluate(_ctx(tool="run_command", intent="run_command", exposure="web"))
    assert web_shell.ask is True
    unattended = pol.evaluate(
        _ctx(
            tool="run_command",
            intent="run_command",
            exposure="web",
            origin="routine",
        )
    )
    assert unattended.allowed is False
    plain = pol.evaluate(_ctx(tool="run_command", intent="run_command"))
    assert plain.allowed is True
    assert plain.ask is False
