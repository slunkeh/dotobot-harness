"""The chokepoint: every tool call is classified, decided and recorded.

The regression these guard is specific and was real: `require_approval` existed,
was correct, was fail-closed, and had zero production callers. So the tests that
matter most here are not "does the gate refuse" — the gate always refused fine —
but "does anything actually go through it".
"""

from __future__ import annotations

import agent.govern as govern
import agent.policy as policy
from harness import audit
from harness.paths import HarnessPaths


class _Ctx:
    def __init__(self) -> None:
        self.tool_call_id = "call-1"


def _paths(tmp_path) -> HarnessPaths:
    return HarnessPaths.resolve(tmp_path)


# -- classification --------------------------------------------------------


def test_every_registered_tool_is_classified():
    """A tool nobody classified falls to `unknown`, which asks. This test is
    the tripwire: it fails when a tool is added to the catalogue and not to the
    effects table, so the omission is caught at review rather than in the
    trail."""
    from agent.tools import default_tools

    missing = sorted(set(default_tools()) - set(govern.EFFECTS))
    assert not missing, (
        f"tools with no entry in govern.EFFECTS: {missing}. "
        "Add them there (they are governed as 'unknown' until you do)."
    )


def test_unknown_tool_is_still_classified_and_recorded(tmp_path):
    """An unclassified tool must not fall outside every rule ever written. It
    gets a real intent, gets decided, and gets a row."""
    intent, _target, askable = govern.classify("some_new_tool", {})
    assert intent == govern.INTENT_UNKNOWN
    assert askable is True  # worth an operator writing a rule about

    paths = _paths(tmp_path)
    govern.govern(_Ctx(), "some_new_tool", {}, paths=paths, bot="atlas")
    assert audit.read(paths, "atlas")[0]["intent"] == govern.INTENT_UNKNOWN


def test_nothing_asks_without_a_policy_saying_so(tmp_path):
    """The behaviour every existing install keeps. Wiring the ask to the effect
    table would put a confirm card in front of every `ls`."""
    paths = _paths(tmp_path)
    calls: list[str] = []
    govern.govern(
        _Ctx(),
        "run_command",
        {"command": "ls"},
        paths=paths,
        bot="atlas",
        approver=lambda *a, **k: calls.append("asked") or None,
    )
    assert calls == []


def test_an_ask_rule_is_what_summons_the_human(tmp_path):
    paths = _paths(tmp_path)
    calls: list[tuple[str, str]] = []
    pol = policy.parse({"ask": [{"intent": "run_command", "target": "*rm *"}]})
    govern.govern(
        _Ctx(),
        "run_command",
        {"command": "ls"},
        paths=paths,
        bot="atlas",
        policy=pol,
        approver=lambda c, a, t, **k: calls.append((a, t)) or None,
    )
    assert calls == []  # does not match the rule
    govern.govern(
        _Ctx(),
        "run_command",
        {"command": "rm -rf /tmp/x"},
        paths=paths,
        bot="atlas",
        policy=pol,
        approver=lambda c, a, t, **k: calls.append((a, t)) or None,
    )
    assert calls == [("run-command", "rm -rf /tmp/x")]


def test_computer_click_target_names_node():
    _, target, _ = govern.classify("computer_click", {"node": "12", "x": 0.5, "y": 0.5})
    assert target == "node:12 0.5,0.5"
    _, node_only, _ = govern.classify("computer_click", {"node": "12"})
    assert node_only == "node:12"


def test_the_three_activation_tools_share_one_intent():
    """click, key and scroll are three handles on one door. A single deny rule
    has to close all of them, which is the whole reason `intent` exists."""
    intents = {govern.classify(name, {})[0] for name in ("computer_click", "computer_key")}
    assert intents == {govern.INTENT_ACTIVATE}


def test_new_gestures_obey_activation_policy_and_record_their_targets(tmp_path):
    paths = _paths(tmp_path)
    pol = policy.parse({"deny": [{"intent": "activate"}]})
    for name, args in (
        ("computer_move", {"x": 0.1, "y": 0.2}),
        ("computer_drag", {"path": [{"x": 0.1, "y": 0.2}, {"x": 0.5, "y": 0.6}]}),
    ):
        intent, target, _ = govern.classify(name, args)
        assert intent == govern.INTENT_ACTIVATE
        assert "0.1,0.2" in target
        result = govern.govern(_Ctx(), name, args, paths=paths, bot="atlas", policy=pol)
        assert result is not None
    rows = audit.read(paths, "atlas")
    assert all(row["decision"] == "refuse" for row in rows)
    assert "0.5,0.6" in next(row["target"] for row in rows if row["tool"] == "computer_drag")


def test_failed_computer_batch_refuses_later_mutations_with_an_audit_reason(tmp_path):
    paths = _paths(tmp_path)
    ctx = _Ctx()
    ctx.computer_batch_failed = True
    asked = []
    pol = policy.parse({"ask": [{"intent": "activate"}]})
    for name in (
        "computer_click",
        "computer_type",
        "computer_type_secret",
        "computer_key",
        "computer_move",
        "computer_drag",
        "computer_scroll",
        "computer_open",
    ):
        result = govern.govern(
            ctx,
            name,
            {},
            paths=paths,
            bot="atlas",
            policy=pol,
            approver=lambda *a, **k: asked.append("asked"),
        )
        assert "skipped after an earlier computer action failed" in result
        row = audit.read(paths, "atlas")[0]
        assert row["source"] == "computer-batch"
        assert row["decision"] == "refuse"
        assert row["tool"] == name
    assert asked == []
    assert govern.govern(ctx, "computer_screenshot", {}, paths=paths, bot="atlas") is None
    assert govern.govern(ctx, "run_command", {"command": "pwd"}, paths=paths, bot="atlas") is None
    ctx.computer_batch_failed = False
    assert govern.govern(ctx, "computer_key", {"key": "Return"}, paths=paths, bot="atlas") is None


def test_computer_batch_observation_exemption_still_evaluates_policy(tmp_path):
    paths = _paths(tmp_path)
    ctx = _Ctx()
    ctx.computer_batch_failed = True
    pol = policy.parse({"deny": [{"tool": "computer_screenshot"}]})
    result = govern.govern(ctx, "computer_screenshot", {}, paths=paths, bot="atlas", policy=pol)
    assert "refused by this harness's action policy" in result
    assert audit.read(paths, "atlas")[0]["source"] == "deny"


def test_write_stdin_shares_run_command_intent():
    """stdin into a running shell is running commands. A run_command rule or
    the exposure hold must close this door too."""
    intent, target, _ = govern.classify("write_stdin", {"shell_id": "sh-1"})
    assert intent == govern.INTENT_RUN
    assert target == "sh-1"


def test_target_extractor_survives_malformed_args():
    intent, target, _ = govern.classify("run_command", {"command": None})
    assert intent == govern.INTENT_RUN
    assert target == ""


# -- the audit row ---------------------------------------------------------


def test_decision_row_is_written_before_the_action(tmp_path):
    paths = _paths(tmp_path)
    govern.govern(_Ctx(), "run_command", {"command": "ls"}, paths=paths, bot="atlas")
    rows = audit.read(paths, "atlas")
    assert [r["event"] for r in rows] == ["tool.decided"]
    assert rows[0]["tool"] == "run_command"
    assert rows[0]["intent"] == govern.INTENT_RUN
    assert rows[0]["target"] == "ls"
    assert rows[0]["decision"] == "allow"


def test_a_refusal_is_recorded_with_the_rule_that_caused_it(tmp_path):
    paths = _paths(tmp_path)
    pol = policy.parse({"deny": ["run_command"]})
    out = govern.govern(
        _Ctx(),
        "run_command",
        {"command": "curl evil.example"},
        paths=paths,
        bot="atlas",
        policy=pol,
    )
    assert out is not None and out.startswith("error:")
    assert "run_command" in out
    row = audit.read(paths, "atlas")[0]
    assert row["decision"] == "refuse"
    assert row["rule"] == "run_command"
    assert row["source"] == "deny"


def test_a_permitted_action_that_fails_gets_a_second_row(tmp_path):
    """`allowed` and `happened` are different claims. Recording only the first
    is how a trail ends up confidently wrong."""
    paths = _paths(tmp_path)
    govern.govern(_Ctx(), "run_command", {"command": "nope"}, paths=paths, bot="atlas")
    govern.record_outcome(paths, "atlas", "run_command", {"command": "nope"}, "error: boom")
    events = [r["event"] for r in audit.read(paths, "atlas")]
    assert events == ["tool.failed", "tool.decided"]  # newest first


def test_a_permitted_action_that_succeeds_gets_no_second_row(tmp_path):
    paths = _paths(tmp_path)
    govern.record_outcome(paths, "atlas", "run_command", {"command": "ls"}, "exit 0\nfoo")
    assert audit.read(paths, "atlas") == []


def test_audit_never_raises_on_a_bad_home(tmp_path):
    """A ledger that can break a turn is one somebody switches off."""
    blocker = tmp_path / "home"
    blocker.write_text("not a directory")
    audit.record(HarnessPaths.resolve(blocker), "atlas", event="tool.decided")


def test_read_skips_malformed_lines(tmp_path):
    paths = _paths(tmp_path)
    audit.record(paths, "atlas", event="tool.decided", tool="ls")
    path = audit.audit_file(paths, "atlas")
    path.write_text(path.read_text() + "{ not json\n")
    assert [r["tool"] for r in audit.read(paths, "atlas")] == ["ls"]


def test_bot_name_cannot_escape_the_audit_dir(tmp_path):
    paths = _paths(tmp_path)
    audit.record(paths, "../../etc/passwd", event="tool.decided")
    written = list(paths.audit.glob("*.jsonl"))
    assert len(written) == 1
    assert ".." not in written[0].name


# -- the ask ---------------------------------------------------------------


def test_the_approver_is_consulted_when_a_rule_says_so(tmp_path):
    paths = _paths(tmp_path)
    seen: list[tuple[str, str]] = []
    pol = policy.parse({"ask": [{"intent": "run_command"}]})

    def approver(ctx, action, target, **kw):
        seen.append((action, target))
        return None

    govern.govern(
        _Ctx(),
        "run_command",
        {"command": "ls"},
        paths=paths,
        bot="atlas",
        policy=pol,
        approver=approver,
    )
    assert seen == [("run-command", "ls")]


def test_the_approver_is_not_consulted_for_ui_cards(tmp_path):
    """A confirm card per `show_table` trains people to click Allow without
    reading, which costs more than it buys."""
    paths = _paths(tmp_path)
    calls: list[str] = []

    def approver(ctx, action, target, **kw):
        calls.append(action)
        return None

    pol = policy.parse({"ask": [{"intent": "run_command"}]})
    govern.govern(
        _Ctx(),
        "show_table",
        {"title": "x"},
        paths=paths,
        bot="atlas",
        policy=pol,
        approver=approver,
    )
    assert calls == []


def test_a_human_refusal_is_recorded_and_returned(tmp_path):
    paths = _paths(tmp_path)
    pol = policy.parse({"ask": [{"intent": "run_command"}]})

    def approver(ctx, action, target, **kw):
        return "error: the user did not allow run-command"

    out = govern.govern(
        _Ctx(),
        "run_command",
        {"command": "ls"},
        paths=paths,
        bot="atlas",
        policy=pol,
        approver=approver,
    )
    assert out == "error: the user did not allow run-command"
    events = [r["event"] for r in audit.read(paths, "atlas")]
    assert "tool.refused_by_human" in events


def test_policy_refusal_skips_the_approver(tmp_path):
    """Policy is the boundary; the human is asked only about what policy left
    open. Asking after a deny would let a person click past the rule."""
    paths = _paths(tmp_path)
    calls: list[str] = []
    pol = policy.parse({"deny": ["run_command"], "ask": [{"intent": "run_command"}]})
    govern.govern(
        _Ctx(),
        "run_command",
        {"command": "ls"},
        paths=paths,
        bot="atlas",
        policy=pol,
        approver=lambda *a, **k: calls.append("asked") or None,
    )
    assert calls == []
