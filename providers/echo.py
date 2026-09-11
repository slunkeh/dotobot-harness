"""Local, dependency-free provider for laptop bring-up and CI.

`echo` is not a real model. It exists so the *harness* (spawn, routing,
messaging, memory, skills, tool loop) can be demonstrated end-to-end with **no
API keys and no network** — the self-hosted-first constraint taken literally.

It also exercises the generic tool loop: when a message starts with
`@<bot> <text>`, echo emits a `message_agent` tool call so bot-to-bot handoff
 runs through the same code path a real model would use.
"""

from __future__ import annotations

import re

from .base import Completion, Message, Provider, ToolCall, ToolSpec

_MENTION = re.compile(r"^\s*@(?P<bot>[A-Za-z0-9_-]+)\s+(?P<text>.+)", re.DOTALL)
_STUCK = re.compile(r"\b(stuck|too hard|help me|take over|can'?t do|need help)\b", re.IGNORECASE)
# The other direction: the bot asks for the desktop back.
_WANT_CONTROL = re.compile(
    r"\b(control back|(?:computer|desktop|mouse|keyboard) back|hand (?:it|control) back)\b",
    re.IGNORECASE,
)
_NEED_SECRET = re.compile(r"\bneed secret\s+(?P<name>[A-Za-z0-9_.-]+)", re.IGNORECASE)
_CHOOSE = re.compile(
    r"^(?P<question>[^\n]*?)\s*choose:\s*(?P<options>.+)$", re.IGNORECASE | re.DOTALL
)
_CONFIRM = re.compile(
    r"^(?P<question>[^\n]*?)\s*confirm:\s*(?P<detail>.+)$", re.IGNORECASE | re.DOTALL
)
_SHOW_PROGRESS = re.compile(r"\bshow progress\b", re.IGNORECASE)
_SHOW_CHART = re.compile(r"\bshow (?:me )?(?:a )?(?P<kind>\w+ )?chart\b", re.IGNORECASE)
# Bare "help" is deliberately absent: "help me"/"need help" belong to _STUCK.
_TIPS = re.compile(
    r"\b(tips?|getting started|what can you do|show me around|give me a tour|tour)\b",
    re.IGNORECASE,
)
_CREATE_BOT = re.compile(
    r"\bcreate (?:a )?(?:new )?bot(?: named (?P<name>[A-Za-z0-9_-]+))?",
    re.IGNORECASE,
)
_USER_CHOSE = re.compile(r"^user chose:\s*(?P<value>.+)$")
_CREATE_ROUTINE = re.compile(
    r"\b(create|set up|add|schedule)\b.{0,40}\b(routine|cron)\b"
    r"|\b(every morning|every day|each morning|daily routine)\b"
    r"|\b(in an hour|in a hour|in \d+\s*(?:hours?|hrs?|minutes?|mins?)"
    r"|remind me|one-?off|once at)\b",
    re.IGNORECASE | re.DOTALL,
)
_TIME_IN_TEXT = re.compile(
    r"\b(other|in an hour|in a hour|in \d+\s*(?:hours?|hrs?|h|minutes?|mins?|min|m)"
    r"|\d{1,2}:\d{2}\s*(?:am|pm)?|\d{1,2}\s*(?:am|pm))\b",
    re.IGNORECASE,
)


def _demo_labels(kind: str) -> list[str]:
    """Category labels for the demo; scatter plots its own x values."""
    return [] if kind == "scatter" else ["Mon", "Tue", "Wed", "Thu", "Fri"]


def _demo_series(kind: str) -> list[dict]:
    """Series for the keyless chart demo, shaped for the kind asked for."""
    if kind == "scatter":
        return [{"name": "echoes", "values": [[1, 3], [2, 7], [3, 5], [4, 9], [5, 6]]}]
    series = [
        {"name": "echoes", "values": [3, 7, 5, 9, 6]},
        {"name": "replies", "values": [2, 4, 6, 5, 8]},
    ]
    # pie/donut are one series of slices; a sparkline is a single trend line.
    return series[:1] if kind in ("pie", "donut", "sparkline") else series


class EchoProvider(Provider):
    id = "echo"

    def __init__(self, model: str = "echo-1", auth=None, **options) -> None:
        super().__init__(model, auth, **options)
        self.persona = options.get("persona", "")

    def _last_user(self, messages: list[Message]) -> Message | None:
        for m in reversed(messages):
            if m.role == "user":
                return m
        return None

    def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> Completion:
        # If a tool already returned content this turn, wrap it up. This must be
        # checked before the @mention branch, otherwise the still-present user
        # message would re-trigger the same handoff on every iteration.
        # Exception: after the user names a new bot, actually create it.
        tool_result = next((m for m in reversed(messages) if m.role == "tool"), None)
        if tool_result is not None:
            chained = _tips_after_table(messages, tool_result)
            if chained is not None:
                return chained
            chained = _create_after_choice(messages, tool_result, tools)
            if chained is not None:
                return chained
            chained = _routine_after_choice(messages, tool_result, tools)
            if chained is not None:
                return chained
            if (tool_result.name or "") == "message_agent":
                return Completion(
                    text=_echo_colleague_summary(tool_result.content, self.persona),
                    finish_reason="stop",
                    usage={"provider": "echo"},
                )
            reply = f"[{self.persona or 'echo'}] relayed reply: {tool_result.content}"
            return Completion(text=reply, finish_reason="stop", usage={"provider": "echo"})

        last = self._last_user(messages)
        text = last.content if last else ""

        tool_names = {t.name for t in (tools or [])}

        # The takeover is over: ask for the desktop back with an accept card.
        if "request_control" in tool_names and _WANT_CONTROL.search(text):
            return Completion(
                tool_calls=[
                    ToolCall(
                        id="call_echo_control",
                        name="request_control",
                        arguments={"reason": "I still have steps left on the desktop."},
                    )
                ],
                finish_reason="tool_use",
            )

        # Simulate "getting stuck": ask a human to take over.
        if "ask_human" in tool_names and _STUCK.search(text):
            return Completion(
                tool_calls=[
                    ToolCall(
                        id="call_echo_help",
                        name="ask_human",
                        arguments={"reason": f"I can't complete: {text.strip()}"},
                    )
                ],
                finish_reason="tool_use",
            )

        # Product tour, keyless: "tips" / "what can you do" -> tips table card.
        # Checked before the interactive patterns below: tips phrasing is broad
        # and none of the specific triggers contain these words.
        if _TIPS.search(text) and "show_table" in tool_names:
            from agent.tips import tips_table_args  # lazy: providers must not import agent at load

            return Completion(
                tool_calls=[
                    ToolCall(
                        id="call_echo_tips",
                        name="show_table",
                        arguments=tips_table_args(),
                    )
                ],
                finish_reason="tool_use",
            )

        # Interactive blocks, keyless: "need secret NAME" -> secure input box;
        # "choose: a | b | c" -> choice box.
        need = _NEED_SECRET.search(text)
        if need and "request_secret" in tool_names:
            return Completion(
                tool_calls=[
                    ToolCall(
                        id="call_echo_secret",
                        name="request_secret",
                        arguments={"name": need.group("name"), "reason": text.strip()},
                    )
                ],
                finish_reason="tool_use",
            )
        # "Delete it? confirm: really irreversible" -> approve/cancel card.
        # Checked before choose: so a text with both patterns confirms.
        confirm = _CONFIRM.match(text.strip())
        if confirm and "confirm" in tool_names:
            return Completion(
                tool_calls=[
                    ToolCall(
                        id="call_echo_confirm",
                        name="confirm",
                        arguments={
                            "question": confirm.group("question").strip() or "Proceed?",
                            "detail": confirm.group("detail").strip(),
                            "destructive": "delete" in text.lower(),
                        },
                    )
                ],
                finish_reason="tool_use",
            )
        if _SHOW_PROGRESS.search(text) and "show_progress" in tool_names:
            return Completion(
                tool_calls=[
                    ToolCall(
                        id="call_echo_progress",
                        name="show_progress",
                        arguments={
                            "title": "Echo progress demo",
                            "steps": [
                                {"label": "Warm up", "status": "done"},
                                {"label": "Echo things", "status": "active"},
                                {"label": "Wrap up", "status": "pending"},
                            ],
                        },
                    )
                ],
                finish_reason="tool_use",
            )
        chart = _SHOW_CHART.search(text)
        if chart and "show_chart" in tool_names:
            # "show chart" draws a line; "show bar chart" (or any other kind
            # word) draws that kind, so the demo covers the whole vocabulary.
            from agent.charts import KINDS  # lazy: providers must not import agent at load

            asked = (chart.group("kind") or "").strip().lower()
            kind = asked if asked in KINDS else "line"
            return Completion(
                tool_calls=[
                    ToolCall(
                        id="call_echo_chart",
                        name="show_chart",
                        arguments={
                            "kind": kind,
                            "title": f"Echo {kind} demo",
                            "labels": _demo_labels(kind),
                            "series": _demo_series(kind),
                        },
                    )
                ],
                finish_reason="tool_use",
            )
        choose = _CHOOSE.match(text.strip())
        if choose and "ask_user_choice" in tool_names:
            options = [o.strip() for o in choose.group("options").split("|") if o.strip()]
            if len(options) >= 2:
                return Completion(
                    tool_calls=[
                        ToolCall(
                            id="call_echo_choice",
                            name="ask_user_choice",
                            arguments={
                                "question": choose.group("question").strip() or "Your pick?",
                                "options": options,
                            },
                        )
                    ],
                    finish_reason="tool_use",
                )

        scheduled = _CREATE_ROUTINE.search(text)
        if scheduled and "create_routine" in tool_names:
            time_hit = _TIME_IN_TEXT.search(text)
            if not time_hit and "ask_user_choice" in tool_names:
                return Completion(
                    tool_calls=[
                        ToolCall(
                            id="call_echo_routine_when",
                            name="ask_user_choice",
                            arguments={
                                "question": "What time should this routine run?",
                                "options": ["8am", "9am", "10am", "Other"],
                            },
                        )
                    ],
                    finish_reason="tool_use",
                )
            when = (time_hit.group(1) if time_hit else "").strip()
            if when and when.lower() != "other":
                title = _routine_title(text)
                return Completion(
                    tool_calls=[
                        ToolCall(
                            id="call_echo_routine",
                            name="create_routine",
                            arguments={"title": title, "prompt": text.strip(), "time": when},
                        )
                    ],
                    finish_reason="tool_use",
                )

        created = _CREATE_BOT.search(text)
        if created and "create_bot" in tool_names:
            name = (created.group("name") or "").strip()
            if not name and "ask_user_choice" in tool_names:
                return Completion(
                    tool_calls=[
                        ToolCall(
                            id="call_echo_create_ask",
                            name="ask_user_choice",
                            arguments={
                                "question": "What should we name the new bot?",
                                "options": ["helper", "researcher", "writer"],
                            },
                        )
                    ],
                    finish_reason="tool_use",
                )
            if name:
                return Completion(
                    tool_calls=[
                        ToolCall(
                            id="call_echo_create",
                            name="create_bot",
                            arguments={"name": name, "provider": "echo"},
                        )
                    ],
                    finish_reason="tool_use",
                )

        mention = _MENTION.match(text)
        if mention and "message_agent" in tool_names:
            return Completion(
                tool_calls=[
                    ToolCall(
                        id="call_echo_1",
                        name="message_agent",
                        arguments={
                            "to": mention.group("bot"),
                            "text": mention.group("text").strip(),
                        },
                    )
                ],
                finish_reason="tool_use",
            )

        handed = _handoff_request(text)
        if handed is not None:
            return Completion(
                text=_echo_handoff_reply(handed),
                finish_reason="stop",
                usage={"provider": "echo"},
            )

        persona = f"{self.persona} " if self.persona else ""
        reply = f"{persona}echo> {text}".strip()
        return Completion(text=reply, finish_reason="stop", usage={"provider": "echo"})


def _handoff_request(text: str) -> str | None:
    """Body of a message_agent wrapper, or None when this is a normal chat."""
    from agent.messaging import handoff_visible_text, is_handoff

    if not is_handoff(text or ""):
        return None
    return handoff_visible_text(text)


def _echo_handoff_reply(request: str) -> str:
    """Asked-bot echo: do the work in this chat, reply with a short note."""
    body = (request or "").strip()
    clarify = re.search(r"\bclarify:\s*(.+)$", body, re.IGNORECASE | re.DOTALL)
    if clarify:
        return f"Need a decision: {clarify.group(1).strip()}"
    snippet = re.sub(r"\s+", " ", body)
    if len(snippet) > 160:
        snippet = snippet[:157].rstrip() + "…"
    return f"Done: {snippet}" if snippet else "Done."


def _echo_colleague_summary(content: str, persona: str) -> str:
    """Asker echo: present the colleague's short reply, not a transcript dump."""
    text = (content or "").strip()
    match = re.match(r"^(\S+) replied:\s*(.*)$", text, re.DOTALL)
    if match:
        who, body = match.group(1), match.group(2).strip()
        label = f"{who}: {body}" if body else text
    else:
        label = text
    prefix = f"{persona} " if persona else ""
    return f"{prefix}{label}".strip()


def _tips_after_table(messages: list[Message], tool_result: Message) -> Completion | None:
    """After the tips table is shown, end the turn with a tour wrap-up."""
    if not (tool_result.content or "").startswith("ok: table shown"):
        return None
    last = next((m for m in reversed(messages) if m.role == "user"), None)
    if not last or not _TIPS.search(last.content or ""):
        return None
    return Completion(
        text="That's the tour — try any phrase in the 'Try it' column, or ask me about a row.",
        finish_reason="stop",
        usage={"provider": "echo"},
    )


def _create_after_choice(
    messages: list[Message],
    tool_result: Message,
    tools: list[ToolSpec] | None,
) -> Completion | None:
    """If the user was asked to name a bot and picked, emit create_bot."""
    tool_names = {t.name for t in (tools or [])}
    if "create_bot" not in tool_names:
        return None
    chose = _USER_CHOSE.match((tool_result.content or "").strip())
    if not chose:
        return None
    last = next((m for m in reversed(messages) if m.role == "user"), None)
    text = last.content if last else ""
    created = _CREATE_BOT.search(text)
    if not created or (created.group("name") or "").strip():
        return None
    name = chose.group("value").strip()
    if not name:
        return None
    return Completion(
        tool_calls=[
            ToolCall(
                id="call_echo_create",
                name="create_bot",
                arguments={"name": name, "provider": "echo"},
            )
        ],
        finish_reason="tool_use",
    )


def _routine_title(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) > 48:
        cleaned = cleaned[:45].rstrip() + "…"
    return cleaned or "Scheduled routine"


def _routine_after_choice(
    messages: list[Message],
    tool_result: Message,
    tools: list[ToolSpec] | None,
) -> Completion | None:
    tool_names = {t.name for t in (tools or [])}
    if "create_routine" not in tool_names:
        return None
    chose = _USER_CHOSE.match((tool_result.content or "").strip())
    if not chose:
        return None
    last = next((m for m in reversed(messages) if m.role == "user"), None)
    text = last.content if last else ""
    if not _CREATE_ROUTINE.search(text):
        return None
    when = chose.group("value").strip()
    if not when:
        return None
    if when.lower() == "other":
        return Completion(
            text="What time should it run? For example 7:30 or 14:00.",
            finish_reason="stop",
        )
    return Completion(
        tool_calls=[
            ToolCall(
                id="call_echo_routine",
                name="create_routine",
                arguments={
                    "title": _routine_title(text),
                    "prompt": text.strip(),
                    "time": when,
                },
            )
        ],
        finish_reason="tool_use",
    )
