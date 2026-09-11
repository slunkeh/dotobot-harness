"""Host-side machine view: bot->machine resolution + exec argv contracts."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from harness import machine_view
from harness.paths import HarnessPaths

FAKE_DOCKER = """#!/bin/sh
printf '%s\\n' "$*" >> "{log}"
case "$2" in
  *) : ;;
esac
if [ "$1" = "exec" ]; then
  for a in "$@"; do
    if [ "$a" = "getdisplaygeometry" ]; then echo "1280 800"; exit 0; fi
    if [ "$a" = "import" ]; then printf 'PNGBYTES'; exit 0; fi
  done
fi
exit 0
"""


def _fake_engine(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "docker-argv.log"
    fake = bindir / "docker"
    fake.write_text(FAKE_DOCKER.format(log=log), encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.delenv("HARNESS_CONTAINER_ENGINE", raising=False)
    machine_view._geom_cache.clear()
    return log


def _paths(tmp_path) -> HarnessPaths:
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas"])
    return p


def test_machine_for_bot_reads_machines_run_file(tmp_path):
    paths = _paths(tmp_path)
    paths.run_file("atlas").write_text(
        json.dumps(
            {"bot": "atlas", "backend": "machines", "pid": 1, "machine": "harness-machine-0"}
        ),
        encoding="utf-8",
    )
    assert machine_view.machine_for_bot(paths, "atlas") == "harness-machine-0"


def test_machine_for_bot_ignores_other_backends_and_missing(tmp_path):
    paths = _paths(tmp_path)
    assert machine_view.machine_for_bot(paths, "atlas") is None
    paths.run_file("atlas").write_text(
        json.dumps({"bot": "atlas", "backend": "process", "pid": 1}), encoding="utf-8"
    )
    assert machine_view.machine_for_bot(paths, "atlas") is None


def test_exec_and_input_argv_shapes(tmp_path, monkeypatch):
    _fake_engine(tmp_path, monkeypatch)
    prefix = machine_view.exec_prefix("harness-machine-0")
    assert prefix[1:] == ["exec", "-e", "DISPLAY=:0", "harness-machine-0"]
    argv = machine_view.input_argv("harness-machine-0")
    assert argv[1:] == ["exec", "-i", "-e", "DISPLAY=:0", "harness-machine-0", "xdotool", "-"]


def test_capture_png_execs_import_inside_machine(tmp_path, monkeypatch):
    log = _fake_engine(tmp_path, monkeypatch)
    data = machine_view.capture_png("harness-machine-0")
    assert data == b"PNGBYTES"
    line = log.read_text(encoding="utf-8").splitlines()[0]
    assert line.startswith("exec -e DISPLAY=:0 harness-machine-0 import ")


def test_capture_for_model_prefers_jpeg_pipeline(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "docker-argv.log"
    fake = bindir / "docker"
    fake.write_text(
        f"""#!/bin/sh
printf '%s\\n' "$*" >> "{log}"
# JPEG SOI then payload
printf '\\377\\330JPEG'
exit 0
""",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.delenv("HARNESS_CONTAINER_ENGINE", raising=False)
    data, mime = machine_view.capture_for_model("harness-machine-0")
    assert mime == "image/jpeg"
    assert data.startswith(b"\xff\xd8")
    line = log.read_text(encoding="utf-8").splitlines()[0]
    assert "sh -c" in line
    assert "convert" in line


def test_geometry_parses_and_caches(tmp_path, monkeypatch):
    log = _fake_engine(tmp_path, monkeypatch)
    assert machine_view.geometry("harness-machine-0") == (1280, 800)
    assert machine_view.geometry("harness-machine-0") == (1280, 800)
    calls = [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if "getdisplaygeometry" in line
    ]
    assert len(calls) == 1  # TTL cache absorbed the second call


def test_launch_uses_detached_exec(tmp_path, monkeypatch):
    log = _fake_engine(tmp_path, monkeypatch)
    monkeypatch.delenv("HARNESS_CHROME_CDP", raising=False)
    assert machine_view.launch("harness-machine-0", ["xterm"]) is True
    line = log.read_text(encoding="utf-8").splitlines()[0]
    assert line == "exec -d -e DISPLAY=:0 -e HARNESS_CHROME_CDP=0 harness-machine-0 xterm"


def test_launch_passes_cdp_kill_switch(tmp_path, monkeypatch):
    log = _fake_engine(tmp_path, monkeypatch)
    monkeypatch.setenv("HARNESS_CHROME_CDP", "0")
    assert machine_view.launch("harness-machine-0", ["xterm"]) is True
    line = log.read_text(encoding="utf-8").splitlines()[0]
    assert "-e HARNESS_CHROME_CDP=0" in line


def test_stream_argv_runs_reaped_ffmpeg_inside_machine(tmp_path, monkeypatch):
    """The stream must die with its exec channel: a bare `docker exec ffmpeg`
    outlives the client, and orphans eat the machine's --pids-limit until the
    dock can't fork launchers (taskbar clicks 'do nothing')."""
    _fake_engine(tmp_path, monkeypatch)
    argv = machine_view.stream_argv("harness-machine-0", 12, (1280, 800))
    # interactive exec: the harness holds stdin open; EOF = client gone
    assert argv[1:6] == ["exec", "-i", "-e", "DISPLAY=:0", "harness-machine-0"]
    assert argv[6:8] == ["sh", "-c"]
    wrapper = argv[8]
    assert "-f x11grab" in wrapper
    assert "-video_size 1280x800" in wrapper
    assert "-i :0 -f mjpeg -q:v 7 -" in wrapper
    # stdin-EOF reaper + exit when ffmpeg dies on its own. fd 3 carries the
    # real stdin into the background watcher (POSIX gives `&` jobs /dev/null).
    assert wrapper.startswith("exec 3<&0; ffmpeg ")
    assert "cat <&3 >/dev/null" in wrapper
    assert "kill $p" in wrapper
    assert wrapper.endswith("wait $p")


def test_stream_argv_omits_video_size_when_unknown(tmp_path, monkeypatch):
    _fake_engine(tmp_path, monkeypatch)
    argv = machine_view.stream_argv("harness-machine-0", 12, None)
    assert "-video_size" not in argv[8]


def test_geometry_caches_failures_too(tmp_path, monkeypatch):
    """A down machine must not cost a blocking docker exec per mouse event."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "docker-argv.log"
    fake = bindir / "docker"
    fake.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "{log}"\nexit 1\n', encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.delenv("HARNESS_CONTAINER_ENGINE", raising=False)
    machine_view._geom_cache.clear()

    assert machine_view.geometry("harness-machine-0") is None
    assert machine_view.geometry("harness-machine-0") is None
    calls = [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if "getdisplaygeometry" in line
    ]
    assert len(calls) == 1  # the failure was cached for the TTL


def test_machine_for_bot_tolerates_unreadable_run_file(tmp_path):
    paths = _paths(tmp_path)
    # a directory in place of the run file raises OSError on read_text
    paths.run_file("atlas").mkdir(parents=True)
    assert machine_view.machine_for_bot(paths, "atlas") is None


def test_machine_attachment_is_readable_and_keeps_connector_path(tmp_path, monkeypatch):
    import sys

    from agent.runtime import _attachment_block, _attachment_meta

    paths = _paths(tmp_path)
    paths.uploads.mkdir(parents=True, exist_ok=True)
    source = paths.uploads / "1234-order prints.zip"
    source.write_bytes(b"PK\x03\x04\x00\xffarchive")
    destination = tmp_path / "machine" / "Uploads"
    monkeypatch.setattr(machine_view, "MACHINE_UPLOADS", str(destination), raising=False)
    seen = []

    def prefix(machine, *, interactive=False):
        seen.append((machine, interactive))
        return []

    monkeypatch.setattr(machine_view, "exec_prefix", prefix)
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + ":" + os.environ["PATH"])
    attachments = [{"name": "order prints.zip", "path": str(source), "size": source.stat().st_size}]
    before = _attachment_meta(attachments)
    block = _attachment_block(attachments, paths=paths, machine="sample-machine")
    assert (destination / source.name).read_bytes() == source.read_bytes()
    assert f"machine_path={destination / source.name}" in block
    assert f"path={source}" in block
    assert "linear_attach_files" in block
    assert _attachment_meta(attachments) == before
    assert seen == [("sample-machine", True)]


def test_machine_attachment_rejects_non_uploads_and_symlinks(tmp_path, monkeypatch):
    from agent.runtime import _attachment_block

    paths = _paths(tmp_path)
    paths.uploads.mkdir(parents=True, exist_ok=True)
    secret = tmp_path / "credentials.txt"
    secret.write_text("private")
    link = paths.uploads / "linked.txt"
    link.symlink_to(secret)
    monkeypatch.setattr(machine_view, "exec_prefix", lambda *a, **kw: pytest.fail("must not copy"))
    for source in (secret, link):
        block = _attachment_block([{"path": str(source)}], paths=paths, machine="sample-machine")
        assert "machine copy unavailable" in block
        assert "machine_path=" not in block


def test_machine_attachment_failure_is_explicit(tmp_path, monkeypatch):
    from agent.runtime import _attachment_block

    paths = _paths(tmp_path)
    paths.uploads.mkdir(parents=True, exist_ok=True)
    source = paths.uploads / "report.zip"
    source.write_bytes(b"zip")
    monkeypatch.setattr(machine_view, "exec_prefix", lambda *a, **kw: ["/missing-engine"])
    block = _attachment_block([{"path": str(source)}], paths=paths, machine="sample-machine")
    assert "machine copy unavailable" in block
    assert "machine_path=" not in block
    assert source.read_bytes() == b"zip"


def test_process_attachment_needs_no_machine_transfer(tmp_path, monkeypatch):
    from agent.runtime import _attachment_block

    source = tmp_path / "note.txt"
    source.write_text("uploaded note")
    monkeypatch.setattr(machine_view, "exec_prefix", lambda *a, **kw: pytest.fail("must not copy"))
    block = _attachment_block([{"path": str(source)}], paths=_paths(tmp_path), machine=None)
    assert "uploaded note" in block
    assert f"path={source}" in block
    assert "machine_path" not in block
