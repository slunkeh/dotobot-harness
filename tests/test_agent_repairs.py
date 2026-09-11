"""Classified retries as prompt repairs + tool-result reminders."""

from copy import deepcopy

import pytest

from agent import repairs
from agent.runtime import build_agent
from harness.paths import HarnessPaths
from harness.roster import Bot
from providers.base import (
    Completion,
    InputLimitError,
    Message,
    OutputLimitError,
    Provider,
    ProviderError,
    ToolCall,
)


def _agent(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    return build_agent(paths, Bot(name="atlas", role="terse", provider="echo"), stream_delay=0.0)


class ScriptedProvider(Provider):
    """Plays back completions / exceptions in order, recording each request.

    Each recorded call is a snapshot, because the
    agent loop mutates its `messages` list in place between calls.
    """

    id = "scripted"

    def __init__(self, script):
        super().__init__("scripted-1")
        self.script = list(script)
        self.calls: list[list[Message]] = []

    def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
        self.calls.append(deepcopy(messages))
        step = self.script.pop(0) if self.script else Completion(text="done")
        if isinstance(step, BaseException):
            raise step
        return step


# -- classification ----------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (OutputLimitError("cut off"), repairs.OUTPUT_LIMIT),
        (InputLimitError("too big"), repairs.INPUT_LIMIT),
        (ProviderError("Grok HTTP 400: exceeded max output tokens"), repairs.OUTPUT_LIMIT),
        (
            ProviderError("Anthropic HTTP 400: prompt is too long: 214511 tokens > 200000"),
            repairs.INPUT_LIMIT,
        ),
        (ProviderError("OpenAI HTTP 400: context_length_exceeded"), repairs.INPUT_LIMIT),
        (
            ProviderError("HTTP 400: input token count exceeds the maximum number of tokens"),
            repairs.INPUT_LIMIT,
        ),
        (ProviderError("chat completions response had no choices"), repairs.EMPTY_RESPONSE),
        (ProviderError("Grok HTTP 500: upstream connect error"), repairs.OTHER),
        (ProviderError("HTTP 401: invalid api key"), repairs.OTHER),
        # Message-string classification does not require a ProviderError type.
        (RuntimeError("Request Size Exceeds model context window"), repairs.INPUT_LIMIT),
    ],
)
def test_classify_provider_error(exc, kind):
    assert repairs.classify_provider_error(exc) == kind


def test_empty_kind_distinguishes_thinking_only():
    thinking = Completion(text="", raw={"choices": [{"message": {"reasoning_content": "hmm..."}}]})
    assert repairs.empty_kind(thinking) == "thinking_only"
    assert repairs.empty_kind(Completion(text="")) == "empty"
    assert repairs.empty_kind(Completion(text="", raw={"thinking": ""})) == "empty"


# -- output_limit: nudge once, then plain retries -----------------------------


def test_output_limit_nudges_once_and_retries(tmp_path):
    agent = _agent(tmp_path)
    agent.provider = ScriptedProvider(
        [
            ProviderError("Grok HTTP 400: exceeded max output tokens"),
            ProviderError("Grok HTTP 400: exceeded max output tokens"),
            Completion(text="finished in pieces"),
        ]
    )
    assert agent._produce("user", "write a novel") == "finished in pieces"
    assert len(agent.provider.calls) == 3
    # Both failures retried, but the nudge was added exactly once.
    nudges = [m for m in agent.provider.calls[-1] if m.content == repairs.OUTPUT_LIMIT_NUDGE]
    assert len(nudges) == 1


def test_repair_retries_bounded_overall(tmp_path):
    agent = _agent(tmp_path)
    agent.provider = ScriptedProvider([ProviderError("exceeded max output tokens")] * 20)
    with pytest.raises(ProviderError, match="max output tokens"):
        agent._produce("user", "hi")
    assert len(agent.provider.calls) == 1 + repairs.MAX_REPAIR_RETRIES


# -- empty_response: nudge, budget of 3 --------------------------------------


def test_empty_response_nudge_then_recovery(tmp_path):
    agent = _agent(tmp_path)
    agent.provider = ScriptedProvider([Completion(text=""), Completion(text="hi there")])
    assert agent._produce("user", "hello") == "hi there"
    assert agent.provider.calls[-1][-1].content == repairs.EMPTY_RESPONSE_NUDGE


def test_empty_response_retries_capped_at_three(tmp_path):
    agent = _agent(tmp_path)
    agent.provider = ScriptedProvider([Completion(text="")] * 10)
    assert agent._produce("user", "hello") == ""
    assert len(agent.provider.calls) == 4  # first try + 3 nudged retries
    nudges = [m for m in agent.provider.calls[-1] if m.content == repairs.EMPTY_RESPONSE_NUDGE]
    assert len(nudges) == 3


# -- input_limit: trim harder, retry once ------------------------------------


def test_input_limit_trims_and_retries_once(tmp_path, monkeypatch):
    import agent.runtime as rt

    budgets: list[int] = []
    real = rt.trim_loop_messages
    monkeypatch.setattr(
        rt,
        "trim_loop_messages",
        lambda msgs, budget, **kw: budgets.append(budget) or real(msgs, budget, **kw),
    )
    agent = _agent(tmp_path)
    agent.provider = ScriptedProvider(
        [ProviderError("prompt is too long"), Completion(text="fits now")]
    )
    assert agent._produce("user", "hi") == "fits now"
    assert len(agent.provider.calls) == 2
    # Admission checks context before the first request; the one repair
    # retries with half that allowance after the provider rejects it.
    assert len(budgets) == 2
    assert 0 < budgets[1] == budgets[0] // 2


def test_input_limit_surfaces_after_single_retry(tmp_path):
    agent = _agent(tmp_path)
    agent.provider = ScriptedProvider([ProviderError("prompt is too long")] * 5)
    with pytest.raises(ProviderError, match="too long"):
        agent._produce("user", "hi")
    assert len(agent.provider.calls) == 2


# -- other: surfaces immediately, no provider switch --------------------------


def test_other_errors_surface_immediately(tmp_path):
    agent = _agent(tmp_path)
    agent.provider = ScriptedProvider([ProviderError("Grok HTTP 500: boom")])
    with pytest.raises(ProviderError, match="boom"):
        agent._produce("user", "hi")
    assert len(agent.provider.calls) == 1


# -- tool-result reminders ----------------------------------------------------


def test_consecutive_tool_failures_add_reminder(tmp_path):
    def fail(i):
        return Completion(tool_calls=[ToolCall(id=f"c{i}", name="frobnicate", arguments={})])

    agent = _agent(tmp_path)
    agent.provider = ScriptedProvider([fail(1), fail(2), fail(3), Completion(text="ok")])
    assert agent._produce("user", "go") == "ok"
    # After the third consecutive failure, the LAST tool result carries the
    # reminder; the earlier ones do not.
    last_tools = [m for m in agent.provider.calls[-1] if m.role == "tool"]
    assert "frobnicate has failed 3 times in a row" in last_tools[-1].content
    assert all("times in a row" not in m.content for m in last_tools[:-1])
    prev_tools = [m for m in agent.provider.calls[-2] if m.role == "tool"]
    assert all("times in a row" not in m.content for m in prev_tools)


def test_call_count_reminder_reaches_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(repairs, "TOOL_CALL_COUNT_THRESHOLD", 2)

    def ok(i):
        return Completion(
            tool_calls=[ToolCall(id=f"c{i}", name="remember", arguments={"text": "x"})]
        )

    agent = _agent(tmp_path)
    agent.provider = ScriptedProvider([ok(1), ok(2), Completion(text="done")])
    assert agent._produce("user", "go") == "done"
    last_tools = [m for m in agent.provider.calls[-1] if m.role == "tool"]
    assert "2 tool calls this turn" in last_tools[-1].content
    assert all("tool calls this turn" not in m.content for m in last_tools[:-1])


def test_progress_text_without_tools_keeps_the_turn_open(tmp_path):
    """Narrating the next GUI step used to finalize and leave the user waiting."""
    agent = _agent(tmp_path)
    agent.provider = ScriptedProvider(
        [
            Completion(
                text="The first forum is tax, legal, and ops. Moving to the next forum.",
            ),
            Completion(text="First story is a sample launch announcement."),
        ]
    )
    assert agent._produce("user", "scan now") == "First story is a sample launch announcement."
    assert len(agent.provider.calls) == 2
    nudge = agent.provider.calls[1][-1]
    assert nudge.role == "user"
    assert "no tool call" in (nudge.content or "")


def test_progress_after_computer_use_keeps_the_turn_open(tmp_path):
    agent = _agent(tmp_path)
    agent.provider = ScriptedProvider(
        [
            Completion(
                tool_calls=[
                    ToolCall(id="s1", name="computer_screenshot", arguments={}),
                ]
            ),
            Completion(text="Ads again. Checking the remaining subs more quickly."),
            Completion(text="Domain Rating is 50."),
        ]
    )
    assert agent._produce("user", "scan reddit") == "Domain Rating is 50."
    assert len(agent.provider.calls) == 3
    assert any(
        "no tool call" in (m.content or "") for m in agent.provider.calls[2] if m.role == "user"
    )


def test_finished_answer_after_computer_still_ends(tmp_path):
    agent = _agent(tmp_path)
    agent.provider = ScriptedProvider(
        [
            Completion(
                tool_calls=[
                    ToolCall(id="c1", name="computer_screenshot", arguments={}),
                ]
            ),
            Completion(text="Domain Rating is 50."),
        ]
    )
    assert agent._produce("user", "what is the rating") == "Domain Rating is 50."
    assert len(agent.provider.calls) == 2


def _phased(text, phase):
    completion = Completion(text=text)
    completion.phase = phase
    return completion


def test_explicit_final_answer_never_needs_a_continue_nudge(tmp_path):
    agent = _agent(tmp_path)
    agent.provider = ScriptedProvider(
        [
            Completion(tool_calls=[ToolCall("s1", "computer_screenshot", {})]),
            _phased("I'll keep the saved settings as requested.", "final_answer"),
        ]
    )
    assert agent._produce("user", "check the settings") == (
        "I'll keep the saved settings as requested."
    )
    assert len(agent.provider.calls) == 2


def test_commentary_continues_without_guessing_from_its_words(tmp_path):
    agent = _agent(tmp_path)
    agent.provider = ScriptedProvider(
        [
            _phased("The checkout form is visible.", "commentary"),
            _phased("The shipping section is below the fold.", "commentary"),
            _phased("The details have been verified.", "final_answer"),
        ]
    )
    assert agent._produce("user", "verify the form") == "The details have been verified."
    assert len(agent.provider.calls) == 3
    assert [m.content for m in agent.provider.calls[-1] if m.role == "assistant"] == [
        "The checkout form is visible.",
        "The shipping section is below the fold.",
    ]
    assert not any(m.content == repairs.CONTINUE_NUDGE for m in agent.provider.calls[-1])


def test_commentary_only_loop_still_obeys_the_turn_limit(tmp_path):
    agent = _agent(tmp_path)
    agent.max_tool_iterations = 3
    agent.provider = ScriptedProvider([_phased("The form is visible.", "commentary")] * 10)
    result = agent._produce("user", "finish the form")
    assert "Stopped after too many" in result
    assert len(agent.provider.calls) == 3


def test_empty_commentary_uses_phase_continuation_instead_of_empty_reply_repair(tmp_path):
    agent = _agent(tmp_path)
    agent.provider = ScriptedProvider(
        [_phased("", "commentary"), _phased("The task is complete.", "final_answer")]
    )
    assert agent._produce("user", "finish the task") == "The task is complete."
    assert len(agent.provider.calls) == 2
    assert not any(m.content == repairs.EMPTY_RESPONSE_NUDGE for m in agent.provider.calls[-1])


def test_repeated_commentary_stops_with_an_explicit_limit_message(tmp_path):
    agent = _agent(tmp_path)
    agent.provider = ScriptedProvider([_phased("The form is visible.", "commentary")] * 20)
    assert "Stopped after repeated progress updates" in agent._produce("user", "finish it")
    assert len(agent.provider.calls) == repairs.MAX_CONTINUE_NUDGES


def _responses_completion(*items):
    import json

    from providers import responses

    event = {
        "type": "response.completed",
        "response": {"model": "gpt-6-astra", "output": list(items)},
    }
    return responses.parse_stream(["data: " + json.dumps(event)], None)


def _assistant_item(text, phase):
    return {
        "type": "message",
        "role": "assistant",
        "phase": phase,
        "content": [{"type": "output_text", "text": text}],
    }


def test_runtime_replays_openai_state_across_commentary_and_tool_rounds(tmp_path):
    from providers import responses

    progress = _assistant_item("The form is visible.", "commentary")
    encrypted = {"type": "reasoning", "id": "rs1", "encrypted_content": "opaque", "summary": []}
    call = {
        "type": "function_call",
        "call_id": "c1",
        "name": "remember",
        "arguments": '{"text":"verified form"}',
    }
    agent = _agent(tmp_path)
    agent.provider = ScriptedProvider(
        [
            _responses_completion(progress),
            _responses_completion(encrypted, call),
            _responses_completion(_assistant_item("The details are saved.", "final_answer")),
        ]
    )
    assert agent._produce("user", "remember the form details") == "The details are saved."
    replay = responses.request_body("gpt-6-astra", agent.provider.calls[-1], None, None, None)[
        "input"
    ]
    assert progress in replay and encrypted in replay and call in replay
    assert replay.index(progress) < replay.index(encrypted) < replay.index(call)
    assert len([item for item in replay if item.get("type") == "function_call"]) == 1


def test_steering_preserves_the_previous_openai_assistant_phase(tmp_path):
    from agent import messaging

    agent = _agent(tmp_path)
    item = _assistant_item("The first check passed.", "final_answer")
    prior = _responses_completion(item)
    provider = ScriptedProvider([prior, _phased("Both checks passed.", "final_answer")])
    original = provider.complete

    def complete(messages, **kwargs):
        result = original(messages, **kwargs)
        if len(provider.calls) == 1:
            messaging.send(agent.paths, messaging.Msg(to="atlas", frm="user", text="check both"))
        return result

    provider.complete = complete
    agent.provider = provider
    assert agent._produce("user", "check the first") == "Both checks passed."
    continued = [m for m in provider.calls[-1] if m.role == "assistant"]
    assert continued[-1].responses_output.items == [item]


def test_shared_computer_action_group_returns_one_observation(tmp_path, monkeypatch):
    actions = []
    monkeypatch.setenv("HARNESS_CHROME_CDP", "0")

    def act(self, action, **params):
        actions.append(action)
        return "ok: " + action

    def screenshot(self):
        actions.append("screenshot")
        return b"\xff\xd8fakejpeg", "image/jpeg"

    monkeypatch.setattr("agent.computer.HostComputer.act", act)
    monkeypatch.setattr("agent.computer.HostComputer.screenshot", screenshot)
    agent = _agent(tmp_path)
    agent.provider = ScriptedProvider(
        [
            Completion(
                tool_calls=[
                    ToolCall("c1", "computer_click", {"x": 0.2, "y": 0.3}),
                    ToolCall("c2", "computer_type", {"text": "penguin"}),
                    ToolCall("c3", "computer_key", {"key": "Return"}),
                    ToolCall("c4", "computer_screenshot", {}),
                ]
            ),
            Completion(text="The search results are visible."),
        ]
    )
    assert agent._produce("user", "search the browser") == "The search results are visible."
    assert actions == ["click", "type", "key", "screenshot"]
    assert len(agent.provider.calls) == 2
    observed = agent.provider.calls[-1]
    assert [m.tool_call_id for m in observed if m.role == "tool"] == ["c1", "c2", "c3", "c4"]
    assert observed[-1].images == [("image/jpeg", b"\xff\xd8fakejpeg")]


def test_failed_computer_action_holds_later_mutations_until_next_round(tmp_path, monkeypatch):
    actions = []
    monkeypatch.setenv("HARNESS_CHROME_CDP", "0")

    def act(self, action, **params):
        actions.append(action)
        return "error: target unavailable" if len(actions) == 1 else "ok: " + action

    def screenshot(self):
        actions.append("screenshot")
        return b"\xff\xd8fakejpeg", "image/jpeg"

    monkeypatch.setattr("agent.computer.HostComputer.act", act)
    monkeypatch.setattr("agent.computer.HostComputer.screenshot", screenshot)
    agent = _agent(tmp_path)
    agent.provider = ScriptedProvider(
        [
            Completion(
                tool_calls=[
                    ToolCall("c1", "computer_click", {"x": 0.2, "y": 0.3}),
                    ToolCall("c2", "computer_type", {"text": "private draft"}),
                    ToolCall("c3", "computer_key", {"key": "Return"}),
                    ToolCall("c4", "computer_screenshot", {}),
                ]
            ),
            Completion(tool_calls=[ToolCall("c5", "computer_click", {"x": 0.3, "y": 0.4})]),
            Completion(text="The correct field is selected."),
        ]
    )
    assert agent._produce("user", "use the browser") == "The correct field is selected."
    assert actions == ["click", "screenshot", "click"]
    results = [m for m in agent.provider.calls[1] if m.role == "tool"]
    assert [m.tool_call_id for m in results] == ["c1", "c2", "c3", "c4"]
    assert all("earlier computer action failed" in m.content for m in results[1:3])


def test_looks_like_progress_and_keep_open():
    assert repairs.looks_like_progress("Moving to the next forum.")
    assert repairs.looks_like_progress("Ads again. Checking the remaining subs more quickly.")
    assert repairs.looks_like_progress("Page is still loading. Waiting, then scrolling.")
    assert repairs.looks_like_progress("Missed Discard. Clicking it again.")
    assert not repairs.looks_like_progress("Domain Rating is 50.")
    assert not repairs.looks_like_progress(
        "I'm stuck: the password field is focused. Can you take over?"
    )
    assert repairs.should_keep_turn_open("Moving to the next forum.", computer_calls=0)
    assert repairs.should_keep_turn_open("", computer_calls=3)
    assert repairs.should_keep_turn_open(
        "That post is tax — skipping it. Back to /new and scrolling the feed.",
        computer_calls=6,
    )
    # A concrete answer finishes immediately, including after GUI work.
    assert not repairs.should_keep_turn_open(
        "Domain Rating is 50.", computer_calls=4, continue_nudges=0
    )
    # A second non-progress answer after the nudge is accepted as done.
    assert not repairs.should_keep_turn_open(
        "Domain Rating is 50.", computer_calls=4, continue_nudges=1
    )
    assert not repairs.should_keep_turn_open(
        "You're on Reddit.", computer_calls=2, continue_nudges=1
    )


def test_completed_activity_is_not_mistaken_for_progress():
    assert not repairs.looks_like_progress("Finished checking.")
    assert not repairs.looks_like_progress("I have completed checking the settings.")
    assert repairs.looks_like_progress("I'm not done checking the page.")
    assert repairs.looks_like_progress("I haven't completed checking.")
    assert repairs.looks_like_progress("I'm not quite finished checking.")
    assert repairs.looks_like_progress("Finished checking the first page. Now opening the next.")


def test_tool_stats_streak_resets_on_success():
    stats = repairs.ToolStats()
    assert stats.note("x", ok=False) is None
    assert stats.note("x", ok=False) is None
    assert stats.note("x", ok=True) is None  # success resets the streak
    assert stats.note("x", ok=False) is None
    assert stats.note("x", ok=False) is None
    reminder = stats.note("x", ok=False)
    assert reminder is not None and "x has failed 3 times in a row" in reminder
    assert stats.tool_call_count == 6


def test_tool_stats_streaks_are_per_tool():
    stats = repairs.ToolStats()
    for _ in range(2):
        assert stats.note("a", ok=False) is None
        assert stats.note("b", ok=False) is None
    assert "a has failed 3 times" in (stats.note("a", ok=False) or "")


def test_attach_reminder_targets_last_tool_result():
    messages = [
        Message(role="user", content="hi"),
        Message(role="tool", content="error: one", tool_call_id="1", name="t"),
        Message(role="tool", content="error: two", tool_call_id="2", name="t"),
        Message(role="assistant", content="thinking"),
    ]
    assert repairs.attach_reminder(messages, "<system_reminder>stop</system_reminder>")
    assert messages[2].content == "error: two\n\n<system_reminder>stop</system_reminder>"
    assert messages[1].content == "error: one"
    assert not repairs.attach_reminder([Message(role="user", content="x")], "r")


# -- the nudge must not classify its own reminders as narration ---------------


def test_continue_nudge_is_not_itself_progress():
    """The reminder contains "waiting" and "then"; matching them looped it.

    A finished answer was nudged, the reply carried the reminder back, that
    read as progress, and the loop ran to MAX_CONTINUE_NUDGES — burning a
    provider call each time and surfacing the reminder as the user's reply.
    """
    assert not repairs.looks_like_progress(repairs.CONTINUE_NUDGE)
    assert not repairs.looks_like_progress(f"echo> {repairs.CONTINUE_NUDGE}")
    assert not repairs.should_keep_turn_open(repairs.CONTINUE_NUDGE, computer_calls=0)


def test_queue_note_is_not_progress():
    """The runtime appends this to a prompt; it carries "back to"."""
    reply = (
        "Domain Rating is 50.\n\n"
        "[2 more request(s) wait in the queue to come back to after this one: a; b]"
    )
    assert not repairs.looks_like_progress(reply)
    assert not repairs.should_keep_turn_open(reply, computer_calls=0)


def test_real_narration_is_still_nudged():
    """The guard this all exists for must keep working."""
    assert repairs.looks_like_progress("Let me check the logs.")
    assert repairs.looks_like_progress("Moving to the next page.")
    assert repairs.should_keep_turn_open("I'll open the report now.", computer_calls=0)


def test_a_finished_report_is_not_progress_just_because_it_says_ill():
    """Ordinary prose ('I'll', 'still', 'then') used to re-nudge a done answer.

    The PayPal connector reply was a complete report that happened to start
    with "I'll pull…" and contain "still" / "then withdrawn". The continue
    nudge stacked the same essay until MAX_CONTINUE_NUDGES.
    """
    report = (
        "I'll pull recent PayPal activity again. There's still no balance "
        "endpoint on this connector. Same picture as last check.\n\n"
        "Recent activity (1 Jan–31 Jan): **100.00 EUR** in on 15 Jan "
        '("Example account payment"), converted to **85.00 GBP**, '
        "then withdrawn to bank on 16 Jan. No invoices, no disputes, no "
        "payment links.\n\n"
        "I don't have a current available-balance figure."
    )
    assert not repairs.looks_like_progress(report)
    assert not repairs.should_keep_turn_open(report, computer_calls=0)


def test_narration_still_caught_alongside_a_reminder():
    """Stripping scaffolding must not hide the model's own narration."""
    assert repairs.looks_like_progress(f"{repairs.CONTINUE_NUDGE}\nLet me check the logs.")


def test_model_words_strips_only_scaffolding():
    assert repairs.model_words(f"{repairs.CONTINUE_NUDGE} Domain Rating is 50.") == (
        "Domain Rating is 50."
    )
    assert repairs.model_words("plain answer") == "plain answer"
    assert repairs.model_words(repairs.CONTINUE_NUDGE) == ""
