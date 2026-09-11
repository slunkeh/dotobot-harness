"""`harness audit`: severity is a cross-product, not a checkbox.

The same open shell capability must read as noise on a loopback-only home and
as critical on one bound to the network, so most tests here build a fixture
home, flip exactly one factor of the cross-product, and assert the severity
moved. The other contracts under test: suppressions can silence a finding but
must self-report (a clean report can never hide a suppressed critical),
`--fix` touches only file modes and the seeded ask-policy, and the JSON shape
is stable because CI parses it.
"""

from __future__ import annotations

import json

from agent import policy as policy_mod
from harness import security_audit as sa
from harness.cli import main as cli_main
from harness.connectors import Connectors
from harness.paths import HarnessPaths

BOTS = ["atlas"]


def _paths(tmp_path) -> HarnessPaths:
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.home.mkdir(parents=True, exist_ok=True)
    # pytest homes inherit the umask; quiet the fs checks unless a test
    # deliberately widens a mode.
    paths.home.chmod(0o700)
    return paths


def _serve(paths: HarnessPaths, host: str = "0.0.0.0") -> None:
    (paths.home / "serve.json").write_text(
        json.dumps({"url": "http://127.0.0.1:8765", "host": host, "port": 8765}),
        encoding="utf-8",
    )


def _policy(paths: HarnessPaths, text: str) -> None:
    (paths.home / "policy.toml").write_text(text, encoding="utf-8")


def _ids(report: sa.Report) -> list[str]:
    return [f.check_id for f in report.findings]


def _one(report: sa.Report, check_id: str) -> sa.Finding:
    hits = [f for f in report.findings if f.check_id == check_id]
    assert len(hits) == 1, f"expected exactly one {check_id}, got {_ids(report)}"
    return hits[0]


# -- exposure × absent policy ----------------------------------------------


def test_absent_policy_on_exposed_serve_is_critical(tmp_path):
    paths = _paths(tmp_path)
    _serve(paths, "0.0.0.0")
    report = sa.run(paths, BOTS)
    assert report.exposed and report.bind_host == "0.0.0.0"
    assert _one(report, "policy.absent").severity == sa.SEV_CRITICAL
    assert _one(report, "gateway.bind").severity == sa.SEV_INFO


def test_absent_policy_on_loopback_is_only_a_warn(tmp_path):
    paths = _paths(tmp_path)
    _serve(paths, "127.0.0.1")
    report = sa.run(paths, BOTS)
    assert not report.exposed
    assert _one(report, "policy.absent").severity == sa.SEV_WARN
    assert "gateway.bind" not in _ids(report)


def test_no_serve_record_means_not_exposed(tmp_path):
    paths = _paths(tmp_path)
    report = sa.run(paths, BOTS)
    assert not report.exposed
    assert _one(report, "policy.absent").severity == sa.SEV_WARN


def test_absent_policy_is_the_one_policy_finding(tmp_path):
    # An absent policy guards nothing; repeating that per capability would
    # bury the finding that matters.
    paths = _paths(tmp_path)
    _serve(paths, "0.0.0.0")
    report = sa.run(paths, BOTS)
    assert "policy.shell_no_ask" not in _ids(report)
    assert "policy.write_no_ask" not in _ids(report)


# -- exposure × capability --------------------------------------------------


def test_unguarded_shell_and_writes_on_exposed_serve_are_critical(tmp_path):
    paths = _paths(tmp_path)
    _serve(paths, "0.0.0.0")
    _policy(paths, 'mode = "enforce"\n')  # a policy that decides nothing
    report = sa.run(paths, BOTS)
    assert _one(report, "policy.shell_no_ask").severity == sa.SEV_CRITICAL
    assert _one(report, "policy.write_no_ask").severity == sa.SEV_CRITICAL


def test_same_open_capabilities_without_exposure_are_info(tmp_path):
    paths = _paths(tmp_path)
    _policy(paths, 'mode = "enforce"\n')
    report = sa.run(paths, BOTS)
    assert _one(report, "policy.shell_no_ask").severity == sa.SEV_INFO
    assert _one(report, "policy.write_no_ask").severity == sa.SEV_INFO


def test_suggested_policy_counts_as_guarded_even_when_conditional(tmp_path):
    # The seeded policy asks for run_command only when exposure = "web"; a
    # conditional rule is still an operator's decision about the capability.
    paths = _paths(tmp_path)
    _serve(paths, "0.0.0.0")
    _policy(paths, policy_mod.SUGGESTED_POLICY)
    report = sa.run(paths, BOTS)
    for check in ("policy.absent", "policy.shell_no_ask", "policy.write_no_ask"):
        assert check not in _ids(report)


def test_denied_shell_raises_no_shell_finding(tmp_path):
    paths = _paths(tmp_path)
    _serve(paths, "0.0.0.0")
    _policy(paths, 'deny = ["run_command"]\n')
    report = sa.run(paths, BOTS)
    assert "policy.shell_no_ask" not in _ids(report)
    assert _one(report, "policy.write_no_ask").severity == sa.SEV_CRITICAL


def test_write_denies_with_an_open_shell_are_decorative(tmp_path):
    paths = _paths(tmp_path)
    _policy(paths, 'deny = [{ intent = "write_tool" }]\n')
    report = sa.run(paths, BOTS)
    finding = _one(report, "policy.decorative")
    assert finding.severity == sa.SEV_WARN
    assert "shell" in finding.detail
    assert "policy.write_no_ask" not in _ids(report)  # the deny guards writes


def test_a_rule_scoped_to_another_bot_guards_nothing_here(tmp_path):
    # The `bot` field selects who a rule governs, not when: an ask naming
    # atlas is no decision about zeus, whose shell stays unguarded at the
    # gate — the audit must not credit it.
    paths = _paths(tmp_path)
    _serve(paths, "0.0.0.0")
    _policy(paths, 'ask = [{ intent = "run_command", bot = "atlas" }]\n')
    report = sa.run(paths, ["atlas", "zeus"])
    shells = [f for f in report.findings if f.check_id == "policy.shell_no_ask"]
    assert [f.severity for f in shells] == [sa.SEV_CRITICAL]
    assert "zeus" in shells[0].title and "atlas" not in shells[0].title


def test_allow_list_naming_vendor_tools_is_a_write_path(tmp_path):
    # A connector write reaches the gate as (tool=<vendor name>,
    # intent="write_tool"), so `allow = ["linear_*"]` passes it unconfirmed —
    # the audit's empty-tool probe alone would read the allow list as a
    # refusal and stay silent.
    paths = _paths(tmp_path)
    _serve(paths, "0.0.0.0")
    _policy(paths, 'allow = ["linear_*"]\n')
    report = sa.run(paths, BOTS)
    assert _one(report, "policy.write_no_ask").severity == sa.SEV_CRITICAL


def test_allow_list_excluding_write_intents_is_no_write_path(tmp_path):
    paths = _paths(tmp_path)
    _serve(paths, "0.0.0.0")
    _policy(paths, 'allow = [{ intent = "read_tool" }]\n')
    report = sa.run(paths, BOTS)
    assert "policy.write_no_ask" not in _ids(report)


def test_per_bot_policy_is_audited_per_bot(tmp_path):
    paths = _paths(tmp_path)
    _serve(paths, "0.0.0.0")
    _policy(paths, policy_mod.SUGGESTED_POLICY + '\n[bots.loose]\nmode = "enforce"\n')
    report = sa.run(paths, ["tidy", "loose"])
    shell = _one(report, "policy.shell_no_ask")
    assert "loose" in shell.title and "tidy" not in shell.title


def test_broken_policy_reports_once_and_skips_capabilities(tmp_path):
    paths = _paths(tmp_path)
    _serve(paths, "0.0.0.0")
    _policy(paths, "not = [toml\n")
    report = sa.run(paths, BOTS)
    assert _one(report, "policy.broken").severity == sa.SEV_WARN
    assert "policy.shell_no_ask" not in _ids(report)
    assert "policy.absent" not in _ids(report)


def test_broken_global_table_still_audits_valid_per_bot_tables(tmp_path):
    # `agent/policy.py` parses each [bots.<name>] table separately, so a bot
    # with a valid table is governed by it even while the global table refuses.
    # An audit that stopped at policy.broken would report that live, unguarded
    # per-bot policy as fail-closed.
    paths = _paths(tmp_path)
    _serve(paths, "0.0.0.0")
    _policy(paths, 'mode = "bogus"\n\n[bots.atlas]\nmode = "enforce"\n')
    report = sa.run(paths, BOTS)
    assert _one(report, "policy.broken").severity == sa.SEV_WARN
    shell = _one(report, "policy.shell_no_ask")
    assert shell.severity == sa.SEV_CRITICAL and "atlas" in shell.title


def test_distinct_per_bot_breakage_is_named_even_under_a_broken_global(tmp_path):
    # Both tables broken, for different reasons: the bot's own error must not
    # be swallowed as a repeat of the global one.
    paths = _paths(tmp_path)
    _policy(paths, 'mode = "bogus"\n\n[bots.atlas]\nnot_a_field = "x"\n')
    report = sa.run(paths, BOTS)
    broken = [f for f in report.findings if f.check_id == "policy.broken"]
    assert len(broken) == 2
    assert any("atlas" in f.title for f in broken)


def test_broken_per_bot_table_gets_its_own_finding(tmp_path):
    paths = _paths(tmp_path)
    _serve(paths, "0.0.0.0")
    _policy(paths, policy_mod.SUGGESTED_POLICY + '\n[bots.atlas]\nmode = "bogus"\n')
    report = sa.run(paths, BOTS)
    broken = _one(report, "policy.broken")
    assert "atlas" in broken.title
    assert "policy.shell_no_ask" not in _ids(report)  # atlas fails closed


# -- connectors -------------------------------------------------------------


def test_write_connector_granted_to_every_bot_warns(tmp_path):
    paths = _paths(tmp_path)
    Connectors(paths).add("linear", "Linear")  # enabled_for defaults to null
    report = sa.run(paths, BOTS)
    finding = _one(report, "connectors.all_bots_write")
    assert finding.severity == sa.SEV_WARN
    assert "linear_create_issue" in finding.detail


def test_scoped_write_connector_is_quiet(tmp_path):
    paths = _paths(tmp_path)
    Connectors(paths).add("linear", "Linear", enabled_for=["atlas"])
    report = sa.run(paths, BOTS)
    assert "connectors.all_bots_write" not in _ids(report)


# -- filesystem modes -------------------------------------------------------


def test_wide_credential_and_home_modes_are_flagged(tmp_path):
    paths = _paths(tmp_path)
    paths.credentials.mkdir(parents=True)
    key = paths.credentials / "ANTHROPIC_API_KEY"
    key.write_text("sk-test", encoding="utf-8")
    key.chmod(0o644)
    paths.credentials.chmod(0o700)
    paths.home.chmod(0o755)
    report = sa.run(paths, BOTS)
    assert str(key) in _one(report, "fs.credentials_mode").detail
    assert str(paths.home) in _one(report, "fs.home_mode").detail


def test_fix_chmods_and_seeds_the_suggested_policy(tmp_path):
    paths = _paths(tmp_path)
    paths.credentials.mkdir(parents=True)
    key = paths.credentials / "ANTHROPIC_API_KEY"
    key.write_text("sk-test", encoding="utf-8")
    key.chmod(0o644)
    paths.credentials.chmod(0o755)

    report = sa.run(paths, BOTS, fix=True)

    assert key.stat().st_mode & 0o777 == 0o600
    assert paths.credentials.stat().st_mode & 0o777 == 0o700
    policy_file = paths.home / "policy.toml"
    assert policy_file.read_text(encoding="utf-8") == policy_mod.SUGGESTED_POLICY
    assert any("chmod 600" in item for item in report.fixed)
    assert any("seeded" in item for item in report.fixed)
    # the report describes the post-fix state
    for check in ("fs.credentials_mode", "fs.home_mode", "policy.absent"):
        assert check not in _ids(report)


def test_fix_scope_is_narrow(tmp_path):
    """--fix never rebinds serve, rescopes connectors, or edits an existing
    policy — those stay advisory findings."""
    paths = _paths(tmp_path)
    _serve(paths, "0.0.0.0")
    _policy(paths, 'mode = "enforce"\n')
    Connectors(paths).add("linear", "Linear")
    serve_before = (paths.home / "serve.json").read_text(encoding="utf-8")

    report = sa.run(paths, BOTS, fix=True)

    assert (paths.home / "serve.json").read_text(encoding="utf-8") == serve_before
    assert (paths.home / "policy.toml").read_text(encoding="utf-8") == 'mode = "enforce"\n'
    assert Connectors(paths).list()[0]["enabled_for"] is None
    assert "connectors.all_bots_write" in _ids(report)
    assert _one(report, "policy.shell_no_ask").severity == sa.SEV_CRITICAL


# -- suppressions -----------------------------------------------------------


def test_active_suppression_silences_but_self_reports(tmp_path):
    paths = _paths(tmp_path)
    _serve(paths, "0.0.0.0")
    (paths.home / sa.AUDIT_CONFIG).write_text('suppress = ["policy.absent"]\n', encoding="utf-8")
    report = sa.run(paths, BOTS)
    assert "policy.absent" not in _ids(report)
    trail = _one(report, sa.SUPPRESSED_CHECK)
    assert trail.severity == sa.SEV_INFO
    assert "policy.absent" in trail.title
    assert sa.SEV_CRITICAL in trail.detail  # the hidden severity is named
    assert report.summary()[sa.SEV_CRITICAL] == 0


def test_inactive_suppression_reports_nothing(tmp_path):
    paths = _paths(tmp_path)
    (paths.home / sa.AUDIT_CONFIG).write_text(
        'suppress = ["fs.credentials_mode"]\n', encoding="utf-8"
    )
    report = sa.run(paths, BOTS)
    assert sa.SUPPRESSED_CHECK not in _ids(report)


def test_the_suppression_trail_cannot_be_suppressed(tmp_path):
    paths = _paths(tmp_path)
    _serve(paths, "0.0.0.0")
    (paths.home / sa.AUDIT_CONFIG).write_text(
        'suppress = ["policy.absent", "audit.suppressed"]\n', encoding="utf-8"
    )
    report = sa.run(paths, BOTS)
    assert sa.SUPPRESSED_CHECK in _ids(report)


def test_unreadable_audit_config_suppresses_nothing(tmp_path):
    paths = _paths(tmp_path)
    _serve(paths, "0.0.0.0")
    (paths.home / sa.AUDIT_CONFIG).write_text("suppress = [broken\n", encoding="utf-8")
    report = sa.run(paths, BOTS)
    assert "policy.absent" in _ids(report)


# -- JSON schema stability --------------------------------------------------


def test_json_shape_is_stable_for_ci(tmp_path):
    paths = _paths(tmp_path)
    _serve(paths, "0.0.0.0")
    payload = sa.run(paths, BOTS).to_dict()
    assert set(payload) == {
        "version",
        "home",
        "exposed",
        "bind_host",
        "summary",
        "findings",
        "fixed",
    }
    assert payload["version"] == sa.SCHEMA_VERSION == 1
    assert set(payload["summary"]) == {sa.SEV_CRITICAL, sa.SEV_WARN, sa.SEV_INFO}
    assert payload["findings"], "the fixture home must produce findings"
    for finding in payload["findings"]:
        assert set(finding) == {"check_id", "severity", "title", "detail", "remediation"}
    # worst first, so a CI log's first line is the one to read
    severities = [f["severity"] for f in payload["findings"]]
    order = [sa.SEV_CRITICAL, sa.SEV_WARN, sa.SEV_INFO]
    assert severities == sorted(severities, key=order.index)


# -- the CLI ----------------------------------------------------------------


def _roster(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(
        '[[bots]]\nname = "atlas"\nrole = "helper"\nprovider = "echo"\n', encoding="utf-8"
    )
    return rp


def test_cli_audit_json_exits_1_on_criticals(tmp_path, capsys):
    paths = _paths(tmp_path)
    _serve(paths, "0.0.0.0")
    rc = cli_main(
        ["--home", str(paths.home), "--roster", str(_roster(tmp_path)), "audit", "--json"]
    )
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"][sa.SEV_CRITICAL] >= 1
    assert any(f["check_id"] == "policy.absent" for f in payload["findings"])


def test_cli_audit_exits_0_when_no_criticals(tmp_path, capsys):
    paths = _paths(tmp_path)
    _serve(paths, "127.0.0.1")
    _policy(paths, policy_mod.SUGGESTED_POLICY)
    rc = cli_main(["--home", str(paths.home), "--roster", str(_roster(tmp_path)), "audit"])
    assert rc == 0
    assert "critical" in capsys.readouterr().out


def test_cli_audit_fix_repairs_and_goes_green(tmp_path, capsys):
    paths = _paths(tmp_path)
    _serve(paths, "0.0.0.0")
    paths.credentials.mkdir(parents=True)
    key = paths.credentials / "LINEAR_API_KEY"
    key.write_text("lin_api_test", encoding="utf-8")
    key.chmod(0o644)
    rc = cli_main(["--home", str(paths.home), "--roster", str(_roster(tmp_path)), "audit", "--fix"])
    out = capsys.readouterr().out
    assert rc == 0  # the seeded policy guards the criticals away
    assert key.stat().st_mode & 0o777 == 0o600
    assert (paths.home / "policy.toml").is_file()
    assert "fixed:" in out
