"""Shared Responses wire format for OpenAI API keys and ChatGPT subscriptions."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterable
from copy import deepcopy
from datetime import date
from typing import Any

from .base import (
    Completion,
    Message,
    OutputLimitError,
    ProviderError,
    RedactedReasoningPart,
    ResponsesOutput,
    ToolCall,
    ToolSpec,
    reasoning_for_model,
)


def request_body(
    model: str,
    messages: list[Message],
    system: str | None,
    tools: list[ToolSpec] | None,
    effort: str | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "instructions": system or "",
        "input": _input_items(messages, model),
        "include": ["reasoning.encrypted_content"],
        "stream": True,
        "store": False,
    }
    if tools:
        body["tools"] = [
            {
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters or {"type": "object", "properties": {}},
                "strict": False,
            }
            for t in tools
        ]
        body["tool_choice"] = "auto"
        body["parallel_tool_calls"] = True
    if effort:
        body["reasoning"] = {"effort": effort, "summary": "auto"}
    return body


def parse_stream(
    lines: Iterable[bytes | str],
    on_delta: Callable[[str], None] | None,
    *,
    label: str = "OpenAI",
    model: str | None = None,
) -> Completion:
    """Fold a Responses-API SSE stream into a Completion.

    Text arrives as `response.output_text.delta`; tool calls as
    `function_call` output items (observed via `response.output_item.done`
    and again in `response.completed`, deduped by call_id).
    """
    text_parts: list[str] = []
    calls: dict[str, ToolCall] = {}
    completed: dict[str, Any] | None = None
    for raw_line in lines:
        line = (
            raw_line.decode("utf-8", "replace") if isinstance(raw_line, bytes) else raw_line
        ).strip()
        if not line.startswith("data:"):
            continue
        chunk = line[len("data:") :].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            event = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                text_parts.append(delta)
                if on_delta:
                    on_delta(delta)
        elif etype == "response.output_item.done":
            _collect_call(event.get("item"), calls)
        elif etype == "response.completed":
            response = event.get("response")
            completed = response if isinstance(response, dict) else {}
        elif etype == "response.incomplete":
            response = event.get("response")
            details = response.get("incomplete_details") if isinstance(response, dict) else None
            reason = details.get("reason") if isinstance(details, dict) else "unknown"
            error = OutputLimitError if reason == "max_output_tokens" else ProviderError
            raise error(f"{label} response incomplete: {reason}")
        elif etype in ("response.failed", "error"):
            response = event.get("response")
            failure = (
                ((response or {}).get("error") if isinstance(response, dict) else None)
                or event.get("error")
                or event
            )
            raise ProviderError(f"{label} response failed: {json.dumps(failure)[:2048]}")
    if completed is None:
        raise ProviderError(f"{label} response ended without response.completed")
    output = completed.get("output") if isinstance(completed.get("output"), list) else []
    producer = _replay_model(completed.get("model"), model)
    reasoning = [
        RedactedReasoningPart(data=json.dumps(item), model=producer)
        for item in output
        if isinstance(item, dict)
        and item.get("type") == "reasoning"
        and item.get("encrypted_content")
    ]
    for item in output:
        _collect_call(item, calls)
    assistant_items = [
        item
        for item in output
        if isinstance(item, dict)
        and item.get("type") == "message"
        and item.get("role", "assistant") == "assistant"
    ]
    phase = assistant_items[-1].get("phase") if assistant_items else None
    replay_items = [
        item
        for item in output
        if isinstance(item, dict)
        and (
            item in assistant_items
            or (item.get("type") == "function_call" and item.get("call_id") in calls)
            or (item.get("type") == "reasoning" and item.get("encrypted_content"))
        )
    ]
    if not text_parts:  # non-streamed fallback: pull text off the final output
        for item in output:
            text_parts.extend(_message_texts(item))
    text = "".join(text_parts)
    # Compatible servers sometimes leave streamed text/calls out of the
    # terminal payload. Fall back to generic replay rather than lose them.
    complete_replay = (
        bool(replay_items)
        and "".join(text for item in assistant_items for text in _message_texts(item)) == text
        and {item.get("call_id") for item in replay_items if item.get("type") == "function_call"}
        == set(calls)
    )
    if phase == "final_answer":
        # Progress was streamed as it arrived; the closing answer should not
        # repeat those preambles. Replay still retains every original item.
        text = "".join(_message_texts(assistant_items[-1]))
    usage = completed.get("usage")
    tool_calls = list(calls.values())
    return Completion(
        text=text,
        tool_calls=tool_calls,
        finish_reason="tool_calls" if tool_calls else "stop",
        usage=usage if isinstance(usage, dict) else {},
        raw=completed,
        reasoning=reasoning,
        phase=phase if isinstance(phase, str) else None,
        responses_output=ResponsesOutput(model=producer, items=deepcopy(replay_items))
        if complete_replay
        else None,
    )


def _replay_model(reported: Any, requested: str | None) -> str | None:
    """Associate a dated API snapshot with the alias used for this request.

    A response to `gpt-5.4` may report `gpt-5.4-2026-03-05`. Only that exact
    alias plus a calendar-date suffix is equivalent: mini, preview, and other
    model variants retain their own tags and cannot replay to the alias.
    """
    if not isinstance(reported, str) or not reported:
        return requested
    if requested and reported.startswith(requested + "-"):
        suffix = reported[len(requested) + 1 :]
        try:
            if date.fromisoformat(suffix).isoformat() == suffix:
                return requested
        except ValueError:
            pass
    return reported


def _input_items(messages: list[Message], model: str | None = None) -> list[dict[str, Any]]:
    """Serialize vendor-neutral messages as Responses-API input items."""
    items: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": m.tool_call_id or "",
                    "output": m.content,
                }
            )
            continue
        if m.role == "assistant":
            replay = m.responses_output
            if replay and model and replay.model == model:
                # Use the original ordered items instead of also serializing
                # their generic text, calls, and reasoning a second time.
                items.extend(deepcopy(replay.items))
                continue
            # Preserve opaque Responses reasoning through a tool loop, only
            # for the model that produced it. Never send foreign vendor data.
            for part in reasoning_for_model(m.reasoning, model):
                if not isinstance(part, RedactedReasoningPart):
                    continue
                try:
                    item = json.loads(part.data)
                except (ValueError, TypeError):
                    continue
                if (
                    isinstance(item, dict)
                    and item.get("type") == "reasoning"
                    and item.get("encrypted_content")
                ):
                    items.append(item)
            if m.content:
                items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": m.content}],
                    }
                )
            for tc in m.tool_calls or []:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": tc.id,
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    }
                )
            continue
        content: list[dict[str, Any]] = []
        if m.content or not m.images:
            content.append({"type": "input_text", "text": m.content})
        for mime, data in m.images or []:
            b64 = base64.b64encode(data).decode("ascii")
            content.append({"type": "input_image", "image_url": f"data:{mime};base64,{b64}"})
        role = m.role if m.role in ("user", "system") else "user"
        items.append({"type": "message", "role": role, "content": content})
    return items


def _collect_call(item: Any, calls: dict[str, ToolCall]) -> None:
    if not isinstance(item, dict) or item.get("type") != "function_call":
        return
    call_id = str(item.get("call_id") or "")
    name = str(item.get("name") or "")
    if not call_id or not name or call_id in calls:
        return
    raw = item.get("arguments")
    if isinstance(raw, str) and raw:
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError:
            arguments = {"_raw": raw}
    else:
        arguments = raw if isinstance(raw, dict) else {}
    calls[call_id] = ToolCall(id=call_id, name=name, arguments=arguments)


def _message_texts(item: Any) -> list[str]:
    """Text parts of a completed `message` output item (non-streamed fallback)."""
    if not isinstance(item, dict) or item.get("type") != "message":
        return []
    content = item.get("content")
    out: list[str] = []
    for part in content if isinstance(content, list) else []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            out.append(part["text"])
    return out
