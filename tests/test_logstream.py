"""Per-server log stream (harness/logstream.py): the server's own output
captured to run/server/serve.log, and every log tailed the same way over HTTP, SSE,
the WebSocket and the CLI.

The contract under test:
- `tail` returns the last N *complete* lines plus the byte offset after them;
  a partial trailing line waits for its newline.
- `read_from(offset)` returns only what landed since; a file that shrank
  below the offset (truncation / rotation) restarts from 0 and says `reset`.
- `LogFollower` reopens a replaced file at 0; `follow` ticks heartbeats.
- Capture tees stdout/stderr to the file with a timestamp, scrubbed, rotated
  at the cap, and never raises.
- Source ids resolve inside run/ only; the API refuses bots not in the roster.
- HTTP / SSE / WS frames: snapshot, appended, heartbeat, stopped.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from argparse import Namespace
from contextlib import contextmanager

import pytest

from harness import logstream
from harness.cli import build_parser, cmd_logs
from harness.linking import get_or_create_key, link_code, pairing_banner
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.redaction import register_secret
from harness.server import EpochSequencer, make_server
from tests.test_ws import WSClient, _recv_type

ROSTER = (
    '[[bots]]\nname = "atlas"\nprovider = "echo"\n'
    # names the roster allows that a stricter id rule would have lost
    '[[bots]]\nname = "_scratch"\nprovider = "echo"\n'
    '[[bots]]\nname = "my bot"\nprovider = "echo"\n'
)
STAMP = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ")


@pytest.fixture
def paths(tmp_path) -> HarnessPaths:
    p = HarnessPaths.resolve(tmp_path / "home")
    p.run.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def fast_poll(monkeypatch):
    monkeypatch.setenv("HARNESS_LOG_POLL_INTERVAL", "0.02")


@pytest.fixture(scope="module")
def keyed_server(tmp_path_factory):
    """A keyed server over an initialized home; no bot processes needed.

    Module-scoped (one shutdown, not one per test): every test rewrites the
    log files it reads, so nothing leaks between them.
    """
    tmp_path = tmp_path_factory.mktemp("logstream")
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    key = get_or_create_key(orch.paths)
    httpd = make_server(orch, "127.0.0.1", 0, key)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    previous = os.environ.get("HARNESS_LOG_POLL_INTERVAL")
    os.environ["HARNESS_LOG_POLL_INTERVAL"] = "0.02"
    try:
        yield f"http://127.0.0.1:{port}", key, orch.paths
    finally:
        if previous is None:
            os.environ.pop("HARNESS_LOG_POLL_INTERVAL", None)
        else:
            os.environ["HARNESS_LOG_POLL_INTERVAL"] = previous
        httpd.shutdown()
        orch.down()


def _get(url: str, key: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def _serve_log(paths):
    """run/server/serve.log, its directory created (serve creates it on install)."""
    path = logstream.server_log_file(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append(path, text: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)


def _wait(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


# -- tail / read_from ------------------------------------------------------


def test_tail_returns_last_lines_and_a_offset_at_the_end(tmp_path):
    log = tmp_path / "x.log"
    log.write_bytes(b"one\ntwo\nthree\nfour\nfive\n")
    page = logstream.tail(log, lines=2)
    assert page.lines == ["four", "five"]
    assert page.offset == page.size == log.stat().st_size
    assert page.truncated is True  # older lines exist
    whole = logstream.tail(log, lines=10)
    assert whole.lines == ["one", "two", "three", "four", "five"]
    assert whole.truncated is False


def test_tail_holds_back_a_partial_trailing_line_until_its_newline_lands(tmp_path):
    log = tmp_path / "x.log"
    log.write_bytes(b"a\nb\npart")
    page = logstream.tail(log, lines=10)
    assert page.lines == ["a", "b"]
    assert page.offset == len(b"a\nb\n")
    _append(log, "ial\n")
    nxt = logstream.read_from(log, page.offset)
    assert nxt.lines == ["partial"]
    assert nxt.offset == log.stat().st_size


def test_tail_of_a_missing_file_is_empty_at_offset_zero(tmp_path):
    page = logstream.tail(tmp_path / "nope.log")
    assert page.lines == [] and page.offset == 0 and page.size == 0
    assert page.truncated is False and page.reset is False
    assert page.exists is False  # the page agrees with the listing
    polled = logstream.read_from(tmp_path / "nope.log", 3)
    assert polled.exists is False and polled.reset is True  # gone, not merely empty


def test_tail_never_returns_a_line_cut_by_its_byte_budget(tmp_path):
    log = tmp_path / "x.log"
    written = [f"line-{i:04d}-" + "x" * 40 for i in range(400)]
    log.write_text("\n".join(written) + "\n", encoding="utf-8")
    page = logstream.tail(log, lines=1000, max_bytes=2000)
    assert page.truncated is True
    assert page.lines, "some complete lines fit the budget"
    assert all(line in written for line in page.lines)
    assert page.lines == written[-len(page.lines) :]


def test_tail_shows_a_line_longer_than_its_budget_cut_instead_of_dropping_it(tmp_path):
    log = tmp_path / "x.log"
    log.write_bytes(b"short\n" + b"y" * 300)  # an unfinished giant last line
    page = logstream.tail(log, lines=5, max_bytes=100)
    assert page.lines == ["y" * 100] and page.truncated is True
    assert page.offset == page.size  # a follower continues from the end
    _append(log, "!\n")
    assert logstream.read_from(log, page.offset).lines == ["!"]  # the remainder lands
    log.write_bytes(b"short\n" + b"z" * 300 + b"\n")  # a finished giant last line
    done = logstream.tail(log, lines=5, max_bytes=100)
    assert done.lines == ["z" * 99] and done.truncated is True
    assert done.offset == done.size


def test_tail_zero_lines_gives_only_the_offset(tmp_path):
    log = tmp_path / "x.log"
    log.write_bytes(b"a\nb\n")
    page = logstream.tail(log, lines=0)
    assert page.lines == [] and page.offset == 4


def test_lines_tolerate_crlf_and_bad_utf8(tmp_path):
    log = tmp_path / "x.log"
    log.write_bytes(b"one\r\ntw\xffo\r\n")
    assert logstream.tail(log).lines == ["one", "tw�o"]


def test_read_from_returns_only_new_complete_lines(tmp_path):
    log = tmp_path / "x.log"
    log.write_bytes(b"a\nb\n")
    offset = logstream.tail(log).offset
    assert logstream.read_from(log, offset).lines == []
    _append(log, "c\nd")
    page = logstream.read_from(log, offset)
    assert page.lines == ["c"]
    assert page.offset == len(b"a\nb\nc\n")
    assert logstream.read_from(log, page.offset).lines == []  # "d" still partial


def test_read_from_restarts_when_the_file_shrank_below_the_offset(tmp_path):
    log = tmp_path / "x.log"
    log.write_bytes(b"old line one\nold line two\n")
    offset = logstream.tail(log).offset
    log.write_bytes(b"new\n")  # truncated + rewritten shorter
    page = logstream.read_from(log, offset)
    assert page.reset is True
    assert page.lines == ["new"]
    assert page.offset == 4


def test_read_from_pages_a_large_backlog_with_truncated(tmp_path):
    log = tmp_path / "x.log"
    written = [f"row {i}" for i in range(300)]
    log.write_text("\n".join(written) + "\n", encoding="utf-8")
    got, offset, rounds = [], 0, 0
    while True:
        page = logstream.read_from(log, offset, max_bytes=500)
        got.extend(page.lines)
        offset = page.offset
        rounds += 1
        if not page.truncated:
            break
    assert got == written
    assert rounds > 1


def test_read_from_keeps_a_multibyte_char_split_across_polls_intact(tmp_path):
    log = tmp_path / "x.log"
    log.write_bytes(b"h\xc3")  # first half of "é"
    page = logstream.read_from(log, 0)
    assert page.lines == [] and page.offset == 0
    with log.open("ab") as fh:
        fh.write(b"\xa9llo\n")  # the second half lands later
    assert logstream.read_from(log, page.offset).lines == ["héllo"]


def test_read_from_with_a_gen_detects_a_rotated_and_regrown_file(tmp_path):
    log = tmp_path / "x.log"
    log.write_bytes(b"abc\n")
    page = logstream.tail(log)
    assert page.gen == log.stat().st_ino
    os.replace(log, log.with_name("x.log.1"))
    log.write_bytes(b"1\n2\n3\n4\n5\n")  # regrown past the old offset (4)
    stale = logstream.read_from(log, page.offset)  # no gen: cannot tell
    assert stale.reset is False and stale.lines == ["3", "4", "5"]
    fixed = logstream.read_from(log, page.offset, gen=page.gen)
    assert fixed.reset is True and fixed.lines == ["1", "2", "3", "4", "5"]
    assert fixed.gen == log.stat().st_ino


def test_read_from_emits_a_line_longer_than_the_window_instead_of_stalling(tmp_path):
    log = tmp_path / "x.log"
    log.write_bytes(b"x" * 40)  # no newline anywhere
    page = logstream.read_from(log, 0, max_bytes=16)
    assert page.lines == ["x" * 16] and page.offset == 16 and page.truncated is True
    page = logstream.read_from(log, page.offset, max_bytes=16)
    assert page.lines == ["x" * 16] and page.offset == 32
    page = logstream.read_from(log, page.offset, max_bytes=16)
    assert page.lines == [] and page.offset == 32  # the last 8 bytes wait for a newline
    _append(log, "\n")
    assert logstream.read_from(log, page.offset, max_bytes=16).lines == ["x" * 8]


def test_reads_refuse_a_symlink_and_a_fifo(tmp_path):
    secret = tmp_path / "GITHUB_TOKEN"
    secret.write_text("ghp_plaintext\n", encoding="utf-8")
    link = tmp_path / "linked.log"
    link.symlink_to(secret)
    assert logstream.tail(link).lines == []
    assert logstream.read_from(link, 0).lines == []
    fifo = tmp_path / "fifo.log"
    os.mkfifo(fifo)
    assert logstream.tail(fifo).lines == []  # and does not block
    assert logstream.LogFollower(fifo).poll() is None


def test_read_from_a_vanished_file_resets(tmp_path):
    page = logstream.read_from(tmp_path / "gone.log", 42)
    assert page.reset is True and page.lines == [] and page.offset == 0


def test_read_scrubs_secrets_the_reader_knows(tmp_path):
    log = tmp_path / "x.log"
    secret = "sk-test-" + "q" * 24
    sentinel = register_secret(secret, "TEST_KEY")
    log.write_text(f"provider said {secret} ok\n", encoding="utf-8")
    line = logstream.tail(log).lines[0]
    assert secret not in line
    assert sentinel in line


# -- follower / follow -----------------------------------------------------


def test_follower_reopens_from_zero_after_rotation(tmp_path):
    log = tmp_path / "x.log"
    log.write_bytes(b"first\nsecond\n")
    follower = logstream.LogFollower(log, log.stat().st_size)
    assert follower.poll() is None
    os.replace(log, log.with_name("x.log.1"))
    assert follower.poll() is None  # the gap between rename and re-create
    log.write_bytes(b"fresh\n")
    page = follower.poll()
    assert page is not None and page.reset is True
    assert page.lines == ["fresh"] and follower.offset == 6


def test_follower_drains_the_old_generation_before_switching(tmp_path):
    log = tmp_path / "x.log"
    log.write_bytes(b"first\n")
    follower = logstream.LogFollower(log, log.stat().st_size)
    _append(log, "late one\nlate two\n")  # lands before the rotation, after the last poll
    os.replace(log, log.with_name("x.log.1"))
    log.write_bytes(b"fresh\n")
    drained = follower.poll()
    assert drained is not None and drained.lines == ["late one", "late two"]
    assert drained.reset is False
    fresh = follower.poll()
    assert fresh is not None and fresh.reset is True and fresh.lines == ["fresh"]
    assert follower.poll() is None
    follower.close()


def test_follower_resets_when_the_file_is_not_the_one_the_offset_came_from(tmp_path):
    log = tmp_path / "x.log"
    log.write_bytes(b"old old old old\n")
    page = logstream.tail(log)
    os.replace(log, log.with_name("x.log.1"))
    log.write_bytes(b"new generation, already longer than the offset\n")
    follower = logstream.LogFollower(log, page.offset, gen=page.gen)
    first = follower.poll()
    assert first is not None and first.reset is True
    assert first.lines == ["new generation, already longer than the offset"]
    follower.close()


def test_follower_announces_a_vanished_log_once_and_recovers(tmp_path):
    log = tmp_path / "x.log"
    log.write_bytes(b"alive\n")
    follower = logstream.LogFollower(log, log.stat().st_size)
    log.unlink()  # the bot was deleted, or the file purged by hand
    assert follower.poll() is None  # one miss could be a rotation's rename gap
    gone = follower.poll()
    assert gone is not None and gone.exists is False
    assert gone.lines == [] and gone.reset is True and gone.offset == 0
    assert follower.poll() is None  # said once, not on every tick
    log.write_bytes(b"back\n")
    back = follower.poll()
    assert back is not None and back.exists is True and back.reset is True
    assert back.lines == ["back"]
    follower.close()


def test_follower_started_after_the_log_vanished_reads_its_return_from_the_start(tmp_path):
    # A client reconnects with the offset (and gen) it had before the file
    # went away; the same file then comes back longer (moved aside and back,
    # so the inode — the gen — is unchanged). The recovery page is a snapshot,
    # and a snapshot's lines start at byte 0, not at the old cursor.
    log = tmp_path / "x.log"
    log.write_bytes(b"old 1\nold 2\n")
    page = logstream.tail(log)
    aside = tmp_path / "x.log.aside"
    os.replace(log, aside)
    follower = logstream.LogFollower(log, page.offset, gen=page.gen)
    gone = follower.poll()
    if gone is None:  # the constructor's own miss may already be the first
        gone = follower.poll()
    assert gone is not None and gone.exists is False
    assert follower.offset == 0  # a heartbeat now says: nothing in view
    with aside.open("ab") as fh:
        fh.write(b"new 3\n")
    os.replace(aside, log)
    back = follower.poll()
    assert back is not None and back.reset is True
    assert back.lines == ["old 1", "old 2", "new 3"]
    follower.close()


def test_follower_treats_gen_zero_as_an_identity_no_file_has(tmp_path):
    # `follow` heartbeats carry gen 0 while the file is gone; a client that
    # resumes from one must be told the file it now sees is a different one,
    # or it follows from its old offset and drops the new file's prefix.
    log = tmp_path / "x.log"
    log.write_bytes(b"line 1\nline 2\n")
    follower = logstream.LogFollower(log, 7, gen=0)
    page = follower.poll()
    assert page is not None and page.reset is True
    assert page.lines == ["line 1", "line 2"]
    follower.close()


def test_stream_pages_sends_a_cleared_page_when_the_log_goes_away(tmp_path):
    log = tmp_path / "x.log"
    log.write_bytes(b"alive\n")
    seen = []

    def write(page, mutation):
        seen.append((mutation, page.exists, list(page.lines)))
        if mutation == "snapshot":
            log.unlink()
            return True
        return False

    logstream.stream_pages(log, lines=5, stop=threading.Event(), write=write, interval=0.01)
    assert seen == [("snapshot", True, ["alive"]), ("cleared", False, [])]


def test_tail_tops_up_from_the_rotated_generation(tmp_path):
    live = tmp_path / "serve.log"
    old = tmp_path / "serve.log.1"
    old.write_bytes(b"old 1\nold 2\nold 3\n")
    live.write_bytes(b"new 1\n")
    assert logstream.previous_generation(live) == old
    page = logstream.tail(live, lines=3, previous=logstream.previous_generation(live))
    assert page.lines == ["old 2", "old 3", "new 1"]
    assert page.offset == 6 and page.gen == live.stat().st_ino  # the live file's
    assert page.truncated is True  # an older line exists beyond the three
    enough = logstream.tail(live, lines=1, previous=old)
    assert enough.lines == ["new 1"]  # the live file satisfied the request alone
    assert logstream.previous_generation(tmp_path / "atlas.log") is None


def test_follower_from_an_empty_view_does_not_reset_when_the_file_is_born(tmp_path):
    # `harness logs -f` (and a stream) on a log that does not exist yet
    # follows from the tail's {offset: 0, gen: 0}: the file's birth is plain
    # new lines onto an empty view, not a restart.
    log = tmp_path / "later.log"
    follower = logstream.LogFollower(log, 0, gen=0)
    assert follower.poll() is None
    log.write_bytes(b"born\n")
    page = follower.poll()
    assert page is not None and page.lines == ["born"] and page.reset is False
    follower.close()


def test_follower_waits_for_a_file_that_does_not_exist_yet(tmp_path):
    log = tmp_path / "later.log"
    follower = logstream.LogFollower(log, 0)
    assert follower.poll() is None
    log.write_bytes(b"born\n")
    page = follower.poll()
    assert page is not None and page.lines == ["born"] and page.reset is False


def test_follow_ticks_heartbeats_when_quiet_and_ends_on_stop(tmp_path):
    log = tmp_path / "x.log"
    log.write_bytes(b"start\n")
    stop = threading.Event()
    pages = []

    def run():
        for page in logstream.follow(log, 6, stop, interval=0.01, heartbeat=0.05):
            pages.append(page)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert _wait(lambda: any(logstream.is_heartbeat(p) for p in pages))
    _append(log, "more\n")
    assert _wait(lambda: any(p.lines == ["more"] for p in pages))
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()
    beat = next(p for p in pages if logstream.is_heartbeat(p))
    assert beat.offset == 6 and beat.lines == []


def test_stream_pages_snapshot_then_appended_then_stops_when_the_client_is_gone(tmp_path):
    log = tmp_path / "x.log"
    log.write_bytes(b"a\nb\nc\n")
    seen = []
    stop = threading.Event()

    def write(page, mutation):
        seen.append((mutation, list(page.lines)))
        if mutation == "snapshot":
            _append(log, "d\n")
            return True
        return False  # client went away

    logstream.stream_pages(log, lines=2, stop=stop, write=write, interval=0.01)
    assert seen == [("snapshot", ["b", "c"]), ("appended", ["d"])]


# -- capture ----------------------------------------------------------------


@contextmanager
def fake_streams():
    """Swap sys.stdout/stderr for StringIOs for the body of a test.

    Done inside the test (not a fixture): pytest re-installs its own capture
    streams when the call phase starts, which would sit on top of a fixture's
    swap and take the tee instead.
    """
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        yield out, err
    finally:
        logstream.uninstall_server_log()
        sys.stdout, sys.stderr = old_out, old_err


def test_capture_tees_stdout_and_stderr_into_serve_log(paths):
    with fake_streams() as (out, err):
        sink = logstream.install_server_log(paths)
        print("hello world")
        print("something broke", file=sys.stderr)
        assert out.getvalue() == "hello world\n"  # the original stream still gets it
        assert err.getvalue() == "something broke\n"
    lines = sink.path.read_text(encoding="utf-8").splitlines()
    assert sink.path == paths.run / "server" / "serve.log"
    assert len(lines) == 2
    assert STAMP.match(lines[0]) and lines[0].endswith("] hello world")
    assert STAMP.match(lines[1]) and lines[1].endswith("] [stderr] something broke")


def test_capture_is_idempotent_and_proxies_the_wrapped_stream(paths):
    with fake_streams() as (out, _err):
        first = logstream.install_server_log(paths)
        second = logstream.install_server_log(paths)
        assert first is second is logstream.installed()
        assert sys.stdout.isatty() is False
        assert sys.stdout.encoding == out.encoding  # arbitrary attribute proxied
        with pytest.raises(io.UnsupportedOperation):
            sys.stdout.fileno()
        logstream.uninstall_server_log()
        assert sys.stdout is out and logstream.installed() is None


def test_capture_buffers_partial_writes_until_the_newline(paths):
    with fake_streams():
        sink = logstream.install_server_log(paths)
        sys.stdout.write("ab")
        sys.stdout.write("c")
        assert not sink.path.exists() or sink.path.read_text(encoding="utf-8") == ""
        sys.stdout.write("\nsecond\n")
        lines = [STAMP.sub("", ln) for ln in sink.path.read_text(encoding="utf-8").splitlines()]
        assert lines == ["abc", "second"]


def test_capture_scrubs_registered_secrets_and_the_link_code(paths):
    with fake_streams():
        sink = logstream.install_server_log(paths)
        key = "k" * 32
        sentinel = register_secret(key, "LINK_KEY")
        code = link_code("http://10.0.0.5:8765", key)
        print(f"paste this: {code} (key {key})")
    body = sink.path.read_text(encoding="utf-8")
    assert code not in body and key not in body
    assert sentinel in body


def test_pairing_banner_seals_a_bearer_nobody_registered(paths):
    # $HARNESS_TOKEN skips get_or_create_key (the one registration point):
    # printing the banner must still leave nothing of it in serve.log.
    token = "override-" + "t" * 30
    with fake_streams():
        sink = logstream.install_server_log(paths)
        print(pairing_banner("http://10.0.0.5:8765", token))
    body = sink.path.read_text(encoding="utf-8")
    assert token not in body
    assert link_code("http://10.0.0.5:8765", token) not in body
    assert "Linking key" in body  # the banner itself was logged, sealed


def test_capture_rotates_once_past_the_cap_without_losing_a_line(paths):
    # ~71 bytes per stamped line: the cap trips after the fifth, and the
    # three that follow start the fresh file. One generation is kept, so
    # nothing written under twice the cap is lost.
    with fake_streams():
        sink = logstream.install_server_log(paths, max_bytes=300)
        for i in range(8):
            print(f"line {i:02d} " + "x" * 40)
    rotated = sink.path.with_name("serve.log.1")
    assert rotated.exists()
    kept = rotated.read_text(encoding="utf-8") + sink.path.read_text(encoding="utf-8")
    assert [STAMP.sub("", ln)[:7] for ln in kept.splitlines()] == [
        f"line {i:02d}" for i in range(8)
    ]
    assert sink.path.stat().st_size < 300


def test_capture_keeps_only_one_rotated_generation(paths):
    with fake_streams():
        sink = logstream.install_server_log(paths, max_bytes=300)
        for i in range(20):
            print(f"line {i:02d} " + "x" * 40)
    names = sorted(p.name for p in sink.path.parent.iterdir())
    assert names == ["serve.log", "serve.log.1"]  # serve.log exists even right after a rotation
    kept = (
        sink.path.with_name("serve.log.1").read_text(encoding="utf-8")
        + sink.path.read_text(encoding="utf-8")
    ).splitlines()
    assert STAMP.sub("", kept[-1]).startswith("line 19")
    assert len(kept) <= 10  # at most two generations' worth survive


def test_capture_never_raises_when_the_file_cannot_be_written(tmp_path):
    home = HarnessPaths.resolve(tmp_path / "home")
    home.home.mkdir(parents=True)
    (home.home / "run").write_text("a file where the directory should be", encoding="utf-8")
    with fake_streams() as (out, err):
        sink = logstream.install_server_log(home)
        print("still printed")
        print("and again")
        assert out.getvalue() == "still printed\nand again\n"
        # the gap is reported: once on the wrapped stderr (the journal) ...
        assert err.getvalue().count("warning: server log") == 1
        assert "not being written" in err.getvalue()
        # ... and on the server entry of the index
        assert logstream.list_sources(home, [])[0]["dropped"] == sink.dropped >= 2


def test_capture_flushes_a_partial_line_on_uninstall(paths):
    with fake_streams():
        sink = logstream.install_server_log(paths)
        sys.stdout.write("no newline yet")
    lines = [STAMP.sub("", ln) for ln in sink.path.read_text(encoding="utf-8").splitlines()]
    assert lines == ["no newline yet"]


def test_capture_keeps_the_stderr_tag_on_a_partial_line_flushed_at_shutdown(paths):
    with fake_streams():
        sink = logstream.install_server_log(paths)
        sys.stderr.write("whole stderr line\n")
        sys.stderr.write("half a stderr line")
    lines = [STAMP.sub("", ln) for ln in sink.path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2 and "stderr" in lines[0]
    assert lines[1] == lines[0].replace("whole stderr line", "half a stderr line")


# -- sources ----------------------------------------------------------------


def test_page_dict_carries_exists(tmp_path):
    log = tmp_path / "x.log"
    log.write_bytes(b"a\n")
    assert logstream.tail(log).to_dict()["exists"] is True
    assert logstream.LogPage(exists=False).to_dict()["exists"] is False


def test_log_path_for_maps_server_and_bot_ids_inside_run(paths):
    assert logstream.log_path_for(paths, "server") == paths.run / "server" / "serve.log"
    assert logstream.log_path_for(paths, "atlas") == paths.run / "atlas.log"
    assert logstream.log_path_for(paths, "bot:atlas") == paths.run / "atlas.log"
    assert logstream.log_path_for(paths, "bot:server") == paths.run / "server.log"
    # a bot named `serve` is a different file from the server's own log
    assert logstream.log_path_for(paths, "serve") == paths.run / "serve.log"
    assert logstream.log_path_for(paths, "serve") != logstream.server_log_file(paths)


@pytest.mark.parametrize("name", ["_scratch", "-lead", "my bot", ".hidden", "ünïcode"])
def test_log_path_for_accepts_every_name_the_roster_accepts(paths, name):
    assert logstream.log_path_for(paths, name) == paths.run / f"{name}.log"


@pytest.mark.parametrize("bad", ["", "..", ".", "../x", "a/b", "a\\b", "x\x00y", "x" * 200])
def test_log_path_for_refuses_ids_that_are_not_one_safe_component(paths, bad):
    assert logstream.log_path_for(paths, bad) is None


def test_server_log_source_refuses_a_symlink_at_the_log_or_its_directory(paths):
    grant = paths.run / "GITHUB_TOKEN"
    grant.write_text("ghp_plaintext\n", encoding="utf-8")
    live = logstream.server_log_file(paths)
    live.parent.mkdir(parents=True)
    live.symlink_to(grant)
    assert logstream.log_path_for(paths, "server") is None
    assert logstream.resolve_source(paths, "server", []) is None
    assert logstream.read_whole(live) is None  # the plain CLI read too
    live.unlink()
    live.parent.rmdir()
    live.parent.symlink_to(paths.run)  # the directory itself as a link
    assert logstream.log_path_for(paths, "server") is None


def test_capture_never_appends_through_a_symlink(paths):
    victim = paths.run / "atlas.json"
    victim.write_text("{}\n", encoding="utf-8")
    live = logstream.server_log_file(paths)
    live.parent.mkdir(parents=True)
    live.symlink_to(victim)
    with fake_streams() as (_out, err):
        sink = logstream.install_server_log(paths)
        print("must not land in atlas.json")
        assert sink.dropped >= 1 and "not being written" in err.getvalue()
    assert victim.read_text(encoding="utf-8") == "{}\n"
    assert live.is_symlink()  # the link was neither followed nor replaced


def test_read_whole_reads_the_file_the_plain_cli_prints(tmp_path):
    log = tmp_path / "x.log"
    assert logstream.read_whole(log) is None
    log.write_bytes(b"a\nb\xff\n")
    assert logstream.read_whole(log) == "a\nb\ufffd\n"


def test_log_path_for_refuses_a_symlink_named_like_a_log(paths):
    (paths.run / "bot-secrets").mkdir()
    grant = paths.run / "bot-secrets" / "GITHUB_TOKEN"
    grant.write_text("ghp_plaintext\n", encoding="utf-8")
    (paths.run / "atlas.log").symlink_to(grant)
    assert logstream.log_path_for(paths, "atlas") is None
    assert logstream.resolve_source(paths, "atlas", ["atlas"]) is None


def test_resolve_source_refuses_bots_outside_the_roster(paths):
    assert logstream.resolve_source(paths, "atlas", ["atlas"]) == paths.run / "atlas.log"
    assert logstream.resolve_source(paths, "nova", ["atlas"]) is None
    assert logstream.resolve_source(paths, "../x", ["atlas", "../x"]) is None
    assert logstream.resolve_source(paths, "server", []) == logstream.server_log_file(paths)
    # a bot literally named `server` is reachable only as bot:server
    assert logstream.resolve_source(paths, "server", ["server"]) == logstream.server_log_file(paths)
    assert logstream.resolve_source(paths, "bot:server", ["server"]) == paths.run / "server.log"


def test_resolve_source_prefers_a_roster_name_that_literally_is_the_id(paths):
    # a hand-edited roster may carry `bot:atlas` as a name; it must round-trip
    assert logstream.resolve_source(paths, "bot:atlas", ["bot:atlas", "atlas"]) == (
        paths.run / "bot:atlas.log"
    )
    assert logstream.resolve_source(paths, "atlas", ["bot:atlas", "atlas"]) == (
        paths.run / "atlas.log"
    )
    assert logstream.resolve_source(paths, "bot:atlas", ["atlas"]) == paths.run / "atlas.log"
    ids = [s["id"] for s in logstream.list_sources(paths, ["bot:atlas", "atlas"])]
    assert ids == ["server", "bot:atlas", "atlas"]


def test_list_sources_puts_the_server_first_and_reports_existence(paths):
    (paths.run / "atlas.log").write_text("hi\n", encoding="utf-8")
    sources = logstream.list_sources(paths, ["atlas", "nova", "server", "my bot", "../x"])
    assert [s["id"] for s in sources] == ["server", "atlas", "nova", "bot:server", "my bot"]
    assert all("path" not in s for s in sources)  # ids route; the host layout stays here
    by_id = {s["id"]: s for s in sources}
    assert by_id["server"]["kind"] == "server" and by_id["server"]["exists"] is False
    assert by_id["atlas"]["kind"] == "bot" and by_id["atlas"]["exists"] is True
    assert by_id["atlas"]["size"] == 3 and by_id["atlas"]["modified"] is not None
    assert by_id["nova"]["exists"] is False and by_id["nova"]["size"] == 0
    assert by_id["bot:server"]["name"] == "server"


# -- HTTP -------------------------------------------------------------------


def test_logs_index_and_tail_over_http(keyed_server):
    base, key, paths = keyed_server
    _serve_log(paths).write_text("boot\nready\nserving\n", encoding="utf-8")
    (paths.run / "atlas.log").write_text("[10:00:00] atlas up\n", encoding="utf-8")
    status, body = _get(f"{base}/api/logs", key)
    assert status == 200
    assert [s["id"] for s in body["sources"]] == ["server", "atlas", "_scratch", "my bot"]
    status, page = _get(f"{base}/api/logs/server?limit=2", key)
    assert status == 200
    assert page["source"] == "server" and page["lines"] == ["ready", "serving"]
    assert page["offset"] == page["size"] == 19 and page["truncated"] is True
    status, bot = _get(f"{base}/api/logs/atlas", key)
    assert status == 200 and bot["lines"] == ["[10:00:00] atlas up"]
    status, same = _get(f"{base}/api/logs/bot:atlas", key)
    assert status == 200 and same["lines"] == bot["lines"]
    (paths.run / "my bot.log").write_text("spaced\n", encoding="utf-8")
    (paths.run / "_scratch.log").write_text("underscored\n", encoding="utf-8")
    assert _get(f"{base}/api/logs/my%20bot", key)[1]["lines"] == ["spaced"]
    assert _get(f"{base}/api/logs/_scratch", key)[1]["lines"] == ["underscored"]


def test_logs_offset_returns_only_what_landed_since(keyed_server):
    base, key, paths = keyed_server
    log = _serve_log(paths)
    log.write_text("boot\n", encoding="utf-8")
    _status, page = _get(f"{base}/api/logs/server", key)
    _status, nothing = _get(f"{base}/api/logs/server?offset={page['offset']}", key)
    assert nothing["lines"] == [] and nothing["offset"] == page["offset"]
    _append(log, "ready\nhalf")
    _status, more = _get(f"{base}/api/logs/server?offset={page['offset']}", key)
    assert more["lines"] == ["ready"] and more["offset"] == len(b"boot\nready\n")
    log.write_text("fresh\n", encoding="utf-8")
    _status, reset = _get(f"{base}/api/logs/server?offset={more['offset']}", key)
    assert reset["reset"] is True and reset["lines"] == ["fresh"]
    # rotated and regrown past the offset: only the gen tells
    _status, page = _get(f"{base}/api/logs/server", key)
    os.replace(log, log.with_name("serve.log.1"))
    log.write_text("a\nb\nc\nd\n", encoding="utf-8")
    _status, regrown = _get(
        f"{base}/api/logs/server?offset={page['offset']}&gen={page['gen']}", key
    )
    assert regrown["reset"] is True and regrown["lines"] == ["a", "b", "c", "d"]


def test_logs_page_of_a_missing_log_says_it_does_not_exist(keyed_server):
    base, key, paths = keyed_server
    (paths.run / "_scratch.log").unlink(missing_ok=True)  # a roster bot that never logged
    status, page = _get(f"{base}/api/logs/_scratch", key)
    assert status == 200 and page["exists"] is False and page["lines"] == []
    status, polled = _get(f"{base}/api/logs/_scratch?offset=3&gen=0", key)
    assert status == 200 and polled["exists"] is False and polled["reset"] is True


def test_logs_reject_unknown_sources_and_bad_queries(keyed_server):
    base, key, _paths = keyed_server
    _serve_log(_paths).write_text("boot\n", encoding="utf-8")
    assert _get(f"{base}/api/logs/nova", key)[0] == 404
    assert _get(f"{base}/api/logs/%2e%2e%2fserve", key)[0] == 404
    assert _get(f"{base}/api/logs/server/other", key)[0] == 404
    assert _get(f"{base}/api/logs/server?limit=abc", key)[0] == 400
    assert _get(f"{base}/api/logs/server?offset=-1", key)[0] == 400
    assert _get(f"{base}/api/logs/server?limit=-5", key)[0] == 400
    assert _get(f"{base}/api/logs/server?offset=1&gen=x", key)[0] == 400


def test_logs_page_reads_back_into_the_rotated_generation(keyed_server):
    base, key, paths = keyed_server
    log = _serve_log(paths)
    log.with_name("serve.log.1").write_text("burst 1\nburst 2\n", encoding="utf-8")
    log.write_text("fresh\n", encoding="utf-8")
    _status, page = _get(f"{base}/api/logs/server?limit=3", key)
    assert page["lines"] == ["burst 1", "burst 2", "fresh"]
    assert page["offset"] == 6 and page["exists"] is True
    log.with_name("serve.log.1").unlink()


def test_arm_keepalive_sets_the_socket_option():
    import socket

    from harness.server import _Handler

    a, b = socket.socketpair()
    try:
        _Handler._arm_keepalive(a, 30.0)
        # Enabled socket options are nonzero (macOS returns the option bit).
        assert a.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
        _Handler._arm_keepalive(a, 0.0)  # floors, never raises
    finally:
        a.close()
        b.close()


def test_logs_page_caps_lines(keyed_server):
    base, key, paths = keyed_server
    _serve_log(paths).write_text(
        "".join(f"{i}\n" for i in range(logstream.MAX_LINES + 50)), encoding="utf-8"
    )
    _status, page = _get(f"{base}/api/logs/server?limit=999999", key)
    assert len(page["lines"]) == logstream.MAX_LINES and page["truncated"] is True


def test_logs_need_the_key(keyed_server):
    base, _key, _paths = keyed_server
    for path in ("/api/logs", "/api/logs/server", "/api/logs/server/stream"):
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}{path}", timeout=10)
        assert exc.value.code == 401


# -- SSE --------------------------------------------------------------------


def _open_sse(base: str, key: str, path: str):
    req = urllib.request.Request(f"{base}{path}", headers={"Authorization": f"Bearer {key}"})
    return urllib.request.urlopen(req, timeout=10)


def _next_frame(resp) -> dict:
    while True:
        raw = resp.readline()
        assert raw, "stream ended"
        if raw.startswith(b"data: "):
            return json.loads(raw[len(b"data: ") :].decode())


def test_logs_stream_over_sse_sends_a_snapshot_then_appended_frames(keyed_server):
    base, key, paths = keyed_server
    log = _serve_log(paths)
    log.write_text("boot\nready\n", encoding="utf-8")
    resp = _open_sse(base, key, "/api/logs/server/stream?limit=1")
    try:
        assert resp.headers.get("Content-Type", "").startswith("text/event-stream")
        first = _next_frame(resp)
        assert first["type"] == "log" and first["mutation"] == "snapshot"
        assert first["source"] == "server" and first["lines"] == ["ready"]
        assert first["offset"] == 11 and "epoch" in first and "seq" in first
        _append(log, "one more\n")
        nxt = _next_frame(resp)
        assert nxt["mutation"] == "appended" and nxt["lines"] == ["one more"]
        assert nxt["offset"] == 20 and nxt["seq"] > first["seq"]
        assert nxt["epoch"] == first["epoch"]
    finally:
        resp.close()


def test_logs_stream_resumes_from_a_offset_without_a_snapshot(keyed_server):
    base, key, paths = keyed_server
    log = _serve_log(paths)
    log.write_text("boot\nready\n", encoding="utf-8")
    resp = _open_sse(base, key, "/api/logs/server/stream?offset=5")
    try:
        first = _next_frame(resp)
        assert first["type"] == "log" and first["mutation"] == "appended"
        assert first["lines"] == ["ready"] and first["offset"] == 11
        # Truncation and writing are separate observable filesystem operations.
        # Hold the empty file until its reset arrives, then append the new line;
        # write_text could race the follower into either one frame or two.
        with log.open("w", encoding="utf-8") as fresh:
            reset = _next_frame(resp)
            assert reset["reset"] is True and reset["lines"] == [] and reset["offset"] == 0
            assert reset["mutation"] == "snapshot"
            fresh.write("fresh\n")
            fresh.flush()
        nxt = _next_frame(resp)
        assert nxt["lines"] == ["fresh"] and nxt["offset"] == 6
        assert nxt["mutation"] == "appended"
    finally:
        resp.close()


def test_logs_resume_from_a_gone_heartbeat_restarts_at_zero(keyed_server):
    # {offset: N, gen: 0} is what a client holds after the log went away
    # under it; once the file is back, both the poll and the stream give it
    # the whole file (as a reset / snapshot), never the tail after N.
    base, key, paths = keyed_server
    _serve_log(paths).write_text("boot\nready\n", encoding="utf-8")
    status, page = _get(f"{base}/api/logs/server?offset=5&gen=0", key)
    assert status == 200 and page["reset"] is True and page["lines"] == ["boot", "ready"]
    resp = _open_sse(base, key, "/api/logs/server/stream?offset=5&gen=0")
    try:
        first = _next_frame(resp)
        assert first["mutation"] == "snapshot" and first["lines"] == ["boot", "ready"]
        assert first["offset"] == 11
    finally:
        resp.close()


def test_logs_stream_heartbeats_while_quiet(keyed_server, monkeypatch):
    base, key, paths = keyed_server
    monkeypatch.setenv("HARNESS_LOG_HEARTBEAT", "1")  # floor is 1s
    _serve_log(paths).write_text("boot\n", encoding="utf-8")
    resp = _open_sse(base, key, "/api/logs/server/stream")
    try:
        assert _next_frame(resp)["mutation"] == "snapshot"
        beat = _next_frame(resp)
        assert beat["type"] == "log_heartbeat" and beat["source"] == "server"
        assert beat["offset"] == 5 and "lines" not in beat
    finally:
        resp.close()


# -- WebSocket --------------------------------------------------------------


def _no_frame(client, timeout=0.4) -> bool:
    client.sock.settimeout(timeout)
    try:
        frame = client.recv()
    except (TimeoutError, OSError):
        return True
    return frame is None


def test_ws_log_start_streams_and_log_stop_ends_it(keyed_server):
    base, key, paths = keyed_server
    host, port = base[len("http://") :].split(":")
    log = _serve_log(paths)
    log.write_text("boot\nready\n", encoding="utf-8")
    client = WSClient(host, int(port), path=f"/ws?token={key}")
    try:
        client.send({"type": "log_start", "source": "server", "limit": 1})
        started = _recv_type(client, "log_started")
        assert started["source"] == "server"
        snap = _recv_type(client, "log")
        assert snap["mutation"] == "snapshot" and snap["lines"] == ["ready"]
        assert snap["seq"] > started["seq"] and snap["epoch"] == started["epoch"]
        _append(log, "more\n")
        more = _recv_type(client, "log")
        assert more["mutation"] == "appended" and more["lines"] == ["more"]
        client.send({"type": "log_stop"})
        stopped = _recv_type(client, "log_stopped")
        assert stopped["source"] == "server"
        _append(log, "unseen\n")
        assert _no_frame(client)
    finally:
        client.close()


def test_ws_log_start_defaults_to_the_server_and_a_new_start_replaces_the_old(keyed_server):
    base, key, paths = keyed_server
    host, port = base[len("http://") :].split(":")
    serve_log = _serve_log(paths)
    atlas_log = paths.run / "atlas.log"
    serve_log.write_text("s1\n", encoding="utf-8")
    atlas_log.write_text("a1\n", encoding="utf-8")
    client = WSClient(host, int(port), path=f"/ws?token={key}")
    try:
        client.send({"type": "log_start"})
        assert _recv_type(client, "log_started")["source"] == "server"
        assert _recv_type(client, "log")["lines"] == ["s1"]
        client.send({"type": "log_start", "source": "atlas"})
        assert _recv_type(client, "log_started")["source"] == "atlas"
        snap = _recv_type(client, "log")
        assert snap["source"] == "atlas" and snap["lines"] == ["a1"]
        _append(serve_log, "s2\n")
        _append(atlas_log, "a2\n")
        nxt = _recv_type(client, "log")
        assert nxt["source"] == "atlas" and nxt["lines"] == ["a2"]
        assert _no_frame(client)  # nothing from the replaced server follower
    finally:
        client.close()


def test_ws_log_start_with_a_offset_resumes_without_a_snapshot(keyed_server):
    base, key, paths = keyed_server
    host, port = base[len("http://") :].split(":")
    log = _serve_log(paths)
    log.write_text("boot\nready\n", encoding="utf-8")
    client = WSClient(host, int(port), path=f"/ws?token={key}")
    try:
        client.send({"type": "log_start", "source": "server", "offset": 5})
        assert _recv_type(client, "log_started")["source"] == "server"
        first = _recv_type(client, "log")
        assert first["mutation"] == "appended" and first["lines"] == ["ready"]
        assert first["offset"] == 11
    finally:
        client.close()


def test_stream_pages_with_a_offset_skips_the_snapshot(tmp_path):
    log = tmp_path / "x.log"
    log.write_bytes(b"a\nb\n")
    seen = []

    def write(page, mutation):
        seen.append((mutation, list(page.lines), page.offset))
        return False

    _append(log, "c\n")
    logstream.stream_pages(log, lines=5, offset=2, stop=threading.Event(), write=write)
    assert seen == [("appended", ["b", "c"], 6)]


def test_ws_follower_clears_the_view_when_the_log_vanishes(keyed_server):
    base, key, paths = keyed_server
    host, port = base[len("http://") :].split(":")
    log = paths.run / "atlas.log"
    log.write_text("alive\n", encoding="utf-8")
    client = WSClient(host, int(port), path=f"/ws?token={key}")
    try:
        client.send({"type": "log_start", "bot": "atlas"})  # `bot` aliases `source`
        assert _recv_type(client, "log_started")["source"] == "bot:atlas"
        assert _recv_type(client, "log")["lines"] == ["alive"]
        log.unlink()
        gone = _recv_type(client, "log")
        assert gone["mutation"] == "cleared" and gone["exists"] is False and gone["lines"] == []
        log.write_text("back\n", encoding="utf-8")
        back = _recv_type(client, "log")
        assert back["mutation"] == "snapshot" and back["lines"] == ["back"]
    finally:
        client.close()


def test_ws_log_start_on_an_unknown_source_is_an_error_frame(keyed_server):
    base, key, _paths = keyed_server
    host, port = base[len("http://") :].split(":")
    client = WSClient(host, int(port), path=f"/ws?token={key}")
    try:
        client.send({"type": "log_start", "source": "nova"})
        err = _recv_type(client, "error")
        assert "nova" in err["error"]
        client.send({"type": "log_stop"})
        assert _recv_type(client, "log_stopped")["source"] == ""
    finally:
        client.close()


def test_epoch_sequencer_fences_log_frames_on_their_own_key():
    key = EpochSequencer.stream_key
    assert key({"type": "log", "source": "server"}) == "log:server"
    assert key({"type": "log_started", "source": "atlas"}) == "log:atlas"
    assert key({"type": "log_stopped", "source": "atlas"}) == "log:atlas"
    assert key({"type": "log_heartbeat", "source": "server"}) == "log:server"
    assert key({"type": "message", "bot": "atlas"}) == "bot:atlas"  # untouched


# -- CLI --------------------------------------------------------------------


def _cli_args(home, *argv):
    return build_parser().parse_args(["--home", str(home), "logs", *argv])


def test_cli_logs_prints_the_whole_bot_log_as_before(paths, capsys):
    (paths.run / "atlas.log").write_text("[10:00:00] up\n[10:00:01] hi\n", encoding="utf-8")
    assert cmd_logs(_cli_args(paths.home, "atlas")) == 0
    assert capsys.readouterr().out == "[10:00:00] up\n[10:00:01] hi\n"
    assert cmd_logs(_cli_args(paths.home, "nova")) == 1
    assert "(no log for nova" in capsys.readouterr().out


def test_cli_logs_server_tails_the_last_n_lines(paths, capsys):
    _serve_log(paths).write_text("a\nb\nc\n", encoding="utf-8")
    assert cmd_logs(_cli_args(paths.home, "server", "-n", "2")) == 0
    assert capsys.readouterr().out == "b\nc\n"


def test_cli_logs_refuses_an_id_that_would_leave_run(paths, capsys):
    assert cmd_logs(_cli_args(paths.home, "../serve")) == 2
    assert "not a log source" in capsys.readouterr().err


def test_cli_logs_follow_prints_new_lines_until_stopped(paths, monkeypatch, fast_poll):
    log = _serve_log(paths)
    log.write_text("boot\n", encoding="utf-8")
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    args = _cli_args(paths.home, "server", "-f", "-n", "1")
    args._stop = threading.Event()
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("rc", cmd_logs(args)), daemon=True)
    t.start()
    assert _wait(lambda: out.getvalue() == "boot\n")
    _append(log, "ready\n")
    assert _wait(lambda: out.getvalue() == "boot\nready\n")
    args._stop.set()
    t.join(timeout=5)
    assert not t.is_alive() and result["rc"] == 0


def test_cli_logs_follow_waits_for_a_log_that_does_not_exist_yet(paths, monkeypatch, fast_poll):
    log = _serve_log(paths)
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    args = _cli_args(paths.home, "server", "-f")
    args._stop = threading.Event()
    t = threading.Thread(target=lambda: cmd_logs(args), daemon=True)
    t.start()
    assert _wait(lambda: "waiting for" in err.getvalue())
    log.write_text("born\n", encoding="utf-8")
    assert _wait(lambda: out.getvalue() == "born\n")
    assert "restarted" not in err.getvalue()  # a birth is not a restart
    args._stop.set()
    t.join(timeout=5)
    assert not t.is_alive()


def test_cli_logs_follow_announces_a_removed_log(paths, monkeypatch, fast_poll):
    log = paths.run / "atlas.log"
    log.write_text("alive\n", encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    args = _cli_args(paths.home, "atlas", "-f")
    args._stop = threading.Event()
    t = threading.Thread(target=lambda: cmd_logs(args), daemon=True)
    t.start()
    assert _wait(lambda: out.getvalue() == "alive\n")
    log.unlink()
    assert _wait(lambda: "(log removed:" in err.getvalue())
    args._stop.set()
    t.join(timeout=5)
    assert not t.is_alive()


def test_cli_parser_takes_lines_and_follow_flags():
    args = build_parser().parse_args(["logs", "server", "-n", "50", "-f"])
    assert args.bot == "server" and args.lines == 50 and args.follow is True
    plain = build_parser().parse_args(["logs", "atlas"])
    assert plain.lines is None and plain.follow is False


def test_cli_namespace_without_new_flags_still_prints_whole_file(paths, capsys):
    (paths.run / "atlas.log").write_text("x\n", encoding="utf-8")
    ns = Namespace(home=str(paths.home), roster="roster.toml", backend="process", bot="atlas")
    assert cmd_logs(ns) == 0
    assert capsys.readouterr().out == "x\n"
