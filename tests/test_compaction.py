from types import SimpleNamespace

from agent.compaction import (
    DURABLE_BLOCKS,
    FALLBACK_NOTE,
    SUMMARY_PREFIX,
    flatten_turns,
    maybe_compact,
    split_point,
)
from agent.history import build_history, summary_chain, user_thread
from agent.memory import Memory
from agent.messaging import Msg, handoff_brief, send
from agent.runtime import Agent
from agent.skills import propose_skill
from agent.soul import soul_path
from agent.streaming import write_prompt
from harness.control import Control
from harness.paths import HarnessPaths
from harness.roster import Bot
from providers.base import Completion, Message, Provider, ProviderError
from providers.echo import EchoProvider


def _memory(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    return Memory(paths=paths, bot="atlas"), paths


def _seed(memory):
    """A thread with a bulky old half and a short current exchange."""
    memory.log_turn("s1", "in:user", "plan the frontend deploy " + "x" * 400, peer="user")
    memory.log_turn("s1", "out", "deploy plan drafted " + "y" * 400, peer="user")
    memory.log_turn("s1", "in:user", "add the feature flag " + "z" * 400, peer="user")
    memory.log_turn("s1", "out", "flag added " + "w" * 400, peer="user")
    memory.log_turn("s1", "in:user", "what is the deploy status?", peer="user")
    memory.log_turn("s1", "out", "all green", peer="user")


class FakeSummarizer(Provider):
    """Captures the summarization call; replies with a distinctive summary."""

    id = "fake"

    def __init__(self, model="fake-1", auth=None, **options):
        super().__init__(model, auth, **options)
        self.calls: list[tuple[list[Message], str | None, object]] = []

    def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
        self.calls.append((list(messages), system, tools))
        return Completion(text="ZALGO the user planned a deploy", finish_reason="stop")


class FailingSummarizer(Provider):
    id = "fail"

    def __init__(self, model="fail-1", auth=None, **options):
        super().__init__(model, auth, **options)

    def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
        raise ProviderError("summarizer down")


def test_split_point_is_last_real_user_message():
    turns = [
        ("user", "old ask", 1.0),
        ("assistant", "old answer", 2.0),
        ("user", "new ask", 3.0),
        ("assistant", "new answer", 4.0),
    ]
    assert split_point(turns) == 2
    # a summary is user-role but never counts as the last user message
    turns[2] = ("user", SUMMARY_PREFIX + "earlier stuff", 3.0)
    assert split_point(turns) == 0


def test_maybe_compact_partitions_at_the_last_user_message(tmp_path):
    memory, paths = _memory(tmp_path)
    _seed(memory)
    provider = FakeSummarizer()
    assert maybe_compact(
        memory,
        peer="user",
        provider=provider,
        budget=250,
        session_id="s1",
        paths=paths,
        bot="atlas",
    )
    # one plain complete() over the flattened pre-split turns, no tools
    messages, _system, tools = provider.calls[0]
    assert tools is None
    flat = messages[0].content
    assert "[user] plan the frontend deploy" in flat
    assert "[assistant] flag added" in flat
    assert "what is the deploy status?" not in flat  # the tail is not summarized

    rebuilt, cutoff = build_history(memory, peer="user", provider=Provider(model="m"))
    assert rebuilt[0].role == "user"
    assert rebuilt[0].content.startswith(SUMMARY_PREFIX)
    assert "ZALGO the user planned a deploy" in rebuilt[0].content
    assert "what is the deploy status?" in rebuilt[0].content  # verbatim tail
    assert rebuilt[-1].content == "all green"
    joined = "\n".join(m.content for m in rebuilt)
    assert "plan the frontend deploy" not in joined  # summarized away
    # the cutoff is the covers_until seam: recall regains the summarized records
    assert cutoff == memory._session_records()[4]["ts"]


def test_flatten_turns_caps_each_turn():
    flat = flatten_turns([("user", "a" * 50, 1.0)], per_turn=10)
    assert flat == "[user] " + "a" * 10 + " …"


def test_summary_records_skipped_by_user_thread_and_recall(tmp_path):
    memory, paths = _memory(tmp_path)
    _seed(memory)
    assert maybe_compact(
        memory,
        peer="user",
        provider=FakeSummarizer(),
        budget=250,
        session_id="s1",
        paths=paths,
        bot="atlas",
    )
    rows = user_thread(memory, peer="user")
    assert len(rows) == 6  # every original bubble, no summary bubble
    assert not any(SUMMARY_PREFIX in r["text"] for r in rows)
    # recall never surfaces the derived summary record...
    assert memory.recall("ZALGO") == []
    # ...but the originals it covers stay searchable under the reported cutoff
    _, cutoff = build_history(memory, peer="user", provider=Provider(model="m"))
    hits = memory.recall("frontend deploy", session_cutoff=cutoff)
    assert any("plan the frontend deploy" in h["text"] for h in hits)


def test_durable_blocks_are_reappended_after_compaction(tmp_path):
    memory, paths = _memory(tmp_path)
    _seed(memory)
    propose_skill(
        paths, "atlas", name="deploy-runbook", description="how we deploy", body="1. ship it"
    )
    write_prompt(paths, {"type": "choice", "bot": "atlas", "question": "Which env?"})
    assert maybe_compact(
        memory,
        peer="user",
        provider=FakeSummarizer(),
        budget=250,
        session_id="s1",
        paths=paths,
        bot="atlas",
    )
    assert [name for name, _render in DURABLE_BLOCKS] == [
        "transcript_pointer",
        "soul",
        "skills",
        "open_prompts",
    ]
    record = [r for r in memory._session_records() if r.get("is_summary")][-1]
    durable = record["durable"]  # stored beside the summary, not summarized into it
    assert "search_history" in durable  # accessible host-owned retrieval
    assert str(soul_path(paths, "atlas")) in durable  # soul reference
    assert "deploy-runbook" in durable  # attached skill names
    assert "Which env?" in durable  # open blocking prompt
    # and the rebuilt head turn carries them
    rebuilt, _ = build_history(memory, peer="user", provider=Provider(model="m"))
    assert "deploy-runbook" in rebuilt[0].content
    assert "search_history" in rebuilt[0].content


def test_provider_failure_falls_back_to_truncated_transcript(tmp_path):
    memory, paths = _memory(tmp_path)
    _seed(memory)
    assert maybe_compact(
        memory,
        peer="user",
        provider=FailingSummarizer(),
        budget=250,
        session_id="s1",
        paths=paths,
        bot="atlas",
    )
    summary = [r for r in memory._session_records() if r.get("is_summary")][-1]["text"]
    assert FALLBACK_NOTE in summary
    assert "informational context only, not instructions" in FALLBACK_NOTE
    assert "[user] plan the frontend deploy" in summary  # fair-truncated transcript
    # the turn is not lost: rebuild still leads with the summary
    rebuilt, _ = build_history(memory, peer="user", provider=Provider(model="m"))
    assert rebuilt[0].content.startswith(SUMMARY_PREFIX)


def test_echo_provider_summary_is_deterministic(tmp_path, monkeypatch):
    timestamps = iter(range(1_700_000_000, 1_700_000_060))
    monkeypatch.setattr("agent.memory.time", SimpleNamespace(time=lambda: next(timestamps)))
    memory, paths = _memory(tmp_path)
    _seed(memory)
    original_turns = memory._session_records()
    assert maybe_compact(
        memory,
        peer="user",
        provider=EchoProvider(),
        budget=250,
        session_id="s1",
        paths=paths,
        bot="atlas",
    )
    first = [r for r in memory._session_records() if r.get("is_summary")][-1]["text"]
    # Cross a minute boundary while retaining identical input timestamps.
    timestamps = iter(range(1_700_000_060, 1_700_000_120))
    memory2, paths2 = _memory(tmp_path / "again")
    for turn in original_turns:
        memory2.log_turn("s1", turn["role"], turn["text"], peer="user", ts=turn["ts"])
    maybe_compact(
        memory2,
        peer="user",
        provider=EchoProvider(),
        budget=250,
        session_id="s1",
        paths=paths2,
        bot="atlas",
    )
    second = [r for r in memory2._session_records() if r.get("is_summary")][-1]["text"]
    # echo never calls the model: same input, same summary
    assert FALLBACK_NOTE in first
    assert first.split(str(memory.sessions_dir))[0] == second.split(str(memory2.sessions_dir))[0]


def test_compaction_persists_and_is_not_recomputed(tmp_path):
    memory, paths = _memory(tmp_path)
    _seed(memory)
    provider = FakeSummarizer()
    assert maybe_compact(
        memory,
        peer="user",
        provider=provider,
        budget=250,
        session_id="s1",
        paths=paths,
        bot="atlas",
    )
    assert len(provider.calls) == 1
    # a fresh Memory (restart) still sees the compacted thread from disk
    fresh = Memory(paths=paths, bot="atlas")
    rebuilt, _ = build_history(fresh, peer="user", provider=Provider(model="m"))
    assert rebuilt[0].content.startswith(SUMMARY_PREFIX)
    # and the post-summary history fits again, so nothing is recomputed
    assert not maybe_compact(
        fresh,
        peer="user",
        provider=provider,
        budget=250,
        session_id="s2",
        paths=paths,
        bot="atlas",
    )
    assert len(provider.calls) == 1


class CountingSummarizer(Provider):
    """Numbered summaries so a rebuild shows which compaction produced what."""

    id = "counting"

    def __init__(self, model="count-1", auth=None, **options):
        super().__init__(model, auth, **options)
        self.calls: list[str] = []

    def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
        self.calls.append(messages[0].content)
        return Completion(text=f"SUM{len(self.calls)}", finish_reason="stop")


def _extend(memory, session, n):
    """Another bulky exchange so the thread outgrows the budget again."""
    memory.log_turn(session, "in:user", f"topic {n} " + "q" * 400, peer="user")
    memory.log_turn(session, "out", f"answer {n} " + "r" * 400, peer="user")
    memory.log_turn(session, "in:user", f"quick check {n}?", peer="user")
    memory.log_turn(session, "out", f"done {n}", peer="user")


def _compact(memory, paths, provider, budget=250):
    return maybe_compact(
        memory,
        peer="user",
        provider=provider,
        budget=budget,
        session_id="s1",
        paths=paths,
        bot="atlas",
    )


def test_recompaction_chains_instead_of_resummarizing(tmp_path):
    memory, paths = _memory(tmp_path)
    _seed(memory)
    provider = CountingSummarizer()
    assert _compact(memory, paths, provider)
    _extend(memory, "s2", 1)
    assert _compact(memory, paths, provider)
    # the second summarizer call saw only the raw records since the seam —
    # never the first summary (the old shape decayed detail multiplicatively)
    assert "SUM1" not in provider.calls[1]
    assert "topic 1" in provider.calls[1]
    chain = summary_chain(memory, "user")
    assert [r["generation"] for r in chain] == [1, 1]
    # ranges tile: each summary picks up exactly where the previous seam ended
    assert chain[0]["covers_from"] == 0.0
    assert chain[1]["covers_from"] == chain[0]["covers_until"]
    # the rebuilt head carries both summaries, oldest first, range-stamped
    rebuilt, _ = build_history(memory, peer="user", provider=Provider(model="m"))
    head = rebuilt[0].content
    assert head.index("SUM1") < head.index("SUM2")
    assert "(covers" in head
    # durable blocks render once, not once per chain record
    assert head.count("Use search_history") == 1


def test_epoch_fold_replaces_the_oldest_summaries(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_SUMMARY_CHAIN", "2")
    memory, paths = _memory(tmp_path)
    _seed(memory)
    provider = CountingSummarizer()
    assert _compact(memory, paths, provider)
    _extend(memory, "s2", 1)
    assert _compact(memory, paths, provider)
    _extend(memory, "s3", 2)
    assert _compact(memory, paths, provider)  # third summary tips the cap of 2
    chain = summary_chain(memory, "user")
    assert len(chain) == 2
    epoch, latest = chain
    assert epoch["generation"] == 2  # folded from two generation-1 records
    assert latest["generation"] == 1
    # the epoch record covers exactly the union of what it folded
    assert epoch["covers_from"] == 0.0
    assert epoch["covers_until"] == latest["covers_from"]
    # the fold summarized the summaries' bodies, not raw turns
    assert "SUM1" in provider.calls[-1] and "SUM2" in provider.calls[-1]
    rebuilt, _ = build_history(memory, peer="user", provider=Provider(model="m"))
    head = rebuilt[0].content
    assert "SUM3" in head  # epoch body first, newest summary still verbatim
    assert "SUM1" not in head  # the folded records no longer render


def test_epoch_fold_is_deterministic_without_a_summarizer(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_SUMMARY_CHAIN", "2")
    memory, paths = _memory(tmp_path)
    _seed(memory)
    provider = EchoProvider()
    assert _compact(memory, paths, provider)
    _extend(memory, "s2", 1)
    assert _compact(memory, paths, provider)
    _extend(memory, "s3", 2)
    assert _compact(memory, paths, provider)
    chain = summary_chain(memory, "user")
    assert len(chain) == 2
    assert FALLBACK_NOTE in chain[0]["text"]  # fold fell back, guarded the same way
    rebuilt, _ = build_history(memory, peer="user", provider=Provider(model="m"))
    assert rebuilt[0].content.startswith(SUMMARY_PREFIX)


def test_legacy_summary_record_chains_forward(tmp_path):
    memory, paths = _memory(tmp_path)
    _seed(memory)
    seam = memory._session_records()[4]["ts"]
    # a pre-chain record: no covers_from, no generation, durable baked in text
    memory.log_turn(
        "s1",
        "summary",
        SUMMARY_PREFIX + "legacy squash",
        peer="user",
        is_summary=True,
        covers_until=seam,
    )
    rebuilt, _ = build_history(memory, peer="user", provider=Provider(model="m"))
    assert "legacy squash" in rebuilt[0].content
    _extend(memory, "s2", 1)
    provider = CountingSummarizer()
    assert _compact(memory, paths, provider, budget=120)
    chain = summary_chain(memory, "user")
    assert [r["covers_from"] for r in chain] == [0.0, seam]  # new record chains off the legacy seam
    assert "legacy squash" not in provider.calls[0]  # and never re-summarizes it


def _agent(tmp_path, provider):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    bot = Bot(name="atlas", role="an assistant", provider="echo")
    agent = Agent(
        paths=paths,
        bot=bot,
        provider=provider,
        memory=Memory(paths=paths, bot="atlas"),
        control=Control(paths),
        stream_delay=0.0,
    )
    return agent, paths


def test_echo_agent_survives_compaction_over_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_HISTORY_TOKENS", "120")
    agent, _ = _agent(tmp_path, EchoProvider())
    out = ""
    for i in range(3):
        out = agent._produce("user", f"ping {i} " + "x" * 300)
    assert "ping 2" in out  # the bot still answers after compacting
    summaries = [r for r in agent.memory._session_records() if r.get("is_summary")]
    assert summaries
    assert summaries[-1]["text"].startswith(SUMMARY_PREFIX)
    assert FALLBACK_NOTE in summaries[-1]["text"]


def test_agent_compacts_with_a_real_style_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_HISTORY_TOKENS", "150")

    class RecordingProvider(Provider):
        id = "recording"

        def __init__(self, model="rec-1", auth=None, **options):
            super().__init__(model, auth, **options)
            self.calls: list[list[Message]] = []

        def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
            self.calls.append(list(messages))
            return Completion(text="ok", finish_reason="stop")

    provider = RecordingProvider()
    agent, _ = _agent(tmp_path, provider)
    for i in range(3):
        agent._produce("user", f"note {i} " + "x" * 400)
    records = agent.memory._session_records()
    summaries = [r for r in records if r.get("is_summary")]
    assert summaries
    assert summaries[-1]["peer"] == "user"
    assert summaries[-1]["covers_until"] <= records[-1]["ts"]
    # the summarizer turn was a plain flattened-transcript request
    assert any(
        len(call) == 1 and call[0].content.lstrip().startswith("Summarize the conversation")
        for call in provider.calls
    )


# -- seam never splits a tool pair --------------------------------
def test_split_point_never_separates_a_tool_pair():
    # a steered follow-up landed a user message between an
    # assistant tool call and its result
    turns = [
        ("user", "old ask", 1.0),
        ("assistant", "running the deploy check", 2.0),
        ("user", "[User follow-up while you were working] also check staging", 3.0),
        ("tool", "deploy check: green", 4.0),
        ("assistant", "all green", 5.0),
    ]
    # pre-fix this returned 2, cutting the call (1) away from its result (3)
    assert split_point(turns) == 1


def test_split_point_leaves_whole_tool_pairs_alone():
    tail_pair = [
        ("user", "old ask", 1.0),
        ("assistant", "old answer", 2.0),
        ("user", "new ask", 3.0),
        ("assistant", "calling the tool", 4.0),
        ("tool", "result", 5.0),
    ]
    assert split_point(tail_pair) == 2  # pair wholly on the verbatim side
    summarized_pair = [
        ("assistant", "calling the tool", 1.0),
        ("tool", "result", 2.0),
        ("user", "new ask", 3.0),
        ("assistant", "answer", 4.0),
    ]
    assert split_point(summarized_pair) == 2  # pair wholly summarized


def test_split_point_chains_back_over_stacked_results():
    turns = [
        ("assistant", "call A", 1.0),
        ("user", "follow-up 1", 2.0),
        ("tool", "result A1", 3.0),
        ("user", "follow-up 2", 4.0),
        ("tool", "result A2", 5.0),
    ]
    # both results pair with the call at 0; the seam walks all the way back
    assert split_point(turns) == 0


def test_split_point_orphan_tool_result_keeps_the_seam():
    turns = [
        ("user", "one", 1.0),
        ("user", "two", 2.0),
        ("tool", "orphan result with no call anywhere", 3.0),
    ]
    assert split_point(turns) == 1  # nothing to pair with; seam stays


# -- validate the summary before persisting -----------------------
class ScriptedSummarizer(Provider):
    """Replies from a fixed script; repeats the last entry when it runs out."""

    id = "scripted"

    def __init__(self, model="script-1", auth=None, replies=None, **options):
        super().__init__(model, auth, **options)
        self.replies = list(replies or [""])
        self.calls: list[str] = []

    def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
        self.calls.append(messages[0].content)
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return Completion(text=reply, finish_reason="stop")


def test_summary_dropping_an_open_prompt_retries_then_aborts(tmp_path):
    memory, paths = _memory(tmp_path)
    _seed(memory)
    # an unanswered choice box whose question the covered records name
    write_prompt(paths, {"type": "choice", "bot": "atlas", "question": "add the feature flag"})
    provider = ScriptedSummarizer(replies=["just some chit chat"])
    before = memory._session_records()
    assert not _compact(memory, paths, provider)
    assert len(provider.calls) == 3  # one candidate + two corrective retries
    # the retry names the rejection so the model can fix exactly that
    head = provider.calls[1].split("Summarize the conversation")[0]
    assert "rejected" in head and "add the feature flag" in head
    # nothing persisted: the original history is untouched
    assert memory._session_records() == before
    assert not [r for r in memory._session_records() if r.get("is_summary")]
    rebuilt, _ = build_history(memory, peer="user", provider=Provider(model="m"))
    assert not rebuilt[0].content.startswith(SUMMARY_PREFIX)


def test_corrective_retry_can_recover(tmp_path):
    memory, paths = _memory(tmp_path)
    _seed(memory)
    write_prompt(paths, {"type": "choice", "bot": "atlas", "question": "add the feature flag"})
    provider = ScriptedSummarizer(
        replies=["just some chit chat", "deploy planned; still open: add the feature flag"]
    )
    assert _compact(memory, paths, provider)
    assert len(provider.calls) == 2
    record = [r for r in memory._session_records() if r.get("is_summary")][-1]
    assert "add the feature flag" in record["text"]


def test_empty_and_over_budget_summaries_never_persist(tmp_path):
    for name, junk in (("empty", ""), ("huge", "blah " * 2000)):
        memory, paths = _memory(tmp_path / name)
        _seed(memory)
        provider = ScriptedSummarizer(replies=[junk])
        assert not _compact(memory, paths, provider)
        assert len(provider.calls) == 3
        assert not [r for r in memory._session_records() if r.get("is_summary")]


def test_inflight_handoff_must_survive_the_summary(tmp_path):
    memory, paths = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "ask scout to audit the deploy " + "x" * 600, peer="user")
    memory.log_turn("s1", "out", "asked scout, waiting on the audit " + "y" * 600, peer="user")
    memory.log_turn("s1", "in:user", "anything else pending?", peer="user")
    memory.log_turn("s1", "out", "nothing else", peer="user")
    # scout's handoff still sits unprocessed in atlas's inbox
    send(paths, Msg(to="atlas", frm="scout", text=handoff_brief("scout", "audit results?")))
    dropped = ScriptedSummarizer(replies=["the user planned a deploy"])
    assert not _compact(memory, paths, dropped)
    kept = ScriptedSummarizer(replies=["the user asked scout to audit the deploy"])
    assert _compact(memory, paths, kept)
    record = [r for r in memory._session_records() if r.get("is_summary")][-1]
    assert "scout" in record["text"]


def test_provider_failure_mid_retry_still_falls_back(tmp_path):
    """A summarizer hiccup keeps the deterministic fallback even after an
    invalid candidate — abort is only for candidates that fail validation."""

    class FlakyThenDown(Provider):
        id = "flaky"

        def __init__(self, model="flaky-1", auth=None, **options):
            super().__init__(model, auth, **options)
            self.calls = 0

        def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
            self.calls += 1
            if self.calls == 1:
                return Completion(text="just some chit chat", finish_reason="stop")
            raise ProviderError("summarizer down")

    memory, paths = _memory(tmp_path)
    _seed(memory)
    write_prompt(paths, {"type": "choice", "bot": "atlas", "question": "add the feature flag"})
    assert _compact(memory, paths, FlakyThenDown())
    record = [r for r in memory._session_records() if r.get("is_summary")][-1]
    assert FALLBACK_NOTE in record["text"]


def test_fallback_path_is_never_validated(tmp_path):
    """Echo (and ProviderError) keep the deterministic fair-truncation
    fallback: an open prompt never makes the keyless path abort."""
    memory, paths = _memory(tmp_path)
    _seed(memory)
    write_prompt(paths, {"type": "choice", "bot": "atlas", "question": "add the feature flag"})
    assert _compact(memory, paths, EchoProvider())
    record = [r for r in memory._session_records() if r.get("is_summary")][-1]
    assert FALLBACK_NOTE in record["text"]


# -- cancellation is not rollback ---------------------------------
def test_stop_during_compaction_keeps_the_summary_and_no_late_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_HISTORY_TOKENS", "120")

    class StopMidCompaction(Provider):
        id = "stopper"

        def __init__(self, model="stop-1", auth=None, **options):
            super().__init__(model, auth, **options)
            self.control = None
            self.summarized = 0

        def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
            if messages[0].content.lstrip().startswith("Summarize the conversation"):
                # the user hits /stop while the summarizer call is in flight
                self.summarized += 1
                self.control.request_stop("atlas")
                return Completion(text="LATECOMER deploy summary", finish_reason="stop")
            return Completion(text="a normal reply", finish_reason="stop")

    provider = StopMidCompaction()
    agent, _paths = _agent(tmp_path, provider)
    provider.control = agent.control
    replies = [agent._produce("user", f"ping {i} " + "x" * 300) for i in range(4)]
    assert provider.summarized  # compaction ran at least once
    assert "Stopped." in replies  # the cancelled turn never got a model reply
    records = agent.memory._session_records()
    summaries = [r for r in records if r.get("is_summary")]
    assert summaries  # the persisted compaction stays counted, not rolled back
    assert any("LATECOMER" in (r.get("text") or "") for r in summaries)
    # the summarizer's completion never surfaces as a chat reply
    outs = [r for r in records if r.get("role") == "out"]
    assert not any("LATECOMER" in (r.get("text") or "") for r in outs)


def test_term_beyond_the_flatten_cap_is_not_required(tmp_path):
    """A required term must be one the summarizer can actually see: the match
    runs over the capped flattened transcript, so an open prompt mentioned
    only past the per-turn cap does not doom every candidate to rejection."""
    memory, paths = _memory(tmp_path)
    memory.log_turn("s1", "in:user", "y" * 1600 + " add the feature flag", peer="user")
    memory.log_turn("s1", "out", "noted " + "z" * 400, peer="user")
    memory.log_turn("s1", "in:user", "anything else?", peer="user")
    memory.log_turn("s1", "out", "nothing else", peer="user")
    write_prompt(paths, {"type": "choice", "bot": "atlas", "question": "add the feature flag"})
    provider = ScriptedSummarizer(replies=["the user sent a long note"])
    assert _compact(memory, paths, provider)  # invisible to the model, not required
    assert len(provider.calls) == 1


def test_required_term_match_is_case_insensitive(tmp_path):
    """The covered-records check lowercases both sides, like summary_problem —
    a question capitalized differently from the transcript still counts."""
    memory, paths = _memory(tmp_path)
    _seed(memory)  # covered records say "add the feature flag" in lowercase
    write_prompt(paths, {"type": "choice", "bot": "atlas", "question": "Add The Feature Flag"})
    dropped = ScriptedSummarizer(replies=["just some chit chat"])
    assert not _compact(memory, paths, dropped)  # pre-fix the case mismatch skipped the term
    assert len(dropped.calls) == 3
    kept = ScriptedSummarizer(replies=["deploy planned; still open: ADD THE FEATURE FLAG"])
    assert _compact(memory, paths, kept)
    record = [r for r in memory._session_records() if r.get("is_summary")][-1]
    assert "ADD THE FEATURE FLAG" in record["text"]
