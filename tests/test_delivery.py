"""Delivery intent → receipt: never replay an uncertain external send.

The four contracts from the ticket, verbatim:
* crash between intent and send → replay allowed;
* crash after receipt → recovery skips the send;
* unknown outcome → the recovered turn has side-effect tools withheld and
  reports the ambiguity instead of retrying;
* intent keys are stable across process restarts.
"""

from __future__ import annotations

import pytest

import agent.govern as govern
from agent import messaging, streaming
from agent.runtime import build_agent
from harness import delivery
from harness.paths import HarnessPaths
from harness.roster import Bot
from providers.base import Completion, Provider, ToolCall

BOT = "atlas"


def _paths(tmp_path) -> HarnessPaths:
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([BOT])
    return paths


def _agent(paths):
    return build_agent(paths, Bot(name=BOT, role="terse", provider="echo"), stream_delay=0.0)


def _send_user_msg(paths, text="hello", msg_id=None):
    msg = messaging.Msg(to=BOT, frm="user", text=text)
    if msg_id:
        msg.id = msg_id
    messaging.send(paths, msg)
    return msg


def _user_replies(paths):
    return [m for _p, m in messaging.read_inbox(paths, "user")]


class _Boom(BaseException):
    """Simulates the process dying mid-turn (not caught by `except Exception`)."""


class _SendOnce(Provider):
    """Scripted provider: one message_agent call, then a final reply.

    Records every system prompt and tool result it is shown, so tests can
    assert what the recovered turn was told without a real model.
    """

    def __init__(self, model="m", to="nova"):
        super().__init__(model)
        self.n = 0
        self.to = to
        self.systems: list[str] = []
        self.tool_results: list[str] = []

    def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
        self.n += 1
        self.systems.append(system or "")
        self.tool_results.extend(
            m.content or "" for m in messages if getattr(m, "role", "") == "tool"
        )
        if self.n == 1:
            return Completion(
                tool_calls=[
                    ToolCall(id="c1", name="message_agent", arguments={"to": self.to, "text": "go"})
                ],
                finish_reason="tool_use",
            )
        return Completion(text="reported back to the user", finish_reason="stop")


# -- the ledger ------------------------------------------------------------


def test_intent_lifecycle(tmp_path):
    paths = _paths(tmp_path)
    led = delivery.Ledger(paths)
    led.begin(BOT, "t1", "peer:nova", session="s1")
    assert led.lookup(BOT, "t1", "peer:nova")["status"] == delivery.STATUS_PENDING
    led.inflight(BOT, "t1", "peer:nova")
    assert led.lookup(BOT, "t1", "peer:nova")["status"] == delivery.STATUS_INFLIGHT
    led.receipt(BOT, "t1", "peer:nova", detail="nova replied: done")
    row = led.lookup(BOT, "t1", "peer:nova")
    assert row["status"] == delivery.STATUS_SENT
    assert row["detail"] == "nova replied: done"
    assert row["resolved_ts"] is not None
    led.clear(BOT, "t1", "peer:nova")
    assert led.lookup(BOT, "t1", "peer:nova") is None


def test_keys_are_stable_across_process_restarts(tmp_path):
    """A fresh Ledger (a new process) finds rows written by the old one:
    keys are built only from persisted identifiers, never a pid or boot
    stamp. The session column is informational and not part of the key."""
    paths = _paths(tmp_path)
    delivery.Ledger(paths).receipt(BOT, "turn-9", "chat:user", detail="hi there")
    reopened = delivery.Ledger(paths).lookup(BOT, "turn-9", "chat:user")
    assert reopened is not None and reopened["status"] == delivery.STATUS_SENT
    # different informational session, same key
    other = delivery.Ledger(paths)
    other.begin(BOT, "turn-9", "chat:user", session="a-new-boot")
    assert len(other.turn_rows(BOT, "turn-9")) == 1


def test_classify_failure_proof_of_unsent(tmp_path):
    for text in (
        "error: could not reach Linear: <urlopen error [Errno 111] Connection refused>",
        "error: could not reach Linear: <urlopen error [Errno -2] Name or service not known>",
        "error: connector 'linear' has no API key yet. Call request_secret",
        "error: unknown tool linear_comment",
        "error: linear_create_issue needs 'title' and 'team' (key or name)",
    ):
        assert delivery.classify_failure(text) == delivery.FAILURE_UNSENT, text


def test_classify_failure_preserves_uncertainty(tmp_path):
    """A timeout or anything unprovable stays uncertain — that direction is
    the invariant (OpenClaw): the request may have been applied before the
    answer was lost."""
    for text in (
        "error: could not reach Linear: <urlopen error timed out>",
        "error: Linear API HTTP 500: internal error",
        "error: something nobody has ever seen before",
    ):
        assert delivery.classify_failure(text) == delivery.FAILURE_UNCERTAIN, text


def test_recovery_state_buckets(tmp_path):
    paths = _paths(tmp_path)
    led = delivery.Ledger(paths)
    led.begin(BOT, "t1", "tool:linear_comment")  # pending: never begun
    led.begin(BOT, "t1", "peer:nova")
    led.inflight(BOT, "t1", "peer:nova")  # unknown outcome
    led.receipt(BOT, "t1", "tool:github_comment", detail="ok")  # confirmed
    led.begin(BOT, "t1", "chat:user")
    led.inflight(BOT, "t1", "chat:user")
    state = delivery.recovery_state(led.turn_rows(BOT, "t1"), delivery.chat_target("user"))
    assert state.terminal_status == delivery.STATUS_INFLIGHT
    assert state.unresolved == ("peer:nova",)  # pending and sent are not ambiguous


def test_clear_pending_leaves_resolved_rows(tmp_path):
    paths = _paths(tmp_path)
    led = delivery.Ledger(paths)
    led.begin(BOT, "t1", "tool:a")
    led.receipt(BOT, "t1", "tool:b", detail="ok")
    led.clear_pending(BOT, "t1")
    rows = led.turn_rows(BOT, "t1")
    assert [r["target"] for r in rows] == ["tool:b"]


def test_prune_turn_scoped_to_that_turn(tmp_path):
    paths = _paths(tmp_path)
    led = delivery.Ledger(paths)
    led.receipt(BOT, "t1", "chat:user")
    led.receipt(BOT, "t2", "chat:user")
    led.prune_turn(BOT, "t1")
    assert led.turn_rows(BOT, "t1") == []
    assert len(led.turn_rows(BOT, "t2")) == 1


def test_receipt_detail_is_capped(tmp_path):
    paths = _paths(tmp_path)
    led = delivery.Ledger(paths)
    led.begin(BOT, "t1", "chat:user")
    led.receipt(BOT, "t1", "chat:user", detail="x" * (delivery.DETAIL_CAP * 2))
    assert len(led.lookup(BOT, "t1", "chat:user")["detail"]) == delivery.DETAIL_CAP


def test_broken_store_never_raises(tmp_path):
    """Bookkeeping degrades to today's behavior on a corrupt store — it must
    never fail a turn (same stance as the usage ledger)."""
    paths = _paths(tmp_path)
    paths.state_db.write_text("this is not a sqlite database", encoding="utf-8")
    led = delivery.Ledger(paths)
    led.begin(BOT, "t1", "chat:user")
    led.receipt(BOT, "t1", "chat:user")
    assert led.lookup(BOT, "t1", "chat:user") is None
    assert led.turn_rows(BOT, "t1") == []


def test_target_helpers():
    assert delivery.chat_target("user") == "chat:user"
    assert delivery.chat_target("") == "chat:user"
    assert delivery.chat_target("nova") == "chat:nova"
    assert delivery.send_target("message", "message_agent", "nova") == "peer:nova"
    assert delivery.send_target("write_tool", "linear_comment", "linear_comment") == (
        "tool:linear_comment"
    )


# -- the gate's recovery hold ----------------------------------------------


class _Ctx:
    def __init__(self, uncertain: bool) -> None:
        self.tool_call_id = "call-1"
        self.delivery_uncertain = uncertain


def test_recovery_hold_withholds_send_intents(tmp_path):
    """On a recovered turn with an uncertain send, side-effecting intents are
    refused unconditionally — the bot's job is to report, not act again."""
    paths = _paths(tmp_path)
    for name, args in (
        ("message_agent", {"to": "nova", "text": "hi"}),
        ("run_command", {"command": "echo hi"}),
        ("create_bot", {"name": "x"}),
    ):
        refusal = govern.govern(_Ctx(True), name, args, paths=paths, bot=BOT)
        assert refusal is not None and "held back" in refusal, name
        assert "uncertain" in refusal
    from harness import audit

    rows = [r for r in audit.read(paths, BOT) if r.get("source") == "delivery-recovery"]
    assert len(rows) == 3


def test_recovery_hold_keeps_read_and_report_tools(tmp_path):
    paths = _paths(tmp_path)
    for name, args in (
        ("recall", {"query": "x"}),
        ("remember", {"text": "the send to nova is uncertain"}),
        ("show_table", {"title": "t"}),
    ):
        assert govern.govern(_Ctx(True), name, args, paths=paths, bot=BOT) is None, name
    # and a normal turn is untouched
    assert (
        govern.govern(_Ctx(False), "message_agent", {"to": "n", "text": "x"}, paths=paths, bot=BOT)
        is None
    )


# -- runtime recovery ------------------------------------------------------


def test_crash_after_receipt_recovery_skips_send(tmp_path, monkeypatch):
    """Ticket: crash after receipt → recovery completes the turn without
    rerunning tools, and the recipient sees exactly one reply. The second
    pass runs on a freshly built agent — a new process — so this also proves
    the intent keys hold across restarts."""
    paths = _paths(tmp_path)
    agent = _agent(paths)
    msg = _send_user_msg(paths)

    real = messaging.mark_processed
    calls = {"n": 0}

    def die_once(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Boom
        return real(*a, **k)

    monkeypatch.setattr(messaging, "mark_processed", die_once)
    with pytest.raises(_Boom):
        agent.process_inbox_once()
    # the reply went out and its receipt is durable; the message still pends
    assert len(_user_replies(paths)) == 1
    assert [m.text for m in messaging.pending(paths, BOT)] == ["hello"]

    monkeypatch.undo()
    recovered = _agent(paths)  # "restarted" process
    recovered.process_inbox_once()
    assert messaging.pending(paths, BOT) == []
    assert len(_user_replies(paths)) == 1  # no duplicate
    assert not (paths.run / f"{BOT}.attempt.json").is_file()
    assert delivery.Ledger(paths).turn_rows(BOT, msg.id) == []  # settled turns are pruned


def test_crash_mid_terminal_send_warns_instead_of_resending(tmp_path, monkeypatch):
    """A crash while the final reply is in flight: the outcome is unknown, so
    recovery neither resends nor pretends — the turn settles and the bot is
    told to warn on next contact (the OpenClaw invariant, verbatim)."""
    paths = _paths(tmp_path)
    agent = _agent(paths)
    msg = _send_user_msg(paths, "ship it")

    def die(*a, **k):
        raise _Boom

    monkeypatch.setattr(agent, "_reply", die)
    with pytest.raises(_Boom):
        agent.process_inbox_once()
    row = delivery.Ledger(paths).lookup(BOT, msg.id, "chat:user")
    assert row is not None and row["status"] == delivery.STATUS_INFLIGHT

    monkeypatch.undo()
    recovered = _agent(paths)
    recovered.process_inbox_once()
    assert messaging.pending(paths, BOT) == []
    assert _user_replies(paths) == []  # the uncertain send was NOT replayed
    notes = streaming.pop_turn_notes(paths, BOT)
    assert any("may never have been delivered" in n for n in notes)
    assert delivery.Ledger(paths).turn_rows(BOT, msg.id) == []


def test_stale_pending_intent_replays_cleanly(tmp_path, monkeypatch):
    """Ticket: crash between intent and send → nothing left the machine, so
    the intent is cleared and the turn replays without restriction."""
    paths = _paths(tmp_path)
    agent = _agent(paths)
    msg = _send_user_msg(paths)

    def die(*a, **k):
        raise _Boom

    monkeypatch.setattr(agent, "_produce", die)
    with pytest.raises(_Boom):
        agent.process_inbox_once()
    # the crashed attempt had recorded an intent but never begun the send
    delivery.Ledger(paths).begin(BOT, msg.id, "tool:linear_comment")

    monkeypatch.undo()
    recovered = _agent(paths)
    recovered.process_inbox_once()
    replies = _user_replies(paths)
    assert len(replies) == 1 and "hello" in replies[0].text  # a normal echo reply
    assert delivery.Ledger(paths).turn_rows(BOT, msg.id) == []


def test_unknown_outcome_withholds_sends_and_reports(tmp_path, monkeypatch):
    """Ticket: unknown outcome → the recovered turn runs with side-effect
    tools withheld and is told to report the ambiguity instead of retrying."""
    paths = _paths(tmp_path)
    agent = _agent(paths)
    msg = _send_user_msg(paths, "message nova for me")
    provider = _SendOnce()
    agent.provider = provider

    def die(*a, **k):
        raise _Boom

    monkeypatch.setattr(agent, "_produce", die)
    with pytest.raises(_Boom):
        agent.process_inbox_once()
    # the crashed attempt died with a peer send in flight
    led = delivery.Ledger(paths)
    led.begin(BOT, msg.id, "peer:nova")
    led.inflight(BOT, msg.id, "peer:nova")

    monkeypatch.undo()
    recovered = _agent(paths)
    recovered.provider = provider
    recovered.process_inbox_once()
    # the model was told what is uncertain…
    assert any("Delivery recovery" in s and "peer:nova" in s for s in provider.systems)
    # …its retry of the send was refused by the gate…
    assert any("held back" in r for r in provider.tool_results)
    # …and nothing reached nova's inbox.
    assert messaging.read_inbox(paths, "nova") == []
    replies = _user_replies(paths)
    assert len(replies) == 1 and "reported back" in replies[0].text


def _seed_receipt(paths, turn, target, tool, tool_args, detail=""):
    """A confirmed send as a crashed previous attempt would have left it."""
    led = delivery.Ledger(paths)
    led.begin(BOT, turn, target, digest=delivery.args_digest(tool, tool_args))
    led.receipt(BOT, turn, target, detail=detail)


def test_midturn_receipt_dedupes_the_send_on_replay(tmp_path):
    """A send the previous attempt confirmed is not made again: the handler
    is skipped and the recorded receipt is handed back as the result."""
    paths = _paths(tmp_path)
    agent = _agent(paths)
    provider = _SendOnce()
    agent.provider = provider
    msg = _send_user_msg(paths, "message nova for me")
    _seed_receipt(
        paths,
        msg.id,
        "peer:nova",
        "message_agent",
        {"to": "nova", "text": "go"},
        detail="nova replied: done",
    )

    agent.process_inbox_once()
    assert any(
        "already delivered" in r and "nova replied: done" in r for r in provider.tool_results
    )
    assert messaging.read_inbox(paths, "nova") == []  # not resent
    assert len(_user_replies(paths)) == 1


def test_args_digest_is_content_identity():
    same = delivery.args_digest("message_agent", {"to": "nova", "text": "go"})
    assert same == delivery.args_digest("message_agent", {"text": "go", "to": "nova"})
    assert same != delivery.args_digest("message_agent", {"to": "nova", "text": "other"})
    assert same != delivery.args_digest("linear_comment", {"to": "nova", "text": "go"})


def test_differing_content_is_refused_not_misbound(tmp_path):
    """A receipt binds by content, never call order: a same-target call with
    different arguments must be neither sent (risking a duplicate of the
    confirmed send) nor claimed delivered (it was not) — it is refused with
    the reason, and the bot reports."""
    paths = _paths(tmp_path)
    agent = _agent(paths)
    provider = _SendOnce()  # replays {"to": "nova", "text": "go"}
    agent.provider = provider
    msg = _send_user_msg(paths, "message nova for me")
    _seed_receipt(
        paths,
        msg.id,
        "peer:nova",
        "message_agent",
        {"to": "nova", "text": "something quite different"},
        detail="nova replied: on it",
    )

    agent.process_inbox_once()
    refusals = [r for r in provider.tool_results if "content differs" in r]
    assert refusals and all("not sent" in r for r in refusals)
    # never the dedup notice — that would claim this call's content delivered
    assert not any("was not sent again" in r for r in provider.tool_results)
    assert messaging.read_inbox(paths, "nova") == []  # neither send made
    assert len(_user_replies(paths)) == 1


class _SendPair(Provider):
    """One round with two message_agent calls to the same peer, then a final."""

    def __init__(self, model="m", texts=("go", "also do this")):
        super().__init__(model)
        self.n = 0
        self.texts = texts
        self.tool_results: list[str] = []

    def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
        self.n += 1
        self.tool_results.extend(
            m.content or "" for m in messages if getattr(m, "role", "") == "tool"
        )
        if self.n == 1:
            return Completion(
                tool_calls=[
                    ToolCall(id=f"c{i}", name="message_agent", arguments={"to": "nova", "text": t})
                    for i, t in enumerate(self.texts)
                ],
                finish_reason="tool_use",
            )
        return Completion(text="both handled", finish_reason="stop")


def test_second_distinct_send_still_goes_out(tmp_path, monkeypatch):
    """Dedupe consumes exactly the matching receipt: a genuinely new send to
    the same target in the replayed turn is still delivered."""
    paths = _paths(tmp_path)
    agent = _agent(paths)
    provider = _SendPair()
    agent.provider = provider
    msg = _send_user_msg(paths, "message nova twice")
    _seed_receipt(paths, msg.id, "peer:nova", "message_agent", {"to": "nova", "text": "go"})
    monkeypatch.setattr(messaging, "wait_for_reply", lambda *a, **k: None)

    agent.process_inbox_once()
    assert any("already delivered" in r for r in provider.tool_results)
    delivered = [m.text for _p, m in messaging.read_inbox(paths, "nova")]
    assert len(delivered) == 1 and "also do this" in delivered[0]


def test_own_sends_never_read_back_as_receipts(tmp_path, monkeypatch):
    """A `sent` row this attempt just wrote is not a previous-attempt receipt:
    two distinct same-target sends in one clean turn must both deliver, not
    have the second refused against the first's fresh receipt."""
    paths = _paths(tmp_path)
    agent = _agent(paths)
    provider = _SendPair(texts=("first thing", "second thing"))
    agent.provider = provider
    msg = _send_user_msg(paths, "message nova twice")
    monkeypatch.setattr(messaging, "wait_for_reply", lambda *a, **k: None)

    agent.process_inbox_once()
    delivered = [m.text for _p, m in messaging.read_inbox(paths, "nova")]
    assert len(delivered) == 2
    assert any("first thing" in t for t in delivered)
    assert any("second thing" in t for t in delivered)
    assert not any("content differs" in r for r in provider.tool_results)
    assert msg.id  # turn settles normally
    assert messaging.pending(paths, BOT) == []


def test_digest_survives_a_lost_begin(tmp_path):
    """The upsert fallbacks exist for a swallowed begin(): they must not
    store an empty digest, or the receipt could never exact-match on replay."""
    paths = _paths(tmp_path)
    led = delivery.Ledger(paths)
    led.inflight(BOT, "t1", "peer:nova", digest="d-inflight")
    led.receipt(BOT, "t1", "peer:nova", detail="ok")
    row = led.lookup(BOT, "t1", "peer:nova")
    assert row["status"] == delivery.STATUS_SENT and row["digest"] == "d-inflight"
    # receipt with no prior row at all still records its digest
    led.receipt(BOT, "t2", "tool:linear_comment", detail="ok", digest="d-direct")
    assert led.lookup(BOT, "t2", "tool:linear_comment")["digest"] == "d-direct"
    # and a digest-less resolve never erases a stored digest
    led.uncertain(BOT, "t1", "peer:nova", detail="timeout")
    assert led.lookup(BOT, "t1", "peer:nova")["digest"] == "d-inflight"


def test_settled_turn_leaves_no_ledger_rows(tmp_path):
    """A clean turn prunes its rows: the inbox message is gone, so they can
    never be consulted again and must not accumulate."""
    paths = _paths(tmp_path)
    agent = _agent(paths)
    msg = _send_user_msg(paths)
    agent.process_inbox_once()
    assert delivery.Ledger(paths).turn_rows(BOT, msg.id) == []
    assert len(_user_replies(paths)) == 1


def test_uncertain_send_restricts_regardless_of_attempt_counter(tmp_path):
    """The per-bot attempt file is single-slot: a newer chat overwrites it,
    so a deferred/crashed turn can come back with the counter reset. The
    recovery consult must not depend on it — an uncertain row for this turn
    id restricts the replay even when the counter says first attempt."""
    paths = _paths(tmp_path)
    agent = _agent(paths)
    provider = _SendOnce()
    agent.provider = provider
    msg = _send_user_msg(paths, "message nova for me")
    led = delivery.Ledger(paths)
    led.begin(BOT, msg.id, "peer:nova")
    led.inflight(BOT, msg.id, "peer:nova")

    agent.process_inbox_once()  # attempt counter reads 1 — consult still runs
    assert any("Delivery recovery" in s and "peer:nova" in s for s in provider.systems)
    assert any("held back" in r for r in provider.tool_results)
    assert messaging.read_inbox(paths, "nova") == []  # the send was NOT replayed


def test_interrupted_turn_records_no_terminal_receipt(tmp_path, monkeypatch):
    """The interrupt notice is not the turn's real reply. A terminal receipt
    for it — even one cleared a moment later — is a window where recovery
    would settle the message and never resume the deferred work."""
    from agent import tools as toolsmod
    from providers.base import Completion as C
    from providers.base import Provider as P
    from providers.base import ToolCall as TC

    class OnceTool(P):
        def __init__(self, model="m"):
            super().__init__(model)
            self.n = 0

        def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
            self.n += 1
            if self.n == 1:
                return C(
                    tool_calls=[TC(id="c1", name="remember", arguments={"text": "x"})],
                    finish_reason="tool_use",
                )
            return C(text="should not finish original", finish_reason="stop")

    paths = _paths(tmp_path)
    agent = _agent(paths)
    agent.provider = OnceTool()

    receipts: list[tuple] = []
    real_receipt = delivery.Ledger.receipt

    def spy(self, bot, turn, target, seq=0, *, detail=""):
        receipts.append((turn, target))
        return real_receipt(self, bot, turn, target, seq, detail=detail)

    monkeypatch.setattr(delivery.Ledger, "receipt", spy)

    def inject(ctx, args):
        messaging.send(paths, messaging.Msg(to=BOT, frm="user", text="do this instead", now=True))
        return "ok: remembered"

    monkeypatch.setattr(toolsmod, "_remember", inject)
    toolsmod._DEFAULT_TOOLS = None
    msg = _send_user_msg(paths, "original work")
    agent.process_inbox_once()  # preempted by the Send-now: interrupted + deferred
    pending = [m.text for m in messaging.pending(paths, BOT)]
    assert "original work" in pending  # still queued for the deferred re-run
    assert (msg.id, "chat:user") not in receipts
    assert delivery.Ledger(paths).lookup(BOT, msg.id, "chat:user") is None
    toolsmod._DEFAULT_TOOLS = None


def test_error_on_interrupted_turn_records_no_terminal_receipt(tmp_path, monkeypatch):
    """An error that escapes after the run was interrupted is still not the
    turn's real reply: a terminal receipt for it would make recovery settle
    the deferred message and never resume the work."""
    paths = _paths(tmp_path)
    agent = _agent(paths)
    msg = _send_user_msg(paths)

    def interrupted_boom(*a, **k):
        agent._turn_local.run.interrupted = True
        raise RuntimeError("late failure")

    monkeypatch.setattr(agent, "_produce", interrupted_boom)
    agent.process_inbox_once()  # handled error + interrupt: message defers
    assert delivery.Ledger(paths).lookup(BOT, msg.id, "chat:user") is None
    assert [m.text for m in messaging.pending(paths, BOT)] == ["hello"]

    monkeypatch.undo()
    agent.process_inbox_once()  # the deferred re-run actually happens
    replies = _user_replies(paths)
    assert any("hello" in r.text for r in replies)  # the real reply, not just the notice
    assert messaging.pending(paths, BOT) == []


def test_error_reply_is_not_duplicated_after_crash(tmp_path, monkeypatch):
    """The error notice is a terminal reply too: it rides the ledger, so a
    crash before mark_processed cannot deliver it twice."""
    paths = _paths(tmp_path)
    agent = _agent(paths)
    _send_user_msg(paths)

    def fail(*a, **k):
        raise RuntimeError("provider down")

    real = messaging.mark_processed
    calls = {"n": 0}

    def die_once(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Boom
        return real(*a, **k)

    monkeypatch.setattr(agent, "_produce", fail)
    monkeypatch.setattr(messaging, "mark_processed", die_once)
    with pytest.raises(_Boom):
        agent.process_inbox_once()
    replies = _user_replies(paths)
    assert len(replies) == 1 and "provider down" in replies[0].text

    monkeypatch.undo()
    recovered = _agent(paths)  # a healthy restart: the provider would succeed now
    recovered.process_inbox_once()
    assert messaging.pending(paths, BOT) == []
    # exactly one user-visible reply for the request — the delivered notice
    assert len(_user_replies(paths)) == 1
