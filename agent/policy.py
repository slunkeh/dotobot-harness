"""Operator-writable action policy: deny before allow, fail closed, dry-run.

Until now nothing about what a bot may do was expressible outside Python. The
gate in `agent/gate.py` decides takeover and shared-display questions, and
`harness/approvals.py` remembers what a human answered, but neither lets the
person running the harness say "this bot never runs `rm`" without editing the
source.

This is that layer, and it is deliberately *not* an expression language.
Adopting one would mean a dependency, and the runtime is dependency-free by
design (CI proves it every push). What is here instead is a small matcher over
the same decision context an expression would have seen, which covers the rules
people actually write and refuses, by name, the syntax it does not support —
rather than accepting a rule and silently never matching it. That failure mode
is not hypothetical in this repo: `harness/routines.py` accepts `0 9 * * 1-5`
and never fires it, because its validator is wider than its matcher.

Three rules the policy keeps, in this order:

* **Deny beats allow.** A rule that removes permission must never be defeated
  by a broader rule that grants it, or nobody can reason about what they have
  forbidden.
* **Fail closed.** An unreadable policy refuses. A malformed deny rule refuses.
  A malformed allow rule does not permit. The one thing a broken policy must
  never do is open the gate it was written to close.
* **Silence permits.** A harness with no policy configured behaves exactly as it
  did before this file existed. This is the one place the openbot design is
  wrong for us: theirs treats an absent policy as permitting nothing, which is
  right for a company deployment that configures one, and would brick every
  existing install here. The default is "no policy, no opinion" and an operator
  opts in.

`dry-run` is why the mode exists at all: an operator writes a rule against real
traffic, reads the audit trail, and only then switches it to `enforce`. A
governance feature nobody dares turn on is not a governance feature.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MODE_ENFORCE = "enforce"
MODE_DRY_RUN = "dry-run"

#: Fields a rule may match on. `intent` is the important one: it describes what
#: an action *does* rather than which tool was called, because a button is
#: activated by a click OR by Enter OR by Space, and a rule naming one tool
#: covers one of those three doors. `origin` is how the turn was scheduled
#: ("dream" for dream turns — "idle" in their first release — "routine" for
#: cron work, empty for chat), so an operator can rule on unattended turns
#: separately from conversations. `exposure` is "web" once this turn has put
#: web content in front of the model (a screenshot, an unfurled link) and
#: empty before that, so an operator can rule on the tainted tail of a turn
#: separately from the clean start — see the exposure default in
#: `agent/govern.py`.
MATCHABLE = ("tool", "intent", "target", "bot", "origin", "exposure")


class PolicyError(ValueError):
    """A policy this harness will not load. Raised at parse time, named."""


@dataclass(frozen=True)
class Rule:
    """One line of policy: every named field must match for the rule to fire.

    A rule with no fields matches everything, which is how `allow: ["*"]` and a
    blanket deny are both written without special-casing them.
    """

    tool: str = ""
    intent: str = ""
    target: str = ""
    bot: str = ""
    origin: str = ""
    exposure: str = ""
    #: the original text, so a refusal can name the rule that caused it
    source: str = ""

    def matches(self, ctx: dict[str, Any]) -> bool:
        for name in MATCHABLE:
            pattern = getattr(self, name)
            if not pattern:
                continue
            value = str(ctx.get(name) or "")
            # Case-insensitive, because an operator writing `rm -rf` should not
            # be defeated by `RM -RF`, and every field here is either an
            # identifier we control or text a model produced.
            if not fnmatch.fnmatch(value.lower(), pattern.lower()):
                return False
        return True


@dataclass(frozen=True)
class Decision:
    allowed: bool
    #: "allow" | "refuse" | "dry-run"
    decision: str
    #: the rule text that decided it, or "" when nothing matched
    rule: str = ""
    #: "deny" | "allow" | "ask" | "default"
    source: str = "default"
    #: a human must confirm before this proceeds. Opt-in and operator-written:
    #: nothing asks by default, because a confirm card on every `ls` is how
    #: people learn to click Allow without reading it.
    ask: bool = False


@dataclass(frozen=True)
class Policy:
    mode: str = MODE_ENFORCE
    deny: tuple[Rule, ...] = field(default_factory=tuple)
    #: actions a human must confirm. Between deny and allow: something already
    #: forbidden is not worth asking about, and something merely permitted may
    #: still warrant a person looking at it.
    ask: tuple[Rule, ...] = field(default_factory=tuple)
    allow: tuple[Rule, ...] = field(default_factory=tuple)
    #: set when the policy could not be read; every check then refuses
    broken: str = ""

    def evaluate(self, ctx: dict[str, Any]) -> Decision:
        """Deny first, then allow, then the default."""
        if self.broken:
            return Decision(False, "refuse", self.broken, "default")

        for rule in self.deny:
            try:
                hit = rule.matches(ctx)
            except Exception:
                # A deny rule that cannot be evaluated denies. This is the
                # whole of "fail closed" in one line.
                return Decision(False, "refuse", rule.source or "<broken deny rule>", "deny")
            if hit:
                if self.mode == MODE_DRY_RUN:
                    return Decision(True, "dry-run", rule.source, "deny")
                return Decision(False, "refuse", rule.source, "deny")

        asked = ""
        for rule in self.ask:
            try:
                hit = rule.matches(ctx)
            except Exception:
                # A broken ask rule ASKS. Every other broken rule here fails
                # towards refusing; this one fails towards a person, which is
                # the same direction — more scrutiny, not less.
                asked = rule.source or "<broken ask rule>"
                break
            if hit:
                asked = rule.source
                break

        if not self.allow:
            # No allow list means "deny rules only", which is the shape almost
            # everyone wants: forbid two things, leave the rest alone.
            return Decision(True, "allow", asked, "ask" if asked else "default", bool(asked))

        for rule in self.allow:
            try:
                hit = rule.matches(ctx)
            except Exception:
                # A broken allow rule does not permit; fall through to the
                # others and then to the default refusal below.
                continue
            if hit:
                return Decision(
                    True, "allow", asked or rule.source, "ask" if asked else "allow", bool(asked)
                )

        if self.mode == MODE_DRY_RUN:
            return Decision(True, "dry-run", "", "default", bool(asked))
        return Decision(False, "refuse", "no allow rule matched", "default")


def _rule(raw: Any, kind: str) -> Rule:
    """Parse one rule. A string is shorthand for a tool-name glob; a table
    names fields explicitly. Anything else is refused by name rather than
    quietly dropped."""
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise PolicyError(f"{kind} rule is empty")
        return Rule(tool=text, source=text)
    if isinstance(raw, dict):
        unknown = sorted(set(raw) - set(MATCHABLE))
        if unknown:
            raise PolicyError(
                f"{kind} rule names unknown field(s) {', '.join(unknown)}; "
                f"policy rules match on {', '.join(MATCHABLE)}"
            )
        fields = {name: _field(raw, name, kind) for name in MATCHABLE}
        if not any(fields.values()):
            raise PolicyError(f"{kind} rule matches on nothing; name at least one of {MATCHABLE}")
        source = " ".join(f"{k}={v}" for k, v in fields.items() if v)
        return Rule(source=source, **fields)
    raise PolicyError(f"{kind} rule must be a string or a table, got {type(raw).__name__}")


def _field(raw: dict[str, Any], name: str, kind: str) -> str:
    """One matchable field, which must be a non-empty string glob.

    TOML happily parses `{ intent = ["run_command", "type_secret"] }` or
    `{ tool = true }`; stringifying those made a glob no real value ever
    equals, so a deny written that way was accepted and never fired. A
    falsy non-string (`[]`, `false`, `0`) is not "absent" either — on an
    allow rule it silently widened the grant. Refuse both by name.
    """
    value = raw.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PolicyError(
            f"{kind} rule field {name} must be a string glob, got {type(value).__name__}; "
            "write one table per value"
        )
    text = value.strip()
    if not text:
        raise PolicyError(f"{kind} rule field {name} is empty; drop it or give it a glob")
    return text


def parse(raw: Any) -> Policy:
    """Build a Policy from roster/config data. Raises PolicyError, named, so a
    bad policy stops startup instead of silently permitting everything."""
    if raw is None:
        return Policy()
    if not isinstance(raw, dict):
        raise PolicyError(f"policy must be a table, got {type(raw).__name__}")

    mode = str(raw.get("mode") or MODE_ENFORCE).strip().lower()
    if mode not in (MODE_ENFORCE, MODE_DRY_RUN):
        raise PolicyError(f"policy mode must be {MODE_ENFORCE!r} or {MODE_DRY_RUN!r}, got {mode!r}")

    unknown = sorted(set(raw) - {"mode", "deny", "ask", "allow"})
    if unknown:
        raise PolicyError(f"policy names unknown key(s): {', '.join(unknown)}")

    def rules(key: str) -> tuple[Rule, ...]:
        value = raw.get(key) or []
        if isinstance(value, (str, dict)):
            value = [value]
        if not isinstance(value, list):
            raise PolicyError(f"policy {key} must be a list, got {type(value).__name__}")
        return tuple(_rule(item, key) for item in value)

    return Policy(mode=mode, deny=rules("deny"), ask=rules("ask"), allow=rules("allow"))


def broken(reason: str) -> Policy:
    """A policy that refuses everything and says why. Used when the configured
    policy could not be read at all — the alternative is running with no policy,
    which is the one outcome a misconfiguration must not produce."""
    return Policy(broken=str(reason) or "policy could not be read")


#: Where an operator writes one. TOML to match `roster.toml`, and `tomllib` is
#: stdlib from 3.11, which is the floor this project already targets.
POLICY_FILE = "policy.toml"

#: Seeded on first `harness serve` when no policy.toml exists. Absent file
#: still means "ask nothing" — tests and existing homes keep that. New
#: deployments get auto-review: type_secret, vendor writes, and
#: web-exposed shell wait for a person; unattended web-exposed shell is refused.
SUGGESTED_POLICY = """\
# Suggested auto-review. Delete this file to ask nothing.
# type_secret / vendor writes / a shell after viewing the web wait for a person.
# Unattended (routine/dream) web-exposed shell is refused, not parked.
mode = "enforce"
ask = [
  { intent = "type_secret" },
  { intent = "write_tool" },
  { intent = "run_command", exposure = "web" },
]
deny = [
  { intent = "run_command", exposure = "web", origin = "routine" },
  { intent = "run_command", exposure = "web", origin = "dream" },
]
allow = []
"""


def policy_path(paths: Any) -> Path:
    return paths.home / POLICY_FILE


def ensure_suggested_policy(paths: Any) -> Path | None:
    """Write SUGGESTED_POLICY once. Never overwrite an operator's file."""
    path = policy_path(paths)
    if path.is_file():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SUGGESTED_POLICY, encoding="utf-8")
    return path


def load(paths: Any, bot: str = "") -> Policy:
    """Read `$HARNESS_HOME/policy.toml`, or an empty policy when there is none.

    Shape::

        mode = "enforce"            # or "dry-run"
        deny = ["run_command"]      # a bare string matches the tool name
        allow = []

        [bots.researcher]           # per-bot, replaces the global one entirely
        deny = [{ intent = "run_command", target = "*rm *" }]

    A per-bot table replaces rather than merges, because a merged policy is one
    nobody can read off the page: the question "what applies to this bot" should
    have one answer in one place.

    A file that exists and cannot be parsed yields a `broken` policy, which
    refuses everything. That is the deliberate asymmetry with "no file at all":
    not configuring a policy is a choice, and mistyping one is an accident.
    """
    path = policy_path(paths)
    if not path.is_file():
        return Policy()
    try:
        import tomllib

        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError) as exc:
        return broken(f"{POLICY_FILE} could not be read: {exc}")

    if not isinstance(data, dict):
        return broken(f"{POLICY_FILE} must be a table")

    per_bot = data.get("bots") if isinstance(data.get("bots"), dict) else {}
    scoped = per_bot.get(bot) if bot and isinstance(per_bot, dict) else None
    raw = scoped if isinstance(scoped, dict) else {k: v for k, v in data.items() if k != "bots"}

    try:
        return parse(raw)
    except PolicyError as exc:
        return broken(f"{POLICY_FILE}: {exc}")
