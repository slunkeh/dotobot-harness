from agent.commands import catalog, find_skill, is_builtin, parse_slash
from agent.mentions import has_everyone, leading_mention, parse_mentions, resolve_mentions
from agent.skills import propose_skill
from harness.colors import PALETTE, bot_color
from harness.paths import HarnessPaths


def test_parse_mentions_order_and_dedupe():
    assert parse_mentions("hi @atlas and @nova and @atlas") == ["atlas", "nova"]
    assert parse_mentions("email foo@example.com") == []
    assert leading_mention("@nova draft this") == "nova"
    assert leading_mention("please @nova") is None


def test_resolve_mentions_uses_roster_casing():
    assert resolve_mentions("Hey @ATLAS", ["atlas", "nova"]) == ["atlas"]
    assert resolve_mentions("@ghost hi", ["atlas"]) == []


def test_has_everyone_mention():
    assert has_everyone("give me your latest @everyone")
    assert has_everyone("@Everyone ping")
    assert not has_everyone("hey @atlas")
    assert parse_mentions("@everyone and @atlas") == ["everyone", "atlas"]


def test_parse_slash_and_builtins():
    cmd = parse_slash("/memory launch date")
    assert cmd is not None
    assert cmd.name == "memory"
    assert cmd.rest == "launch date"
    assert is_builtin("soul")
    assert not is_builtin("cite-sources")
    assert parse_slash("not a command") is None
    inline = parse_slash("please use /cite-sources on this")
    assert inline is not None
    assert inline.name == "cite-sources"
    assert "please use" in inline.rest
    assert "on this" in inline.rest
    assert parse_slash("see http://example.com/path") is None


def test_catalog_includes_builtins_and_private_skill(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    propose_skill(
        paths,
        "atlas",
        name="brief-me",
        description="write a short brief",
        body="Keep it to 3 bullets.",
        when_to_use="user wants a briefing",
    )
    names = {i["name"] for i in catalog(paths, "atlas")}
    assert "memory" in names
    assert "soul" in names
    assert "stop" in names
    assert "queue" in names
    assert "brief-me" in names
    assert find_skill(paths, "atlas", "brief_me") is not None

    propose_skill(
        paths,
        "atlas",
        name="Order Groceries",
        description="buy the weekly shop",
        body="1. open the store",
    )
    names = {i["name"] for i in catalog(paths, "atlas")}
    assert "order-groceries" in names
    assert "Order Groceries" not in names
    assert find_skill(paths, "atlas", "order-groceries") is not None


def test_bot_color_is_stable_and_in_palette():
    assert bot_color("atlas") == bot_color("ATLAS")
    assert bot_color("atlas") in PALETTE
    assert bot_color("nova") in PALETTE
