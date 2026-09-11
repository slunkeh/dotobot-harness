"""Current report evidence survives follow-up image tools in a crowded chat."""

from agent.history import estimate_message_tokens, trim_loop_messages
from agent.memory import Memory
from agent.runtime import build_agent
from agent.tools import Tool, default_tools
from harness.paths import HarnessPaths
from harness.roster import Bot
from providers.base import Completion, Message, Provider, ToolCall


def report_messages():
    history = [Message(role="user", content="older conversation " * 2000)]
    current = Message(role="user", content="Report yesterday's orders with the purchased image.")
    report = Message(
        role="tool",
        content='{"orderCount":1,"ordersComplete":true,"size":"A3"}',
        tool_call_id="report",
    )
    messages = [
        *history,
        current,
        Message(role="assistant", content="", tool_calls=[ToolCall("report", "run_command", {})]),
        report,
        Message(role="assistant", content="", tool_calls=[ToolCall("image", "post_image", {})]),
        Message(
            role="tool",
            content="![Purchased product](/api/uploads/product.jpg)",
            tool_call_id="image",
        ),
    ]
    return history, current, report, messages


def test_trim_replayed_history_before_report_evidence():
    history, current, report, messages = report_messages()
    trim_loop_messages(messages, budget=2000, replayed_history=history)
    assert '"ordersComplete":true' in report.content
    assert "omitted" in history[0].content
    assert current.content == "Report yesterday's orders with the purchased image."
    assert sum(estimate_message_tokens(m) for m in messages) <= 2000


def test_history_is_unchanged_when_request_fits():
    history, _, report, messages = report_messages()
    previous = [m.content for m in messages]
    trim_loop_messages(messages, budget=100000, replayed_history=history)
    assert [m.content for m in messages] == previous
    assert '"orderCount":1' in report.content


def test_trim_does_not_change_current_request_or_steered_input():
    history, current, _, messages = report_messages()
    current.content = history[0].content  # identical text is still a new instruction
    instruction = current.content
    steered = Message(
        role="user", content="Only use the purchased variant, never substitute another."
    )
    messages.append(steered)
    trim_loop_messages(messages, budget=50, replayed_history=history)
    assert current.content == instruction
    assert steered.content == "Only use the purchased variant, never substitute another."
    assert messages[-2].content.startswith("![Purchased product]")


def test_report_then_image_reaches_final_model_call_with_evidence(tmp_path, monkeypatch):
    import agent.runtime as runtime

    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    history = [Message(role="user", content="older conversation " * 2000)]
    saved_history = history[0].content
    agent.memory.log_turn("previous", "in:user", saved_history, peer="user")
    monkeypatch.setattr(runtime, "maybe_compact", lambda *a, **kw: None)
    monkeypatch.setattr(runtime, "build_history", lambda *a, **kw: (history, None))
    monkeypatch.setattr(runtime, "loop_message_budget", lambda *a, **kw: 2000)
    report = '{"date":"2026-09-04","orderCount":1,"ordersComplete":true,"size":"A3"}'
    image = "![Purchased product](/api/uploads/product.jpg)"
    for name, output in (("run_command", report), ("post_image", image)):
        original = default_tools()[name]
        monkeypatch.setitem(
            default_tools(), name, Tool(original.spec, lambda ctx, args, output=output: output)
        )

    class ReportProvider(Provider):
        id = "scripted"
        round = 0

        def complete(self, messages, **kwargs):
            self.round += 1
            if self.round == 1:
                return Completion(
                    tool_calls=[ToolCall("report", "run_command", {"command": "read report"})]
                )
            if self.round == 2:
                assert any(m.role == "tool" and m.content == report for m in messages)
                return Completion(
                    tool_calls=[
                        ToolCall(
                            "image", "post_image", {"source": "https://example.com/product.jpg"}
                        )
                    ]
                )
            assert any(m.role == "tool" and m.content == report for m in messages)
            assert any(m.role == "tool" and m.content == image for m in messages)
            return Completion(text="Verified: one A3 order; pagination complete. " + image)

    agent.provider = ReportProvider("scripted")
    assert agent._produce("user", "Report yesterday's orders with the purchased image.").startswith(
        "Verified:"
    )
    # Request shaping must not rewrite the saved conversation.
    assert any(
        row.get("text") == saved_history for row in Memory(paths, "atlas").recall("conversation")
    )
