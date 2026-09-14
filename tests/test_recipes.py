"""Bundled recipes: catalog, featured gallery, install seeds soul/skills/facts."""

from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.request

import pytest

from agent.memory import Memory
from agent.skills import load_skills
from agent.soul import load_soul
from harness.connectors import catalog as connector_catalog
from harness.orchestrator import Orchestrator
from harness.prefs import llm_defaults, resolve_llm, set_llm_defaults
from harness.recipes import RecipeError, catalog, get, unused_bot_name
from harness.server import make_server

ROSTER = """
[[bots]]
name = "atlas"
role = "a terse research assistant"
provider = "echo"
"""


@pytest.fixture
def server(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    orch.use_json_store()
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
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def test_catalog_has_featured_jobs_and_souls():
    rows = catalog()
    ids = [r["id"] for r in rows]
    assert "pr-reviewer" in ids
    assert "inbox-triage" in ids
    assert "research-desk" in ids
    featured = [r for r in rows if r.get("featured")]
    assert len(featured) >= 8
    ranks = [r["featured_rank"] for r in featured]
    assert ranks == sorted(ranks)
    for row in rows:
        assert row["name"]
        assert row["tagline"]
        assert row["category"]
        assert row["personality"].strip()
        assert row["role"]
        assert "never" in row["personality"].lower() or row["never"]


def test_engineering_category_has_skill_pack_recipes():
    rows = {r["id"]: r for r in catalog()}
    for rid in ("debug-detective", "performance-lab", "security-audit", "spec-and-plan"):
        assert rows[rid]["category"] == "Engineering"
        assert rows[rid]["never"]
        assert rows[rid]["skills"]
    engineering = [r for r in catalog() if r["category"] == "Engineering"]
    assert len(engineering) >= 7


def test_agency_recipes_land_in_their_categories():
    rows = {r["id"]: r for r in catalog()}
    assert rows["design-review"]["category"] == "Design"
    assert rows["feedback-synthesizer"]["category"] == "Product"
    assert rows["content-drafts"]["category"] == "Marketing"
    assert rows["project-shepherd"]["category"] == "Ops"


def test_recipe_ids_are_unique_slugs():
    ids = [r["id"] for r in catalog()]
    assert len(ids) == len(set(ids))
    for rid in ids:
        assert rid == rid.lower() and " " not in rid


def test_recipe_plugins_are_catalog_connector_types():
    # A recipe lists plugins for the user to connect; an unknown type would
    # render a dead tile in the Plugins gallery.
    types = {c["type"] for c in connector_catalog()}
    for row in catalog():
        for plugin in row["plugins"]:
            assert plugin in types, f"{row['id']} names unknown plugin {plugin!r}"


def test_grokbot_batch_lands_in_categories():
    # Recipes adapted from the grokbot.wtf directory
    # (docs/research-grokbot-templates.md). Assistants and Creative are the
    # two categories that batch introduced.
    rows = {r["id"]: r for r in catalog()}
    expected = {
        "dispatcher": "Assistants",
        "gatekeeper": "Assistants",
        "bot-builder": "Assistants",
        "template-vet": "Assistants",
        "routine-audit": "Ops",
        "decision-log": "Ops",
        "build-loop": "Engineering",
        "model-watch": "Research",
        "reddit-listener": "Research",
        "seo-desk": "Marketing",
        "post-call": "Sales",
        "negotiation-desk": "Sales",
        "deck-review": "Sales",
        "receipt-ledger": "Finance",
        "refund-hunter": "Finance",
        "newsletter-cleanup": "Personal",
        "todo-harvester": "Personal",
        "home-search": "Home",
        "travel-desk": "Personal",
        "meal-planner": "Home",
        "weekly-wellbeing": "Personal",
        "interview-prep": "Learning",
        "brand-atelier": "Creative",
        "alt-text": "Creative",
        "meme-maker": "Creative",
    }
    for rid, category in expected.items():
        assert rows[rid]["category"] == category
        assert rows[rid]["never"], rid
        assert rows[rid]["skills"], rid
        assert rows[rid]["first_task"], rid
        assert rows[rid]["updated"] == "2026-09-02"
    # The batch never loosens the gate: every soul carries its refusals.
    for rid in expected:
        assert "never" in rows[rid]["personality"].lower()
    # Merged sources keep both procedures.
    assert {s["id"] for s in rows["seo-desk"]["skills"]} == {"site-audit", "keyword-brief"}
    assert {s["id"] for s in rows["receipt-ledger"]["skills"]} == {
        "log-receipts",
        "monthly-pack",
    }
    assert {s["id"] for s in rows["gatekeeper"]["skills"]} == {
        "classify-requests",
        "commitment-check",
    }
    assert {s["id"] for s in rows["decision-log"]["skills"]} == {
        "log-decision",
        "daily-timeline",
    }
    # Earlier batches keep their date; only this batch moved it.
    assert rows["pr-reviewer"]["updated"] == "2026-08-30"


def test_second_grokbot_batch_and_everyday_recipes_land():
    # Second batch: grokbot jobs that turned out feasible plus everyday
    # recipes of our own (docs/research-grokbot-templates.md, last table).
    rows = {r["id"]: r for r in catalog()}
    expected = {
        # re-adopted from the directory
        "urgent-mail-watch": "Personal",
        "calendar-brief": "Personal",
        "deal-compare": "Home",
        "channel-recap": "Research",
        "sports-desk": "Personal",
        "date-night": "Personal",
        "workspace-keeper": "Ops",
        "clip-cutter": "Creative",
        "rewards-picker": "Finance",
        "site-librarian": "Research",
        "gig-scout": "Sales",
        "bot-tuner": "Assistants",
        # everyday recipes of our own
        "expense-claims": "Finance",
        "bill-tracker": "Finance",
        "tariff-compare": "Finance",
        "warranty-vault": "Home",
        "parcel-tracker": "Home",
        "home-maintenance": "Home",
        "packing-list": "Personal",
        "occasions": "Personal",
        "contract-reader": "Personal",
        "study-coach": "Learning",
        "language-tutor": "Learning",
        "job-application": "Learning",
        "slot-finder": "Ops",
        "standup": "Engineering",
        "dependency-watch": "Engineering",
        "docs-gardener": "Engineering",
        "kb-writer": "Support",
        "copy-editor": "Creative",
    }
    for rid, category in expected.items():
        assert rows[rid]["category"] == category, rid
        assert rows[rid]["never"], rid
        assert rows[rid]["skills"], rid
        assert rows[rid]["first_task"], rid
        assert "never" in rows[rid]["personality"].lower(), rid
    assert len(catalog()) >= 80


def test_money_adjacent_recipes_refuse_payment_details():
    # Household money recipes read and draft; none may touch a card,
    # a checkout, or a payee login.
    rows = {r["id"]: r for r in catalog()}
    for rid in ("rewards-picker", "deal-compare", "bill-tracker", "tariff-compare"):
        text = " ".join(rows[rid]["never"]).lower() + rows[rid]["personality"].lower()
        assert any(
            phrase in text
            for phrase in ("payment details", "card number", "account details", "pay,")
        ), rid
    assert "cvv" in " ".join(rows["rewards-picker"]["never"]).lower()


def test_install_everyday_recipe_seeds_skill_and_soul(server):
    base, orch = server
    out = _req(f"{base}/api/recipes/bill-tracker/install", "POST", {"provider": "echo"})
    assert out["name"] == "bill-tracker"
    assert out["title"] == "Bill Tracker"
    skills = {s.skill_id for s in load_skills(orch.paths, "bill-tracker")}
    assert "track-bills" in skills
    soul = load_soul(orch.paths, "bill-tracker")
    assert "Never pay" in soul


def test_bot_builder_hands_the_drafted_soul_to_create_bot():
    # The interview produces rules and a never-list; the create step must
    # pass them as create_bot's personality or the new bot starts blank.
    row = get("bot-builder")
    body = next(s for s in row["skills"] if s["id"] == "draft-bot")["body"]
    create_step = next(line for line in body.splitlines() if "create_bot" in line)
    assert "personality" in create_step
    assert "personality" in row["personality"]


def test_bot_tuner_works_through_the_target_bot():
    # read_soul / write_soul / propose_skill act on the caller only, so the
    # tuner must fetch and apply through message_agent, never its own tools.
    row = get("bot-tuner")
    body = next(s for s in row["skills"] if s["id"] == "tune-bot")["body"]
    assert "message_agent" in body
    apply_step = next(line for line in body.splitlines() if line.startswith("5."))
    assert "message_agent" in apply_step
    soul = row["personality"].lower()
    assert "act on you" in soul
    assert any("write_soul" in n for n in row["never"])


FALLBACK_HINTS = ("pasted", "shared image", "photos shared", "export", "owner shares", "otherwise")


def test_souls_do_not_stop_on_a_plugin_the_skill_can_do_without():
    # A soul is read every turn. If any skill body names a no-plugin
    # fallback, the soul must not tell the bot to stop when that plugin is
    # missing, or the fallback is unreachable.
    for row in catalog():
        bodies = " ".join(s["body"].lower() for s in row["skills"])
        if not any(h in bodies for h in FALLBACK_HINTS):
            continue
        for line in row["personality"].lower().splitlines():
            if "not connected" in line:
                assert "and stop" not in line, f"{row['id']}: {line.strip()}"


def test_third_batch_fills_thin_categories():
    # Third batch: Design, Product, Support, Marketing get real depth, plus
    # more everyday recipes and two on harness-native tools.
    rows = {r["id"]: r for r in catalog()}
    expected = {
        "reminders": "Assistants",
        "room-scribe": "Assistants",
        "accessibility-audit": "Design",
        "ux-copy": "Design",
        "asset-prep": "Design",
        "prd-writer": "Product",
        "metrics-readout": "Product",
        "backlog-groomer": "Product",
        "launch-checklist": "Product",
        "ticket-triage": "Support",
        "incident-comms": "Support",
        "churn-watch": "Support",
        "brand-mentions": "Marketing",
        "landing-page-review": "Marketing",
        "campaign-qa": "Marketing",
        "case-study": "Marketing",
        "onboarding-plan": "Ops",
        "vendor-register": "Ops",
        "runbook-writer": "Ops",
        "test-author": "Engineering",
        "codebase-guide": "Engineering",
        "flaky-hunter": "Engineering",
        "migration-plan": "Engineering",
        "paper-digest": "Research",
        "fact-check": "Research",
        "proposal-desk": "Sales",
        "crm-hygiene": "Sales",
        "invoice-drafter": "Finance",
        "tax-pack": "Finance",
        "training-log": "Personal",
        "reading-list": "Learning",
        "family-logistics": "Home",
        "garden-planner": "Home",
        "house-move": "Home",
        "declutter-seller": "Home",
        "journal": "Personal",
        "pet-care": "Home",
        "show-notes": "Creative",
        "naming-desk": "Creative",
        "slide-deck": "Creative",
        "shot-list": "Creative",
    }
    for rid, category in expected.items():
        assert rows[rid]["category"] == category, rid
        assert rows[rid]["never"], rid
        assert rows[rid]["skills"], rid
        assert rows[rid]["first_task"], rid
        assert "never" in rows[rid]["personality"].lower(), rid
    by_cat = {}
    for r in catalog():
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
    for cat in ("Design", "Product", "Support", "Marketing"):
        assert by_cat[cat] >= 5, (cat, by_cat[cat])
    assert len(catalog()) >= 120


def test_home_and_learning_split_out_of_personal():
    # Personal outgrew the gallery; household jobs live under Home and
    # study / career jobs under Learning. Mirrors categoryOrder in
    # RecipesView.swift.
    rows = {r["id"]: r for r in catalog()}
    for rid in ("home-maintenance", "pet-care", "house-move", "meal-planner", "parcel-tracker"):
        assert rows[rid]["category"] == "Home", rid
    for rid in ("study-coach", "language-tutor", "interview-prep", "job-application"):
        assert rows[rid]["category"] == "Learning", rid
    for rid in ("inbox-triage", "journal", "travel-desk", "occasions"):
        assert rows[rid]["category"] == "Personal", rid
    counts = {}
    for r in catalog():
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    assert counts["Personal"] <= 20
    assert counts["Home"] >= 8 and counts["Learning"] >= 4


def test_harness_native_recipes_name_their_tools():
    # Reminders is built on create_routine; Room Scribe on rooms. The skill
    # text must name the mechanism so the bot reaches for it.
    rows = {r["id"]: r for r in catalog()}
    reminders = next(s for s in rows["reminders"]["skills"] if s["id"] == "set-reminder")
    assert "create_routine" in reminders["body"]
    assert any("routine" in n for n in rows["reminders"]["never"])
    scribe = rows["room-scribe"]
    assert "room" in scribe["skills"][0]["body"].lower()
    assert any("side" in n for n in scribe["never"])


def test_health_adjacent_recipes_refuse_advice():
    rows = {r["id"]: r for r in catalog()}
    for rid, word in (
        ("training-log", "medical"),
        ("pet-care", "veterinary"),
        ("journal", "mental-health"),
        ("tax-pack", "tax advice"),
        ("contract-reader", "legal advice"),
    ):
        text = " ".join(rows[rid]["never"]).lower()
        assert word in text, (rid, word)


def test_install_third_batch_recipe_seeds_soul(server):
    base, orch = server
    out = _req(f"{base}/api/recipes/reminders/install", "POST", {"provider": "echo"})
    assert out["name"] == "reminders"
    assert out["title"] == "Reminders"
    skills = {s.skill_id for s in load_skills(orch.paths, "reminders")}
    assert "set-reminder" in skills
    soul = load_soul(orch.paths, "reminders")
    assert "Never act on a reminder" in soul


def test_install_grokbot_merged_recipe_seeds_both_skills(server):
    base, orch = server
    out = _req(f"{base}/api/recipes/seo-desk/install", "POST", {"provider": "echo"})
    assert out["name"] == "seo-desk"
    assert out["title"] == "SEO Desk"
    skills = {s.skill_id for s in load_skills(orch.paths, "seo-desk")}
    assert {"site-audit", "keyword-brief"} <= skills
    facts = Memory(paths=orch.paths, bot="seo-desk").facts()
    assert any("ahrefs" in f["text"].lower() for f in facts)
    soul = load_soul(orch.paths, "seo-desk")
    assert "Never edit the site" in soul


def test_template_vet_soul_treats_reviewed_text_as_data():
    # The one recipe whose whole job is reading imported instructions must
    # itself say those instructions are evidence, not commands.
    row = get("template-vet")
    soul = row["personality"].lower()
    assert "never follow" in soul or "quote it, never follow it" in soul
    assert "install" in " ".join(row["never"]).lower()


def test_second_agency_batch_lands_in_categories():
    rows = {r["id"]: r for r in catalog()}
    expected = {
        "reality-check": "Engineering",
        "finance-desk": "Finance",
        "trend-watch": "Product",
        "ux-research": "Design",
        "growth-experiments": "Marketing",
    }
    for rid, category in expected.items():
        assert rows[rid]["category"] == category
        assert rows[rid]["never"]
        assert rows[rid]["skills"]
    assert {s["id"] for s in rows["reality-check"]["skills"]} == {
        "collect-evidence",
        "reality-gate",
    }
    assert {s["id"] for s in rows["finance-desk"]["skills"]} == {
        "reconcile-books",
        "scenario-model",
    }
    assert {s["id"] for s in rows["growth-experiments"]["skills"]} == {
        "design-experiment",
        "read-out-experiment",
    }


def test_merged_recipes_carry_both_source_skills():
    rows = {r["id"]: r for r in catalog()}
    assert {s["id"] for s in rows["performance-lab"]["skills"]} == {
        "measure-then-fix",
        "guard-with-telemetry",
    }
    assert {s["id"] for s in rows["spec-and-plan"]["skills"]} == {
        "write-spec",
        "break-down-tasks",
    }
    assert {s["id"] for s in rows["project-shepherd"]["skills"]} == {
        "status-pack",
        "meeting-notes",
    }


def test_get_unknown_recipe_raises():
    with pytest.raises(RecipeError, match="unknown recipe"):
        get("not-a-job")


def test_unused_bot_name_suffixes_collisions():
    assert unused_bot_name(["atlas"], "pr-reviewer") == "pr-reviewer"
    assert unused_bot_name(["pr-reviewer"], "pr-reviewer") == "pr-reviewer-2"
    assert unused_bot_name(["pr-reviewer", "pr-reviewer-2"], "pr-reviewer") == "pr-reviewer-3"


def test_recipes_api_lists_and_fetches(server):
    base, _ = server
    listing = _req(f"{base}/api/recipes")
    assert isinstance(listing, list)
    by_id = {r["id"]: r for r in listing}
    assert "pr-reviewer" in by_id
    assert by_id["pr-reviewer"]["plugins"] == ["github"]
    one = _req(f"{base}/api/recipes/pr-reviewer")
    assert one["name"] == "PR Reviewer"
    assert "Risk" in one["personality"] or "risk" in one["personality"]
    with pytest.raises(urllib.error.HTTPError) as exc:
        _req(f"{base}/api/recipes/nope")
    assert exc.value.code == 404


def test_install_creates_bot_soul_skill_and_memories(server):
    base, orch = server
    out = _req(
        f"{base}/api/recipes/pr-reviewer/install",
        "POST",
        {"provider": "echo"},
    )
    assert out["name"] == "pr-reviewer"
    assert out["title"] == "PR Reviewer"
    assert out["provider"] == "echo"
    bot = orch.roster.get("pr-reviewer")
    assert "review" in bot.role.lower() or "pull" in bot.role.lower()
    soul = load_soul(orch.paths, "pr-reviewer")
    assert "PR Reviewer" in soul
    facts = Memory(paths=orch.paths, bot="pr-reviewer").facts()
    assert any("green check" in f["text"] for f in facts)
    skills = load_skills(orch.paths, "pr-reviewer")
    assert any(s.skill_id == "review-pr-risk" for s in skills)


def test_install_merged_recipe_seeds_both_skills(server):
    base, orch = server
    out = _req(
        f"{base}/api/recipes/performance-lab/install",
        "POST",
        {"provider": "echo"},
    )
    assert out["name"] == "performance-lab"
    skills = {s.skill_id for s in load_skills(orch.paths, "performance-lab")}
    assert {"measure-then-fix", "guard-with-telemetry"} <= skills
    facts = Memory(paths=orch.paths, bot="performance-lab").facts()
    assert any("baseline" in f["text"].lower() for f in facts)


def test_install_collision_gets_a_suffix(server):
    base, orch = server
    _req(f"{base}/api/recipes/research-desk/install", "POST", {"provider": "echo"})
    second = _req(
        f"{base}/api/recipes/research-desk/install",
        "POST",
        {"provider": "echo"},
    )
    assert second["name"] == "research-desk-2"
    assert second["title"] == "Research Desk 2"
    assert "research-desk" in orch.roster.names()
    assert "research-desk-2" in orch.roster.names()


def test_install_without_provider_uses_account_default(server):
    base, orch = server
    assert llm_defaults(orch.paths)["default_provider"] == ""
    first = _req(f"{base}/api/recipes/inbox-triage/install", "POST", {})
    assert first["provider"] == "echo"
    _req(
        f"{base}/api/settings",
        "PATCH",
        {"default_provider": "grok", "default_model": "grok-4"},
    )
    assert llm_defaults(orch.paths) == {
        "default_provider": "grok",
        "default_model": "grok-4",
        "default_reasoning": "",
    }
    got = _req(f"{base}/api/settings")
    assert got["default_provider"] == "grok"
    assert got["default_model"] == "grok-4"
    second = _req(f"{base}/api/recipes/pr-reviewer/install", "POST", {})
    assert second["provider"] == "grok"
    assert second["model"] == "grok-4"
    override = _req(
        f"{base}/api/recipes/research-desk/install",
        "POST",
        {"provider": "echo", "model": ""},
    )
    assert override["provider"] == "echo"


def test_resolve_llm_fills_omitted_fields_from_account_default(tmp_path):
    from harness.paths import HarnessPaths

    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    assert resolve_llm(paths) == ("echo", "", "")
    set_llm_defaults(paths, provider="grok", model="grok-4", reasoning="high")
    assert resolve_llm(paths) == ("grok", "grok-4", "high")
    assert resolve_llm(paths, provider="echo") == ("echo", "", "")
    assert resolve_llm(paths, model="other") == ("grok", "other", "high")


def test_set_llm_defaults_merges_without_wiping_voice(tmp_path):
    from harness.paths import HarnessPaths

    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    (paths.home / "settings.json").write_text('{"voice_fallback": "openai"}\n', encoding="utf-8")
    set_llm_defaults(paths, provider="claude")
    data = (paths.home / "settings.json").read_text(encoding="utf-8")
    assert "openai" in data
    assert "claude" in data


def test_install_payload_matches_bots_list_shape(server):
    """Install JSON must decode as the app Bot model (needs `status`).

    First-tap "The data couldn't be read because it is missing" was
    recipe install returning roster.to_dict() with no status/busy/queued.
    """
    base, _ = server
    listed = {row["name"]: row for row in _req(f"{base}/api/bots")}
    out = _req(
        f"{base}/api/recipes/changelog/install",
        "POST",
        {"provider": "echo"},
    )
    assert out["name"] == "changelog"
    assert "status" in out, out
    assert "busy" in out, out
    assert "queued" in out, out
    sample = next(iter(listed.values()))
    missing = set(sample) - set(out)
    assert not missing, missing


def test_install_unknown_recipe_is_404(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _req(f"{base}/api/recipes/not-real/install", "POST", {"provider": "echo"})
    assert exc.value.code == 404


def test_recipes_require_auth_when_token_set(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    httpd = make_server(orch, "127.0.0.1", 0, token="secret-token")
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/recipes")
        res = conn.getresponse()
        assert res.status == 401
        res.read()
        conn.request("GET", "/api/recipes", headers={"Authorization": "Bearer secret-token"})
        res = conn.getresponse()
        assert res.status == 200
        body = json.loads(res.read().decode())
        assert any(r["id"] == "inbox-triage" for r in body)
        conn.close()
    finally:
        httpd.shutdown()
        orch.down()


# --- colours ------------------------------------------------------------------


def test_every_recipe_carries_a_swatch_from_the_full_palette():
    from harness.colors import SWATCHES, recipe_color

    rows = catalog()
    assert rows
    for row in rows:
        assert row["color"] == recipe_color(row["id"])
        assert row["color"] in SWATCHES
    # The gallery uses the whole range, not the five bot defaults.
    used = {row["color"] for row in rows}
    assert used == set(SWATCHES), sorted(set(SWATCHES) - used)


def test_recipe_colour_is_stable_and_case_insensitive():
    from harness.colors import recipe_color

    assert recipe_color("pr-reviewer") == recipe_color("PR-Reviewer")
    assert recipe_color("pr-reviewer") == recipe_color("pr-reviewer")


def test_install_seeds_the_recipe_colour_on_the_bot(server):
    base, orch = server
    recipe = get("pr-reviewer")
    out = _req(f"{base}/api/recipes/pr-reviewer/install", "POST", {})
    assert out["color"] == recipe["color"]
    listed = next(b for b in _req(f"{base}/api/bots") if b["name"] == out["name"])
    assert listed["color"] == recipe["color"]
