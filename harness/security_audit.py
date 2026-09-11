"""`harness audit` — blast-radius security audit.

Modeled on OpenClaw v2's `openclaw security audit` (`src/security/audit.ts`),
with one idea carried over whole: **severity is a cross-product, not a
per-setting check.** A shell capability with no ask rule is unremarkable on a
loopback-only harness and critical on one serving `0.0.0.0`, so most findings
here are computed as inbound exposure × tool capability rather than read off a
single file. The audit changes nothing (`--fix` excepted) and never needs the
network: it reads `serve.json`, `policy.toml`, `connectors.json` and file
modes, and reports.

What counts as what:

* **Exposure** is the recorded bind of the running (or last) `harness serve`,
  from `$HARNESS_HOME/serve.json`. A non-loopback host is exposed. No
  `serve.json` means nothing is known to listen, so exposure multipliers stay
  off — the audit reports the state on disk, not a hypothetical flag.
* **Capability** is asked of the same policy the gate evaluates
  (`agent/policy.py`), per bot, with the same context shape `agent/govern.py`
  builds. A capability is *unguarded* when the policy would allow it AND no
  ask/deny rule anywhere in that bot's policy names the capability — a
  conditional rule (e.g. ask only when `exposure = "web"`) still counts as
  guarded, because the operator visibly made a decision about it — but a rule
  whose `bot` field names a different bot does not: that field selects who a
  rule governs, not when, so it is no decision about this bot. Rules that
  match on connector tool-name globs cannot be credited against the
  `write_tool` intent; the audit reads intents, the way operators are told to
  write rules.

Suppressions live in `$HARNESS_HOME/audit.toml` as `suppress = ["check.id"]`.
An *active* suppression (one that removed findings) emits its own info-level
`audit.suppressed` finding, so a clean report can never silently hide a
suppressed critical; `audit.suppressed` itself cannot be suppressed.

`--fix` is deliberately narrow: chmod the credential files/dirs and seed the
suggested ask-policy when none exists. Everything else stays advisory — an
audit that rewrites policies or connector grants is an audit nobody dares run.
"""

from __future__ import annotations

import fnmatch
import json
import stat
from dataclasses import dataclass, field
from pathlib import Path

from agent import policy as policy_mod
from agent.govern import INTENT_RUN, INTENT_WRITE_STATE, INTENT_WRITE_TOOL

from .paths import HarnessPaths

SEV_CRITICAL = "critical"
SEV_WARN = "warn"
SEV_INFO = "info"
_SEV_ORDER = (SEV_CRITICAL, SEV_WARN, SEV_INFO)

#: Bumped only when the JSON shape changes incompatibly; CI parses this.
SCHEMA_VERSION = 1

#: Operator-written audit config (suppressions). TOML like `policy.toml`.
AUDIT_CONFIG = "audit.toml"

#: The one check id suppression can never remove.
SUPPRESSED_CHECK = "audit.suppressed"


@dataclass(frozen=True)
class Finding:
    check_id: str
    severity: str
    title: str
    detail: str
    remediation: str

    def to_dict(self) -> dict:
        return {
            "check_id": self.check_id,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "remediation": self.remediation,
        }


@dataclass
class Report:
    home: str
    exposed: bool
    bind_host: str
    findings: list[Finding] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        counts = {sev: 0 for sev in _SEV_ORDER}
        for f in self.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        return counts

    def to_dict(self) -> dict:
        ordered = sorted(
            self.findings, key=lambda f: (_SEV_ORDER.index(f.severity), f.check_id, f.detail)
        )
        return {
            "version": SCHEMA_VERSION,
            "home": self.home,
            "exposed": self.exposed,
            "bind_host": self.bind_host,
            "summary": self.summary(),
            "findings": [f.to_dict() for f in ordered],
            "fixed": list(self.fixed),
        }


# -- exposure ---------------------------------------------------------------


def _loopback(host: str) -> bool:
    h = str(host or "").strip().lower()
    return h in ("localhost", "::1") or h.startswith("127.")


def serve_exposure(paths: HarnessPaths) -> tuple[bool, str]:
    """(exposed, bind host) from `serve.json` — the record `make_server` writes."""
    path = paths.home / "serve.json"
    if not path.is_file():
        return False, ""
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, ""
    host = str(info.get("host") or "").strip()
    if not host:
        return False, ""
    return not _loopback(host), host


# -- policy capability analysis ---------------------------------------------


def _capability_ctx(intent: str, tool: str, bot: str) -> dict:
    """The same context shape `agent/govern.py` hands `Policy.evaluate`."""
    return {"tool": tool, "intent": intent, "target": "", "bot": bot, "origin": "", "exposure": ""}


def _covers(rules: tuple[policy_mod.Rule, ...], intent: str, tool: str = "", bot: str = "") -> bool:
    """Does any rule name this capability *for this bot*, whatever its other
    conditions?

    A conditional rule counts: an ask on `{intent = "run_command", exposure =
    "web"}` is an operator's decision about the shell, and the audit's job is
    to find capabilities nobody decided about, not to second-guess the
    condition they chose. The `bot` field is different in kind from those
    conditions — it selects WHO the rule governs, not when — so a rule scoped
    to another bot is no decision about this one and is not credited.
    """
    for rule in rules:
        try:
            if rule.bot and not fnmatch.fnmatch(bot.lower(), rule.bot.lower()):
                continue
            if rule.intent and fnmatch.fnmatch(intent, rule.intent.lower()):
                return True
            if tool and rule.tool and fnmatch.fnmatch(tool, rule.tool.lower()):
                return True
        except Exception:
            continue
    return False


def _allow_passes_writes(pol: policy_mod.Policy, bot: str) -> bool:
    """With a non-empty allow list, could a connector write tool get through?

    A connector write reaches the gate as `(tool=<vendor name>,
    intent="write_tool")`, and the audit cannot enumerate vendor tool names —
    so probing `evaluate` with an empty tool misses an allow rule like
    `allow = ["linear_*"]`, which the gate WOULD match. Any allow rule whose
    intent field is absent or matches `write_tool` (and whose bot field
    matches this bot) is counted as a pass path, failing toward reporting.
    """
    for rule in pol.allow:
        try:
            if rule.bot and not fnmatch.fnmatch(bot.lower(), rule.bot.lower()):
                continue
            if rule.intent and not fnmatch.fnmatch(INTENT_WRITE_TOOL, rule.intent.lower()):
                continue
            return True
        except Exception:
            continue
    return False


def _exposed_severity(exposed: bool) -> str:
    return SEV_CRITICAL if exposed else SEV_INFO


def _policy_findings(paths: HarnessPaths, bots: list[str], exposed: bool) -> list[Finding]:
    out: list[Finding] = []
    policy_file = policy_mod.policy_path(paths)

    if not policy_file.is_file():
        out.append(
            Finding(
                check_id="policy.absent",
                severity=SEV_CRITICAL if exposed else SEV_WARN,
                title="no policy.toml — the tool gate asks nothing",
                detail=(
                    f"{policy_file} does not exist, so every bot capability is allowed "
                    "without a person confirming anything"
                    + (
                        "; this harness is serving a non-loopback interface, so anyone who "
                        "reaches the API reaches those capabilities"
                        if exposed
                        else ""
                    )
                ),
                remediation="run `harness audit --fix` (or `harness serve` once) to seed "
                "the suggested ask-policy, then edit policy.toml to taste",
            )
        )
        # Everything the per-bot capability checks would say is already said:
        # an absent policy guards nothing, and repeating that per bot per
        # capability buries the one finding that matters.
        return out

    # A broken table is scoped, not total: `agent/policy.py` parses the global
    # table and each `[bots.<name>]` table separately, and a bot whose own
    # table parses is governed by it even while the global one refuses. So a
    # broken global table must not end the audit — a valid, unguarded per-bot
    # table would then be reported as fail-closed — and a broken per-bot table
    # gets its own finding rather than a silent skip.
    global_policy = policy_mod.load(paths)
    if global_policy.broken:
        out.append(
            Finding(
                check_id="policy.broken",
                severity=SEV_WARN,
                title="policy.toml global table could not be read",
                detail=f"{global_policy.broken} — a broken policy fails closed, so a bot "
                "without its own [bots.<name>] table cannot act; bots with a valid "
                "table of their own are still governed by it (audited below)",
                remediation="fix the file until `harness audit` reports no policy.broken",
            )
        )

    for bot in bots or [""]:
        pol = policy_mod.load(paths, bot)
        label = bot or "any bot"
        if pol.broken:
            # A bot broken for the same reason as the global table is the
            # whole-file case (or the fallback to that same global table);
            # repeating the global finding per bot says nothing new. A bot
            # whose OWN table is broken — including differently from a broken
            # global one — is a different fact and gets named.
            if pol.broken != global_policy.broken:
                out.append(
                    Finding(
                        check_id="policy.broken",
                        severity=SEV_WARN,
                        title=f"{label}: policy table could not be read",
                        detail=f"{pol.broken} — a broken policy fails closed, so "
                        f"{label} cannot act until its table parses",
                        remediation="fix the [bots.] table in policy.toml",
                    )
                )
            continue

        shell = pol.evaluate(_capability_ctx(INTENT_RUN, "run_command", bot))
        shell_guarded = shell.ask or _covers(pol.ask + pol.deny, INTENT_RUN, "run_command", bot)
        if shell.allowed and not shell_guarded:
            out.append(
                Finding(
                    check_id="policy.shell_no_ask",
                    severity=_exposed_severity(exposed),
                    title=f"{label}: run_command allowed with no ask or deny rule",
                    detail=(
                        f"the policy for {label} permits shell execution unconditionally"
                        + (
                            " and the API is network-exposed — an inbound chat is a shell"
                            if exposed
                            else " (loopback-only today; critical the day serve is exposed)"
                        )
                    ),
                    remediation='add an ask rule such as { intent = "run_command" } '
                    "(or a deny) to policy.toml",
                )
            )

        # The empty-tool probe alone misses an allow list naming vendor tools
        # by glob (`allow = ["linear_*"]`) — the gate would match those, so
        # they count as a pass path too.
        write = pol.evaluate(_capability_ctx(INTENT_WRITE_TOOL, "", bot))
        write_allowed = write.allowed or _allow_passes_writes(pol, bot)
        write_guarded = write.ask or _covers(pol.ask + pol.deny, INTENT_WRITE_TOOL, bot=bot)
        if write_allowed and not write_guarded:
            out.append(
                Finding(
                    check_id="policy.write_no_ask",
                    severity=_exposed_severity(exposed),
                    title=f"{label}: connector write tools allowed with no ask or deny rule",
                    detail=(
                        f"the policy for {label} lets writes into connected services "
                        "(write_tool) run unconfirmed"
                        + (" while the API is network-exposed" if exposed else "")
                    ),
                    remediation='add an ask rule such as { intent = "write_tool" } to policy.toml',
                )
            )

        # A policy that closes the write doors but leaves the shell open is
        # decorative: run_command reaches the filesystem (and, via curl, the
        # services) those denies meant to protect.
        denies_writes = _covers(pol.deny, INTENT_WRITE_TOOL, bot=bot) or _covers(
            pol.deny, INTENT_WRITE_STATE, bot=bot
        )
        if denies_writes and shell.allowed and not shell_guarded:
            out.append(
                Finding(
                    check_id="policy.decorative",
                    severity=SEV_WARN,
                    title=f"{label}: write tools are denied but the shell is open",
                    detail=f"the policy for {label} denies write capabilities yet allows "
                    "run_command without an ask — the shell reaches the filesystem "
                    "anyway, so the deny rules are decorative",
                    remediation="guard run_command with the same seriousness as the "
                    "writes it can reproduce",
                )
            )
    return out


# -- connectors -------------------------------------------------------------


def _connector_findings(paths: HarnessPaths) -> list[Finding]:
    from connectors.effects import EFFECT_WRITE, classify

    from .connectors import Connectors

    out: list[Finding] = []
    for record in Connectors(paths).list():
        if record.get("enabled_for") is not None:
            continue
        type_ = str(record.get("type") or "")
        writes = sorted(
            t for t in (record.get("tools") or []) if classify(str(t), type_) == EFFECT_WRITE
        )
        if not writes:
            continue
        name = str(record.get("name") or type_ or record.get("id") or "connector")
        out.append(
            Finding(
                check_id="connectors.all_bots_write",
                severity=SEV_WARN,
                title=f"{name}: write-capable connector granted to every bot",
                detail=f"connector {name!r} ({type_}) has enabled_for = null, so every "
                f"current and future bot can call {', '.join(writes)}",
                remediation="scope it: PATCH /api/connectors/<id> with an enabled_for "
                "list naming only the bots that need it",
            )
        )
    return out


# -- filesystem modes -------------------------------------------------------


def _mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.lstat().st_mode)
    except OSError:
        return None


def _fs_offenders(paths: HarnessPaths) -> tuple[list[Path], list[Path]]:
    """(files that should be 0600, dirs that should be 0700) with wider modes."""
    files: list[Path] = []
    dirs: list[Path] = []
    for d in (paths.home, paths.credentials):
        mode = _mode(d) if d.is_dir() else None
        if mode is not None and mode & 0o077:
            dirs.append(d)
    if paths.credentials.is_dir():
        for entry in sorted(paths.credentials.rglob("*")):
            mode = _mode(entry)
            if mode is None:
                continue
            if entry.is_dir():
                if mode & 0o077:
                    dirs.append(entry)
            elif mode & 0o177:
                files.append(entry)
    return files, dirs


def _fs_findings(paths: HarnessPaths) -> list[Finding]:
    files, dirs = _fs_offenders(paths)
    out: list[Finding] = []
    if files:
        listed = ", ".join(str(p) for p in files)
        out.append(
            Finding(
                check_id="fs.credentials_mode",
                severity=SEV_WARN,
                title="credential files are readable by other users",
                detail=f"not 0600: {listed}",
                remediation="chmod 600 each file, or run `harness audit --fix`",
            )
        )
    if dirs:
        listed = ", ".join(str(p) for p in dirs)
        out.append(
            Finding(
                check_id="fs.home_mode",
                severity=SEV_WARN,
                title="harness directories are open to other users",
                detail=f"not 0700: {listed}",
                remediation="chmod 700 each directory, or run `harness audit --fix`",
            )
        )
    return out


# -- suppressions -----------------------------------------------------------


def load_suppressions(paths: HarnessPaths) -> list[str]:
    """Check ids the operator chose to silence, from `audit.toml`.

    An unreadable config suppresses nothing: failing open here means MORE
    findings, which is the safe direction for an audit.
    """
    path = paths.home / AUDIT_CONFIG
    if not path.is_file():
        return []
    try:
        import tomllib

        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return []
    raw = data.get("suppress") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _apply_suppressions(findings: list[Finding], suppress: list[str]) -> list[Finding]:
    active = [s for s in suppress if s != SUPPRESSED_CHECK]
    kept = [f for f in findings if f.check_id not in active]
    for check_id in active:
        removed = [f for f in findings if f.check_id == check_id]
        if not removed:
            continue  # inactive suppression: nothing to report
        worst = min(removed, key=lambda f: _SEV_ORDER.index(f.severity)).severity
        kept.append(
            Finding(
                check_id=SUPPRESSED_CHECK,
                severity=SEV_INFO,
                title=f"{len(removed)} finding(s) for {check_id} suppressed",
                detail=f"{AUDIT_CONFIG} suppresses {check_id}; "
                f"highest suppressed severity: {worst}",
                remediation=f"remove {check_id!r} from `suppress` in {AUDIT_CONFIG} to see them",
            )
        )
    return kept


# -- --fix ------------------------------------------------------------------


def apply_fixes(paths: HarnessPaths) -> list[str]:
    """The narrow fixes: chmod credentials/home, seed the suggested policy.

    Driven off the filesystem state, not a findings list, so a suppressed
    finding is still fixed — suppression silences the report, not the repair.
    """
    fixed: list[str] = []
    files, dirs = _fs_offenders(paths)
    for d in dirs:
        try:
            d.chmod(0o700)
            fixed.append(f"chmod 700 {d}")
        except OSError:
            pass
    for f in files:
        try:
            f.chmod(0o600)
            fixed.append(f"chmod 600 {f}")
        except OSError:
            pass
    seeded = policy_mod.ensure_suggested_policy(paths)
    if seeded is not None:
        fixed.append(f"seeded suggested ask-policy at {seeded}")
    return fixed


# -- entry point ------------------------------------------------------------


def run(paths: HarnessPaths, bots: list[str], fix: bool = False) -> Report:
    """Audit one harness home. Fixes (when asked) run first, so the report
    describes the state the operator is left with, not the one --fix removed."""
    fixed = apply_fixes(paths) if fix else []
    exposed, host = serve_exposure(paths)

    findings: list[Finding] = []
    if exposed:
        findings.append(
            Finding(
                check_id="gateway.bind",
                severity=SEV_INFO,
                title=f"API served on a non-loopback interface ({host})",
                detail="serve.json records a network-exposed bind; every capability "
                "finding in this report is weighted by it",
                remediation="bind loopback (`harness serve --host 127.0.0.1`) unless "
                "remote clients need this harness",
            )
        )
    findings += _policy_findings(paths, bots, exposed)
    findings += _connector_findings(paths)
    findings += _fs_findings(paths)
    findings = _apply_suppressions(findings, load_suppressions(paths))

    return Report(
        home=str(paths.home), exposed=exposed, bind_host=host, findings=findings, fixed=fixed
    )


def render(report: Report) -> str:
    """Human-readable report, one block per finding, worst first."""
    where = f"network-exposed on {report.bind_host}" if report.exposed else "loopback only"
    lines = [f"audit of {report.home} ({where})"]
    for item in report.fixed:
        lines.append(f"  fixed: {item}")
    ordered = sorted(
        report.findings, key=lambda f: (_SEV_ORDER.index(f.severity), f.check_id, f.detail)
    )
    for f in ordered:
        lines.append(f"[{f.severity}] {f.check_id}: {f.title}")
        lines.append(f"    {f.detail}")
        lines.append(f"    fix: {f.remediation}")
    counts = report.summary()
    if not report.findings:
        lines.append("ok: no findings")
    lines.append(
        f"{counts[SEV_CRITICAL]} critical, {counts[SEV_WARN]} warn, {counts[SEV_INFO]} info"
    )
    return "\n".join(lines)


__all__ = [
    "AUDIT_CONFIG",
    "Finding",
    "Report",
    "SCHEMA_VERSION",
    "SEV_CRITICAL",
    "SEV_INFO",
    "SEV_WARN",
    "SUPPRESSED_CHECK",
    "apply_fixes",
    "load_suppressions",
    "render",
    "run",
    "serve_exposure",
]
