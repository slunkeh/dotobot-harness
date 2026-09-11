"""End-to-end: spawn 2 bots as processes and talk to them.

The echo-based test always runs (no API key needed) — it is the "fake/process
bots" path from the kickoff prompt. The real-provider test skips when no key is
present so CI never fails for a missing secret.
"""

from __future__ import annotations

import os
import time

import pytest

from harness.orchestrator import Orchestrator
from harness.roster import RosterError

ECHO_ROSTER = """
[[bots]]
name = "atlas"
role = "a terse research assistant"
provider = "echo"

[[bots]]
name = "nova"
role = "a friendly writing partner"
provider = "echo"
"""


def _make_orch(tmp_path, roster_text):
    roster_path = tmp_path / "roster.toml"
    roster_path.write_text(roster_text, encoding="utf-8")
    home = tmp_path / "home"
    return Orchestrator.create(home=home, roster_path=roster_path, backend="process")


def _wait_running(orch, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        statuses = {h.bot: h.status.value for h in orch.status()}
        if all(v == "running" for v in statuses.values()):
            return True
        time.sleep(0.2)
    return False


def test_two_process_bots_reply_and_handoff(tmp_path):
    orch = _make_orch(tmp_path, ECHO_ROSTER)
    handles = orch.up()
    try:
        assert len(handles) == 2
        assert _wait_running(orch), "bots did not reach running state"

        # 1) user -> atlas, get a reply with attribution
        reply = orch.chat("atlas", "hello there", timeout=20.0)
        assert reply is not None, "no reply from atlas"
        assert reply.frm == "atlas"
        assert "hello there" in reply.text

        # Hold the recipient until the nonblocking acknowledgement arrives.
        orch._stop_bot("nova")

        # 2) bot-to-bot handoff: atlas acknowledges before nova can do the work.
        reply2 = orch.chat("atlas", "@nova draft an intro", timeout=10.0)
        assert reply2 is not None, "no handoff acknowledgement"
        assert reply2.frm == "atlas"
        assert "nova" in reply2.text.lower()
        from agent import messaging
        from agent.history import user_thread
        from agent.memory import Memory

        assert not user_thread(Memory(paths=orch.paths, bot="nova"), peer="user")
        orch._start_bot("nova")
        completed = messaging.wait_for_reply(orch.paths, "user", reply2.reply_to, timeout=20.0)
        assert completed is not None, "no resumed reply after nova finished"
        assert completed.frm == "atlas"
        assert "Done: draft an intro" in completed.text

        # Asked bot's 1:1 is where the completed work lives.
        nova_rows = user_thread(Memory(paths=orch.paths, bot="nova"), peer="user")
        assert nova_rows, "asked bot chat stayed empty"
        assert nova_rows[0]["frm"] == "atlas"
        assert "draft an intro" in nova_rows[0]["text"]
        atlas_rows = user_thread(Memory(paths=orch.paths, bot="atlas"), peer="user")
        atlas_text = " ".join(r["text"] for r in atlas_rows)
        assert "Handoff from" not in atlas_text
        assert "Private consult" not in atlas_text
    finally:
        orch.down()

    # after down, bots are no longer running
    assert all(h.status.value != "running" for h in orch.status())


def test_unknown_bot_chat_raises(tmp_path):
    orch = _make_orch(tmp_path, ECHO_ROSTER)
    with pytest.raises(RosterError):
        orch.chat("ghost", "hi")


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="no ANTHROPIC_API_KEY set; skipping real-provider integration test",
)
@pytest.mark.timeout(90)
def test_real_claude_bot(tmp_path):
    roster = """
[[bots]]
name = "atlas"
role = "a terse assistant"
provider = "claude"
auth_ref = "anthropic"
"""
    orch = _make_orch(tmp_path, roster)
    orch.up()
    try:
        assert _wait_running(orch)
        reply = orch.chat("atlas", "Say the single word: pong", timeout=60.0)
        assert reply is not None
        assert reply.text.strip()
    finally:
        orch.down()
