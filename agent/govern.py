"""The one place a tool call is authorized and recorded, before it runs.

Reimplemented in repo style from openbot's `govern()` (CopilotKit/openbot,
`server/src/computer/gateway.ts`), which is the one idea in that codebase worth
copying wholesale: every acting method goes through a single function that
resolves what the action is about, decides it, records the decision, and only
then acts.

What was wrong here before is worth stating, because the machinery was already
built. `require_approval()` in `agent/tools.py` is a complete, fail-closed
approval gate over `harness/approvals.py`'s epoch-bound refusal memory — and
`grep -rn require_approval --include=*.py .` returned its definition and its
tests, and nothing else. Every handler *could* call it; none did. The runtime
faithfully bumped the approval epoch at the top of each turn and retired scopes
at the bottom of each call, around a check that was never invoked.

The fix is not to remember harder. It is to move the gate off the handlers and
into the loop, so a tool is governed by *existing* rather than by its author
having read a convention:

    intent, target = classify(name, args)   # what is this action about
    decision       = policy.evaluate(...)   # deny before allow, fail closed
    shellguard     = inspect(command)       # rm -rf of / and ~, always on
    audit.record(...)                       # written BEFORE the handler runs
    refusal        = require_approval(...)  # the human, when policy is silent

A tool nobody has classified gets `("unknown", ...)` and is treated as an
unknown effect rather than falling outside every rule ever written — the same
reason openbot derives intent inside the gateway instead of letting call sites
pass one in.

The descriptor lives here and on `agent.tools.Tool`, deliberately not on
`ToolSpec`: `ToolSpec` is what gets serialized into the provider request and
counted by `agent/history.py`'s budget, so governance metadata hung off it
would ride in the model's context and on the token bill.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Container
from dataclasses import dataclass
from typing import Any

from harness import audit as audit_log

from . import policy as policy_mod
from . import shellguard
from .messaging import ORIGIN_DREAM, ORIGIN_IDLE, ORIGIN_ROUTINE
from .terminals import pending_stdin

#: What an action *does*, rather than which tool was called. An operator thinks
#: in effects — "nothing may run a shell" — and mechanism is a poor proxy for
#: effect. `activate` covers a click, Enter and Space, which are three ways
#: through the same door and were three separate rules before.
INTENT_RUN = "run_command"
INTENT_ACTIVATE = "activate"
INTENT_TYPE = "type"
INTENT_NAVIGATE = "navigate"
INTENT_READ = "read"
INTENT_WRITE_STATE = "write_state"
INTENT_READ_SECRET = "read_secret"
#: A secret typed into a field on the computer. Its OWN intent rather than
#: `read_secret`, because the two carry different risk and an operator must be
#: able to rule on them separately: `read_secret` tells a bot whether a
#: credential exists, while this one puts it into a login box. Someone who
#: wants the second confirmed every time should not have to confirm the first.
INTENT_TYPE_SECRET = "type_secret"
INTENT_MESSAGE = "message"
INTENT_MANAGE = "manage"
INTENT_UI = "ui"
#: A connector or MCP tool, split by effect for the same reason the browser
#: intents are: an operator thinks "nothing may change anything in Linear", not
#: "nothing may call linear_create_issue, linear_update_issue, linear_comment
#: and linear_attach_files". See `connectors/effects.py` for how the split is
#: decided, and why it is fail-closed only for servers nobody reviewed.
INTENT_READ_TOOL = "read_tool"
INTENT_WRITE_TOOL = "write_tool"
INTENT_UNKNOWN = "unknown"


def _arg(args: dict[str, Any], *names: str) -> str:
    for name in names:
        value = args.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


@dataclass(frozen=True)
class Effect:
    """What a tool does, and what it does it to."""

    intent: str
    #: pulls the human-meaningful target out of the call's arguments
    target: Callable[[dict[str, Any]], str]
    #: this effect is one an operator might reasonably want to confirm. It does
    #: NOT cause an ask on its own — `policy.toml`'s `ask` list decides that,
    #: and nothing asks by default. Kept as documentation of which effects are
    #: worth writing a rule about, and used by `askable()` for the example file.
    ask: bool = False


def _coords(args: dict[str, Any]) -> str:
    node = _arg(args, "node")
    x, y = args.get("x"), args.get("y")
    if node and x is None and y is None:
        return f"node:{node}"
    if node:
        return f"node:{node} {x},{y}"
    if x is None and y is None:
        return _arg(args, "selector", "text")
    return f"{x},{y}"


def _drag_target(args: dict[str, Any]) -> str:
    path = args.get("path")
    if not isinstance(path, list):
        return ""
    return " → ".join(_coords(point) for point in path if isinstance(point, dict))


#: The classification table. Adding a tool without adding it here is safe by
#: construction: it lands in `unknown`, which is still classified, decided and
#: recorded — and `tests/test_govern.py` fails until it is named here.
EFFECTS: dict[str, Effect] = {
    # -- the shell -------------------------------------------------------
    "run_command": Effect(INTENT_RUN, lambda a: _arg(a, "command"), ask=True),
    "run_command_background": Effect(INTENT_RUN, lambda a: _arg(a, "command"), ask=True),
    # stdin into a running shell IS running commands — `type` would leave a
    # door open that an intent rule (or the exposure hold) on run_command
    # meant to close, via a terminal left running from earlier. The target
    # is the shell id, so a run_command rule matching on `target` sees the
    # shell, not the bytes; the shell guard sees the bytes (`_shell_command`).
    "write_stdin": Effect(INTENT_RUN, lambda a: _arg(a, "shell_id")),
    "stop_terminal": Effect(INTENT_MANAGE, lambda a: _arg(a, "shell_id")),
    "read_terminal": Effect(INTENT_READ, lambda a: _arg(a, "shell_id")),
    "load_connector_tools": Effect(INTENT_READ, lambda a: _arg(a, "names", "query")),
    # -- the computer ----------------------------------------------------
    # click / key / type are one door with three handles. They share an intent
    # so that a single deny rule closes all three, which is the whole reason
    # `intent` exists rather than matching on tool names.
    "computer_click": Effect(INTENT_ACTIVATE, _coords),
    "computer_browser": Effect(INTENT_ACTIVATE, lambda a: _arg(a, "goal")),
    "computer_submit_approved": Effect(INTENT_MESSAGE, lambda a: _arg(a, "approval_id")),
    "computer_move": Effect(INTENT_ACTIVATE, _coords),
    "computer_drag": Effect(INTENT_ACTIVATE, _drag_target),
    "computer_key": Effect(INTENT_ACTIVATE, lambda a: _arg(a, "key", "keys")),
    "computer_type": Effect(INTENT_TYPE, lambda a: _arg(a, "text")),
    "computer_scroll": Effect(INTENT_ACTIVATE, _coords),
    "computer_open": Effect(INTENT_NAVIGATE, lambda a: _arg(a, "name", "url", "app")),
    "computer_screenshot": Effect(INTENT_READ, lambda a: ""),
    # -- secrets ---------------------------------------------------------
    "get_secret": Effect(INTENT_READ_SECRET, lambda a: _arg(a, "name"), ask=True),
    "credential_status": Effect(INTENT_READ, lambda a: _arg(a, "name")),
    "computer_type_secret": Effect(INTENT_TYPE_SECRET, lambda a: _arg(a, "name"), ask=True),
    "use_secret_file": Effect(INTENT_TYPE_SECRET, lambda a: _arg(a, "name"), ask=True),
    "request_secret": Effect(INTENT_READ_SECRET, lambda a: _arg(a, "name")),
    # -- durable state the bot owns --------------------------------------
    "write_soul": Effect(INTENT_WRITE_STATE, lambda a: _arg(a, "section", "name")),
    "remember": Effect(INTENT_WRITE_STATE, lambda a: _arg(a, "text", "fact")),
    "publish_fact": Effect(INTENT_WRITE_STATE, lambda a: _arg(a, "text", "fact")),
    "propose_skill": Effect(INTENT_WRITE_STATE, lambda a: _arg(a, "name")),
    "load_skill": Effect(INTENT_READ, lambda a: _arg(a, "name", "path", "file")),
    "read_soul": Effect(INTENT_READ, lambda a: ""),
    "recommend_handoff": Effect(INTENT_READ, lambda a: _arg(a, "task")),
    "recall": Effect(INTENT_READ, lambda a: _arg(a, "query")),
    "search_history": Effect(INTENT_READ, lambda a: _arg(a, "query", "record_id")),
    "read_shared_facts": Effect(INTENT_READ, lambda a: _arg(a, "query")),
    # -- the deployment --------------------------------------------------
    "create_bot": Effect(INTENT_MANAGE, lambda a: _arg(a, "name"), ask=True),
    # Adding a plugin from chat is the same door as adding a bot: it reshapes
    # what every turn can reach (Grok Bot's "Add the Notion connector?").
    "add_connector": Effect(INTENT_MANAGE, lambda a: _arg(a, "type"), ask=True),
    "duplicate_bot": Effect(INTENT_MANAGE, lambda a: _arg(a, "as", "name", "source"), ask=True),
    # A bot rewriting a profile, memory, or skill — its own or a peer's — is
    # reshaping the deployment, the same door create_bot opens. `manage`
    # rather than `write_state` so one rule covers the cross-bot case, and
    # the self case stays askable on the same rule. The target is the bot
    # acted on; an omitted `bot` means the caller itself.
    "update_bot": Effect(INTENT_MANAGE, lambda a: _arg(a, "bot") or "self", ask=True),
    "teach_bot": Effect(INTENT_MANAGE, lambda a: _arg(a, "bot") or "self", ask=True),
    "share_skill": Effect(INTENT_MANAGE, lambda a: _arg(a, "bot") or "self", ask=True),
    "create_routine": Effect(
        INTENT_MANAGE, lambda a: _arg(a, "time", "when", "schedule", "title", "name"), ask=True
    ),
    "run_routine": Effect(INTENT_MANAGE, lambda a: _arg(a, "id", "routine_id")),
    "list_routines": Effect(INTENT_READ, lambda a: "routines"),
    # Enabling a schedule or deleting one reshapes what the deployment does
    # unattended — the same door create_routine opens.
    "update_routine": Effect(INTENT_MANAGE, lambda a: _arg(a, "id", "routine_id"), ask=True),
    "delete_routine": Effect(INTENT_MANAGE, lambda a: _arg(a, "id", "routine_id"), ask=True),
    "message_agent": Effect(INTENT_MESSAGE, lambda a: _arg(a, "to", "agent", "name")),
    "collect_agent_replies": Effect(INTENT_READ, lambda a: str(a.get("request_ids", []))),
    # Posting into a group from a 1:1 is a send to every member at once.
    "message_room": Effect(INTENT_MESSAGE, lambda a: _arg(a, "room", "group", "to")),
    "stay_silent": Effect(INTENT_UI, lambda a: "group"),
    "preview_link": Effect(INTENT_NAVIGATE, lambda a: _arg(a, "url")),
    # post_image fetches remote URLs on the agent host (local paths are just a
    # copy into uploads), so it shares preview_link's door: one navigate rule
    # closes both ways a bot reaches out to a web host.
    "post_image": Effect(INTENT_NAVIGATE, lambda a: _arg(a, "source", "url", "path")),
    "request_control": Effect(INTENT_MANAGE, lambda a: _arg(a, "reason")),
    # -- cards: they render, they do not act ------------------------------
    "show_block": Effect(INTENT_UI, lambda a: _arg(a, "title", "name")),
    "update_block": Effect(INTENT_UI, lambda a: _arg(a, "id", "title")),
    "show_file": Effect(INTENT_UI, lambda a: _arg(a, "name", "path")),
    "show_table": Effect(INTENT_UI, lambda a: _arg(a, "title")),
    "show_chart": Effect(INTENT_UI, lambda a: _arg(a, "title", "kind")),
    "show_progress": Effect(INTENT_UI, lambda a: _arg(a, "title")),
    "confirm": Effect(INTENT_UI, lambda a: _arg(a, "question")),
    "list_chat_permissions": Effect(INTENT_READ, lambda a: "current chat permissions"),
    "request_chat_issue_permission": Effect(INTENT_UI, lambda a: _arg(a, "repo")),
    "request_routine_credential_permission": Effect(INTENT_UI, lambda a: _arg(a, "routine_id")),
    "revoke_chat_permission": Effect(INTENT_WRITE_STATE, lambda a: _arg(a, "id")),
    "ask_user_choice": Effect(INTENT_UI, lambda a: _arg(a, "question")),
    "ask_human": Effect(INTENT_UI, lambda a: _arg(a, "reason")),
}

#: An unclassified tool. Marked askable because a tool nobody described is the
#: case most worth an operator writing a rule about — it is still permitted by
#: default like everything else, and still recorded.
UNKNOWN = Effect(INTENT_UNKNOWN, lambda a: "", ask=True)

#: Intents held back on a dream turn (origin="dream"; "idle" from the first
#: release) when no operator rule speaks: an unattended, self-scheduled turn
#: must not spend credentials, write into external services, or reshape the
#: deployment on its own. Applied only when the policy decided by *default* —
#: an operator's explicit rule (allow, deny, or ask, e.g. one matching
#: `origin = "dream"`) always stands.
DREAM_HELD_INTENTS = frozenset(
    {
        INTENT_WRITE_TOOL,
        INTENT_TYPE_SECRET,
        INTENT_READ_SECRET,
        INTENT_MANAGE,
        INTENT_UNKNOWN,
    }
)


#: Intents withheld while the active task carries an inflight or uncertain
#: external send (`harness/delivery.py`). The hold applies immediately and on
#: resumed turns, including after restart. Unlike the dream and
#: exposure defaults this hold is unconditional — like the shell guard, it is
#: a safety interlock, not an operator preference: no allow rule can make a
#: likely duplicate not a duplicate. Unknown outcomes require reconciliation;
#: a new provider call id or a reworded payload cannot clear the task's hold.
RECOVERY_HELD_INTENTS = frozenset(
    {
        INTENT_RUN,
        INTENT_ACTIVATE,
        INTENT_TYPE,
        INTENT_TYPE_SECRET,
        INTENT_NAVIGATE,
        INTENT_MESSAGE,
        INTENT_MANAGE,
        INTENT_WRITE_TOOL,
        INTENT_UNKNOWN,
    }
)

# Missing task state or a newer user instruction prevents mutations planned
# against the old state. Observation and conversation remain available.
TASK_HELD_INTENTS = RECOVERY_HELD_INTENTS | {INTENT_WRITE_STATE}

#: Value of the policy-matchable `exposure` field once web content has entered
#: this turn's transcript ("" before that). One value rather than a set of
#: sources, because the rule an operator writes is "after the web", not "after
#: a screenshot but not a link card".
EXPOSURE_WEB = "web"

#: Tool results that put attacker-authored web text in front of the model: a
#: screenshot of a browser page, an unfurled link's page-provided title. The
#: dispatch loop flips `ctx.web_exposed` on the first successful one.
#: `run_command` (curl) is a known residual channel, deliberately not here —
#: most shell output is the bot's own tooling, and tainting every `ls` would
#: hold the whole harness hostage; see SECURITY.md.
EXPOSURE_SOURCES = frozenset({"computer_screenshot", "computer_browser", "preview_link"})

#: Intents escalated once this turn has seen web content, when the policy
#: decided by *default* — an operator's explicit rule (e.g. one matching
#: `exposure = "web"`) always stands. The set is the two escalations
#: SECURITY.md puts in scope (credential use, shell execution) plus
#: exfiltration into connectors and the dream set's tail. `activate`, `type`,
#: `navigate` and `read` stay open on purpose: holding them would make
#: browsing itself impossible, and the point is to contain what an injected
#: page can *spend*, not to stop the bot reading it.
EXPOSURE_HELD_INTENTS = frozenset(
    {
        INTENT_RUN,
        INTENT_TYPE_SECRET,
        INTENT_READ_SECRET,
        INTENT_WRITE_TOOL,
        INTENT_MANAGE,
        INTENT_UNKNOWN,
    }
)


def askable() -> tuple[str, ...]:
    """Intents worth writing an `ask` rule about. Documentation, not policy."""
    return tuple(sorted({e.intent for e in EFFECTS.values() if e.ask} | {UNKNOWN.intent}))


def _connector_effect(name: str) -> Effect:
    """A connector or MCP tool, as an Effect.

    Connector tools are not in `EFFECTS` and cannot be: they are discovered at
    runtime from whatever the operator has configured, so the table would
    always be out of date. They were landing in `UNKNOWN`, which decided them
    correctly but told a policy nothing useful — every Linear tool looked
    exactly like every GitHub tool and like a tool nobody had ever seen.
    """
    from connectors.effects import EFFECT_WRITE
    from connectors.effects import classify as classify_effect

    effect = classify_effect(name)
    intent = INTENT_WRITE_TOOL if effect == EFFECT_WRITE else INTENT_READ_TOOL
    # The target is the tool's own name: a connector call's arguments are
    # vendor-shaped and there is no field that reliably names what is being
    # acted on, so inventing one would put a guess in the audit trail.
    return Effect(intent, lambda a, n=name: n, ask=(effect == EFFECT_WRITE))


def classify(
    name: str,
    args: dict[str, Any],
    connector_tools: Container[str] | None = None,
) -> tuple[str, str, bool]:
    """(intent, target, askable) for one call. Never raises: a target extractor
    that trips over a malformed argument yields an empty target and the action
    is still decided, because an action nobody could describe must still get a
    decision rather than skipping the gate.

    `connector_tools` is the set of names that came from a connector this turn.
    It is passed rather than inferred from the name, because inferring would
    mean guessing from an underscore: a built-in that somebody forgot to add to
    `EFFECTS` would be reported as a vendor write tool, which is a worse lie
    than `unknown` — it names an effect on a system the call never touches.
    """
    effect = EFFECTS.get(name)
    if effect is None:
        if connector_tools is not None and name in connector_tools:
            effect = _connector_effect(name)
        else:
            effect = UNKNOWN
    try:
        target = effect.target(args if isinstance(args, dict) else {})
    except Exception:
        target = ""
    return effect.intent, target, effect.ask


def _task_hold_reason(ctx: Any, intent: str) -> str:
    if intent not in TASK_HELD_INTENTS:
        return ""
    if getattr(ctx, "task_revision_changed", False):
        return "new user instructions changed this task; reassess before acting"
    if getattr(ctx, "task_state_error", ""):
        return "the task state could not be saved; no consequential action was started"
    if getattr(ctx, "delivery_error", ""):
        return "the delivery intent could not be saved; no consequential action was started"
    return ""


def govern(
    ctx: Any,
    name: str,
    args: dict[str, Any],
    *,
    paths: Any,
    bot: str,
    policy: policy_mod.Policy | None = None,
    approver: Callable[..., str | None] | None = None,
    connector_tools: Container[str] | None = None,
    delivery_guard: Callable[[], str | None] | None = None,
) -> str | None:
    """Decide and record one tool call. None to proceed, else the refusal.

    The order is the contract, and it is the same order openbot's gateway keeps:
    classify, decide, **record**, then act. The audit row is written before the
    handler is reached, so there is no path that acts without the record
    existing first.
    """
    args = args if isinstance(args, dict) else {}
    intent, target, _askable = classify(name, args, connector_tools)
    outgoing_row = None
    outgoing_error = None
    if name == "computer_submit_approved":
        ctx.outgoing_approval = None
        from harness.statestore import store_for

        from .outgoing import proposal

        try:
            store = store_for(paths)
            outgoing_row = store.prompt(str(args.get("approval_id") or ""))
            row = outgoing_row or {}
            message = proposal((row.get("payload") or {}).get("outgoing_message"))
            resolution = row.get("resolution") or {}
            if (
                set(args) != {"approval_id"}
                or row.get("bot") != bot
                or not row.get("task_id")
                or row.get("task_id") != getattr(ctx, "task_id", None)
                or row.get("task_revision") != getattr(ctx, "task_revision", None)
                or row.get("task_conversation") != getattr(ctx, "task_conversation", None)
                or not store.prompt_current(row)
                or resolution.get("state") != "answered"
                or resolution.get("responded_value") != "confirm"
                or row.get("execution_started")
                or row.get("subject") != {"outgoing_message": {k: message[k] for k in ("target_url", "text")}}
            ):
                raise ValueError("approval does not match this task")
            target = message["target_url"]
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
            outgoing_error = "error: exact outgoing approval is missing, declined, stale, or already used; nothing was posted"
    active = policy or policy_mod.Policy()
    origin = str(getattr(ctx, "origin", None) or "")
    exposed = bool(getattr(ctx, "web_exposed", False))

    decision = active.evaluate(
        {
            "tool": name,
            "intent": intent,
            "target": target,
            "bot": bot,
            "origin": origin,
            "exposure": EXPOSURE_WEB if exposed else "",
        }
    )

    # Shell guard: a safety interlock, not an operator preference. Policy
    # silence permits almost everything, and an allow rule that names every
    # run_command would re-open `rm -rf /` if this waited on source ==
    # "default". It does not. Dream/exposure still decide the rest.
    shell_hit = None
    if intent == INTENT_RUN:
        shell_hit = shellguard.inspect(
            _shell_command(name, args, target, paths=paths, bot=bot),
            extra_sensitive=_extra_sensitive(paths),
            cwd=_guard_cwd(paths),
        )

    # Task delivery hold: an earlier action is inflight or uncertain. The
    # runtime restores this from durable state on every continuation; a fresh
    # model call or process cannot turn an unknown outcome into an unsent one.
    recovery_held = (
        not shell_hit
        and bool(getattr(ctx, "delivery_uncertain", False))
        and intent in RECOVERY_HELD_INTENTS
    )
    task_hold = _task_hold_reason(ctx, intent)

    # Ordered GUI actions rely on preceding actions succeeding. A failed
    # click must not be followed by typing into whatever was already focused.
    # Observation remains available, but does not reopen this same group.
    computer_batch_held = (
        bool(getattr(ctx, "computer_batch_failed", False))
        and name.startswith("computer_")
        and name != "computer_screenshot"
    )

    # Dream default: an unattended dream turn keeps read/reflect capability
    # but not side effects, unless the operator wrote a rule that decided this
    # call (source != "default"). A built-in default rather than policy text,
    # because opting a bot into dreaming must not silently open every
    # connector write on installs that configured no policy at all.
    dream_held = (
        decision.allowed
        and decision.source == "default"
        and origin in (ORIGIN_DREAM, ORIGIN_IDLE)
        and intent in DREAM_HELD_INTENTS
    )

    # Exposure default: once web content has entered the turn, a sensitive
    # intent the policy decided by *default* is escalated — to a confirm card
    # when a person is there to answer, to a refusal otherwise. Built-in
    # rather than policy text for the same reason the dream default is: a bot
    # that browses must not spend credentials or run shell on an injected
    # page's say-so just because nobody configured a policy.
    exposure_held = (
        not dream_held
        and decision.allowed
        and decision.source == "default"
        and exposed
        and intent in EXPOSURE_HELD_INTENTS
    )
    # Attended means the ask can actually reach a person and be answered:
    # an approver AND a wired approval store (require_approval proceeds
    # silently without one, which here would be a fail-open gate), on a turn
    # a person is watching (not dream/idle/routine, not a colleague consult —
    # confirm cards refuse to render in a consult).
    attended = (
        approver is not None
        and getattr(ctx, "approvals", None) is not None
        and origin not in (ORIGIN_DREAM, ORIGIN_IDLE, ORIGIN_ROUTINE)
        and str(getattr(ctx, "sender", "user") or "user") == "user"
    )
    exposure_refused = exposure_held and not attended

    if shell_hit:
        audit_decision = audit_log.DECISION_REFUSE
        audit_rule, audit_source = shell_hit.reason, "shell-guard"
    elif task_hold:
        audit_decision = audit_log.DECISION_REFUSE
        audit_rule, audit_source = task_hold, "task-state"
    elif recovery_held:
        audit_decision = audit_log.DECISION_REFUSE
        audit_rule, audit_source = "uncertain send recovery", "delivery-recovery"
    elif computer_batch_held:
        audit_decision = audit_log.DECISION_REFUSE
        audit_rule, audit_source = "earlier computer action failed", "computer-batch"
    elif dream_held:
        audit_decision = audit_log.DECISION_REFUSE
        audit_rule, audit_source = "dream default", "dream-default"
    elif exposure_held:
        audit_decision = audit_log.DECISION_REFUSE if exposure_refused else decision.decision
        audit_rule, audit_source = "exposure default", "exposure-default"
    else:
        audit_decision = decision.decision
        audit_rule, audit_source = decision.rule, decision.source

    audit_log.record(
        paths,
        bot,
        event="tool.decided",
        tool=name,
        intent=intent,
        target=target,
        decision=audit_decision,
        rule=audit_rule,
        source=audit_source,
        tool_call_id=getattr(ctx, "tool_call_id", "") or "",
    )

    if shell_hit:
        return shellguard.refusal_text(name, shell_hit)

    if task_hold:
        return f"error: {name} is held back — {task_hold}."

    if recovery_held:
        return (
            f"error: {name} is held back — an outbound send in this task "
            "is still in flight or its outcome is uncertain, and whether it "
            "arrived is unknown. Do not retry any send. Tell the user what you "
            "were doing, name the send whose delivery is uncertain, and let "
            "them check before anything goes out again."
        )

    if computer_batch_held:
        return (
            f"error: {name} skipped after an earlier computer action failed; "
            "inspect a screenshot before retrying in a new response."
        )

    if dream_held:
        return (
            f"error: {name} is held back while dreaming — this turn is "
            "unattended, so side-effect tools wait for a person. Note what you "
            "wanted to do with `remember` and pick it up next conversation. "
            '(The operator can widen this with an allow rule matching origin = "dream".)'
        )

    if not decision.allowed:
        named = f" (rule: {decision.rule})" if decision.rule else ""
        return (
            f"error: {name} refused by this harness's action policy{named}. "
            "Do not retry it and do not work around it; tell the user what you "
            "were trying to do and why it would help."
        )

    if outgoing_error:
        audit_log.record(paths, bot, event="tool.outgoing_held", tool=name, intent=intent,
                         target=target, decision=audit_log.DECISION_REFUSE, source="outgoing-approval")
        return outgoing_error

    if exposure_refused:
        return (
            f"error: {name} is held back — this turn has viewed web content, "
            "which can carry hidden instructions, and no one is here to "
            "confirm the action. Tell the user what you wanted to do, or the "
            "operator can write an allow or ask rule matching "
            'exposure = "web".'
        )

    # Policy permitted it. Whether a human is also asked is the operator's
    # call, written as an `ask` rule — nothing asks by default. Wiring the ask
    # to the effect table instead would put a confirm card in front of every
    # `ls` on every existing install, which is both a behaviour change nobody
    # asked for and the fastest way to teach people to click Allow blind.
    # The one built-in exception is the exposure default above, which
    # escalates an attended exposed turn to this same ask.
    if (decision.ask or exposure_held) and approver is not None and outgoing_row is None:
        detail = target
        if exposure_held and not decision.ask:
            detail = f"after viewing web content: {target or name}"
        refusal = approver(
            ctx,
            _action_name(intent, name),
            target or name,
            detail=detail,
            tool_name=name,
            tool_arguments=args,
        )
        if refusal:
            audit_log.record(
                paths,
                bot,
                event="tool.refused_by_human",
                tool=name,
                intent=intent,
                target=target,
                decision=audit_log.DECISION_REFUSE,
                source="human",
                detail=refusal,
                tool_call_id=getattr(ctx, "tool_call_id", "") or "",
            )
            return refusal
    # Waiting for a person can admit a cancellation or task revision. An
    # acceptance of the old card cannot reopen a plan that changed meanwhile.
    if late_hold := _task_hold_reason(ctx, intent):
        refusal = f"error: {name} is held back — {late_hold}."
        audit_log.record(
            paths,
            bot,
            event="tool.task_changed",
            tool=name,
            intent=intent,
            target=target,
            decision=audit_log.DECISION_REFUSE,
            source="task-state",
            detail=refusal,
            tool_call_id=getattr(ctx, "tool_call_id", "") or "",
        )
        return refusal
    # Protected deliveries claim execution at the same gate as policy and
    # approvals. Failed storage must never become a handler bypass or a send
    # with no durable intent. The callback is supplied by the runtime only.
    if delivery_guard is not None:
        from harness.delivery import DeliveryUnavailable

        try:
            refusal = delivery_guard()
        except DeliveryUnavailable:
            refusal = "error: delivery state could not be saved; the action was not started."
        if refusal:
            audit_log.record(
                paths,
                bot,
                event="tool.delivery_held",
                tool=name,
                intent=intent,
                target=target,
                decision=audit_log.DECISION_REFUSE,
                source="delivery-state",
                detail=refusal,
                tool_call_id=getattr(ctx, "tool_call_id", "") or "",
            )
            return refusal
    if outgoing_row is not None:
        ctx.outgoing_approval = outgoing_row
    return None


def _shell_command(
    name: str, args: dict[str, Any], target: str, *, paths: Any = None, bot: str = ""
) -> str:
    """The text a shell-guard check actually sees.

    `write_stdin` classifies with the shell id so a run_command rule still
    closes that door, but the bytes being typed *are* the command — a
    deny of `rm -rf /` that only looked at `target` would miss them. The
    bytes are prefixed with the shell's unfinished line (`pending_stdin`):
    `rm -rf` in one call and ` /\n` in the next is one command to the shell,
    so it is one command here. A tail that cannot be read is treated as
    empty — the guard never refuses on its own bookkeeping failing.
    """
    if name == "write_stdin":
        chars = _arg(args, "chars")
        try:
            return pending_stdin(paths, bot, args.get("shell_id")) + chars
        except Exception:
            return chars
    return _arg(args, "command") or target


def _extra_sensitive(paths: Any) -> tuple[str, ...]:
    home = getattr(paths, "home", None)
    if not home:
        return ()
    return (str(home), str(home / "credentials"), str(home / "machine-state"))


def _guard_cwd(paths: Any) -> str:
    env_cwd = shellguard.default_cwd()
    if env_cwd:
        return env_cwd
    workspace = getattr(paths, "workspace", None)
    return str(workspace) if workspace else ""


def _action_name(intent: str, tool: str) -> str:
    """The verb a confirm card shows. Keyed on intent so the three activation
    tools read as one action to the person answering."""
    if tool == "use_secret_file":
        return "use-secret-file"
    return {
        INTENT_RUN: "run-command",
        INTENT_READ_SECRET: "read-secret",
        INTENT_TYPE_SECRET: "type-secret",
        INTENT_MANAGE: f"manage-{tool}",
    }.get(intent, tool)


def record_outcome(
    paths: Any,
    bot: str,
    name: str,
    args: dict[str, Any],
    result: Any,
    *,
    tool_call_id: str = "",
    connector_tools: Container[str] | None = None,
) -> None:
    """The second row: a permitted action that then failed.

    `allowed` and `happened` are different claims, and recording only the first
    is how a trail ends up confidently wrong. Only failures get a row — a
    success is implied by the decision row and a second row per call would
    double the ledger for no added fact.
    """
    text = str(result or "")
    if not text.startswith("error:"):
        return
    intent, target, _ = classify(name, args if isinstance(args, dict) else {}, connector_tools)
    audit_log.record(
        paths,
        bot,
        event="tool.failed",
        tool=name,
        intent=intent,
        target=target,
        decision=audit_log.OUTCOME_FAILED,
        source="handler",
        detail=text,
        tool_call_id=tool_call_id,
    )
