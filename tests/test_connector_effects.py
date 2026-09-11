"""Read or write, per connector tool — and the asymmetry that makes it useful.

An operator thinks "nothing may change anything in Linear", not "nothing may
call linear_create_issue, linear_update_issue, linear_comment and
linear_attach_files". Without an effect, every one of those looked identical to
`agent/govern.py` and identical to a tool nobody had ever seen.

The asymmetry is the part that is easy to get backwards:

* a **curated** connector's write list is authoritative, because somebody read
  its tools — anything not on it is a read;
* a **custom** server's tools nobody reviewed, so anything not positively
  recognised as a read is a write.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import agent.govern as govern
from connectors.effects import CURATED_WRITES, EFFECT_READ, EFFECT_WRITE, classify, curated

ROOT = Path(__file__).resolve().parent.parent


# -- curated connectors ----------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "github_create_issue",
        "github_comment",
        "github_merge_pull",
        "linear_create_issue",
        "linear_comment",
        "gmail_create_draft",
        "gmail_work_create_draft",
        "gmail_send",
        "gmail_send_draft",
        "gmail_work_send",
    ],
)
def test_curated_writes_are_writes(name):
    assert classify(name) == EFFECT_WRITE


@pytest.mark.parametrize(
    "name",
    [
        "github_list_repos",
        "github_get_issue",
        "github_list_pulls",
        "linear_list_teams",
        "linear_search_issues",
        "gmail_search_threads",
        "gmail_get_thread",
        "gmail_read_attachment",
        "gmail_work_read_attachment",
        "gmail_work_search_threads",
    ],
)
def test_everything_else_a_curated_connector_offers_is_a_read(name):
    """We looked, so absence from the write list means something here."""
    assert classify(name) == EFFECT_READ


def test_a_curated_tool_with_an_unhelpful_name_is_still_classified_correctly():
    """`linear_comment` reads like a noun and writes. This is exactly why a
    curated list beats a verb heuristic where one is available."""
    assert classify("linear_comment") == EFFECT_WRITE


def test_the_write_lists_only_name_tools_that_exist():
    """A stale entry is a rule that silently protects nothing."""
    for type_, writes in CURATED_WRITES.items():
        source = (ROOT / "connectors" / f"{type_}.py").read_text(encoding="utf-8")
        declared = set(re.findall(r'name="([a-z_]+)"', source))
        missing = sorted(w for w in writes if w not in declared)
        assert not missing, f"{type_}.py no longer declares: {missing}"


def test_every_shipped_connector_tool_is_classified():
    """The tripwire. A new tool added to a curated connector is treated as a
    read by default, so this fails until somebody decides — the same shape as
    govern.EFFECTS' tripwire, for the same reason."""
    for type_ in CURATED_WRITES:
        source = (ROOT / "connectors" / f"{type_}.py").read_text(encoding="utf-8")
        for name in sorted(set(re.findall(r'name="([a-z_]+)"', source))):
            assert classify(name) in (EFFECT_READ, EFFECT_WRITE)


# -- unreviewed servers ----------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["jira_get_issue", "jira_list_projects", "drive_search_files", "notion_read_page"],
)
def test_a_recognised_read_verb_on_an_unknown_server_is_a_read(name):
    assert classify(name) == EFFECT_READ


@pytest.mark.parametrize(
    "name",
    [
        "jira_transition_issue",
        "drive_delete_file",
        "notion_append_block",
        "vendor_frobnicate",
        "vendor_do_the_thing",
    ],
)
def test_anything_unrecognised_on_an_unknown_server_is_a_write(name):
    """Guessing 'read' about an unknown verb is how a policy that forbids
    writes lets one through."""
    assert classify(name) == EFFECT_WRITE


def test_an_unknown_server_is_not_curated():
    assert curated("github") is True
    assert curated("some_vendor") is False


def test_the_type_prefix_is_stripped_before_the_verb_is_read():
    """`jira_get_issue` must be seen as `get_`, not as a name starting with
    `jira`."""
    assert classify("jira_get_issue") == EFFECT_READ


# -- how govern uses it ----------------------------------------------------


def test_connector_tools_get_a_real_intent():
    svc = {"github_create_issue", "github_list_issues"}
    assert govern.classify("github_list_issues", {}, svc)[0] == govern.INTENT_READ_TOOL
    assert govern.classify("github_create_issue", {}, svc)[0] == govern.INTENT_WRITE_TOOL


def test_a_tool_not_from_a_connector_is_not_guessed_at():
    """Inferring 'connector' from an underscore would report a built-in
    somebody forgot to classify as a vendor write tool — a worse lie than
    `unknown`, because it names an effect on a system never touched."""
    assert govern.classify("some_new_tool", {}, set())[0] == govern.INTENT_UNKNOWN
    assert govern.classify("some_new_tool", {}, None)[0] == govern.INTENT_UNKNOWN


def test_a_builtin_is_never_reclassified_as_a_connector_tool():
    """Even if a connector somehow offers a colliding name, the built-in table
    wins — it is the one we can reason about."""
    assert govern.classify("run_command", {}, {"run_command"})[0] == govern.INTENT_RUN


def test_the_target_is_the_tool_name():
    svc = {"linear_comment"}
    _, target, _ = govern.classify("linear_comment", {"body": "hi"}, svc)
    assert target == "linear_comment"


def test_one_policy_rule_can_forbid_every_vendor_write():
    """The whole point: an operator writes one rule, not nine."""
    import agent.policy as policy

    pol = policy.parse({"deny": [{"intent": "write_tool"}]})
    svc = {"linear_create_issue", "linear_comment", "linear_list_teams", "jira_frobnicate"}

    for name in ("linear_create_issue", "linear_comment", "jira_frobnicate"):
        intent, target, _ = govern.classify(name, {}, svc)
        decision = pol.evaluate({"tool": name, "intent": intent, "target": target, "bot": "a"})
        assert decision.allowed is False, name

    intent, target, _ = govern.classify("linear_list_teams", {}, svc)
    assert (
        pol.evaluate(
            {"tool": "linear_list_teams", "intent": intent, "target": target, "bot": "a"}
        ).allowed
        is True
    )
