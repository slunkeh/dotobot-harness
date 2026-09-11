"""Production guarded streams and saved history identify the same assistant reply."""

import json

import pytest

from agent import messaging
from agent.history import user_thread
from agent.runtime import build_agent
from agent.streaming import StreamReader
from harness.paths import HarnessPaths
from harness.roster import Bot
from harness.server import asdict_event
from providers.base import Completion, Provider


@pytest.mark.parametrize("origin", [None, "routine", "dream", "welcome", "voice"])
@pytest.mark.parametrize("text", ["hello there", "/memory"])
def test_inbox_stream_and_history_preserve_reply_origin_and_id(tmp_path, origin, text):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    msg = messaging.Msg(
        to="atlas",
        frm="user",
        text=f"[Routine: Daily check]\n{text}"
        if origin == "routine" and text != "/memory"
        else text,
        origin=origin,
        voice_call_id="voice-call" if origin == "voice" else None,
    )
    messaging.send(paths, msg)

    # This entrypoint creates GuardedStreamWriter around the actual writer.
    # A direct _produce(..., StreamWriter(...)) test misses proxy assignments.
    assert agent.process_inbox_once()
    assert messaging.pending(paths, "atlas") == []
    events = StreamReader(paths, msg.id)._read_new()
    finals = [asdict_event(event) for event in events if event.type == "final"]
    closing = [event for event in events if event.type == "message" and event.streaming is False]
    saved = [row for row in user_thread(agent.memory) if row.get("frm") == "atlas"]
    assert len(finals) == len(closing) == len(saved) == 1
    assert closing[0].origin == finals[0].get("origin") == saved[0].get("origin") == origin
    assert closing[0].id == finals[0]["message_id"] == saved[0]["message_id"]
    assert closing[0].text == finals[0]["text"] == saved[0]["text"]


def test_inbox_busy_record_persists_client_message_and_thread_identity(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    observed = []

    class InspectBusy(Provider):
        def complete(self, messages, **kwargs):
            observed.append(json.loads((paths.run / "atlas.busy").read_text()))
            return Completion(text="Checked.")

    agent.provider = InspectBusy("test")
    msg = messaging.Msg(
        to="atlas",
        frm="user",
        id="request-id",
        message_id="client-message-id",
        thread_id="parent-message-id",
        text="Check this reply thread",
    )
    messaging.send(paths, msg)
    assert agent.process_inbox_once()
    assert len(observed) == 1
    assert observed[0]["request_id"] == "request-id"
    assert observed[0]["message_id"] == "client-message-id"
    assert observed[0]["thread_id"] == "parent-message-id"
    assert not (paths.run / "atlas.busy").exists()
