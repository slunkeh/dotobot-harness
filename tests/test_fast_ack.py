"""Fast-ack receipts are gone: one provider call, one assistant bubble."""

from __future__ import annotations

from agent.memory import Memory
from agent.runtime import Agent
from agent.streaming import StreamReader, StreamWriter
from harness.control import Control
from harness.paths import HarnessPaths
from harness.roster import Bot
from providers.base import Completion, Provider


def _paths(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    return paths


class _AckProvider(Provider):
    id = "grok"

    def __init__(self):
        super().__init__(model="g")
        self.calls: list[dict] = []

    def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
        self.calls.append({"tools": tools, "last": messages[-1].content if messages else ""})
        if not tools:
            return Completion(text="Got it — posting all of them.", finish_reason="stop")
        return Completion(text="Hey. What's up?", finish_reason="stop")


def test_turn_does_not_emit_a_fast_ack(tmp_path):
    paths = _paths(tmp_path)
    mem = Memory(paths=paths, bot="atlas")
    mem.log_turn("s", "in:user", "which reddit posts should I submit?", peer="user")
    mem.log_turn("s", "out", "The three in your drafts — which ones?", peer="user")
    agent = Agent(
        paths=paths,
        bot=Bot(name="atlas", role="assistant", provider="grok"),
        provider=_AckProvider(),
        memory=mem,
        control=Control(paths),
        stream_delay=0.0,
    )
    writer = StreamWriter(paths, "no-ack")
    final = agent._produce("user", "hey", writer=writer)
    events = list(StreamReader(paths, "no-ack").events(timeout=2.0))

    assert all(c["tools"] for c in agent.provider.calls)
    assert not any((e.id or "").endswith(":fast-ack") for e in events)
    assert not any(r.get("origin") == "ack" for r in mem._session_records())
    assert final == "Hey. What's up?"
    assert events[-1].type == "final"
    assert events[-1].text == "Hey. What's up?"
