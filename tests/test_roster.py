import pytest

from harness.roster import Bot, Roster, RosterError, bot_slug, load_live_roster, load_roster

ROSTER = """
[[bots]]
name = "atlas"
role = "researcher"
personality = "precise"
provider = "echo"

[[bots]]
name = "nova"
role = "writer"
provider = "claude"
model = "claude-3-5-sonnet-latest"
auth_ref = "anthropic"
"""


def _write(tmp_path, text):
    p = tmp_path / "roster.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_roster_basic(tmp_path):
    roster = load_roster(_write(tmp_path, ROSTER))
    assert roster.names() == ["atlas", "nova"]
    nova = roster.get("nova")
    assert nova.provider == "claude"
    assert nova.model == "claude-3-5-sonnet-latest"
    assert nova.secret_ref() == "anthropic"


def test_reasoning_round_trips_the_roster(tmp_path):
    from harness.roster import save_roster

    path = tmp_path / "roster.json"
    save_roster(path, Roster(bots=[Bot(name="atlas", provider="codex", reasoning="high")]))
    loaded = load_roster(path)
    assert loaded.get("atlas").reasoning == "high"
    assert loaded.get("atlas").to_dict()["reasoning"] == "high"
    empty = Bot(name="x")
    assert empty.reasoning == ""


def test_caveman_override_round_trips_the_roster(tmp_path):
    from harness.roster import caveman_flag, save_roster

    p = tmp_path / "roster.toml"
    p.write_text(
        '[[bots]]\nname = "atlas"\ncaveman = true\n\n'
        '[[bots]]\nname = "nova"\ncaveman = false\n\n'
        '[[bots]]\nname = "rune"\n',
        encoding="utf-8",
    )
    roster = load_roster(p)
    assert roster.get("atlas").caveman is True
    assert roster.get("nova").caveman is False
    assert roster.get("rune").caveman is None
    assert Bot(name="x").caveman is None
    # The JSON store keeps the tri-state: null is "follow the account".
    j = tmp_path / "roster.json"
    save_roster(j, roster)
    again = load_roster(j)
    assert [b.caveman for b in again.bots] == [True, False, None]
    assert again.get("rune").to_dict()["caveman"] is None
    assert caveman_flag("on") is True and caveman_flag("off") is False
    assert caveman_flag("") is None and caveman_flag("inherit") is None
    with pytest.raises(RosterError):
        caveman_flag("sometimes")


def test_private_browser_round_trips_the_roster(tmp_path):
    roster = load_roster(
        _write(
            tmp_path,
            '[[bots]]\nname = "atlas"\nprivate_browser = true\n\n[[bots]]\nname = "nova"\n',
        )
    )
    assert roster.get("atlas").private_browser is True
    assert roster.get("nova").private_browser is False
    assert roster.get("atlas").to_dict()["private_browser"] is True
    assert Bot(name="x").private_browser is False


def test_display_name_skips_slug_title():
    bot = Bot(
        name="amazon-seller-manager",
        title="amazon-seller-manager",
        role="Amazon Seller Manager",
    )
    assert bot.display_name() == "Amazon Seller Manager"
    bot.title = "Amazon Seller Manager"
    assert bot.display_name() == "Amazon Seller Manager"


def test_roster_match_display_name_and_slug():
    roster = Roster(
        bots=[
            Bot(name="cloud-engineer", title="Cloud Engineer", role="owns AWS"),
            Bot(name="chief-of-staff", title="Chief of Staff"),
            Bot(name="atlas"),
        ]
    )
    hits = roster.match("Cloud Engineer")
    assert [b.name for b in hits] == ["cloud-engineer"]
    assert roster.match("cloud engineer")[0].name == "cloud-engineer"
    assert roster.match("cloud-engineer")[0].name == "cloud-engineer"
    none = roster.match("Cloud Engineer", exclude="cloud-engineer")
    assert none == []
    amb = roster.match("cloud")  # cloud-engineer only
    assert [b.name for b in amb] == ["cloud-engineer"]


def test_load_live_roster_prefers_json(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "roster.json").write_text(
        '{"bots": [{"name": "nova", "role": "writer", "provider": "echo"}]}',
        encoding="utf-8",
    )
    roster = load_live_roster(home)
    assert roster is not None
    assert roster.names() == ["nova"]


def test_system_prompt_includes_identity_and_handoff(tmp_path):
    roster = load_roster(_write(tmp_path, ROSTER))
    prompt = roster.get("atlas").system_prompt()
    assert "atlas" in prompt
    assert "researcher" in prompt
    assert "precise" in prompt
    assert "@<botname>" in prompt  # handoff instructions present


def test_missing_file_raises(tmp_path):
    with pytest.raises(RosterError):
        load_roster(tmp_path / "nope.toml")


def test_duplicate_names_raise(tmp_path):
    dup = ROSTER + '\n[[bots]]\nname = "atlas"\n'
    with pytest.raises(RosterError):
        load_roster(_write(tmp_path, dup))


def test_empty_roster_is_allowed(tmp_path):
    roster = load_roster(_write(tmp_path, "# no bots\n"))
    assert roster.names() == []
    json_path = tmp_path / "roster.json"
    json_path.write_text('{"bots": []}', encoding="utf-8")
    assert load_roster(json_path).names() == []


def test_unknown_bot_lookup_raises(tmp_path):
    roster = load_roster(_write(tmp_path, ROSTER))
    with pytest.raises(RosterError):
        roster.get("ghost")


def test_bot_slug_keeps_valid_ids():
    assert bot_slug("Test") == "Test"
    assert bot_slug("test2") == "test2"
    assert bot_slug("Chief of Staff") == "chief-of-staff"
    assert bot_slug("  ") == ""


def test_display_name_prefers_title_then_pretty_slug():
    assert Bot(name="Test").display_name() == "Test"
    assert Bot(name="chief-of-staff").display_name() == "Chief Of Staff"
    assert Bot(name="chief-of-staff", role="Chief of Staff").display_name() == "Chief of Staff"
    assert Bot(name="chief-of-staff", title="Chief of Staff").display_name() == "Chief of Staff"
    assert Bot(name="chief-of-staff", title="  CHIEF  ").display_name() == "CHIEF"
