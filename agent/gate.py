"""Control gate v2: fail-closed checks + model-facing refusal vocabulary.

Two rules every gate in the harness follows:

* **Fail closed.** A gate that cannot be evaluated (unreadable control state,
  corrupt approvals store, plain bug) refuses the action instead of leaking a
  traceback into the tool loop. `fail_closed` wraps the check; whatever the
  check raises becomes a `GateCheckFailed` refusal that names the failure.

* **Refusals teach.** The model reads refusals as tool results, so each one
  names the concrete alternative and says whether retrying helps. The
  vocabulary is centralized here so every gate speaks the same way.

Reimplemented in repo style from grok-bot 0.18's `withFailClosed` hook-error
handling and local-tool-permission machinery.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


class GateRefusal(RuntimeError):
    """A gate refused the action; str(exc) is the model-facing refusal."""


class GateCheckFailed(GateRefusal):
    """The gate check itself could not be evaluated; the action fails closed."""


#: appended to every denial so the model does not route around the gate
DENIAL_NOTE = "Do not suggest workarounds to the blocked action."


def deny(message: str) -> str:
    """A refusal with the agent denial note appended (idempotent)."""
    if DENIAL_NOTE in message:
        return message
    return f"{message} {DENIAL_NOTE}"


# -- vocabulary ------------------------------------------------------------
# Short, instructive, and honest about retrying: these strings land in tool
# results, and the model does what they say.
_TAKEOVER_HELD = (
    "{action} refused: GUI actions are paused while the human has control of "
    "{bot}. Call request_control with a short reason to ask for it back, or "
    "use run_command for non-GUI work. Do not retry GUI actions until control "
    "returns."
)
_SHARED_DESKTOP_HELD = (
    "{action} refused: GUI actions are paused while the human drives the "
    "shared desktop (they took control via {holder}). Use run_command for "
    "non-GUI work. Do not retry GUI actions until they hand control back."
)
_TEACH_RECORDED = (
    "recorded: {step} — teach mode is on, so your actions are recorded as "
    "steps for the skill instead of executed. Keep suggesting steps, or wait "
    "for the human to finish teaching."
)
_CHECK_FAILED = (
    "the action was blocked because the {check} check could not be evaluated "
    "({failure}). This is fail-closed safety: when the gate cannot run, the "
    "action does not run. Retrying will not help until that failure is fixed."
)
_APPROVAL_DECLINED = (
    "the user declined {action} for this target. Do not retry it; do "
    "something else, or ask the user what they would prefer."
)
_APPROVAL_REFUSED_EARLIER = (
    "{action} was already refused for this target this turn, and a later "
    "approval does not retroactively authorize it. Do not retry; if it still "
    "matters, say so and let the user ask for it."
)
_EPOCH_SATURATED = (
    "too many actions were refused this turn, so further approval checks from "
    "it are refused outright. Do not retry; tell the user what you still need "
    "and wait for their next message."
)
_APPROVAL_TIMEOUT = (
    "the user did not answer the approval request for {action}, so nothing "
    "ran. Do not retry unprompted; tell the user you are waiting on their "
    "approval."
)


def takeover_refusal(bot: str, action: str) -> str:
    return deny(_TAKEOVER_HELD.format(bot=bot, action=action))


def shared_desktop_refusal(holder: str, action: str) -> str:
    return deny(_SHARED_DESKTOP_HELD.format(holder=holder, action=action))


def teach_recorded(step: str) -> str:
    return _TEACH_RECORDED.format(step=step)


def check_failed_message(check: str, exc: BaseException) -> str:
    failure = f"{type(exc).__name__}: {exc}".strip().rstrip(":")
    return deny(_CHECK_FAILED.format(check=check, failure=failure))


def approval_declined(action: str) -> str:
    return deny(_APPROVAL_DECLINED.format(action=action))


def approval_refused_earlier(action: str) -> str:
    return deny(_APPROVAL_REFUSED_EARLIER.format(action=action))


def epoch_saturated() -> str:
    return deny(_EPOCH_SATURATED)


def approval_timeout(action: str) -> str:
    return deny(_APPROVAL_TIMEOUT.format(action=action))


# -- fail-closed boundary --------------------------------------------------
def fail_closed(check: str) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Decorate a gate check so ANY exception becomes a fail-closed refusal.

    A `GateRefusal` raised inside passes through untouched — that is the gate
    working. Anything else means the check could not be evaluated, and the
    action is blocked with a `GateCheckFailed` naming the failure instead of
    a traceback escaping into the tool loop.
    """

    def wrap(fn: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(fn)
        def guarded(*args: P.args, **kwargs: P.kwargs) -> T:
            try:
                return fn(*args, **kwargs)
            except GateRefusal:
                raise
            except Exception as exc:
                raise GateCheckFailed(check_failed_message(check, exc)) from exc

        return guarded

    return wrap
