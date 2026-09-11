"""Unified workflow store: merged catalog, trigger auto-routing,
negative-space enablement, folder skills, and the /api/workflows surface."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from agent.commands import find_skill
from agent.skills import load_skills, skill_turn_block, skills_prompt
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.routines import list_routines
from harness.server import make_server
from harness.workflows import (
    WorkflowError,
    create_workflow,
    disabled_workflows,
    get_workflow,
    is_enabled,
    list_workflows,
    set_workflow_enabled,
)


def _paths(tmp_path) -> HarnessPaths:
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas", "nova"])
    return p


def _write_shared_skill(paths: HarnessPaths, folder: str, *, helpers: dict | None = None):
    d = paths.skills / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {folder}\ndescription: a {folder} skill\nwhen_to_use: testing\n---\n1. do it\n",
        encoding="utf-8",
    )
    for name, text in (helpers or {}).items():
        (d / name).write_text(text, encoding="utf-8")
    return d


# -- merged catalog ---------------------------------------------------------
def test_merged_catalog_across_sources(tmp_path):
    paths = _paths(tmp_path)
    _write_shared_skill(paths, "deploy-docs")
    create_workflow(paths, "atlas", name="book-flights", description="fly", body="1. search")
    create_workflow(paths, "atlas", name="Morning check", body="check crawl errors", trigger="8am")

    by_id = {w.id: w for w in list_workflows(paths, "atlas")}
    assert by_id["cite-sources"].source == "seeded"  # harness default skill
    assert by_id["deploy-docs"].source == "shared"
    assert by_id["deploy-docs"].owner_bot is None
    assert by_id["book-flights"].source == "private"
    assert by_id["book-flights"].owner_bot == "atlas"

    routines = [w for w in by_id.values() if w.source == "routine"]
    assert len(routines) == 1
    assert routines[0].name == "Morning check"
    assert routines[0].trigger == "0 8 * * *"
    assert routines[0].owner_bot == "atlas"
    # every record shares one shape
    keys = set(by_id["cite-sources"].to_dict())
    assert all(set(w.to_dict()) == keys for w in by_id.values())
    # another bot does not see atlas' private skill or routine
    nova_ids = {w.id for w in list_workflows(paths, "nova")}
    assert "book-flights" not in nova_ids
    assert routines[0].id not in nova_ids
    assert {"cite-sources", "deploy-docs"} <= nova_ids


# -- trigger auto-routing ---------------------------------------------------
def test_create_with_trigger_routes_to_routine(tmp_path):
    paths = _paths(tmp_path)
    w = create_workflow(paths, "atlas", name="Crawl check", body="check ahrefs", trigger="9:30am")
    assert w.source == "routine"
    assert w.trigger == "30 9 * * *"
    rows = list_routines(paths, "atlas")
    assert rows and rows[0]["title"] == "Crawl check"
    # nothing landed in the skills tree
    assert not any(s.name == "Crawl check" for s in load_skills(paths, "atlas"))

    plain = create_workflow(paths, "atlas", name="Cite well", description="cite", body="1. cite")
    assert plain.source == "private"
    assert plain.trigger is None
    assert plain.path is not None and plain.path.name == "SKILL.md"
    assert any(s.name == "Cite well" for s in load_skills(paths, "atlas"))

    with pytest.raises(WorkflowError):
        create_workflow(paths, "atlas", name="", body="x")
    with pytest.raises(WorkflowError):
        create_workflow(paths, "atlas", name="no-body")


# -- negative-space enablement ---------------------------------------------
def test_disabled_list_enablement_default_on(tmp_path):
    paths = _paths(tmp_path)
    # nothing stored -> everything on, including skills seeded after the bot
    assert disabled_workflows(paths, "atlas") == set()
    _write_shared_skill(paths, "late-arrival")  # "shared later": no backfill needed
    assert is_enabled(paths, "atlas", "late-arrival")
    assert "late-arrival" in skills_prompt(paths, "atlas")

    set_workflow_enabled(paths, "atlas", "cite-sources", False)
    assert not is_enabled(paths, "atlas", "cite-sources")
    assert "cite-sources" not in skills_prompt(paths, "atlas")
    # per-bot: nova is untouched
    assert "cite-sources" in skills_prompt(paths, "nova")
    # explicit /cite-sources still resolves (slash back-compat)
    assert find_skill(paths, "atlas", "cite-sources") is not None
    # the store holds ONLY the disabled list
    stored = json.loads(
        (paths.bot_memory("atlas") / "disabled-workflows.json").read_text(encoding="utf-8")
    )
    assert stored == {"disabled": ["cite-sources"]}

    set_workflow_enabled(paths, "atlas", "cite-sources", True)
    assert is_enabled(paths, "atlas", "cite-sources")
    assert "cite-sources" in skills_prompt(paths, "atlas")

    with pytest.raises(WorkflowError):
        set_workflow_enabled(paths, "atlas", "no-such-workflow", False)


def test_enablement_toggle_routes_to_routine_flag(tmp_path):
    paths = _paths(tmp_path)
    w = create_workflow(paths, "atlas", name="Ping", body="say hi", trigger="8am")
    set_workflow_enabled(paths, "atlas", w.id, False)
    assert list_routines(paths, "atlas")[0]["enabled"] is False
    # routines use their own flag, not the skill disabled list
    assert disabled_workflows(paths, "atlas") == set()
    assert get_workflow(paths, "atlas", w.id).enabled is False


# -- folder skills ----------------------------------------------------------
def test_folder_skill_discovery_and_helper_listing(tmp_path):
    paths = _paths(tmp_path)
    _write_shared_skill(
        paths, "invoice-run", helpers={"fetch.py": "print('hi')\n", "notes.txt": "ref\n"}
    )
    skills = {s.skill_id: s for s in load_skills(paths, "atlas")}
    folder = skills["invoice-run"]
    # one skill, not three
    assert [s for s in skills.values() if s.skill_id == "invoice-run"] == [folder]
    assert [p.name for p in folder.helpers] == ["fetch.py", "notes.txt"]

    block = skill_turn_block(folder, "run the invoices")
    assert "Helper files" in block
    assert str(folder.path.parent / "fetch.py") in block
    prompt = skills_prompt(paths, "atlas")
    assert str(folder.path.parent / "notes.txt") in prompt

    # single-file skills keep the old rendering
    plain = skills["cite-sources"]
    assert plain.helpers == []
    assert "Helper files" not in skill_turn_block(plain, "")

    wf = get_workflow(paths, "atlas", "invoice-run")
    assert wf.helpers == [
        str(folder.path.parent / "fetch.py"),
        str(folder.path.parent / "notes.txt"),
    ]


# -- API surface ------------------------------------------------------------
ROSTER = """
[[bots]]
name = "atlas"
role = "helper"
provider = "echo"
"""


@pytest.fixture
def server(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    httpd = make_server(orch, "127.0.0.1", 0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", orch
    finally:
        httpd.shutdown()
        orch.down()


def _req(url, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def test_workflows_api_shape_and_toggle(server):
    base, orch = server
    listing = _req(f"{base}/api/workflows?bot=atlas")
    by_id = {w["id"]: w for w in listing}
    assert "cite-sources" in by_id
    expected_keys = {
        "id",
        "name",
        "description",
        "source",
        "path",
        "trigger",
        "owner_bot",
        "when_to_use",
        "helpers",
        "enabled",
    }
    assert all(set(w) == expected_keys for w in listing)
    assert by_id["cite-sources"]["source"] == "seeded"
    assert by_id["cite-sources"]["enabled"] is True
    assert by_id["cite-sources"]["trigger"] is None

    # create: trigger auto-routes to a routine
    routine = _req(
        f"{base}/api/bots/atlas/workflows",
        "POST",
        {"name": "Crawl check", "body": "check ahrefs", "trigger": "8am"},
    )
    assert routine["source"] == "routine"
    assert routine["trigger"] == "0 8 * * *"
    assert list_routines(orch.paths, "atlas")[0]["title"] == "Crawl check"

    # create: no trigger -> private skill
    skill = _req(
        f"{base}/api/bots/atlas/workflows",
        "POST",
        {"name": "book-flights", "description": "fly", "body": "1. search"},
    )
    assert skill["source"] == "private"
    assert skill["owner_bot"] == "atlas"

    listing = {w["id"]: w for w in _req(f"{base}/api/workflows?bot=atlas")}
    assert {"cite-sources", "book-flights", routine["id"]} <= set(listing)

    # per-bot disable via PATCH, and back on again
    off = _req(f"{base}/api/bots/atlas/workflows/cite-sources", "PATCH", {"enabled": False})
    assert off["enabled"] is False
    assert skills_prompt(orch.paths, "atlas") and "cite-sources" not in skills_prompt(
        orch.paths, "atlas"
    )
    on = _req(f"{base}/api/bots/atlas/workflows/cite-sources", "PATCH", {"enabled": True})
    assert on["enabled"] is True

    # routines toggle through the same endpoint
    r_off = _req(f"{base}/api/bots/atlas/workflows/{routine['id']}", "PATCH", {"enabled": False})
    assert r_off["enabled"] is False
    assert list_routines(orch.paths, "atlas")[0]["enabled"] is False

    # errors
    with pytest.raises(urllib.error.HTTPError) as exc:
        _req(f"{base}/api/bots/atlas/workflows/nope", "PATCH", {"enabled": False})
    assert exc.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as exc:
        _req(f"{base}/api/bots/atlas/workflows", "POST", {"name": ""})
    assert exc.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as exc:
        _req(f"{base}/api/workflows?bot=ghost")
    assert exc.value.code == 404

    # /api/skills stays the slash-picker surface (back-compat)
    names = {s["name"] for s in _req(f"{base}/api/skills?bot=atlas")}
    assert {"memory", "remember", "soul", "skills"} <= names
    assert "book-flights" in names
