import pytest

from agent.memory import Memory
from agent.runtime import build_agent
from agent.streaming import StreamReader, StreamWriter
from harness.control import Control
from harness.paths import HarnessPaths
from harness.roster import Bot


def _paths(tmp_path):
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas"])
    return p


def test_stream_writer_reader_roundtrip(tmp_path):
    paths = _paths(tmp_path)
    w = StreamWriter(paths, "req1")
    w.status("typing")
    w.delta("hello ")
    w.delta("world")
    w.final("hello world", "atlas")

    reader = StreamReader(paths, "req1")
    events = list(reader.events(timeout=2.0))
    types = [e.type for e in events]
    assert types[0] == "status"
    assert "".join(e.text for e in events if e.type == "delta") == "hello world"
    assert events[-1].type == "final"
    assert events[-1].text == "hello world"


def test_stream_writer_tool_event(tmp_path):
    paths = _paths(tmp_path)
    w = StreamWriter(paths, "t")
    w.tool("linear_list_issues", "active", "Checking Linear")
    w.tool("linear_list_issues", "done", "Checking Linear")
    w.final("ok", "atlas")
    events = list(StreamReader(paths, "t").events(timeout=2.0))
    # final is preceded by its closing full-content upsert
    assert [e.type for e in events] == ["tool", "tool", "message", "final"]
    assert events[0].name == "linear_list_issues"
    assert events[0].value == "active"
    assert events[0].text == "Checking Linear"


def test_stream_writer_tool_event_command_detail(tmp_path):
    paths = _paths(tmp_path)
    w = StreamWriter(paths, "cmd")
    w.tool("run_command", "active", "Running a command", title="echo hi")
    w.tool(
        "run_command",
        "done",
        "Ran a command",
        title="echo hi",
        detail="exit 0\nhi",
    )
    w.final("ok", "atlas")
    events = list(StreamReader(paths, "cmd").events(timeout=2.0))
    tools = [e for e in events if e.type == "tool"]
    assert tools[0].title == "echo hi"
    assert tools[1].text == "Ran a command"
    assert tools[1].detail == "exit 0\nhi"


def _agent(tmp_path, provider="echo"):
    paths = _paths(tmp_path)
    bot = Bot(name="atlas", role="a terse assistant", provider=provider)
    agent = build_agent(paths, bot, stream_delay=0.0)
    return agent, paths


def test_agent_streams_typing_deltas_and_final(tmp_path):
    agent, paths = _agent(tmp_path)
    writer = StreamWriter(paths, "r1")
    final = agent._produce("user", "hello there", writer=writer)

    events = list(StreamReader(paths, "r1").events(timeout=2.0))
    assert any(e.type == "status" and e.value == "typing" for e in events)
    streamed = "".join(e.text for e in events if e.type == "delta")
    assert streamed == final
    assert "hello there" in final


@pytest.mark.parametrize("origin", [None, "routine", "dream", "welcome"])
@pytest.mark.parametrize("text", ["hello there", "/memory"])
def test_final_stream_and_history_keep_the_same_origin_and_identity(tmp_path, origin, text):
    from agent.history import user_thread
    from harness.server import asdict_event
    from tests.test_transcript_merge import merge_history

    agent, paths = _agent(tmp_path)
    writer = StreamWriter(paths, "same-reply")
    agent._produce("user", text, writer=writer, origin=origin)
    events = StreamReader(paths, "same-reply")._read_new()
    final = asdict_event(next(e for e in events if e.type == "final"))
    upsert = next(e for e in reversed(events) if e.type == "message")
    history = next(r for r in reversed(user_thread(agent.memory)) if r.get("frm") == "atlas")
    assert final.get("origin") == history.get("origin") == origin
    assert upsert.id == history["message_id"]
    assert final.get("message_id") == history["message_id"]
    # Exercise the existing Mac/iOS merge contract with actual server output.
    live = {
        "kind": "assistant",
        "author": final["frm"],
        "text": final["text"],
        "origin": final.get("origin"),
    }
    saved = {
        "kind": "assistant",
        "author": history["frm"],
        "text": history["text"],
        "origin": history.get("origin"),
    }
    assert len(merge_history([live], [saved])) == 1


def test_agent_requests_takeover_when_stuck(tmp_path):
    agent, paths = _agent(tmp_path)
    writer = StreamWriter(paths, "r2")
    final = agent._produce("user", "I'm stuck, take over please", writer=writer)

    events = list(StreamReader(paths, "r2").events(timeout=2.0))
    assert any(e.type == "takeover" for e in events)
    assert "take over" in final.lower()
    # control state now reflects the request
    assert Control(paths).state("atlas").takeover_requested


def test_memory_written_during_stream(tmp_path):
    agent, paths = _agent(tmp_path)
    agent._produce("user", "remember the demo", writer=StreamWriter(paths, "r3"))
    assert Memory(paths=paths, bot="atlas").recall("demo")


# -- real provider-side streaming into the live path ----------------------
def _stream_agent(tmp_path, provider):
    from agent.runtime import Agent

    paths = _paths(tmp_path)
    bot = Bot(name="atlas", role="a terse assistant", provider="echo")
    agent = Agent(
        paths=paths,
        bot=bot,
        provider=provider,
        memory=Memory(paths=paths, bot="atlas"),
        control=Control(paths),
        stream_delay=0.0,
    )
    return agent, paths


def test_agent_streams_real_provider_deltas(tmp_path):
    """Provider-token deltas reach the stream verbatim, not word-chunked."""
    from providers.base import Completion, Provider

    class FakeStreaming(Provider):
        def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
            raise AssertionError("complete() must not be called when streaming works")

        def stream_completion(
            self,
            messages,
            *,
            system=None,
            tools=None,
            max_tokens=1024,
            temperature=0.7,
            on_delta=None,
        ):
            for chunk in ("hel", "lo wor", "ld"):
                on_delta(chunk)
            return Completion(text="hello world", finish_reason="stop")

    agent, paths = _stream_agent(tmp_path, FakeStreaming(model="m"))
    final = agent._produce("user", "greet me", writer=StreamWriter(paths, "s1"))

    events = list(StreamReader(paths, "s1").events(timeout=2.0))
    deltas = [e.text for e in events if e.type == "delta"]
    assert deltas == ["hel", "lo wor", "ld"]  # exact provider chunks
    # typing status precedes the first delta
    types = [e.type for e in events]
    assert types.index("status") < types.index("delta")
    assert events[-1].type == "final"
    assert events[-1].text == "hello world" == final


def test_multiplex_uses_idle_timeout(tmp_path):
    from agent.streaming import multiplex

    paths = _paths(tmp_path)
    StreamWriter(paths, "idle").status("thinking")
    reader = StreamReader(paths, "idle")
    got = list(multiplex([("atlas", reader)], timeout=0.25))
    assert not any(e.type == "final" for _n, e in got)


def test_multiplex_settles_on_choice(tmp_path):
    from agent.streaming import multiplex

    paths = _paths(tmp_path)
    w = StreamWriter(paths, "ask")
    w.choice("atlas", "c1", "when?", ["8am", "9am"])
    reader = StreamReader(paths, "ask")
    got = list(multiplex([("atlas", reader)], timeout=2.0))
    assert [e.type for _n, e in got] == ["choice"]


def test_multiplex_follows_choice_when_not_parking(tmp_path):
    from agent.streaming import multiplex

    paths = _paths(tmp_path)
    w = StreamWriter(paths, "ask2")
    w.choice("atlas", "c1", "when?", ["8am", "9am"])
    w.status("working")
    w.final("ok", "atlas")
    reader = StreamReader(paths, "ask2")
    got = list(multiplex([("atlas", reader)], timeout=2.0, park_prompts=False))
    # final is preceded by its closing full-content upsert
    assert [e.type for _n, e in got] == ["choice", "status", "message", "final"]


def test_multiplex_keeps_waiting_while_busy(tmp_path):
    from agent.streaming import multiplex

    paths = _paths(tmp_path)
    reader = StreamReader(paths, "later")
    calls = {"n": 0}

    def keep(_names):
        calls["n"] += 1
        if calls["n"] == 1:
            StreamWriter(paths, "later").final("ok", "atlas")
            return True
        return False

    got = list(multiplex([("atlas", reader)], timeout=0.15, keep_waiting=keep))
    assert any(e.type == "final" for _n, e in got)
    assert calls["n"] >= 1


def test_stop_ends_a_tool_loop(tmp_path):
    from providers.base import Completion, Provider, ToolCall

    class AlwaysTool(Provider):
        def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
            return Completion(
                tool_calls=[ToolCall(id="c1", name="remember", arguments={"text": "keep going"})],
                finish_reason="tool_use",
            )

    agent, paths = _stream_agent(tmp_path, AlwaysTool(model="m"))
    agent.control.request_stop("atlas")
    final = agent._produce("user", "loop forever", writer=StreamWriter(paths, "stop1"))
    assert final == "Stopped."


def test_tool_loop_emits_activity_events(tmp_path):
    from providers.base import Completion, Provider, ToolCall

    class OnceTool(Provider):
        def __init__(self, model):
            super().__init__(model)
            self.n = 0

        def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
            self.n += 1
            if self.n == 1:
                return Completion(
                    tool_calls=[ToolCall(id="c1", name="remember", arguments={"text": "x"})],
                    finish_reason="tool_use",
                )
            return Completion(text="done", finish_reason="stop")

    agent, paths = _stream_agent(tmp_path, OnceTool(model="m"))
    agent._produce("user", "remember x", writer=StreamWriter(paths, "act"))
    events = list(StreamReader(paths, "act").events(timeout=2.0))
    tools = [e for e in events if e.type == "tool"]
    assert [e.value for e in tools] == ["active", "done"]
    assert tools[0].text == "Checking memory"


def test_run_command_trail_includes_command_and_output(tmp_path):
    from providers.base import Completion, Provider, ToolCall

    class OnceCmd(Provider):
        def __init__(self, model):
            super().__init__(model)
            self.n = 0

        def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
            self.n += 1
            if self.n == 1:
                return Completion(
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="run_command",
                            arguments={"command": "echo hello-trail"},
                        )
                    ],
                    finish_reason="tool_use",
                )
            return Completion(text="done", finish_reason="stop")

    agent, paths = _stream_agent(tmp_path, OnceCmd(model="m"))
    agent._produce("user", "run it", writer=StreamWriter(paths, "runcmd"))
    tools = [e for e in StreamReader(paths, "runcmd").events(timeout=2.0) if e.type == "tool"]
    assert tools[0].text == "Running a command"
    assert tools[0].title == "echo hello-trail"
    assert tools[1].text == "Ran a command"
    assert tools[1].title == "echo hello-trail"
    assert "hello-trail" in (tools[1].detail or "")


def test_agent_streams_tool_turn_then_text_turn(tmp_path):
    from providers.base import Completion, Provider, ToolCall

    class FakeToolStreaming(Provider):
        def __init__(self, model):
            super().__init__(model)
            self.turn = 0

        def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
            raise AssertionError("complete() must not be called")

        def stream_completion(
            self,
            messages,
            *,
            system=None,
            tools=None,
            max_tokens=1024,
            temperature=0.7,
            on_delta=None,
        ):
            self.turn += 1
            if self.turn == 1:
                on_delta("Saving that. ")
                return Completion(
                    text="Saving that. ",
                    tool_calls=[ToolCall(id="c1", name="remember", arguments={"text": "demo tue"})],
                    finish_reason="tool_calls",
                )
            on_delta("Done.")
            return Completion(text="Done.", finish_reason="stop")

    agent, paths = _stream_agent(tmp_path, FakeToolStreaming(model="m"))
    final = agent._produce("user", "note the demo", writer=StreamWriter(paths, "s2"))

    assert final == "Done."
    # the tool actually executed against memory
    assert Memory(paths=paths, bot="atlas").recall("demo tue")
    events = list(StreamReader(paths, "s2").events(timeout=2.0))
    streamed = "".join(e.text for e in events if e.type == "delta")
    # interim text, a separator, then the final turn's text
    assert streamed == "Saving that. \nDone."
    assert events[-1].text == "Done."


def test_agent_falls_back_to_complete_when_stream_fails_early(tmp_path):
    from providers import ProviderError
    from providers.base import Completion, Provider

    class FlakyStream(Provider):
        def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
            return Completion(text="fallback answer", finish_reason="stop")

        def stream_completion(self, messages, **kwargs):
            raise ProviderError("stream endpoint down")

    agent, paths = _stream_agent(tmp_path, FlakyStream(model="m"))
    final = agent._produce("user", "hi", writer=StreamWriter(paths, "s3"))

    assert final == "fallback answer"
    events = list(StreamReader(paths, "s3").events(timeout=2.0))
    # fallback path uses the word-chunk typing effect
    streamed = "".join(e.text for e in events if e.type == "delta")
    assert streamed == "fallback answer"
    assert events[-1].type == "final"


# -- create-bot step labels --------------------------------------
def test_choice_steps_read_as_a_flow_not_three_identical_rows():
    from agent.runtime import _tool_step_label

    def label(question: str) -> str:
        return _tool_step_label("ask_user_choice", {"question": question})

    assert label("What should I name the personal assistant bot?") == "Choosing a name"
    assert label("What role should it have?") == "Choosing a role"
    assert label("Which provider should it use?") == "Choosing a provider"
    # an unrelated question keeps the generic label
    assert label("Tea or coffee?") == "Waiting for a choice"


def test_create_bot_trail_names_the_bot(tmp_path):
    from providers.base import Completion, Provider, ToolCall

    class OnceCreate(Provider):
        def __init__(self, model):
            super().__init__(model)
            self.n = 0

        def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
            self.n += 1
            if self.n == 1:
                return Completion(
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="create_bot",
                            arguments={"name": "pa", "title": "Personal Assistant"},
                        )
                    ],
                    finish_reason="tool_use",
                )
            return Completion(text="done", finish_reason="stop")

    agent, paths = _stream_agent(tmp_path, OnceCreate(model="m"))
    agent._produce("user", "make one", writer=StreamWriter(paths, "mkbot"))
    tools = [e for e in StreamReader(paths, "mkbot").events(timeout=2.0) if e.type == "tool"]
    assert tools[0].text == "Creating Personal Assistant"
    # no server is running here, so the call errors and keeps the active wording
    assert (tools[1].value, tools[1].text) == ("error", "Creating Personal Assistant")


def test_create_bot_trail_says_created_when_it_works():
    from agent.runtime import _emit_trail_tool

    seen: list[tuple] = []

    class Writer:
        def tool(self, name, state, label, *, title=None, detail=None):
            seen.append((state, label))

    args = {"name": "pa", "title": "Personal Assistant"}
    _emit_trail_tool(Writer(), "create_bot", "done", args, "ok: created bot 'pa'")
    assert seen == [("done", "Created Personal Assistant")]


# -- self-healing full-content upserts ---------------------------
def test_stream_writer_message_upserts_converge(tmp_path):
    """Upserts re-send the WHOLE text at rate-limited boundaries; the first
    (the `appended` insert) and the closing frame are never throttled, so a
    client that drops any frame converges on the final text."""
    paths = _paths(tmp_path)
    w = StreamWriter(paths, "up1")
    w.delta("hel")
    w.delta("lo")  # inside the rate-limit window: no upsert for this chunk
    w.final("hello", "atlas")

    events = list(StreamReader(paths, "up1").events(timeout=2.0))
    msgs = [e for e in events if e.type == "message"]
    assert msgs[0].mutation == "appended"
    assert msgs[0].streaming is True
    assert [m.mutation for m in msgs[1:]] == ["updated"] * (len(msgs) - 1)
    assert msgs[-1].streaming is False
    assert msgs[-1].text == "hello"
    assert {m.id for m in msgs} == {"msg-up1"}
    # every upsert is a prefix of the final text (monotonic convergence)
    assert all("hello".startswith(m.text) for m in msgs)
    # the closing upsert lands BEFORE final, so bubbles settle before the
    # stream ends (a client's final handler clears the live-typing state)
    assert [e.type for e in events][-2:] == ["message", "final"]
    # legacy deltas still reconstruct the same text for old clients
    assert "".join(e.text for e in events if e.type == "delta") == "hello"


def test_stream_writer_upserts_are_rate_limited(tmp_path):
    """A dense chunk burst must not re-send the accumulated text per chunk —
    that was O(n²) per turn across the file, the relay and every client. The
    stream stays correct: all deltas present, closing upsert authoritative."""
    paths = _paths(tmp_path)
    w = StreamWriter(paths, "up-burst")
    chunks = [f"c{i} " for i in range(200)]
    for c in chunks:  # far faster than the 100ms/512B upsert boundaries
        w.delta(c)
    full = "".join(chunks)
    w.final(full, "atlas")

    events = list(StreamReader(paths, "up-burst").events(timeout=2.0))
    deltas = [e for e in events if e.type == "delta"]
    msgs = [e for e in events if e.type == "message"]
    assert "".join(d.text for d in deltas) == full
    # first upsert immediate + size-boundary resyncs + closing frame, but
    # nowhere near one per chunk
    assert 2 <= len(msgs) < len(chunks) // 4
    assert msgs[0].mutation == "appended"
    assert msgs[-1].streaming is False and msgs[-1].text == full


def test_stream_final_upsert_is_authoritative(tmp_path):
    """Interim tool-loop text streams, but the closing upsert carries the
    final text — the full-content model self-corrects any divergence."""
    paths = _paths(tmp_path)
    w = StreamWriter(paths, "up2")
    w.delta("thinking out loud")
    w.final("the real answer", "atlas")

    events = list(StreamReader(paths, "up2").events(timeout=2.0))
    closing = [e for e in events if e.type == "message" and e.streaming is False]
    assert len(closing) == 1
    assert closing[0].text == "the real answer"
    assert closing[0].mutation == "updated"


def test_final_without_deltas_appends_message(tmp_path):
    """First sight of an id is `appended` even when it is the closing frame —
    clients ignore an `updated` for an unknown id, so it must never be one."""
    paths = _paths(tmp_path)
    w = StreamWriter(paths, "up3")
    w.final("hi", "atlas")
    msgs = [e for e in StreamReader(paths, "up3").events(timeout=2.0) if e.type == "message"]
    assert [(m.mutation, m.streaming, m.text) for m in msgs] == [("appended", False, "hi")]


def test_card_mutation_appended_then_updated(tmp_path):
    """Cards share the one mutation model: first emit inserts, re-emits update."""
    paths = _paths(tmp_path)
    w = StreamWriter(paths, "c1")
    w.card("atlas", "card-1", "progress", {"title": "Working"})
    w.card("atlas", "card-1", "progress", {"title": "Done"})
    w.card("atlas", "card-2", "progress", {"title": "Other"})
    w.final("ok", "atlas")
    cards = [e for e in StreamReader(paths, "c1").events(timeout=2.0) if e.type == "card"]
    assert [(c.id, c.mutation) for c in cards] == [
        ("card-1", "appended"),
        ("card-1", "updated"),
        ("card-2", "appended"),
    ]


def test_agent_stream_message_upserts_match_final(tmp_path):
    """The live agent path emits converging upserts alongside its deltas."""
    agent, paths = _agent(tmp_path)
    final = agent._produce("user", "hello there", writer=StreamWriter(paths, "r-up"))
    msgs = [e for e in StreamReader(paths, "r-up").events(timeout=2.0) if e.type == "message"]
    assert msgs, "no message upserts on the agent path"
    assert msgs[0].mutation == "appended"
    assert all(m.mutation == "updated" for m in msgs[1:])
    assert msgs[-1].streaming is False
    assert msgs[-1].text == final
