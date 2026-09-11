"""Exporting a deployment, and the one thing that must never come with it.

Configuration was imperative state accreted into `$HARNESS_HOME` with no way to
see it whole, diff it, or stand a second deployment up the same. The risk in
fixing that is obvious: a file describing a deployment is a file somebody
emails around, so the tests that matter most here are the ones that plant
secrets in every store and assert none of them reach the output.
"""

from __future__ import annotations

import json

import pytest

from harness import bundle
from harness.orchestrator import Orchestrator

SECRET = "sk-live-DO-NOT-EXPORT-0123456789"


def _orch(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(
        '[[bots]]\nname = "atlas"\nrole = "a terse research assistant"\nprovider = "echo"\n',
        encoding="utf-8",
    )
    return Orchestrator.create(home=str(tmp_path / "home"), roster_path=rp)


# -- nothing secret escapes ------------------------------------------------


def test_no_credential_reaches_the_bundle(tmp_path):
    """The whole point. Every store gets a secret planted in it first."""
    orch = _orch(tmp_path)
    paths = orch.paths

    (paths.home / "credentials").mkdir(parents=True, exist_ok=True)
    (paths.home / "credentials" / "ANTHROPIC_API_KEY").write_text(SECRET, encoding="utf-8")
    (paths.home / "link-key").write_text(SECRET, encoding="utf-8")
    (paths.home / "connectors.json").write_text(
        json.dumps(
            {
                "connectors": [
                    {
                        "id": "c1",
                        "type": "linear",
                        "name": "Linear",
                        "enabled_for": ["atlas"],
                        "config": {
                            "team": "ENG",
                            "api_key": SECRET,
                            "access_token": SECRET,
                            "clientSecret": SECRET,
                            "Authorization": SECRET,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    text = bundle.dumps(bundle.export(paths, orch.bots()))
    assert SECRET not in text


def test_the_useful_half_of_a_connector_still_travels(tmp_path):
    orch = _orch(tmp_path)
    orch.paths.home.mkdir(parents=True, exist_ok=True)
    (orch.paths.home / "connectors.json").write_text(
        json.dumps(
            {
                "connectors": [
                    {
                        "id": "c1",
                        "type": "linear",
                        "name": "Linear",
                        "enabled_for": ["atlas"],
                        "config": {"team": "ENG", "api_key": SECRET},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    out = bundle.export(orch.paths, orch.bots())
    conn = out["connectors"][0]
    assert conn["name"] == "Linear"
    assert conn["enabled_for"] == ["atlas"]
    assert conn["config"] == {"team": "ENG"}


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "API_KEY",
        "token",
        "refresh_token",
        "clientSecret",
        "password",
        "passphrase",
        "Authorization",
        "private_key",
        "session_id",
        "signature",
        "cert_pem",
        "credentials_json",
    ],
)
def test_credential_shaped_keys_are_dropped(key):
    assert bundle._safe_config({key: SECRET, "keep": "yes"}) == {"keep": "yes"}


def test_auth_ref_does_not_travel(tmp_path):
    """A reference rather than a secret, but it names which credential to look
    for and belongs to the machine it was set up on."""
    orch = _orch(tmp_path)
    orch.add_bot(name="scout", role="r", provider="echo", auth_ref="XAI_API_KEY")
    out = bundle.export(orch.paths, orch.bots())
    scout = next(b for b in out["bots"] if b["name"] == "scout")
    assert "auth_ref" not in scout


# -- what does travel ------------------------------------------------------


def test_bots_and_souls_travel(tmp_path):
    from agent.soul import save_soul

    orch = _orch(tmp_path)
    save_soul(orch.paths, "atlas", "I prefer bullet points and cite sources.")
    out = bundle.export(orch.paths, orch.bots())
    atlas = next(b for b in out["bots"] if b["name"] == "atlas")
    assert atlas["role"]
    assert "bullet points" in atlas["soul"]


def test_private_skills_travel_and_shared_ones_do_not(tmp_path):
    """A shared skill is seeded by the harness itself, so carrying it would
    duplicate what the destination already creates for itself."""
    orch = _orch(tmp_path)
    folder = orch.paths.bot_memory("atlas") / "skills" / "cite"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        "---\nname: cite\ndescription: cite sources\nwhen_to_use: when writing\n"
        "tools: recall\n---\nAlways cite.\n",
        encoding="utf-8",
    )
    out = bundle.export(orch.paths, orch.bots())
    ids = {s["id"] for s in out["skills"]}
    assert "cite" in ids
    assert all(s["bot"] == "atlas" for s in out["skills"] if s["id"] == "cite")
    cite = next(s for s in out["skills"] if s["id"] == "cite")
    assert cite["tools"] == ["recall"]


def test_a_bundle_round_trips(tmp_path):
    orch = _orch(tmp_path)
    text = bundle.dumps(bundle.export(orch.paths, orch.bots()))
    assert bundle.loads(text)["version"] == bundle.BUNDLE_VERSION


# -- validation ------------------------------------------------------------


def test_a_bundle_from_another_version_is_refused():
    with pytest.raises(bundle.BundleError) as exc:
        bundle.loads(json.dumps({"version": 99, "bots": []}))
    assert "99" in str(exc.value)


def test_garbage_is_refused_by_name():
    with pytest.raises(bundle.BundleError):
        bundle.loads("not json at all")
    with pytest.raises(bundle.BundleError):
        bundle.loads(json.dumps(["a", "list"]))
    with pytest.raises(bundle.BundleError):
        bundle.loads(json.dumps({"version": 1, "bots": "not a list"}))


def test_a_bot_without_a_name_is_refused():
    with pytest.raises(bundle.BundleError):
        bundle.loads(json.dumps({"version": 1, "bots": [{"role": "r"}]}))


# -- importing is additive -------------------------------------------------


def test_import_adds_missing_bots(tmp_path):
    orch = _orch(tmp_path)
    data = bundle.loads(
        json.dumps(
            {
                "version": 1,
                "bots": [{"name": "scout", "role": "a scout", "provider": "echo"}],
                "skills": [],
                "routines": [],
                "connectors": [],
            }
        )
    )
    report = bundle.apply(orch.paths, data, orch)
    assert report.added_bots == ["scout"]
    assert "scout" in {b.name for b in orch.bots()}


def test_import_never_overwrites_an_existing_bot(tmp_path):
    """Restoring onto a running deployment must not be able to lose work."""
    orch = _orch(tmp_path)
    before = next(b for b in orch.bots() if b.name == "atlas").role
    data = bundle.loads(
        json.dumps(
            {
                "version": 1,
                "bots": [{"name": "atlas", "role": "COMPLETELY DIFFERENT", "provider": "echo"}],
            }
        )
    )
    report = bundle.apply(orch.paths, data, orch)
    assert report.skipped_bots == ["atlas"]
    assert next(b for b in orch.bots() if b.name == "atlas").role == before


def test_import_never_clobbers_an_existing_soul(tmp_path):
    from agent.soul import load_soul, save_soul

    orch = _orch(tmp_path)
    orch.add_bot(name="scout", role="r", provider="echo")
    save_soul(orch.paths, "scout", "mine, written by me")
    data = bundle.loads(
        json.dumps(
            {
                "version": 1,
                "bots": [{"name": "scout", "role": "r", "provider": "echo", "soul": "theirs"}],
            }
        )
    )
    bundle.apply(orch.paths, data, orch)
    assert "mine" in load_soul(orch.paths, "scout")


def test_connectors_are_declared_not_created(tmp_path):
    """A connector imported without its credential would look configured and
    fail on every call — the hardest shape of failure to diagnose."""
    orch = _orch(tmp_path)
    data = bundle.loads(
        json.dumps(
            {
                "version": 1,
                "bots": [],
                "connectors": [{"id": "c1", "type": "linear", "name": "Linear"}],
            }
        )
    )
    report = bundle.apply(orch.paths, data, orch)
    assert report.connectors_declared == ["Linear"]
    assert not (orch.paths.home / "connectors.json").is_file()
    assert "credential" in report.summary()


def test_a_skill_for_an_unknown_bot_is_skipped(tmp_path):
    orch = _orch(tmp_path)
    data = bundle.loads(
        json.dumps(
            {
                "version": 1,
                "bots": [],
                "skills": [{"bot": "ghost", "id": "x", "name": "x", "body": "b"}],
            }
        )
    )
    report = bundle.apply(orch.paths, data, orch)
    assert report.added_skills == []


def test_the_summary_reads_as_a_sentence(tmp_path):
    orch = _orch(tmp_path)
    data = bundle.loads(json.dumps({"version": 1, "bots": []}))
    text = bundle.apply(orch.paths, data, orch).summary()
    assert "bots:" in text and "routines:" in text


# -- the two defects this feature exposed ----------------------------------


def test_a_failed_add_is_reported_as_failed_not_as_skipped(tmp_path):
    """`skipped` means "already here and left alone". Reporting a failure as a
    skip lets an import claim it did nothing wrong while having done nothing
    at all — which is how the machines-backend spawn failure below hid."""
    orch = _orch(tmp_path)

    def boom(**kw):
        raise RuntimeError("no docker here")

    orch.add_bot = boom
    data = bundle.loads(
        json.dumps({"version": 1, "bots": [{"name": "scout", "role": "r", "provider": "echo"}]})
    )
    report = bundle.apply(orch.paths, data, orch)
    assert report.skipped_bots == []
    assert "scout" in report.failed
    assert "no docker" in report.failed["scout"]
    assert "FAILED" in report.summary()


def test_import_registers_without_starting_anything(tmp_path):
    """Importing configuration must not require a container engine. On the
    machines backend `add_bot` provisions a machine, so an import on a laptop
    with no Docker failed once per bot."""
    orch = _orch(tmp_path)
    seen = {}

    real = orch.add_bot

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    orch.add_bot = spy
    data = bundle.loads(
        json.dumps({"version": 1, "bots": [{"name": "scout", "role": "r", "provider": "echo"}]})
    )
    bundle.apply(orch.paths, data, orch)
    assert seen.get("start") is False


def test_import_reports_corrupt_routines_and_continues_to_other_bots(tmp_path):
    from harness.routines import list_routines

    orch = _orch(tmp_path)
    path = orch.paths.bot_routines("atlas")
    path.parent.mkdir(parents=True, exist_ok=True)
    original = '{"routines": [{"id": "existing"}]} trailing data'
    path.write_text(original)
    data = bundle.loads(
        json.dumps(
            {
                "version": 1,
                "bots": [{"name": "scout", "role": "helper", "provider": "echo"}],
                "routines": [
                    {
                        "bot": "atlas",
                        "title": "Damaged destination",
                        "prompt": "Keep",
                        "cron": "0 8 * * *",
                    },
                    {
                        "bot": "scout",
                        "title": "Healthy destination",
                        "prompt": "Continue",
                        "cron": "0 9 * * *",
                    },
                ],
                "connectors": [{"id": "c1", "type": "linear", "name": "Linear"}],
            }
        )
    )
    report = bundle.apply(orch.paths, data, orch)
    assert "cannot read routines" in report.failed["routine Damaged destination"]
    assert report.added_routines == 1
    assert report.connectors_declared == ["Linear"]
    assert path.read_text() == original
    assert list_routines(orch.paths, "scout")[0]["prompt"] == "Continue"


def test_add_bot_still_starts_by_default(tmp_path):
    """The `start=False` escape hatch must not change what every other caller
    gets."""
    import inspect

    from harness.orchestrator import Orchestrator as O

    assert inspect.signature(O.add_bot).parameters["start"].default is True
