"""Responses transport contracts shared by API-key Astra and ChatGPT sign-in."""

import json

import pytest

from providers import responses
from providers.base import Message, OutputLimitError, ProviderError, ToolCall


def sse(*events):
    return [f"data: {json.dumps(event)}\n" for event in events]


def test_tool_reply_round_trips_through_responses():
    body = responses.request_body(
        "gpt-6-astra",
        [
            Message(role="user", content="Read it", images=[("image/png", b"png")]),
            Message(
                role="assistant",
                content="Reading",
                tool_calls=[ToolCall("c1", "read", {"path": "a"})],
            ),
            Message(role="tool", content="file contents", tool_call_id="c1"),
        ],
        "system",
        None,
        "high",
    )
    assert body["input"][0]["content"][1]["image_url"] == "data:image/png;base64,cG5n"
    assert body["input"][-2]["call_id"] == body["input"][-1]["call_id"] == "c1"
    assert body["input"][-1]["output"] == "file contents"
    assert body["store"] is False


def test_tool_calls_are_deduplicated_and_text_is_streamed():
    item = {"type": "function_call", "call_id": "c1", "name": "read", "arguments": '{"path":"a"}'}
    deltas = []
    result = responses.parse_stream(
        sse(
            {"type": "response.output_text.delta", "delta": "Reading"},
            {"type": "response.output_item.done", "item": item},
            {"type": "response.completed", "response": {"output": [item]}},
        ),
        deltas.append,
        model="gpt-6-astra",
    )
    assert deltas == ["Reading"]
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].arguments == {"path": "a"}
    history = [
        Message(
            role="assistant",
            content=result.text,
            tool_calls=result.tool_calls,
            responses_output=result.responses_output,
        ),
    ]
    # Some compatible servers only include calls in the terminal payload.
    # Retaining native output must not erase text received through deltas.
    items = responses.request_body("gpt-6-astra", history, None, None, None)["input"]
    assert items[0]["content"] == [{"type": "output_text", "text": "Reading"}]
    assert items[1]["call_id"] == "c1"


@pytest.mark.parametrize(
    "event",
    [
        {"type": "response.failed", "response": {"error": {"message": "failed"}}},
        {
            "type": "response.incomplete",
            "response": {"incomplete_details": {"reason": "max_output_tokens"}},
        },
        {"type": "response.created"},
    ],
)
def test_failed_or_truncated_response_does_not_return_pending_calls(event):
    with pytest.raises(ProviderError):
        responses.parse_stream(
            sse(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "call_id": "c1",
                        "name": "write",
                        "arguments": "{}",
                    },
                },
                event,
            ),
            None,
        )


@pytest.mark.parametrize("reason", ["max_output_tokens", "content_filter"])
def test_incomplete_responses_classify_output_limits_without_returning_calls(reason):
    error_type = OutputLimitError if reason == "max_output_tokens" else ProviderError
    with pytest.raises(error_type, match=reason):
        responses.parse_stream(
            sse(
                {
                    "type": "response.incomplete",
                    "response": {
                        "incomplete_details": {"reason": reason},
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": "c1",
                                "name": "write",
                                "arguments": "{}",
                            }
                        ],
                    },
                }
            ),
            None,
        )


def test_encrypted_reasoning_survives_tool_loop_only_for_its_model():
    item = {"type": "reasoning", "id": "rs1", "encrypted_content": "opaque", "summary": []}
    completed = {
        "type": "response.completed",
        "response": {"model": "gpt-6-astra", "output": [item]},
    }
    result = responses.parse_stream(sse(completed), None)
    history = [Message(role="assistant", content="", reasoning=result.reasoning)]
    assert responses.request_body("gpt-6-astra", history, None, None, None)["input"] == [item]
    assert responses.request_body("gpt-5.6-sol", history, None, None, None)["input"] == []


def _assistant_item(text, phase):
    return {
        "type": "message",
        "id": f"msg-{phase}",
        "role": "assistant",
        "phase": phase,
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def test_phased_assistant_items_and_reasoning_replay_in_original_order():
    output = [
        {"type": "reasoning", "id": "rs1", "encrypted_content": "opaque", "summary": []},
        _assistant_item("Inspecting the page.", "commentary"),
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "computer_screenshot",
            "arguments": "{}",
        },
        _assistant_item("The page is open.", "final_answer"),
    ]
    result = responses.parse_stream(
        sse({"type": "response.completed", "response": {"output": output}}),
        None,
        model="gpt-6-astra",
    )
    assert result.phase == "final_answer"
    assert result.text == "The page is open."
    history = [
        Message(
            role="assistant",
            content=result.text,
            tool_calls=result.tool_calls,
            reasoning=result.reasoning,
            responses_output=result.responses_output,
        ),
        Message(role="tool", content="Screenshot captured", tool_call_id="c1"),
    ]
    items = responses.request_body("gpt-6-astra", history, None, None, None)["input"]
    assert items[:-1] == output
    assert items[-1] == {
        "type": "function_call_output",
        "call_id": "c1",
        "output": "Screenshot captured",
    }


def test_streamed_commentary_is_shown_but_the_completed_answer_contains_only_final_text():
    output = [
        _assistant_item("Checking the page.\n", "commentary"),
        _assistant_item("The settings are saved.", "final_answer"),
    ]
    deltas = []
    result = responses.parse_stream(
        sse(
            {"type": "response.output_text.delta", "delta": "Checking the page.\n"},
            {"type": "response.output_text.delta", "delta": "The settings are saved."},
            {"type": "response.completed", "response": {"output": output}},
        ),
        deltas.append,
        model="gpt-6-astra",
    )
    assert deltas == ["Checking the page.\n", "The settings are saved."]
    assert result.text == "The settings are saved."
    assert result.phase == "final_answer"
    message = Message(
        role="assistant", content=result.text, responses_output=result.responses_output
    )
    assert responses.request_body("gpt-6-astra", [message], None, None, None)["input"] == output


@pytest.mark.parametrize("phase", ["commentary", "final_answer", None])
def test_text_only_response_retains_phase_for_continuation(phase):
    output = [_assistant_item("Checking now.", phase)]
    result = responses.parse_stream(
        sse({"type": "response.completed", "response": {"model": "gpt-6-astra", "output": output}}),
        None,
    )
    assert result.phase == phase
    message = Message(
        role="assistant", content=result.text, responses_output=result.responses_output
    )
    assert responses.request_body("gpt-6-astra", [message], None, None, None)["input"] == output


@pytest.mark.parametrize("target_model", ["gpt-5.6-sol", None])
def test_responses_metadata_never_replays_to_a_foreign_or_unknown_model(target_model):
    output = [
        {"type": "reasoning", "encrypted_content": "opaque", "summary": []},
        _assistant_item("Checking now.", "commentary"),
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "computer_screenshot",
            "arguments": "{}",
        },
    ]
    result = responses.parse_stream(
        sse({"type": "response.completed", "response": {"model": "gpt-6-astra", "output": output}}),
        None,
    )
    message = Message(
        role="assistant",
        content=result.text,
        tool_calls=result.tool_calls,
        reasoning=result.reasoning,
        responses_output=result.responses_output,
    )
    items = responses._input_items([message], target_model)
    assert items == [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Checking now."}],
        },
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "computer_screenshot",
            "arguments": "{}",
        },
    ]


def test_response_phase_does_not_leak_to_other_provider_formats():
    from providers.base import Auth
    from providers.vendors import DeepSeekProvider

    result = responses.parse_stream(
        sse(
            {
                "type": "response.completed",
                "response": {"output": [_assistant_item("Checking now.", "commentary")]},
            }
        ),
        None,
        model="gpt-6-astra",
    )
    message = Message(
        role="assistant", content=result.text, responses_output=result.responses_output
    )
    provider = DeepSeekProvider(auth=Auth(api_key="test"))
    body = provider._body([message], None, None, 1024, 0.7, True)
    assert body["messages"] == [{"role": "assistant", "content": "Checking now."}]


@pytest.mark.parametrize(
    "requested,reported,can_replay",
    [
        ("gpt-5.4", "gpt-5.4-2026-03-05", True),
        ("gpt-5.4-mini", "gpt-5.4-mini-2026-03-17", True),
        ("gpt-5.4", "gpt-5.4-mini-2026-03-17", False),
        ("gpt-5.4", "gpt-5.4-preview", False),
        ("gpt-5.4", "gpt-5.4-2026-13-05", False),
        ("gpt-5.4-2026-03-05", "gpt-5.4-2026-04-05", False),
    ],
)
def test_api_dated_snapshot_replays_to_its_requested_alias_only(requested, reported, can_replay):
    reasoning = {"type": "reasoning", "encrypted_content": "opaque", "summary": []}
    output = [
        reasoning,
        _assistant_item("Checking now.", "commentary"),
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "computer_screenshot",
            "arguments": "{}",
        },
    ]
    result = responses.parse_stream(
        sse({"type": "response.completed", "response": {"model": reported, "output": output}}),
        None,
        model=requested,
    )
    message = Message(
        role="assistant",
        content=result.text,
        tool_calls=result.tool_calls,
        reasoning=result.reasoning,
        responses_output=result.responses_output,
    )
    items = responses.request_body(requested, [message], None, None, None)["input"]
    assert (items == output) == can_replay
    assert (reasoning in items) == can_replay
    assert any("phase" in item for item in items) == can_replay
    assert result.raw["model"] == reported
    # The same alias association also protects the legacy reasoning-only
    # replay path, used if a compatible server omits other terminal items.
    message.responses_output = None
    fallback = responses.request_body(requested, [message], None, None, None)["input"]
    assert (reasoning in fallback) == can_replay
