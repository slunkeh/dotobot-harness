"""Prompt repairs and tool-result reminders for the agent loop.

Grok-bot parity: a provider failure is not always fatal — some classes of
failure are fixed by *changing the prompt* and asking again. This module
classifies failures (typed exceptions first, then vendor error message
strings), budgets the retries, and owns every reminder string the loop may
inject, so the wording lives in one place.

Categories:

* ``output_limit`` — the reply was cut off at max output tokens. Repair: one
  break-it-into-pieces nudge per turn, then plain retries.
* ``input_limit`` — the request itself no longer fits the context window.
  Repair: trim harder through the existing loop budget, once per turn.
* ``empty_response`` — the model answered with no text and no tool calls.
  Repair: a "please continue" nudge, at most three per turn.
* ``other`` — everything else surfaces unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from providers.base import Completion, InputLimitError, Message, OutputLimitError

# -- classification -----------------------------------------------------------

OUTPUT_LIMIT = "output_limit"
INPUT_LIMIT = "input_limit"
EMPTY_RESPONSE = "empty_response"
OTHER = "other"

#: Vendor phrasings observed for "your reply hit max output tokens".
_OUTPUT_LIMIT_SNIPPETS = (
    "exceeded max output tokens",
    "max output tokens",
    "output token limit",
)

#: Vendor phrasings observed for "the request itself is too big".
_INPUT_LIMIT_SNIPPETS = (
    "prompt is too long",
    "input is too long",
    "context_length_exceeded",
    "maximum context length",
    "exceeds the maximum number of tokens",
    "exceed context limit",
    "input token count",
    "input token limit",
    "context window",
    "maximum prompt length",
    "payload too large",
    "request size cannot exceed",
    "request size exceeds",
)

#: The provider technically answered, but with nothing in it.
_EMPTY_RESPONSE_SNIPPETS = (
    "response had no choices",
    "empty response",
    "no content in response",
)


def classify_provider_error(exc: BaseException) -> str:
    """Map a provider failure to a repair category.

    Typed errors win; otherwise the vendor's message text decides. Anything
    unrecognized is ``other`` and must surface to the caller unchanged.
    """
    if isinstance(exc, OutputLimitError):
        return OUTPUT_LIMIT
    if isinstance(exc, InputLimitError):
        return INPUT_LIMIT
    msg = str(exc).lower()
    if any(s in msg for s in _OUTPUT_LIMIT_SNIPPETS):
        return OUTPUT_LIMIT
    if any(s in msg for s in _INPUT_LIMIT_SNIPPETS):
        return INPUT_LIMIT
    if any(s in msg for s in _EMPTY_RESPONSE_SNIPPETS):
        return EMPTY_RESPONSE
    return OTHER


def is_empty_completion(completion: Completion) -> bool:
    """No text and no tool calls: nothing the loop can act on."""
    return (
        not completion.tool_calls
        and not (completion.text or "").strip()
        and getattr(completion, "phase", None) != "commentary"
    )


def empty_kind(completion: Completion) -> str:
    """``thinking_only`` vs ``empty``, when the raw payload lets us tell.

    Both retry the same way; the distinction is for the log line, so a model
    that burned its whole budget on reasoning is visible as such.
    """
    return "thinking_only" if _has_thinking(completion.raw) else "empty"


_THINKING_KEYS = frozenset({"thinking", "reasoning", "reasoning_content"})


def _has_thinking(raw: object) -> bool:
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in _THINKING_KEYS and isinstance(value, str) and value.strip():
                return True
            if _has_thinking(value):
                return True
        return False
    if isinstance(raw, (list, tuple)):
        return any(_has_thinking(v) for v in raw)
    return False


# -- reminder strings (the one place their wording lives) ---------------------

OUTPUT_LIMIT_NUDGE = (
    "<system_reminder>Your response was cut off because it exceeded the "
    "output token limit — break the work into smaller pieces and continue "
    "from where you left off.</system_reminder>"
)

EMPTY_RESPONSE_NUDGE = (
    "<system_reminder>Please continue. Respond to the user or make tool calls.</system_reminder>"
)

CONTINUE_NUDGE = (
    "<system_reminder>You sent a progress update with no tool call, which "
    "ends the turn and leaves the user waiting. If the job is not finished, "
    "call the next tool now — do not only narrate. If you are actually done, "
    "give one complete answer and stop.</system_reminder>"
)

#: Progress-only replies nudged per turn before the text is allowed to finalize.
MAX_CONTINUE_NUDGES = 8

#: A real answer can contain "I'll" / "still" / "then" in ordinary prose.
#: Progress narration is a short next-step line (the cases this guard
#: exists for), not a multi-paragraph report. Anything longer is done.
_PROGRESS_MAX_CHARS = 240

_PROGRESS_MARKERS = (
    "moving to",
    "checking",
    "continuing",
    "continue",
    "waiting",
    "scrolling",
    "clicking",
    "loading",
    "i'll ",
    "i will ",
    "let me ",
    "going to",
    "next i",
    "still ",
    "then ",
    "now opening",
    "opening ",
    "now to ",
    "skipping",
    "back to",
)

_DONE_MARKERS = (
    "i'm stuck",
    "i am stuck",
    "take over",
    "can you take",
    "type it yourself",
    "type that",
)


#: Reminders this module injects. They are our words, not the model's.
_REMINDER_BLOCK = re.compile(r"<system_reminder>.*?</system_reminder>", re.DOTALL | re.IGNORECASE)

#: The queued-request note the runtime appends to a user prompt
#: (`agent/runtime.py`), which a model may carry into its reply.
_QUEUE_NOTE = re.compile(r"\[\d+ more request\(s\) wait in the queue.*?\]", re.DOTALL)

# A completed activity is not a promised next action. Strip only the verb
# phrase, so a later "now opening ..." still keeps the task running.
_COMPLETED_ACTIVITY = re.compile(
    r"\b(?:finished|completed|done)\s+(?:checking|scrolling|clicking|loading|waiting)\b"
)


def model_words(text: str) -> str:
    """Only what the model itself said, with harness scaffolding removed.

    Both scaffolds we may put in front of a model contain progress markers:
    ``CONTINUE_NUDGE`` has "waiting" and "then", and the queued-request note
    has "back to". Classifying them as the model's own narration makes the
    nudge self-sustaining — a finished answer is nudged, the reply carries the
    reminder, that reads as progress, and the loop runs to
    ``MAX_CONTINUE_NUDGES``, burning a provider call each time and surfacing
    the reminder itself as the user-visible reply.
    """
    stripped = _REMINDER_BLOCK.sub(" ", text or "")
    stripped = _QUEUE_NOTE.sub(" ", stripped)
    return stripped.strip()


def looks_like_progress(text: str) -> bool:
    """True when the model narrated the next step instead of taking it."""
    t = model_words(text).lower()
    if not t:
        return False
    if any(m in t for m in _DONE_MARKERS):
        return False
    if "?" in t:
        return False
    if len(t) > _PROGRESS_MAX_CHARS:
        return False

    def completed_activity(match: re.Match) -> str:
        clause = re.split(r"[.!?;]", t[: match.start()])[-1]
        negated = re.search(r"\b(?:not|never)\b|n['’]t\b", clause)
        return match.group() if negated else ""

    t = _COMPLETED_ACTIVITY.sub(completed_activity, t)
    return any(m in t for m in _PROGRESS_MARKERS)


def looks_like_done(text: str) -> bool:
    """True when the model is asking the user or admitting it is stuck."""
    t = model_words(text).lower()
    if not t:
        return False
    if any(m in t for m in _DONE_MARKERS):
        return True
    return "?" in t


def should_keep_turn_open(text: str, *, computer_calls: int, continue_nudges: int = 0) -> bool:
    """Fallback for providers without an explicit progress/final phase.

    Nudge recognizable progress narration, but accept a concrete answer on
    its first appearance. Merely having used the computer is not evidence
    that another provider call is needed.
    """
    if looks_like_done(text):
        return False
    if looks_like_progress(text):
        return True
    return computer_calls > 0 and not (text or "").strip()


def consecutive_failure_reminder(tool: str, count: int) -> str:
    return (
        f"<system_reminder>{tool} has failed {count} times in a row — "
        "reconsider the approach or ask the user.</system_reminder>"
    )


def call_count_reminder(count: int) -> str:
    return (
        f"<system_reminder>You have made {count} tool calls this turn. "
        "If the job is not finished, keep calling tools. Do not stop with a "
        "progress sentence. Only stop for a complete answer or a question "
        "for the user.</system_reminder>"
    )


# -- budgets ------------------------------------------------------------------

#: Prompt repairs (of any kind) per turn before the error surfaces.
MAX_REPAIR_RETRIES = 5
#: Empty completions nudged per turn before the empty reply surfaces.
MAX_EMPTY_RETRIES = 3
#: A tool erroring this many times in a row earns a rethink reminder.
CONSECUTIVE_FAILURE_THRESHOLD = 3
#: A turn burning this many tool calls earns a wrap-up reminder (the loop
#: itself still hard-stops at Agent.max_tool_iterations).
TOOL_CALL_COUNT_THRESHOLD = 40


@dataclass
class RepairState:
    """Per-turn budget for prompt-repair retries."""

    retries: int = 0
    output_nudge_added: bool = False
    input_trim_used: bool = False
    empty_retries: int = 0
    continue_nudges: int = 0

    def spend(self) -> bool:
        """Consume one retry from the overall budget; False when exhausted."""
        if self.retries >= MAX_REPAIR_RETRIES:
            return False
        self.retries += 1
        return True


@dataclass
class ToolStats:
    """Per-turn tool bookkeeping behind the tool-result reminders."""

    tool_call_count: int = 0
    computer_calls: int = 0
    consecutive_failures_by_tool: dict[str, int] = field(default_factory=dict)

    def note(self, tool: str, *, ok: bool) -> str | None:
        """Record one finished tool call.

        Returns the reminder to attach to that call's result, or None. A
        success resets the tool's failure streak; a streak nags again at
        every further multiple of the threshold.
        """
        self.tool_call_count += 1
        if tool.startswith("computer_"):
            self.computer_calls += 1
        streak = 0 if ok else self.consecutive_failures_by_tool.get(tool, 0) + 1
        self.consecutive_failures_by_tool[tool] = streak
        reminders = []
        if streak and streak % CONSECUTIVE_FAILURE_THRESHOLD == 0:
            reminders.append(consecutive_failure_reminder(tool, streak))
        if self.tool_call_count == TOOL_CALL_COUNT_THRESHOLD:
            reminders.append(call_count_reminder(self.tool_call_count))
        return "\n\n".join(reminders) or None


def attach_reminder(messages: list[Message], text: str) -> bool:
    """Append `text` to the LAST tool result, so the model reads the reminder
    next to the output it is about (grok-bot apply-reminders shape)."""
    for msg in reversed(messages):
        if msg.role == "tool":
            msg.content = f"{msg.content}\n\n{text}" if msg.content else text
            return True
    return False
