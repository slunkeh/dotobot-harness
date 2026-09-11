"""Human answers remain available after later tools exhaust the loop budget."""

import json

import pytest

from agent import runtime
from agent import tools as agent_tools
from agent.runtime import build_agent
from agent.streaming import StreamReader, StreamWriter, write_answer
from agent.tools import Tool, default_tools
from harness.paths import HarnessPaths
from harness.roster import Bot
from providers.base import Completion, Provider, ToolCall
from providers.responses import request_body


@pytest.mark.parametrize(
    ("tool", "args", "answer", "expected"),
    [
        (
            "confirm",
            {"question": "Post this reply?", "detail": "Target: X/Theo. Exact reply: hello."},
            "confirm",
            "user confirmed",
        ),
        (
            "confirm",
            {"question": "Post this reply?", "detail": "Target: X/Theo. Exact reply: hello."},
            "cancel",
            "user cancelled",
        ),
        (
            "ask_user_choice",
            {"question": "Which account?", "options": ["Work", "Personal"]},
            "Work",
            "user chose: Work",
        ),
    ],
)
def test_human_answer_survives_browser_steps(tmp_path, monkeypatch, tool, args, answer, expected):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
    monkeypatch.setattr(runtime, "loop_message_budget", lambda *a, **kw: 2000)
    original = agent_tools._open_prompt

    def answer_prompt(ctx, payload):
        prompt_id = original(ctx, payload)
        write_answer(paths, prompt_id, answer)
        return prompt_id

    monkeypatch.setattr(agent_tools, "_open_prompt", answer_prompt)
    screenshot = default_tools()["computer_screenshot"]
    monkeypatch.setitem(
        default_tools(),
        "computer_screenshot",
        Tool(screenshot.spec, lambda ctx, args: "Screenshot observation. " * 600),
    )

    class AnswerProvider(Provider):
        round = 0

        def complete(self, messages, **kwargs):
            self.round += 1
            if self.round == 1:
                return Completion(tool_calls=[ToolCall("answer", tool, args)])
            body = request_body("test", messages, "", [], None)
            result = next(
                x
                for x in body["input"]
                if x.get("type") == "function_call_output" and x["call_id"] == "answer"
            )
            assert result["output"] == expected
            question = next(
                x
                for x in body["input"]
                if x.get("type") == "function_call" and x["call_id"] == "answer"
            )
            assert json.loads(question["arguments"]) == args
            if self.round < 5:
                return Completion(
                    tool_calls=[ToolCall(f"screen{self.round}", "computer_screenshot", {})]
                )
            return Completion(text=expected)

    agent.provider = AnswerProvider("test")
    # Later observations exhaust the small budget left by a large tool catalogue.
    writer = StreamWriter(paths, "approval")
    assert agent._produce("user", "Inspect X and ask before posting.", writer=writer) == expected
    cards = [
        e
        for e in StreamReader(paths, "approval")._read_new()
        if e.type == "choice"
        or (e.type == "card" and e.mutation == "appended" and e.card_type == "confirm")
    ]
    assert len(cards) == 1
