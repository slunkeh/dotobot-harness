"""Host package install for computer use (no real apt)."""

from __future__ import annotations

from types import SimpleNamespace

from harness import hostdeps


def test_missing_when_binaries_absent(monkeypatch):
    monkeypatch.setattr(hostdeps, "_which", lambda _n: None)
    names = [b for b, _ in hostdeps.missing()]
    assert names == ["ffmpeg", "xdotool", "scrot", "xclip"]


def test_missing_empty_when_present(monkeypatch):
    monkeypatch.setattr(hostdeps, "_which", lambda _n: f"/usr/bin/{_n}")
    assert hostdeps.missing() == []


def test_install_noops_when_present(monkeypatch):
    monkeypatch.setattr(hostdeps, "_which", lambda _n: f"/usr/bin/{_n}")
    logs: list[str] = []
    code = hostdeps.install(log=logs.append, run=lambda *a, **k: SimpleNamespace(returncode=0))
    assert code == 0
    assert any("already installed" in m for m in logs)


def test_install_runs_apt_for_missing(monkeypatch):
    def fake_which(name: str):
        if name in {"apt-get", "sudo"}:
            return f"/usr/bin/{name}"
        return None

    monkeypatch.setattr(hostdeps, "_which", fake_which)
    monkeypatch.setattr(hostdeps.os, "geteuid", lambda: 1000)
    seen: list[list[str]] = []

    def fake_run(cmd, check=False):
        seen.append(cmd)
        # Pretend install succeeded: binaries now exist.
        monkeypatch.setattr(
            hostdeps,
            "_which",
            lambda n: f"/usr/bin/{n}",
        )
        return SimpleNamespace(returncode=0)

    logs: list[str] = []
    code = hostdeps.install(log=logs.append, run=fake_run)
    assert code == 0
    assert seen, "expected apt-get to run"
    cmd = seen[0]
    assert cmd[:2] == ["sudo", "-n"]
    assert "apt-get" in cmd
    assert "xdotool" in cmd
    assert "ffmpeg" in cmd
    assert "scrot" in cmd
    assert "xclip" in cmd


def test_install_without_apt_returns_1(monkeypatch):
    monkeypatch.setattr(hostdeps, "_which", lambda _n: None)
    logs: list[str] = []
    code = hostdeps.install(log=logs.append)
    assert code == 1
    assert any("missing host tools" in m for m in logs)


def test_ensure_silent_when_present(monkeypatch, capsys):
    monkeypatch.setattr(hostdeps, "_which", lambda _n: f"/usr/bin/{_n}")
    hostdeps.ensure()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_ensure_silent_on_non_linux_without_apt(monkeypatch, capsys):
    monkeypatch.setattr(hostdeps, "_which", lambda _n: None)
    monkeypatch.setattr(hostdeps.sys, "platform", "darwin")
    hostdeps.ensure()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_machine_server_does_not_install_host_desktop_packages(monkeypatch):
    from harness import cli, server

    calls = []
    monkeypatch.setattr(hostdeps, "ensure", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(server, "serve", lambda **kwargs: 0)
    assert cli.cmd_serve(cli.build_parser().parse_args(["serve"])) == 0
    assert calls == []
    assert cli.cmd_serve(cli.build_parser().parse_args(["--backend", "process", "serve"])) == 0
    assert calls == [{"desktop": False}]
