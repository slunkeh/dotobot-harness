"""A slow or missing colleague must not prevent the rest of a roundup."""

import json
import time
from types import SimpleNamespace

import pytest

from agent import messaging
from agent.runtime import build_agent
from agent.tools import ToolContext, default_tools
from harness import audit, delivery
from harness.paths import HarnessPaths
from harness.roster import Bot
from providers.base import Completion, Provider, ToolCall


def setup_roundup(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["chief", "slow", "fast"])
    (paths.home / "roster.json").write_text(
        json.dumps({"bots": [{"name": n, "provider": "echo"} for n in ("chief", "slow", "fast")]})
    )
    agent = build_agent(paths, Bot(name="chief", provider="echo"), stream_delay=0)
    ctx = ToolContext(paths=paths, bot="chief", memory=agent.memory)
    return paths, agent, ctx


def test_missing_colleague_does_not_hold_later_sends(tmp_path, monkeypatch):
    paths, agent, _ = setup_roundup(tmp_path)
    monkeypatch.setattr(messaging, "wait_for_reply", lambda *a, **k: None)

    class Roundup(Provider):
        n = 0

        def complete(self, messages, **kw):
            self.n += 1
            if self.n == 1:
                return Completion(
                    tool_calls=[
                        ToolCall(
                            n, "message_agent", {"to": n, "text": "Report today's completions"}
                        )
                        for n in ("deleted-bot", "slow", "fast")
                    ]
                )
            return Completion(text="The available bots were asked.")

    agent.provider = Roundup("test")
    agent._produce("user", "Ask everyone for an update", turn_id="roundup")
    assert len(messaging.pending(paths, "slow")) == 1
    assert len(messaging.pending(paths, "fast")) == 1
    assert not any(r.get("source") == "delivery-recovery" for r in audit.read(paths, "chief"))


def test_nonblocking_handoffs_send_before_waiting(tmp_path, monkeypatch):
    paths, _, ctx = setup_roundup(tmp_path)

    def must_not_wait(*a, **k):
        pytest.fail("A nonblocking handoff waited for a colleague")

    monkeypatch.setattr(messaging, "wait_for_reply", must_not_wait)
    receipts = []
    for name in ("slow", "fast"):
        receipts.append(
            json.loads(
                default_tools()["message_agent"].handler(
                    ctx, {"to": name, "text": "Status only", "wait": False}
                )
            )
        )
    assert [r["bot"] for r in receipts] == ["slow", "fast"]
    assert all(r["status"] == "sent" for r in receipts)
    for name, receipt in zip(("slow", "fast"), receipts, strict=True):
        assert messaging.pending(paths, name)[0].id == receipt["request_id"]


def test_collection_returns_fast_reply_and_keeps_late_reply_retrievable(tmp_path):
    paths, _, ctx = setup_roundup(tmp_path)
    messaging.send(paths, messaging.Msg(to="chief", frm="fast", text="Done", reply_to="fast-id"))
    collect = default_tools()["collect_agent_replies"].handler
    args = {"request_ids": ["slow-id", "fast-id"], "timeout": 0}
    first = json.loads(collect(ctx, args))
    assert first["pending"] == ["slow-id"]
    assert first["replies"] == [{"request_id": "fast-id", "bot": "fast", "text": "Done"}]
    assert messaging.read_inbox(paths, "chief") == []
    messaging.send(
        paths, messaging.Msg(to="chief", frm="slow", text="Also done", reply_to="slow-id")
    )
    # A new context models a resumed/restarted sender. Collection must not resend
    # or lose the already-collected answer.
    restarted = ToolContext(paths=paths, bot="chief", memory=ctx.memory)
    later = json.loads(collect(restarted, args))
    assert later["pending"] == []
    assert {r["text"] for r in later["replies"]} == {"Done", "Also done"}
    assert not messaging.pending(paths, "slow")
    assert not messaging.pending(paths, "fast")


def test_collection_wait_budget_is_shared_and_only_reads_own_replies(tmp_path, monkeypatch):
    paths, _, ctx = setup_roundup(tmp_path)
    from agent import tools

    ticks = [0.0]
    monkeypatch.setattr(
        tools,
        "time",
        SimpleNamespace(
            monotonic=lambda: ticks[0],
            sleep=lambda seconds: ticks.__setitem__(0, ticks[0] + seconds),
            time=time.time,
        ),
    )
    messaging.send(paths, messaging.Msg(to="fast", frm="slow", text="Other inbox", reply_to="a"))
    messaging.send(paths, messaging.Msg(to="chief", frm="user", text="New request", id="b"))
    ctx.turn_started = tools.time.time() + 1  # This input predates the active turn.
    result = json.loads(
        default_tools()["collect_agent_replies"].handler(
            ctx, {"request_ids": ["a", "b", "c"], "timeout": 1}
        )
    )
    assert result == {"replies": [], "pending": ["a", "b", "c"]}
    assert ticks[0] == pytest.approx(1)
    assert len(messaging.pending(paths, "chief")) == 1


def test_live_roster_replaces_deleted_peers_in_prompt(tmp_path):
    paths, agent, _ = setup_roundup(tmp_path)
    prompt = agent.system_prompt("What has everyone achieved?", include_memory=False)
    assert "Current colleague roster" in prompt
    assert '"slow"' in prompt and '"fast"' in prompt
    (paths.home / "roster.json").write_text(
        json.dumps({"bots": [{"name": "chief", "provider": "echo"}]})
    )
    refreshed = agent.system_prompt("What has everyone achieved?", include_memory=False)
    assert '"slow"' not in refreshed and '"fast"' not in refreshed
    assert "Current colleague roster: []" in refreshed


def test_roundup_instructions_send_all_before_collecting_and_stay_on_status(tmp_path):
    _, agent, _ = setup_roundup(tmp_path)
    prompt = agent.system_prompt("What has everyone achieved?", include_memory=False)
    assert "wait=false" in prompt and "collect_agent_replies" in prompt
    consult = agent.system_prompt("Report today's work", consult=True, include_memory=False)
    assert "status-only request" in consult
    assert "do not resume" in consult


@pytest.mark.parametrize(
    "error", ["error: timed out", "error: connection reset", "error: server 500"]
)
def test_ambiguous_send_errors_still_hold_actions(error):
    assert delivery.classify_failure(error) == delivery.FAILURE_UNCERTAIN


def test_roundup_dispatch_collect_and_restart_do_not_duplicate_sends(tmp_path, monkeypatch):
    paths, agent, _ = setup_roundup(tmp_path)
    receipts = []

    class Roundup(Provider):
        n = 0

        def complete(self, messages, **kw):
            self.n += 1
            if self.n == 1:
                return Completion(
                    tool_calls=[
                        ToolCall(
                            n, "message_agent", {"to": n, "text": "Status only", "wait": False}
                        )
                        for n in ("slow", "fast")
                    ]
                )
            if self.n == 2:
                receipts.extend(
                    json.loads(m.content) for m in messages if m.name == "message_agent"
                )
                assert len(messaging.pending(paths, "slow")) == 1
                assert len(messaging.pending(paths, "fast")) == 1
                messaging.send(
                    paths,
                    messaging.Msg(
                        to="chief", frm="fast", text="Completed", reply_to=receipts[1]["request_id"]
                    ),
                )
                return Completion(
                    tool_calls=[
                        ToolCall(
                            "collect",
                            "collect_agent_replies",
                            {
                                "request_ids": [r["request_id"] for r in receipts],
                                "timeout": 0,
                            },
                        )
                    ]
                )
            result = json.loads(
                next(m.content for m in messages if m.name == "collect_agent_replies")
            )
            assert result["pending"] == [receipts[0]["request_id"]]
            assert result["replies"][0]["text"] == "Completed"
            return Completion(text="Fast completed; slow is still working.")

    agent.provider = Roundup("test")
    agent._produce("user", "Ask both bots for status", turn_id="roundup")

    class Retry(Provider):
        n = 0

        def complete(self, messages, **kw):
            self.n += 1
            if self.n == 1:
                return Completion(
                    tool_calls=[
                        ToolCall(
                            "retry",
                            "message_agent",
                            {
                                "to": "slow",
                                "text": "Status only",
                            },
                        )
                    ]
                )
            assert receipts[0]["request_id"] in next(
                m.content for m in messages if m.name == "message_agent"
            )
            return Completion(text="Already asked; waiting for the original reply.")

    restarted = build_agent(paths, Bot(name="chief", provider="echo"), stream_delay=0)
    monkeypatch.setattr(messaging, "wait_for_reply", lambda *a, **k: pytest.fail("Duplicate send"))
    restarted.provider = Retry("test")
    restarted._produce("user", "continue", turn_id="continued-roundup")
    assert len(messaging.pending(paths, "slow")) == 1


@pytest.mark.parametrize("interrupt", ["stop", "followup"])
def test_collection_yields_to_new_user_input(tmp_path, monkeypatch, interrupt):
    paths, agent, ctx = setup_roundup(tmp_path)
    ctx.control = agent.control
    from agent import tools

    def must_not_sleep(*a):
        pytest.fail("Collection blocked new user input")

    monkeypatch.setattr(
        tools,
        "time",
        SimpleNamespace(
            monotonic=time.monotonic,
            sleep=must_not_sleep,
            time=time.time,
        ),
    )
    if interrupt == "stop":
        monkeypatch.setattr(ctx.control, "stop_requested", lambda bot: True)
    else:
        messaging.send(paths, messaging.Msg(to="chief", frm="user", text="Stop collecting"))
    result = json.loads(
        default_tools()["collect_agent_replies"].handler(
            ctx, {"request_ids": ["still-working"], "timeout": 30}
        )
    )
    assert result["pending"] == ["still-working"]


def test_timed_out_synchronous_handoff_can_collect_without_resending(tmp_path, monkeypatch):
    paths, _, ctx = setup_roundup(tmp_path)
    monkeypatch.setattr(messaging, "wait_for_reply", lambda *a, **k: None)
    result = default_tools()["message_agent"].handler(
        ctx, {"to": "slow", "text": "Status", "wait": True}
    )
    sent = messaging.pending(paths, "slow")[0]
    assert sent.id in result and "Do not resend" in result
    assert "collect_agent_replies" in result
    messaging.send(paths, messaging.Msg(to="chief", frm="slow", text="Finished", reply_to=sent.id))
    collected = json.loads(
        default_tools()["collect_agent_replies"].handler(
            ctx, {"request_ids": [sent.id], "timeout": 0}
        )
    )
    assert collected["replies"][0]["text"] == "Finished"
    assert len(messaging.pending(paths, "slow")) == 1


@pytest.mark.parametrize("target", ["chief", "../outside"])
def test_local_recipient_rejections_are_definitely_unsent(tmp_path, target):
    paths, _, ctx = setup_roundup(tmp_path)
    result = default_tools()["message_agent"].handler(ctx, {"to": target, "text": "Status"})
    assert delivery.classify_failure(result, tool="message_agent") == delivery.FAILURE_UNSENT
    assert messaging.pending(paths, "chief") == []


def test_external_tool_cannot_claim_local_handoff_validation_failure():
    error = "error: message_agent not sent: no bot matching 'deleted-bot'"
    assert (
        delivery.classify_failure(error, tool="github_create_issue") == delivery.FAILURE_UNCERTAIN
    )
