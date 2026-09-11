"""Problem reports (harness/reports.py): what a client saw plus the server's
context, filed over POST /api/reports, read back over GET /api/reports and
`harness reports`.

The contract under test:
- a submission is bounded, scrubbed and typed (`normalize`); no summary and
  no error is a 400 / ReportError;
- the stored record carries the server's context: version/boot, the server
  log tail, the bot's status/log/audit/queue/control, the request's stream;
- the store lists newest first, loads by id only for id-shaped names, deletes,
  prunes to HARNESS_REPORTS_KEEP;
- the routes sit behind the bearer (tests/test_trust_scope.py sweeps them).
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from agent.streaming import StreamWriter
from harness import audit, logstream, reports
from harness.cli import build_parser, cmd_reports
from harness.linking import get_or_create_key
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.redaction import register_secret
from harness.server import make_server

ROSTER = '[[bots]]\nname = "atlas"\nprovider = "echo"\n'


@pytest.fixture
def orch(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    o = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    o.init()
    return o


@pytest.fixture(scope="module")
def keyed_server(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("reports")
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    o = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    o.init()
    key = get_or_create_key(o.paths)
    httpd = make_server(o, "127.0.0.1", 0, key)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", key, o.paths
    finally:
        httpd.shutdown()
        o.down()


def _call(url: str, key: str, method: str = "GET", payload=None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Authorization": f"Bearer {key}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


# -- normalize --------------------------------------------------------------


def test_normalize_bounds_scrubs_and_types_a_submission():
    secret = "sk-live-" + "z" * 30
    sentinel = register_secret(secret, "PROVIDER_KEY")
    out = reports.normalize(
        {
            "kind": "user_report",
            "summary": "  bot answered with my key " + secret + "  ",
            "description": "d" * 30_000,
            "bot": "atlas",
            "room": "room-1/../x",
            "request_id": "rid 123",
            "message_id": "m1",
            "message_text": "t" * 5000,
            "message_ts": 12.5,
            "app": {"version": "1.2.3", "platform": "macos", "os": "macOS 15", "junk": 1},
            "client_log": [f"line {i}" for i in range(300)] + [{"not": "a string"}],
        }
    )
    assert out["kind"] == "user_report"
    assert secret not in out["summary"] and sentinel in out["summary"]
    assert len(out["description"]) == reports.MAX_DESCRIPTION
    assert out["bot"] == "atlas"
    assert out["room"] == "room-1..x"  # id-shaped: no slashes
    assert out["request_id"] == "rid123"
    assert len(out["message_text"]) == reports.MAX_MESSAGE_TEXT
    assert out["message_ts"] == 12.5
    assert out["app"] == {"version": "1.2.3", "platform": "macos", "os": "macOS 15"}
    assert len(out["client_log"]) == reports.MAX_CLIENT_LOG_LINES
    assert out["client_log"][-1] == '{"not": "a string"}'
    assert out["error"] is None


def test_normalize_derives_kind_and_summary_from_an_error():
    out = reports.normalize({"error": {"message": "stream broke", "where": "chat.error"}})
    assert out["kind"] == "app_error"
    assert out["summary"] == "stream broke"
    assert out["error"] == {"message": "stream broke", "where": "chat.error"}


@pytest.mark.parametrize("payload", [{}, {"summary": "   "}, [], "x", {"error": {}}])
def test_normalize_refuses_an_empty_submission(payload):
    with pytest.raises(reports.ReportError):
        reports.normalize(payload)


def test_normalize_drops_a_bot_name_that_is_not_a_path_component():
    assert reports.normalize({"summary": "x", "bot": "../etc"})["bot"] == ""
    assert reports.normalize({"summary": "x", "bot": 42})["bot"] == ""


# -- file_report + context -----------------------------------------------------


def test_file_report_attaches_the_servers_context(orch, capsys):
    paths = orch.paths
    server_log = logstream.server_log_file(paths)
    server_log.parent.mkdir(parents=True, exist_ok=True)
    server_log.write_text("boot\nready\n", encoding="utf-8")
    (paths.run / "atlas.log").write_text("[10:00:00] atlas up\n", encoding="utf-8")
    paths.streams.mkdir(parents=True, exist_ok=True)
    paths.stream_file("rid1").write_text('{"type":"status"}\n{"type":"final"}\n', encoding="utf-8")
    audit.record(
        paths,
        "atlas",
        event="decision",
        tool="run_command",
        intent="run_command",
        target="ls",
        decision=audit.DECISION_ALLOW,
        rule="",
        source="policy",
        now=100.0,
    )
    record = reports.file_report(
        orch,
        {
            "kind": "user_report",
            "summary": "wrong answer",
            "bot": "atlas",
            "request_id": "rid1",
            "message_text": "hello",
        },
        boot_id="boot-1",
        started_at=1000.0,
        now=2000.0,
    )
    assert reports.valid_id(record["id"]) and record["id"].startswith("rpt-19700101-003320-")
    assert record["ts"] == 2000.0 and record["kind"] == "user_report"
    ctx = record["context"]
    assert ctx["harness"]["boot_id"] == "boot-1" and ctx["harness"]["version"]
    assert ctx["harness"]["backend"] == "process" and ctx["harness"]["uptime_s"] > 0
    assert ctx["server_log"] == ["boot", "ready"]
    bot = ctx["bot"]
    assert bot["name"] == "atlas" and bot["in_roster"] is True and bot["provider"] == "echo"
    assert bot["status"] in ("stopped", "dead", "unknown", "running", "starting")
    assert bot["log"] == ["[10:00:00] atlas up"]
    assert bot["audit"][0]["tool"] == "run_command"
    assert bot["control"]["mode"] == "bot" and bot["busy"] is False and bot["queued"] == 0
    assert ctx["request"]["id"] == "rid1"
    assert ctx["request"]["stream"] == ['{"type":"status"}', '{"type":"final"}']
    # persisted as one file, and announced on the server's own log line
    stored = json.loads((paths.reports / f"{record['id']}.json").read_text(encoding="utf-8"))
    assert stored == record
    assert f"report {record['id']}: user_report bot=atlas" in capsys.readouterr().out


def test_file_report_without_a_bot_or_request_carries_only_the_harness_half(orch, capsys):
    record = reports.file_report(orch, {"error": {"message": "boom"}}, announce=False)
    assert record["kind"] == "app_error" and record["summary"] == "boom"
    assert "bot" not in record["context"] and "request" not in record["context"]
    assert capsys.readouterr().out == ""


def test_file_report_survives_a_bot_the_roster_does_not_know(orch):
    record = reports.file_report(orch, {"summary": "x", "bot": "ghost"}, announce=False)
    assert record["context"]["bot"]["in_roster"] is False
    assert record["context"]["bot"]["log"] == []


# -- the store ----------------------------------------------------------------


def test_list_load_delete_and_prune(orch, monkeypatch):
    paths = orch.paths
    ids = []
    for i in range(4):
        rec = reports.file_report(orch, {"summary": f"r{i}"}, now=1000.0 + i, announce=False)
        ids.append(rec["id"])
    rows = reports.list_reports(paths)
    assert [r["summary"] for r in rows] == ["r3", "r2", "r1", "r0"]  # newest first
    assert rows[0]["id"] == ids[3] and rows[0]["kind"] == "user_report"
    assert reports.list_reports(paths, limit=2)[1]["summary"] == "r2"
    assert reports.load_report(paths, ids[0])["summary"] == "r0"
    assert reports.load_report(paths, "nope") is None
    assert reports.load_report(paths, "../serve.json") is None
    assert reports.delete_report(paths, ids[0]) is True
    assert reports.delete_report(paths, ids[0]) is False
    assert reports.delete_report(paths, "../x") is False
    monkeypatch.setenv("HARNESS_REPORTS_KEEP", "2")
    reports.file_report(orch, {"summary": "r4"}, now=1010.0, announce=False)
    assert [r["summary"] for r in reports.list_reports(paths)] == ["r4", "r3"]


def test_store_ignores_files_that_are_not_reports(orch):
    paths = orch.paths
    paths.reports.mkdir(parents=True, exist_ok=True)
    (paths.reports / "notes.txt").write_text("hi", encoding="utf-8")
    (paths.reports / "rpt-20260907-120000-abcdef.json").write_text("{not json", encoding="utf-8")
    assert reports.list_reports(paths) == []
    assert reports.load_report(paths, "rpt-20260907-120000-abcdef") is None


# -- HTTP ---------------------------------------------------------------------


@pytest.mark.parametrize("request_fields", [{}, {"request_id": ""}])
def test_report_of_completed_message_recovers_request_stream(keyed_server, request_fields):
    base, key, paths = keyed_server
    rid = "0123456789abcdef0123456789abcdef"
    writer = StreamWriter(paths, rid)
    writer.final("Posted and verified.", frm="atlas")
    events = [json.loads(line) for line in paths.stream_file(rid).read_text().splitlines()]
    mid = next(event["message_id"] for event in events if event["type"] == "final")
    status, receipt = _call(
        f"{base}/api/reports",
        key,
        "POST",
        {
            "kind": "user_report",
            "summary": "Message reported",
            "bot": "atlas",
            "message_id": mid,
            "message_text": "Posted and verified.",
            **request_fields,
        },
    )
    assert status == 201
    status, record = _call(f"{base}/api/reports/{receipt['id']}", key)
    assert status == 200
    assert record["report"]["request_id"] == rid
    assert record["context"]["request"]["id"] == rid
    attached = [json.loads(line) for line in record["context"]["request"]["stream"]]
    assert any(event.get("message_id") == mid for event in attached)
    assert _call(f"{base}/api/reports/{receipt['id']}", key, "DELETE")[0] == 200


def test_explicit_report_request_id_takes_precedence_over_message_id():
    out = reports.normalize(
        {"summary": "Message reported", "request_id": "explicit", "message_id": "msg-other"}
    )
    assert out["request_id"] == "explicit"


@pytest.mark.parametrize(
    "mid",
    [None, 42, "", "legacy-hash", "msg-", "msg-../rid", "msg-r/id", "msg-r.id", "msg-" + "a" * 201],
)
def test_report_does_not_infer_request_from_unrecognized_message_id(mid):
    assert reports.normalize({"summary": "Message reported", "message_id": mid})["request_id"] == ""


def test_reports_round_trip_over_http(keyed_server):
    base, key, paths = keyed_server
    status, body = _call(
        f"{base}/api/reports",
        key,
        "POST",
        {
            "kind": "app_error",
            "error": {"message": "Couldn't save your message", "where": "chat.save"},
            "bot": "atlas",
            "app": {"version": "0.2.79", "platform": "ios"},
            "client_log": ["[10:00:00] chat.save: disk full"],
        },
    )
    assert status == 201 and body["kind"] == "app_error" and reports.valid_id(body["id"])
    status, listing = _call(f"{base}/api/reports", key)
    assert status == 200
    row = listing["reports"][0]
    assert row["id"] == body["id"] and row["where"] == "chat.save"
    assert row["app_version"] == "0.2.79" and row["platform"] == "ios" and row["bot"] == "atlas"
    status, record = _call(f"{base}/api/reports/{body['id']}", key)
    assert status == 200
    assert record["report"]["client_log"] == ["[10:00:00] chat.save: disk full"]
    assert record["context"]["harness"]["boot_id"]
    assert record["context"]["bot"]["name"] == "atlas"
    status, gone = _call(f"{base}/api/reports/{body['id']}", key, "DELETE")
    assert status == 200 and gone["deleted"] == body["id"]
    assert _call(f"{base}/api/reports/{body['id']}", key)[0] == 404
    assert _call(f"{base}/api/reports/{body['id']}", key, "DELETE")[0] == 404


def test_reports_reject_an_empty_submission_and_bad_ids(keyed_server):
    base, key, _paths = keyed_server
    assert _call(f"{base}/api/reports", key, "POST", {})[0] == 400
    assert _call(f"{base}/api/reports", key, "POST", {"summary": " "})[0] == 400
    assert _call(f"{base}/api/reports/..%2Fserve.json", key)[0] == 404
    assert _call(f"{base}/api/reports?limit=x", key)[0] == 400


def test_reports_need_the_key(keyed_server):
    base, _key, _paths = keyed_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{base}/api/reports", timeout=10)
    assert exc.value.code == 401


# -- CLI ----------------------------------------------------------------------


def _args(home, *argv):
    return build_parser().parse_args(["--home", str(home), "reports", *argv])


def test_cli_lists_shows_and_removes_reports(orch, capsys):
    paths = orch.paths
    assert cmd_reports(_args(paths.home)) == 0
    assert "(no reports in" in capsys.readouterr().out
    rec = reports.file_report(
        orch,
        {
            "summary": "the bot froze",
            "bot": "atlas",
            "app": {"version": "1.0", "platform": "macos"},
        },
        announce=False,
    )
    assert cmd_reports(_args(paths.home)) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith(rec["id"]) and "bot=atlas" in line and "app=1.0/macos" in line
    assert "the bot froze" in line
    assert cmd_reports(_args(paths.home, rec["id"])) == 0
    assert json.loads(capsys.readouterr().out)["id"] == rec["id"]
    assert cmd_reports(_args(paths.home, "rpt-20260907-120000-abcdef")) == 1
    assert "(no report" in capsys.readouterr().err
    assert cmd_reports(_args(paths.home, rec["id"], "--rm")) == 0
    assert "removed" in capsys.readouterr().out
    assert reports.load_report(paths, rec["id"]) is None


def test_reports_directory_is_part_of_the_layout(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    assert paths.reports.is_dir()
