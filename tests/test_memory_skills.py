from agent.memory import Memory
from agent.skills import (
    SkillError,
    load_skills,
    missing_procedure_headings,
    procedure_body,
    propose_skill,
    skill_slug,
    skills_prompt,
)
from harness.paths import HarnessPaths


def _paths(tmp_path):
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas"])
    return p


def test_memory_remember_and_recall(tmp_path):
    mem = Memory(paths=_paths(tmp_path), bot="atlas")
    mem.remember("the sky demo happens on Tuesday")
    mem.remember("nova prefers short sentences")
    hits = mem.recall("demo tuesday")
    assert hits
    assert "Tuesday" in hits[0]["text"]


def test_memory_is_private_per_bot(tmp_path):
    paths = _paths(tmp_path)
    Memory(paths=paths, bot="atlas").remember("atlas secret")
    nova = Memory(paths=paths, bot="nova")
    assert nova.recall("atlas secret") == []


def test_session_recall_after_logging(tmp_path):
    mem = Memory(paths=_paths(tmp_path), bot="atlas")
    mem.log_turn("s1", "in:user", "please research widgets")
    mem.log_turn("s1", "out", "widgets are small")
    hits = mem.recall("widgets")
    assert any("widget" in h["text"].lower() for h in hits)


def test_context_block_renders_recent(tmp_path):
    mem = Memory(paths=_paths(tmp_path), bot="atlas")
    mem.remember("fact one")
    block = mem.context_block()
    assert "fact one" in block


def test_propose_and_load_skill(tmp_path):
    paths = _paths(tmp_path)
    propose_skill(
        paths,
        "atlas",
        name="cite sources",
        description="always cite",
        body="1. find source\n2. cite it",
        when_to_use="user asks for a fact",
    )
    skills = load_skills(paths, "atlas")
    assert any(s.name == "cite sources" for s in skills)
    prompt = skills_prompt(paths, "atlas")
    assert "cite sources" in prompt
    assert "user asks for a fact" in prompt
    assert "load the body when relevant" not in prompt


def test_shared_facts_publish_and_read(tmp_path):
    from agent.memory import shared_facts

    paths = _paths(tmp_path)
    paths.ensure_layout(["atlas", "nova"])
    Memory(paths=paths, bot="atlas").publish_fact("the deploy key lives in vault")
    Memory(paths=paths, bot="nova").publish_fact("standup moved to 9am")

    facts = shared_facts(paths)
    assert [f["bot"] for f in facts] == ["atlas", "nova"]

    hits = shared_facts(paths, query="deploy")
    assert len(hits) == 1
    assert hits[0]["bot"] == "atlas"

    assert shared_facts(paths, query="zzz-nothing") == []


def test_learning_loop_tools(tmp_path):
    """Bots can author skills and share facts through the tool surface."""
    from agent.memory import shared_facts
    from agent.tools import ToolContext, default_tools

    paths = _paths(tmp_path)
    paths.ensure_layout(["atlas", "nova"])
    tools = default_tools()
    atlas = ToolContext(paths=paths, bot="atlas", memory=Memory(paths=paths, bot="atlas"))

    body = procedure_body(
        when="travel requests",
        inputs="a browser session",
        sequence="1. open browser\n2. search",
        validate="a booking confirmation is on screen",
        returns="the confirmation code",
        approval="paying",
    )
    out = tools["propose_skill"].handler(
        atlas,
        {
            "name": "book-flights",
            "description": "how to book a flight",
            "body": body,
            "when_to_use": "travel requests",
        },
    )
    assert out.startswith("ok:")
    assert "/book-flights" in out
    assert any(s.name == "book-flights" for s in load_skills(paths, "atlas"))
    assert tools["propose_skill"].handler(atlas, {"name": "x"}).startswith("error:")
    incomplete = tools["propose_skill"].handler(
        atlas,
        {
            "name": "half-done",
            "description": "missing headings",
            "body": "1. open browser\n2. search",
        },
    )
    assert incomplete.startswith("error:")
    assert "When to use" in incomplete
    loaded = tools["load_skill"].handler(atlas, {"name": "book-flights"})
    assert "open browser" in loaded
    via_path = tools["load_skill"].handler(
        atlas,
        {"path": "shared/memory/atlas/skills/book-flights/SKILL.md"},
    )
    assert "open browser" in via_path
    assert tools["load_skill"].handler(atlas, {"name": "nope"}).startswith("error:")

    assert tools["publish_fact"].handler(atlas, {"text": "wifi password rotated"}) == (
        "ok: published to shared facts"
    )
    # visible from another bot's context, with attribution
    nova = ToolContext(paths=paths, bot="nova", memory=Memory(paths=paths, bot="nova"))
    listing = tools["read_shared_facts"].handler(nova, {})
    assert "[atlas] wifi password rotated" in listing
    assert "wifi" in tools["read_shared_facts"].handler(nova, {"query": "wifi"})
    # but private memory stays private
    assert (
        shared_facts(paths, query="wifi") and Memory(paths=paths, bot="nova").recall("wifi") == []
    )


def test_propose_skill_wraps_incomplete_bodies_unless_strict(tmp_path):
    paths = _paths(tmp_path)
    path = propose_skill(
        paths,
        "atlas",
        name="loose",
        description="legacy",
        body="1. do the thing",
    )
    text = path.read_text(encoding="utf-8")
    assert not missing_procedure_headings(text)
    assert "1. do the thing" in text
    try:
        propose_skill(
            paths,
            "atlas",
            name="strict",
            description="must be complete",
            body="1. do the thing",
            strict=True,
        )
        raise AssertionError("strict propose_skill should reject")
    except SkillError as exc:
        assert "When to use" in str(exc)


def test_propose_skill_slugs_pretty_names(tmp_path):
    paths = _paths(tmp_path)
    path = propose_skill(
        paths,
        "atlas",
        name="Order Groceries",
        description="buy the weekly shop",
        body="1. open the store",
    )
    assert path.parent.name == "order-groceries"
    text = path.read_text(encoding="utf-8")
    assert "name: Order Groceries" in text
    assert skill_slug("Order Groceries") == "order-groceries"
    skills = load_skills(paths, "atlas")
    found = next(s for s in skills if s.skill_id == "order-groceries")
    assert found.name == "Order Groceries"


def _procedure():
    return procedure_body(
        when="the weekly shop",
        inputs="a signed-in store session",
        sequence="1. search for {item}\n2. add to basket",
        validate="the item is in the basket",
        returns="a confirmation",
        approval="checkout",
    )


def test_propose_skill_prompts_then_saves(tmp_path):
    import json
    import threading
    import time

    from agent.streaming import StreamReader, StreamWriter, write_answer
    from agent.tools import ToolContext, default_tools

    paths = _paths(tmp_path)
    writer = StreamWriter(paths, "r-skill")
    ctx = ToolContext(
        paths=paths,
        bot="atlas",
        memory=Memory(paths=paths, bot="atlas"),
        writer=writer,
        user_input_timeout=5.0,
    )
    body = _procedure()

    def submit():
        deadline = time.time() + 4
        while time.time() < deadline:
            ev = next(
                (e for e in StreamReader(paths, "r-skill")._read_new() if e.type == "block"),
                None,
            )
            if ev:
                write_answer(
                    paths,
                    ev.id,
                    json.dumps(
                        {
                            "action": "submit",
                            "values": {
                                "name": "order-milk",
                                "description": "buy milk",
                                "body": body,
                            },
                        }
                    ),
                )
                return
            time.sleep(0.05)

    t = threading.Thread(target=submit)
    t.start()
    out = default_tools()["propose_skill"].handler(
        ctx,
        {
            "name": "Order Groceries",
            "description": "buy the weekly shop",
            "body": body,
        },
    )
    t.join()
    assert out.startswith("ok:")
    assert "/order-milk" in out
    assert any(s.skill_id == "order-milk" for s in load_skills(paths, "atlas"))
    events = StreamReader(paths, "r-skill")._read_new()
    assert any(e.type == "block" and e.blocking for e in events)
    assert any(e.type == "skill_saved" and e.name == "order-milk" for e in events)


def test_propose_skill_discard_does_not_write(tmp_path):
    import json
    import threading
    import time

    from agent.streaming import StreamReader, StreamWriter, write_answer
    from agent.tools import ToolContext, default_tools

    paths = _paths(tmp_path)
    writer = StreamWriter(paths, "r-discard")
    ctx = ToolContext(
        paths=paths,
        bot="atlas",
        memory=Memory(paths=paths, bot="atlas"),
        writer=writer,
        user_input_timeout=5.0,
    )
    body = _procedure()

    def discard():
        deadline = time.time() + 4
        while time.time() < deadline:
            ev = next(
                (e for e in StreamReader(paths, "r-discard")._read_new() if e.type == "block"),
                None,
            )
            if ev:
                write_answer(paths, ev.id, json.dumps({"action": "discard", "values": {}}))
                return
            time.sleep(0.05)

    t = threading.Thread(target=discard)
    t.start()
    out = default_tools()["propose_skill"].handler(
        ctx,
        {"name": "order-milk", "description": "buy milk", "body": body},
    )
    t.join()
    assert "discarded" in out
    assert not any(s.skill_id == "order-milk" for s in load_skills(paths, "atlas"))


def test_propose_skill_skips_prompt_on_dream(tmp_path):
    from agent.streaming import StreamReader, StreamWriter
    from agent.tools import ToolContext, default_tools

    paths = _paths(tmp_path)
    writer = StreamWriter(paths, "r-dream")
    ctx = ToolContext(
        paths=paths,
        bot="atlas",
        memory=Memory(paths=paths, bot="atlas"),
        writer=writer,
        origin="dream",
        user_input_timeout=0.4,
    )
    out = default_tools()["propose_skill"].handler(
        ctx,
        {
            "name": "deploy-runbook",
            "description": "how we deploy",
            "body": _procedure(),
        },
    )
    assert out.startswith("ok:")
    assert any(s.skill_id == "deploy-runbook" for s in load_skills(paths, "atlas"))
    types = {e.type for e in StreamReader(paths, "r-dream")._read_new()}
    assert "block" not in types
    assert "skill_saved" in types
