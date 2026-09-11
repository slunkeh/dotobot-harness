"""Routine outcomes remain discoverable from another conversation after restart."""

from datetime import datetime

from agent.memory import Memory
from agent.tools import ToolContext, default_tools
from harness.paths import HarnessPaths


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


def seed(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    mem = Memory(paths, "social")
    query = "today Reddit published replies verified posts example-owner links"
    for day in range(1, 9):
        mem.log_turn(
            "session",
            "in:user",
            "[Routine: Reddit]\n" + query * 5,
            origin="routine",
            ts=stamp(f"2026-09-{day:02d}T08:00:00+01:00"),
        )
        mem.log_turn(
            "session",
            "out",
            "I cannot find " + query,
            room="group",
            ts=stamp(f"2026-09-{day:02d}T08:01:00+01:00"),
        )
    for date, link in [("07", "old"), ("08", "first"), ("08", "second")]:
        mem.log_turn(
            "session",
            "out",
            f"Posted the approved reply as example-owner and verified it in https://old.reddit.com/{link}",
            origin="routine",
            ts=stamp(f"2026-09-{date}T09:32:00+01:00"),
        )
    mem.log_turn(
        "session",
        "out",
        "Chrome could not read Reddit threads or Reddit rules. No replies suggested.",
        origin="routine",
        ts=stamp("2026-09-08T12:00:00+01:00"),
    )
    return paths, query


def test_existing_recall_finds_routine_results_among_repeated_instructions(tmp_path):
    paths, query = seed(tmp_path)
    # A new instance reads persisted records; no live-turn or 1:1 history needed.
    mem = Memory(paths, "social")
    hits = mem.recall(query)
    assert len(hits) <= 5
    assert {"first", "second"} <= {
        h["text"].rsplit("/", 1)[-1] for h in hits if h.get("origin") == "routine"
    }
    assert "reddit.com/first" in mem.context_block(query)
    assert Memory(paths, "other").recall(query) == []


def test_routine_results_filter_and_date_window_preserve_repeated_runs(tmp_path):
    paths, _ = seed(tmp_path)
    mem = Memory(paths, "social")
    for instant in ["2026-09-07T23:30:00+00:00", "2026-09-08T22:59:59+00:00"]:
        mem.log_turn("session", "out", "Nothing posted.", origin="routine", ts=stamp(instant))
    mem.log_turn(
        "session", "out", "Outside window", origin="routine", ts=stamp("2026-09-08T23:00:00+00:00")
    )
    mem.log_turn(
        "session",
        "out",
        "Summary",
        origin="routine",
        is_summary=True,
        ts=stamp("2026-09-08T09:00:00+00:00"),
    )
    hits = mem.recall(
        "",
        source="routine_results",
        since=stamp("2026-09-08T00:00:00+01:00"),
        before=stamp("2026-09-09T00:00:00+01:00"),
        limit=20,
    )
    assert len(hits) == 5
    assert sum(h["text"] == "Nothing posted." for h in hits) == 2
    assert all(h.get("origin") == "routine" and h["role"] == "out" for h in hits)
    assert [h["ts"] for h in hits] == sorted([h["ts"] for h in hits], reverse=True)


def test_recall_tool_returns_dated_pages_with_links(tmp_path):
    paths, _ = seed(tmp_path)
    ctx = ToolContext(paths=paths, bot="social", memory=Memory(paths, "social"))
    tool = default_tools()["recall"]
    args = dict(
        query="example-owner",
        source="routine_results",
        since="2026-09-08T00:00:00+01:00",
        before="2026-09-09T00:00:00+01:00",
        limit=1,
    )
    first = tool.handler(ctx, args)
    second = tool.handler(ctx, {**args, "offset": 1})
    assert "2026-09-08T08:32:00+00:00" in first
    assert "routine" in first
    assert "next_offset=1" in first
    assert "next_offset" not in second
    assert "reddit.com/first" in first + second
    assert "reddit.com/second" in first + second
    assert "reddit.com/old" not in first + second


def test_recall_tool_rejects_ambiguous_date_instead_of_searching_all_time(tmp_path):
    paths, _ = seed(tmp_path)
    ctx = ToolContext(paths=paths, bot="social", memory=Memory(paths, "social"))
    result = default_tools()["recall"].handler(ctx, {"query": "reddit", "since": "today"})
    assert result.startswith("error:")
    assert "ISO" in result


def test_general_recall_pagination_has_no_missing_or_duplicate_records(tmp_path):
    paths, query = seed(tmp_path)
    ctx = ToolContext(paths=paths, bot="social", memory=Memory(paths, "social"))
    tool = default_tools()["recall"]
    pages = [tool.handler(ctx, dict(query=query, limit=1, offset=n)) for n in range(6)]
    import json

    ids = [
        json.loads(
            next(line for line in page.splitlines() if line.startswith("- "))[2:].split("} ", 1)[0]
            + "}"
        )["message_id"]
        for page in pages
    ]
    assert len(set(ids)) == 6
    assert "reddit.com/first" in pages[0] + pages[1]
    assert "reddit.com/second" in pages[0] + pages[1]


def test_group_turn_can_recall_saved_routine_results_through_tool_loop(tmp_path):
    from agent.runtime import build_agent
    from harness.rooms import create_room
    from harness.roster import Bot
    from providers.base import Completion, Provider, ToolCall

    paths, _ = seed(tmp_path)
    room = create_room(paths, "Team", ["social", "other"])

    class Reporter(Provider):
        def complete(self, messages, **kwargs):
            results = [m.content for m in messages if m.role == "tool"]
            if not results:
                return Completion(
                    tool_calls=[
                        ToolCall(
                            "saved-work",
                            "recall",
                            {
                                "query": "",
                                "source": "routine_results",
                                "since": "2026-09-08T00:00:00+01:00",
                                "before": "2026-09-09T00:00:00+01:00",
                            },
                        )
                    ]
                )
            assert "reddit.com/first" in results[-1]
            assert "reddit.com/second" in results[-1]
            assert "reddit.com/old" not in results[-1]
            assert "2026-09-08T08:32:00+00:00" in results[-1]
            return Completion(
                text="The saved results report two Reddit replies.", finish_reason="stop"
            )

    agent = build_agent(paths, Bot(name="social", provider="echo"), stream_delay=0)
    agent.provider = Reporter("offline")
    assert "two Reddit replies" in agent._produce(
        "user", "List today's Reddit replies", room=room.id
    )
