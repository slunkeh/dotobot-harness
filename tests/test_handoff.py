"""bot-to-bot work lives in the asked bot's chat."""

from __future__ import annotations

import json
import threading
import time

from agent.history import user_thread
from agent.memory import Memory
from agent.messaging import handoff_brief, handoff_visible_text, is_handoff
from agent.runtime import _CONSULT_PROMPT, _RELAY_PROMPT, Agent
from agent.streaming import StreamWriter
from harness.control import Control
from harness.paths import HarnessPaths
from harness.roster import Bot
from providers.base import Message
from providers.echo import EchoProvider


def _paths(tmp_path, names=("atlas", "nova")):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(list(names))
    return paths


def _agent(paths, name, provider=None):
    return Agent(
        paths=paths,
        bot=Bot(name=name, role="assistant", provider="echo"),
        provider=provider or EchoProvider(persona=name),
        memory=Memory(paths=paths, bot=name),
        control=Control(paths),
        stream_delay=0.0,
        reply_timeout=2.0,
    )


def test_handoff_wrapper_strips_to_the_request():
    wrapped = handoff_brief("atlas", "create three tickets")
    assert is_handoff(wrapped)
    assert "Handoff from atlas" in wrapped
    assert handoff_visible_text(wrapped) == "create three tickets"
    assert not is_handoff("create three tickets")
    assert handoff_visible_text("plain") == "plain"


def test_consult_prompt_keeps_work_in_asked_bot_chat():
    assert "YOUR chat" in _CONSULT_PROMPT
    assert "short summary" in _CONSULT_PROMPT
    assert "not posted" not in _CONSULT_PROMPT
    assert "That bot's own chat" in _RELAY_PROMPT
    assert "never their transcript" in _RELAY_PROMPT
    assert "connected plugin" in _RELAY_PROMPT
    assert "message_agent for a plugin" in _RELAY_PROMPT


def test_consult_turn_lands_in_asked_bot_user_thread(tmp_path):
    paths = _paths(tmp_path)
    nova = _agent(paths, "nova")
    writer = StreamWriter(paths, "handoff-1")
    wrapped = handoff_brief("atlas", "create three tickets")
    reply = nova._produce("atlas", wrapped, writer=writer)

    assert reply.startswith("Done:")
    assert "create three tickets" in reply
    assert "Handoff from" not in reply

    rows = user_thread(nova.memory, peer="user")
    assert [(r["frm"], r["text"]) for r in rows] == [
        ("atlas", "create three tickets"),
        ("nova", reply),
    ]
    events = [
        json.loads(line)
        for line in paths.stream_file("handoff-1").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    kinds = [e["type"] for e in events]
    assert "handoff" in kinds
    handoff = next(e for e in events if e["type"] == "handoff")
    assert handoff["frm"] == "atlas"
    assert handoff["bot"] == "nova"
    assert handoff["text"] == "create three tickets"


def test_consult_history_is_the_user_thread_not_a_private_peer(tmp_path):
    from providers.base import Completion, Provider

    class Rec(Provider):
        id = "rec"

        def __init__(self):
            super().__init__(model="rec")
            self.calls = []

        def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
            self.calls.append(list(messages))
            return Completion(text="ok", finish_reason="stop")

    paths = _paths(tmp_path)
    provider = Rec()
    nova = _agent(paths, "nova", provider=provider)
    nova._produce("user", "earlier user chat")
    nova._produce("atlas", handoff_brief("atlas", "file the tickets"))
    # Second consult should see the user thread (including the first handoff).
    nova._produce("atlas", handoff_brief("atlas", "and the third one"))
    last = provider.calls[-1]
    contents = [m.content for m in last]
    assert any("file the tickets" in (c or "") for c in contents)
    assert any("earlier user chat" in (c or "") for c in contents)


def test_echo_handoff_replies_with_summary_or_question():
    p = EchoProvider()
    out = p.complete([Message(role="user", content=handoff_brief("atlas", "draft an intro"))])
    assert out.text == "Done: draft an intro"
    q = p.complete(
        [Message(role="user", content=handoff_brief("atlas", "clarify: which project?"))]
    )
    assert q.text == "Need a decision: which project?"


def test_echo_presents_colleague_summary_not_a_dump():
    p = EchoProvider(persona="atlas")
    out = p.complete(
        [
            Message(role="user", content="@nova draft an intro"),
            Message(
                role="tool", content="nova replied: Done: draft an intro", name="message_agent"
            ),
        ]
    )
    assert out.text == "atlas nova: Done: draft an intro"
    assert "relayed reply" not in out.text
    assert "Handoff from" not in out.text


def test_askers_chat_does_not_store_the_asked_bots_wrapper(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.tools._CONSULT_TIMEOUT", 2.0)
    paths = _paths(tmp_path)
    atlas = _agent(paths, "atlas")
    nova = _agent(paths, "nova")
    (paths.home / "roster.json").write_text(
        json.dumps(
            {
                "bots": [
                    {"name": "atlas", "provider": "echo"},
                    {"name": "nova", "provider": "echo"},
                ]
            }
        ),
        encoding="utf-8",
    )

    def drain():
        deadline = time.time() + 4
        while time.time() < deadline:
            nova.process_inbox_once()
            time.sleep(0.05)

    worker = threading.Thread(target=drain, daemon=True)
    worker.start()
    atlas._produce("user", "@nova draft an intro")
    worker.join(timeout=4)

    nova_rows = user_thread(nova.memory, peer="user")
    assert nova_rows, "asked bot chat stayed empty"
    assert nova_rows[0]["frm"] == "atlas"
    assert nova_rows[0]["text"] == "draft an intro"
    assert nova_rows[-1]["frm"] == "nova"
    assert nova_rows[-1]["text"].startswith("Done:")

    atlas_rows = user_thread(atlas.memory, peer="user")
    texts = " ".join(r["text"] for r in atlas_rows)
    assert "Handoff from" not in texts
    assert "Private consult" not in texts
    assert any(r["frm"] == "user" and "@nova" in r["text"] for r in atlas_rows)
    # asker only keeps its own short presentation, not nova's wrapper
    assert not any("echo> [Handoff" in r["text"] for r in atlas_rows)
