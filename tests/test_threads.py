"""Side threads: parent stays on the main 1:1; replies live off to the side."""

from __future__ import annotations

import json
import threading
import urllib.request

from agent.history import (
    _SECONDARY_HEAD,
    _THREAD_STARTED,
    build_history,
    message_id_of,
    secondary_main_block,
    thread_context_block,
    thread_history_messages,
    thread_root_id,
    user_thread,
)
from agent.memory import Memory
from agent.messaging import Msg, send, take_steer
from agent.runtime import Agent
from harness.control import Control
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.roster import Bot
from harness.server import make_server
from harness.statestore import SCHEMA_VERSION, StateStore
from providers.base import Completion, Message, Provider
from providers.echo import EchoProvider


class _RecordingProvider(Provider):
    id = "recording"

    def __init__(self, model="rec-1", auth=None, **options):
        super().__init__(model, auth, **options)
        self.calls: list[list[Message]] = []
        self.systems: list[str] = []

    def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
        self.calls.append(list(messages))
        self.systems.append(system or "")
        return Completion(text="ok", finish_reason="stop")


def _memory(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    return Memory(paths=paths, bot="atlas"), paths


def test_log_turn_assigns_a_message_id(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "hello", peer="user")
    rec = memory._session_records()[0]
    assert rec["message_id"]
    assert message_id_of(rec) == rec["message_id"]


def test_log_turn_keeps_a_caller_message_id(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "hello", peer="user", message_id="root-1")
    assert memory._session_records()[0]["message_id"] == "root-1"


def test_main_history_hides_side_thread_replies_and_counts_them(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "parent", peer="user", message_id="root-1")
    memory.log_turn("s1", "out", "ok", peer="user", message_id="root-2")
    memory.log_turn(
        "s1", "in:user", "in the thread", peer="user", thread_id="root-2", message_id="t-1"
    )
    memory.log_turn("s1", "out", "thread reply", peer="user", thread_id="root-2", message_id="t-2")
    main = user_thread(memory, peer="user")
    assert [r["text"] for r in main] == ["parent", "ok"]
    assert main[1]["message_id"] == "root-2"
    assert main[1]["reply_count"] == 2
    assert "reply_count" not in main[0]


def test_thread_page_orders_by_timestamp_across_sessions(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "out", "parent", peer="user", message_id="root-2")
    memory.log_turn(
        "20260901-080000",
        "in:user",
        "later reply",
        peer="user",
        thread_id="root-2",
        message_id="t-1",
    )
    rows = user_thread(memory, peer="user", thread_id="root-2")
    assert [r["text"] for r in rows] == ["parent", "later reply"]


def test_thread_page_returns_parent_plus_replies(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "parent", peer="user", message_id="root-1")
    memory.log_turn("s1", "out", "ok", peer="user", message_id="root-2")
    memory.log_turn(
        "s1", "in:user", "in the thread", peer="user", thread_id="root-2", message_id="t-1"
    )
    memory.log_turn("s1", "out", "thread reply", peer="user", thread_id="root-2", message_id="t-2")
    rows = user_thread(memory, peer="user", thread_id="root-2")
    assert [r["text"] for r in rows] == ["ok", "in the thread", "thread reply"]
    assert rows[1]["thread_id"] == "root-2"


def test_build_history_excludes_side_thread_replies(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "main ask", peer="user", message_id="m1")
    memory.log_turn("s1", "out", "main answer", peer="user", message_id="m2")
    memory.log_turn("s1", "in:user", "thread ask", peer="user", thread_id="m2", message_id="t1")
    messages, _cutoff = build_history(memory, peer="user", provider=Provider(model="m"))
    assert [(m.role, m.content) for m in messages] == [
        ("user", "main ask"),
        ("assistant", "main answer"),
    ]


def test_thread_turn_logs_replies_off_the_main_feed(tmp_path):
    memory, paths = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "Want a dry run?", peer="user", message_id="root-1")
    memory.log_turn("s1", "out", "Sure — r/ChatGPT?", peer="user", message_id="root-2")
    agent = Agent(
        paths=paths,
        bot=Bot(name="atlas", role="assistant", provider="echo"),
        provider=EchoProvider(),
        memory=memory,
        control=Control(paths),
        stream_delay=0.0,
    )
    agent.session_id = "s1"
    send(
        paths,
        Msg(
            to="atlas",
            frm="user",
            text="No dry run. What's next?",
            thread_id="root-2",
            message_id="t-1",
        ),
    )
    agent.process_inbox_once()
    main = user_thread(memory, peer="user")
    assert [r["text"] for r in main] == ["Want a dry run?", "Sure — r/ChatGPT?"]
    thread = user_thread(memory, peer="user", thread_id="root-2")
    assert any("No dry run" in (r.get("text") or "") for r in thread)
    assert any(r.get("frm") == "atlas" for r in thread)
    assert main[-1]["reply_count"] >= 1


def test_thread_root_id_snaps_a_reply_to_the_parent(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "out", "parent", peer="user", message_id="root-2")
    memory.log_turn("s1", "in:user", "reply", peer="user", thread_id="root-2", message_id="t-1")
    assert thread_root_id(memory, "root-2") == "root-2"
    assert thread_root_id(memory, "t-1") == "root-2"
    assert thread_root_id(memory, "unknown") == "unknown"


def test_produce_snaps_nested_thread_id_onto_the_root(tmp_path):
    memory, paths = _memory(tmp_path)
    memory.log_turn("s1", "out", "parent", peer="user", message_id="root-2")
    memory.log_turn(
        "s1", "in:user", "first reply", peer="user", thread_id="root-2", message_id="t-1"
    )
    agent = Agent(
        paths=paths,
        bot=Bot(name="atlas", role="assistant", provider="echo"),
        provider=EchoProvider(),
        memory=memory,
        control=Control(paths),
        stream_delay=0.0,
    )
    agent.session_id = "s1"
    agent._produce("user", "no nesting", thread_id="t-1", message_id="t-2")
    thread = user_thread(memory, peer="user", thread_id="root-2")
    assert any("no nesting" in (r.get("text") or "") for r in thread)
    assert not any(r.get("thread_id") == "t-1" for r in memory._session_records())


def test_send_assigns_a_message_id(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    path = send(paths, Msg(to="atlas", frm="user", text="hello"))
    stored = Msg.from_file(path)
    assert stored.message_id
    assert stored.message_id.isalnum()


def test_thread_history_messages_are_the_primary_turns(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "out", "Want a dry run?", peer="user", message_id="root-2")
    memory.log_turn(
        "s1", "in:user", "already in thread", peer="user", thread_id="root-2", message_id="t-0"
    )
    memory.log_turn("s1", "in:user", "live ask", peer="user", thread_id="root-2", message_id="t-1")
    msgs = thread_history_messages(memory, "root-2", exclude_message_id="t-1")
    assert msgs[0].role == "user"
    assert _THREAD_STARTED in msgs[0].content
    assert "Want a dry run?" in msgs[0].content
    assert "already in thread" in msgs[0].content
    assert not any("live ask" in (m.content or "") for m in msgs)


def test_secondary_main_block_is_the_non_thread_chat(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "Learn from demonstration.", peer="user", message_id="m1")
    memory.log_turn("s1", "out", "Want a dry run?", peer="user", message_id="root-2")
    memory.log_turn(
        "s1", "in:user", "thread only", peer="user", thread_id="root-2", message_id="t-1"
    )
    block = secondary_main_block(memory, omit_message_id="root-2")
    assert block.startswith(_SECONDARY_HEAD)
    assert "Learn from demonstration." in block
    assert "Want a dry run?" not in block
    assert "thread only" not in block


def test_thread_turn_primary_is_thread_secondary_is_main_and_memory(tmp_path):
    memory, paths = _memory(tmp_path)
    memory.remember("The user's reddit community is r/ChatGPT")
    memory.log_turn("s1", "in:user", "Learn from demonstration.", peer="user", message_id="m1")
    memory.log_turn("s1", "out", "Want a dry run on r/ChatGPT?", peer="user", message_id="root-2")
    memory.log_turn(
        "s1",
        "in:user",
        "already in this thread",
        peer="user",
        thread_id="root-2",
        message_id="t-0",
    )
    provider = _RecordingProvider()
    agent = Agent(
        paths=paths,
        bot=Bot(name="atlas", role="assistant", provider="echo"),
        provider=provider,
        memory=memory,
        control=Control(paths),
        stream_delay=0.0,
    )
    agent.session_id = "s1"
    agent._produce(
        "user",
        "No dry run. What's next on reddit?",
        thread_id="root-2",
        message_id="t-1",
    )
    assert provider.calls
    msgs = provider.calls[0]
    system = provider.systems[0]
    contents = [m.content or "" for m in msgs]
    assert any("Want a dry run on r/ChatGPT?" in c for c in contents)
    assert any("already in this thread" in c for c in contents)
    assert "No dry run. What's next on reddit?" in contents[-1]
    assert not any(c == "Learn from demonstration." for c in contents)
    assert "primary conversation" in system
    assert _SECONDARY_HEAD in system
    assert "Learn from demonstration." in system
    assert "already in this thread" not in system
    assert "Relevant memory" in system
    assert "r/ChatGPT" in system


def test_thread_context_block_skips_the_in_flight_line(tmp_path):
    memory, _ = _memory(tmp_path)
    memory.log_turn("s1", "out", "parent", peer="user", message_id="root-2")
    memory.log_turn("s1", "in:user", "live ask", peer="user", thread_id="root-2", message_id="t-1")
    block = thread_context_block(memory, "root-2", exclude_message_id="t-1")
    assert "parent" in block
    assert "live ask" not in block
    assert thread_context_block(memory, "missing") == ""


def test_steer_stays_inside_the_same_thread(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    send(
        paths,
        Msg(to="atlas", frm="user", text="main follow-up", message_id="m-follow"),
    )
    send(
        paths,
        Msg(
            to="atlas",
            frm="user",
            text="thread follow-up",
            thread_id="root-2",
            message_id="t-follow",
        ),
    )
    taken = take_steer(paths, "atlas", None, thread_id="root-2", after_ts=0)
    assert [m.text for m in taken] == ["thread follow-up"]
    leftover = take_steer(paths, "atlas", None, after_ts=0)
    assert [m.text for m in leftover] == ["main follow-up"]


def test_schema_v4_adds_thread_id_column(tmp_path):
    memory, paths = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "hello", peer="user", thread_id="root-1")
    store = StateStore(paths.state_db)
    assert store.path.exists()
    import sqlite3

    with sqlite3.connect(str(store.path)) as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
        cols = {r[1] for r in conn.execute("PRAGMA table_info(transcripts)")}
        assert "thread_id" in cols


def test_http_thread_roundtrip(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text('[[bots]]\nname = "atlas"\nprovider = "echo"\n', encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    orch.up()
    httpd = make_server(orch, "127.0.0.1", 0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        mem = orch.memory_for("atlas")
        mem.log_turn("s1", "in:user", "parent", peer="user", message_id="root-1")
        mem.log_turn("s1", "out", "ok parent", peer="user", message_id="root-2")

        def _post(payload):
            req = urllib.request.Request(
                f"{base}/api/chat",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read()

        _post(
            {
                "bot": "atlas",
                "text": "thread ask",
                "thread_id": "root-2",
                "message_id": "t-1",
            }
        )
        # echo bot replies; wait for the out turn
        import time

        deadline = time.time() + 8
        thread = []
        while time.time() < deadline:
            with urllib.request.urlopen(f"{base}/api/bots/atlas/threads/root-2", timeout=5) as r:
                thread = json.loads(r.read().decode())
            if len(thread) >= 2:
                break
            time.sleep(0.2)
        assert thread[0]["message_id"] == "root-2"
        assert any(row.get("text") == "thread ask" for row in thread)
        with urllib.request.urlopen(f"{base}/api/bots/atlas/history", timeout=5) as r:
            main = json.loads(r.read().decode())
        assert all(row.get("thread_id") is None for row in main)
        parent = next(row for row in main if row.get("message_id") == "root-2")
        assert parent["reply_count"] >= 1
    finally:
        httpd.shutdown()
        orch.down()
