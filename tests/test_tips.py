"""Product tips: source, echo tips card, skill seeding, empty default roster."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

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


_OLD_CITATION_SEED = b"""\
---
name: cite-sources
description: Always cite sources when stating a fact
when_to_use: user asks for a fact or /cite-sources
---
1. Find a source for each claim.
2. Quote or link the source in the reply.
3. If you cannot cite, say so.
"""


def test_untouched_citation_seed_upgrades_without_resetting_user_state(tmp_path):
    from agent.skills import load_skills
    from harness.workflows import set_enabled

    paths = HarnessPaths.resolve(tmp_path / "home")
    shared = paths.skills / "cite-sources" / "SKILL.md"
    shared.parent.mkdir(parents=True)
    shared.write_bytes(_OLD_CITATION_SEED)
    helper = shared.with_name("notes.txt")
    helper.write_text("Keep this helper.")
    private = paths.bot_memory("atlas") / "skills" / "cite-sources" / "SKILL.md"
    private.parent.mkdir(parents=True)
    private.write_bytes(_OLD_CITATION_SEED)
    set_enabled(paths, "atlas", "cite-sources", False)
    enablement = paths.bot_memory("atlas") / "disabled-workflows.json"
    saved_enablement = enablement.read_bytes()

    ensure_default_skills(paths)

    assert shared.read_bytes() != _OLD_CITATION_SEED
    assert "review context" in Skill.from_file(shared, "shared").body
    assert private.read_bytes() == _OLD_CITATION_SEED
    assert helper.read_text() == "Keep this helper."
    assert enablement.read_bytes() == saved_enablement
    assert not any(s.skill_id == "cite-sources" for s in load_skills(paths, "atlas", enabled_only=True))
    updated = shared.stat().st_mtime_ns
    ensure_default_skills(paths)
    assert shared.stat().st_mtime_ns == updated


def test_fresh_citation_seed_distinguishes_review_and_approved_outgoing_text(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    ensure_default_skills(paths)
    skill = Skill.from_file(paths.skills / "cite-sources" / "SKILL.md", "shared")
    assert "Always cite" not in skill.description
    assert "review context" in skill.body
    assert "exact approved outgoing text unchanged" in skill.body
    assert "explicitly requests citations in the outgoing message" in skill.body
    assert "revised proposal and new approval" in skill.body


@pytest.mark.parametrize("mode", [0o640, 0o644])
def test_citation_upgrade_preserves_shared_file_mode(tmp_path, mode):
    paths = HarnessPaths.resolve(tmp_path / "home")
    shared = paths.skills / "cite-sources" / "SKILL.md"
    shared.parent.mkdir(parents=True)
    shared.write_bytes(_OLD_CITATION_SEED)
    shared.chmod(mode)

    ensure_default_skills(paths)

    assert shared.read_bytes() != _OLD_CITATION_SEED
    assert stat.S_IMODE(shared.stat().st_mode) == mode


def test_fresh_default_skills_keep_normal_creation_mode(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    normal = tmp_path / "normal.md"
    normal.write_text("Normal creation permissions.")
    mode = stat.S_IMODE(normal.stat().st_mode)

    ensure_default_skills(paths)

    for skill in paths.skills.glob("*/SKILL.md"):
        assert stat.S_IMODE(skill.stat().st_mode) == mode


@pytest.mark.parametrize("content", [
    _OLD_CITATION_SEED + b"\nCustom note.\n",
    _OLD_CITATION_SEED.replace(b"each claim", b"important claims"),
    _OLD_CITATION_SEED.replace(b"\n", b"\r\n"),
])
def test_citation_seed_migration_preserves_edited_bytes(tmp_path, content):
    paths = HarnessPaths.resolve(tmp_path / "home")
    shared = paths.skills / "cite-sources" / "SKILL.md"
    shared.parent.mkdir(parents=True)
    shared.write_bytes(content)
    ensure_default_skills(paths)
    assert shared.read_bytes() == content


@pytest.mark.parametrize("kind", ["root", "directory", "file", "dangling-file"])
def test_citation_seeding_never_writes_through_symlinks(tmp_path, kind):
    paths = HarnessPaths.resolve(tmp_path / "home")
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "SKILL.md"
    if kind != "dangling-file":
        target.write_bytes(_OLD_CITATION_SEED)
    if kind == "root":
        paths.home.mkdir()
        target.parent.joinpath("cite-sources").mkdir()
        target.rename(outside / "cite-sources" / "SKILL.md")
        target = outside / "cite-sources" / "SKILL.md"
        paths.skills.symlink_to(outside, target_is_directory=True)
    elif kind == "directory":
        paths.skills.mkdir(parents=True)
        (paths.skills / "cite-sources").symlink_to(outside, target_is_directory=True)
    else:
        skill_dir = paths.skills / "cite-sources"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").symlink_to(target)
    before = {str(p.relative_to(outside)): p.read_bytes() for p in outside.rglob("*") if p.is_file()}

    ensure_default_skills(paths)

    assert {str(p.relative_to(outside)): p.read_bytes() for p in outside.rglob("*") if p.is_file()} == before
    if kind == "dangling-file":
        assert not target.exists()


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
