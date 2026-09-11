"""Regression tests for harness.boot — the `harness` console-script entry.

`boot.main` must install the live-talk routes BEFORE the CLI serves anything,
and must hand argv through to `harness.cli.main` unchanged. A regression here
ships a `harness` script whose voice endpoints 404 while `python -m harness`
works, which nothing else would catch.
"""

import harness.boot
import harness.cli
import harness.voice


def test_main_patches_voice_routes_then_dispatches_to_cli(monkeypatch):
    calls = []
    monkeypatch.setattr(harness.voice, "patch_server", lambda: calls.append("voice"))

    def fake_cli(argv=None):
        calls.append(("cli", argv))
        return 7

    monkeypatch.setattr(harness.cli, "main", fake_cli)
    assert harness.boot.main(["roster"]) == 7
    assert calls == ["voice", ("cli", ["roster"])]


def test_main_runs_a_real_harmless_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "home"))
    assert harness.boot.main(["settings"]) == 0
    out = capsys.readouterr().out
    assert "HARNESS_" in out
