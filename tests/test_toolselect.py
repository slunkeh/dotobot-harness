"""Per-run tool narrowing, and the rule that it must never fail closed.

34 built-in tools ship before a connector is enabled, and a bot with GitHub and
Linear connected is past seventy — long enough that the choosing becomes the
failure. But narrowing is not a boundary: `agent/govern.py` decides what may be
called, this decides only what the model sees. So a boundary fails closed and
this fails OPEN, and most of what is below pins that down.
"""

from __future__ import annotations

import agent.toolselect as ts


class _Skill:
    def __init__(self, name, when_to_use="", tools=(), description=""):
        self.name = name
        self.when_to_use = when_to_use
        self.description = description
        self.tools = tools if isinstance(tools, str) else list(tools)


def _many(n=30):
    return [f"tool_{i}" for i in range(n)]


# -- it narrows ------------------------------------------------------------


def test_harness_tips_only_matches_an_actual_ask():
    skill = _Skill(
        "harness-tips",
        'user asks for tips, onboarding, "what can you do", or /harness-tips',
        description="Product tour: what the harness can do and how to try each feature",
    )
    assert ts.matching_skills([skill], "nova") == []
    assert ts.matching_skills([skill], "PayPal invoices") == []
    assert ts.matching_skills([skill], "try that again") == []
    assert ts.matching_skills([skill], "what can you do") == [skill]
    assert ts.matching_skills([skill], "any tips?") == [skill]
    assert ts.matching_skills([skill], "/harness-tips") == [skill]


def test_learn_from_demonstration_matches_only_when_named():
    """Its when_to_use talks about chat/skill/recording; those must not inject it."""
    skill = _Skill(
        "learn-from-demonstration",
        "a teach recording has finished, or ordinary chat, or an existing skill",
        ["propose_skill"],
        description="Turn a screen-recorded demonstration into a reusable skill",
    )
    assert ts.matching_skills([skill], "what's on the screen") == []
    assert ts.matching_skills([skill], "save this as a skill") == []
    assert ts.matching_skills([skill], "ordinary chat please") == []
    assert ts.matching_skills([skill], "/learn-from-demonstration") == [skill]
    assert ts.matching_skills([skill], "The recording is finished. /learn-from-demonstration") == [
        skill
    ]


def test_naming_the_skill_in_the_message_matches():
    granted = [*_many(), "run_command", "computer_click"]
    skills = [
        _Skill("check-amazon-orders", "when checking seller orders", ["run_command"]),
        _Skill("browse", "when asked to open a page", ["computer_click"]),
    ]
    text = "Ok use check-amazon-orders now so i can see"
    sel = ts.select(granted, skills, text)
    assert sel.narrowed
    assert sel.skills == ("check-amazon-orders",)
    assert "computer_click" not in sel.tools
    matched = ts.matching_skills(skills, text)
    assert [s.name for s in matched] == ["check-amazon-orders"]


def test_a_matching_skill_narrows_the_offer():
    granted = [*_many(), "run_command", "computer_click"]
    skills = [
        _Skill("deploy", "when asked to deploy or ship", ["run_command"]),
        _Skill("browse", "when asked to open a page", ["computer_click"]),
    ]
    sel = ts.select(granted, skills, "please deploy the service")
    assert sel.narrowed
    assert "run_command" in sel.tools
    assert "computer_click" not in sel.tools  # claimed by a skill that did not match
    assert sel.skills == ("deploy",)


def test_tools_no_skill_claims_always_stay_on_the_menu():
    """The half that keeps narrowing from removing capability."""
    granted = [*_many(), "run_command"]
    skills = [_Skill("deploy", "when asked to deploy", ["run_command"])]
    sel = ts.select(granted, skills, "please deploy")
    for name in _many():
        assert name in sel.tools


def test_a_declaration_cannot_grant_a_tool_the_bot_lacks():
    """Writing a skill must never hand anybody a tool. The offer is always
    intersected with what was already granted."""
    granted = [*_many(), "run_command"]
    skills = [_Skill("deploy", "when asked to deploy", ["run_command", "launch_missiles"])]
    sel = ts.select(granted, skills, "please deploy")
    assert "launch_missiles" not in sel.tools


# -- every failure is open -------------------------------------------------


def test_no_skill_declares_tools_offers_everything():
    """Every deployment, on day one."""
    granted = _many()
    sel = ts.select(granted, [_Skill("x", "when asked about x")], "anything")
    assert sel.reason == "no-skill-tools"
    assert list(sel.tools) == granted


def test_a_small_catalogue_is_left_alone():
    granted = ["a", "b", "c"]
    skills = [_Skill("deploy", "when asked to deploy", ["a"])]
    sel = ts.select(granted, skills, "deploy")
    assert sel.reason == "below-floor"
    assert list(sel.tools) == granted


def test_a_message_matching_nothing_offers_everything():
    granted = [*_many(), "run_command"]
    skills = [_Skill("deploy", "when asked to deploy", ["run_command"])]
    sel = ts.select(granted, skills, "tell me a joke")
    assert sel.reason == "no-match"
    assert list(sel.tools) == granted


def test_a_skill_naming_only_unknown_tools_offers_everything():
    """A typo, or a connector since disabled, must not make a real tool vanish
    by shrinking the 'unclaimed' set."""
    granted = _many()
    skills = [_Skill("deploy", "when asked to deploy", ["no_such_tool"])]
    sel = ts.select(granted, skills, "deploy")
    assert sel.reason == "no-skill-tools"
    assert list(sel.tools) == granted


def test_anything_raising_offers_everything():
    class Exploding:
        name = "boom"

        @property
        def tools(self):
            raise RuntimeError("boom")

    granted = _many()
    sel = ts.select(granted, [Exploding()], "deploy")
    assert sel.reason == "error"
    assert list(sel.tools) == granted


def test_no_skills_at_all_offers_everything():
    granted = _many()
    assert list(ts.select(granted, [], "anything").tools) == granted


def test_an_empty_message_offers_everything():
    granted = [*_many(), "run_command"]
    skills = [_Skill("deploy", "when asked to deploy", ["run_command"])]
    assert ts.select(granted, skills, "").reason in ("no-match", "no-skill-tools")


# -- the matcher -----------------------------------------------------------


def test_matching_ignores_common_words():
    """'the', 'a', 'is' must not make every skill match every message."""
    granted = [*_many(), "run_command"]
    skills = [_Skill("deploy", "use this when the user wants to deploy", ["run_command"])]
    assert ts.select(granted, skills, "what is the weather").reason == "no-match"


def test_matching_is_case_insensitive():
    granted = [*_many(), "run_command", "computer_click"]
    skills = [
        _Skill("deploy", "when asked to deploy", ["run_command"]),
        _Skill("browse", "when asked to open a webpage", ["computer_click"]),
    ]
    sel = ts.select(granted, skills, "please DEPLOY it")
    assert sel.narrowed
    assert "computer_click" not in sel.tools


def test_the_skill_name_itself_is_a_match():
    granted = [*_many(), "run_command", "computer_click"]
    skills = [
        _Skill("changelog", "", ["run_command"]),
        _Skill("browse", "when asked to open a webpage", ["computer_click"]),
    ]
    sel = ts.select(granted, skills, "update the changelog")
    assert sel.narrowed
    assert sel.skills == ("changelog",)


def test_nothing_removed_reports_no_match_rather_than_a_fake_narrowing():
    """When every claimed tool belongs to a skill that matched, the offer is
    the whole catalogue — and saying "narrowed" would put a misleading row in
    the audit trail."""
    granted = [*_many(), "run_command"]
    skills = [_Skill("deploy", "when asked to deploy", ["run_command"])]
    sel = ts.select(granted, skills, "please deploy")
    assert sel.reason == "no-match"
    assert list(sel.tools) == granted


# -- declarations ----------------------------------------------------------


def test_declared_tools_accepts_a_comma_or_space_list():
    assert ts.declared_tools(_Skill("x", tools="a, b c")) == ("a", "b", "c")
    assert ts.declared_tools(_Skill("x", tools=["a", "b"])) == ("a", "b")


def test_a_skill_with_no_tools_key_declares_nothing():
    class Bare:
        name = "bare"

    assert ts.declared_tools(Bare()) == ()


# -- applying it -----------------------------------------------------------


def test_apply_keeps_catalogue_order():
    tools = {"a": 1, "b": 2, "c": 3}
    sel = ts.Selection(("c", "a"), 3, "narrowed")
    assert list(ts.apply(tools, sel)) == ["a", "c"]


def test_apply_is_a_no_op_when_nothing_narrowed():
    tools = {"a": 1, "b": 2}
    assert ts.apply(tools, ts.Selection(("a",), 2, "below-floor")) == tools


# -- the frontmatter key ---------------------------------------------------


def test_skill_md_carries_a_tools_key(tmp_path):
    from agent.skills import Skill

    folder = tmp_path / "deploy"
    folder.mkdir()
    path = folder / "SKILL.md"
    path.write_text(
        "---\nname: deploy\ndescription: ship it\nwhen_to_use: when deploying\n"
        "tools: run_command, read_terminal\n---\nBody here.\n",
        encoding="utf-8",
    )
    skill = Skill.from_file(path, "shared")
    assert skill.tools == ["run_command", "read_terminal"]
    assert skill.name == "deploy"


def test_a_skill_without_a_tools_key_still_loads(tmp_path):
    from agent.skills import Skill

    folder = tmp_path / "plain"
    folder.mkdir()
    path = folder / "SKILL.md"
    path.write_text("---\nname: plain\ndescription: d\n---\nBody.\n", encoding="utf-8")
    assert Skill.from_file(path, "shared").tools == []
