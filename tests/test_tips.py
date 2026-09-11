"""Product tips: source, echo tips card, skill seeding, empty default roster."""

from __future__ import annotations

import json
from pathlib import Path

from agent.skills import Skill, ensure_default_skills
from agent.tips import TABLE_COLUMNS, TIPS, tips_skill_md, tips_table_args
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.roster import load_roster
from providers.base import Message, ToolSpec
from providers.echo import EchoProvider

REPO_ROOT = Path(__file__).resolve().parents[1]


def _table_tools():
    return [ToolSpec(name="show_table", description="", parameters={})]


# -- tips content ----------------------------------------------------------
def test_tips_fit_show_table_limits():
    assert 1 <= len(TABLE_COLUMNS) <= 6
    assert 1 <= len(TIPS) <= 20
    args = tips_table_args()
    assert args["columns"] == TABLE_COLUMNS
    for tip, row in zip(TIPS, args["rows"], strict=True):
        assert len(row) == len(TABLE_COLUMNS)
        assert tip.topic and tip.tip


def test_tips_skill_md_parses(tmp_path):
    md = tmp_path / "harness-tips" / "SKILL.md"
    md.parent.mkdir()
    md.write_text(tips_skill_md(), encoding="utf-8")
    skill = Skill.from_file(md, "shared")
    assert skill.name == "harness-tips"
    assert skill.description
    assert skill.when_to_use
    assert "show_table" in skill.body
    for tip in TIPS:
        assert tip.topic in skill.body


def test_tips_name_the_shared_workspace_handoff():
    tip = next(t for t in TIPS if t.topic == "Shared workspace")
    assert "/workspace/<project>/" in tip.tip
    assert "secrets" in tip.tip.lower()
    assert "/workspace/<project>/" in tips_skill_md()


# -- echo demo -------------------------------------------------------------
def test_echo_tips_emits_table_card():
    p = EchoProvider()
    for phrase in ("what can you do?", "any tips?", "show me around"):
        out = p.complete([Message(role="user", content=phrase)], tools=_table_tools())
        assert out.tool_calls, phrase
        call = out.tool_calls[0]
        assert call.name == "show_table"
        assert call.arguments == tips_table_args()


def test_echo_tips_without_table_tool_echoes():
    p = EchoProvider()
    out = p.complete([Message(role="user", content="any tips?")], tools=[])
    assert not out.tool_calls
    assert "tips" in out.text


def test_echo_wraps_tips_table_result():
    p = EchoProvider()
    out = p.complete(
        [
            Message(role="user", content="tips"),
            Message(role="tool", content="ok: table shown (8 rows)", name="show_table"),
        ],
        tools=_table_tools(),
    )
    assert not out.tool_calls
    assert "Try it" in out.text
    assert "relayed reply" not in out.text


def test_echo_wraps_unrelated_table_result_generically():
    p = EchoProvider(persona="atlas")
    out = p.complete(
        [
            Message(role="user", content="list my open pulls"),
            Message(role="tool", content="ok: table shown (2 rows)", name="show_table"),
        ],
        tools=_table_tools(),
    )
    assert not out.tool_calls
    assert "relayed reply" in out.text


# -- seed rosters ----------------------------------------------------------
def test_default_roster_ships_no_bots():
    roster = load_roster(REPO_ROOT / "roster.toml")
    assert roster.names() == []


def test_example_roster_is_an_opt_in_template():
    example = load_roster(REPO_ROOT / "roster.example.toml")
    assert example.names()
    assert "guide" in example.names()


# -- default skill seeding -------------------------------------------------
def test_fresh_home_seeds_harness_tips(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    md = paths.skills / "harness-tips" / "SKILL.md"
    assert md.is_file()
    assert Skill.from_file(md, "shared").name == "harness-tips"
    assert (paths.skills / "cite-sources" / "SKILL.md").is_file()
    assert (paths.skills / "learn-from-demonstration" / "SKILL.md").is_file()


def test_existing_home_gains_missing_default_skill(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.skills.mkdir(parents=True)
    old = paths.skills / "cite-sources"
    old.mkdir()
    (old / "SKILL.md").write_text("user-edited", encoding="utf-8")
    ensure_default_skills(paths)
    assert (paths.skills / "harness-tips" / "SKILL.md").is_file()
    assert (old / "SKILL.md").read_text(encoding="utf-8") == "user-edited"


def test_seeding_never_clobbers_edits(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    ensure_default_skills(paths)
    md = paths.skills / "harness-tips" / "SKILL.md"
    md.write_text("my version", encoding="utf-8")
    ensure_default_skills(paths)
    assert md.read_text(encoding="utf-8") == "my version"


# -- roster.json backfill --------------------------------------------------
_ATLAS_ONLY = '[[bots]]\nname = "atlas"\nrole = "helper"\nprovider = "echo"\n'
_WITH_GUIDE = _ATLAS_ONLY + '\n[[bots]]\nname = "guide"\nrole = "tips"\nprovider = "echo"\n'


def _store_names(home: Path) -> list[str]:
    data = json.loads((home / "roster.json").read_text(encoding="utf-8"))
    return [b["name"] for b in data["bots"]]


def test_fresh_store_is_not_seeded_with_demo_bots(tmp_path):
    home = tmp_path / "home"
    orch = Orchestrator.create(home=home, roster_path=REPO_ROOT / "roster.toml")
    orch.use_json_store()
    assert orch.roster.names() == []
    assert _store_names(home) == []


def test_new_seed_bot_backfills_existing_store(tmp_path):
    rp = tmp_path / "roster.toml"
    home = tmp_path / "home"
    rp.write_text(_ATLAS_ONLY, encoding="utf-8")
    Orchestrator.create(home=home, roster_path=rp).use_json_store()
    assert _store_names(home) == ["atlas"]

    rp.write_text(_WITH_GUIDE, encoding="utf-8")  # upgrade ships a new seed bot
    orch = Orchestrator.create(home=home, roster_path=rp)
    orch.use_json_store()
    assert _store_names(home) == ["atlas", "guide"]
    assert "guide" in orch.roster.names()


def test_deleted_seed_bot_stays_deleted(tmp_path):
    rp = tmp_path / "roster.toml"
    home = tmp_path / "home"
    rp.write_text(_WITH_GUIDE, encoding="utf-8")
    Orchestrator.create(home=home, roster_path=rp).use_json_store()
    assert _store_names(home) == ["atlas", "guide"]

    store = home / "roster.json"
    data = json.loads(store.read_text(encoding="utf-8"))
    data["bots"] = [b for b in data["bots"] if b["name"] != "guide"]
    store.write_text(json.dumps(data), encoding="utf-8")

    orch = Orchestrator.create(home=home, roster_path=rp)
    orch.use_json_store()
    assert _store_names(home) == ["atlas"]


def test_user_bot_with_seed_name_is_untouched(tmp_path):
    rp = tmp_path / "roster.toml"
    home = tmp_path / "home"
    rp.write_text(_ATLAS_ONLY, encoding="utf-8")
    Orchestrator.create(home=home, roster_path=rp).use_json_store()

    store = home / "roster.json"
    data = json.loads(store.read_text(encoding="utf-8"))
    data["bots"].append({"name": "guide", "role": "mine", "provider": "claude"})
    store.write_text(json.dumps(data), encoding="utf-8")

    rp.write_text(_WITH_GUIDE, encoding="utf-8")
    orch = Orchestrator.create(home=home, roster_path=rp)
    orch.use_json_store()
    assert orch.roster.get("guide").provider == "claude"
    assert _store_names(home).count("guide") == 1
