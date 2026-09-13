"""Container install contracts without an engine, credentials or host changes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import container_install as installer  # noqa: E402


def config(tmp_path):
    root = tmp_path / "dotobot"
    root.mkdir()
    return installer.configuration(root, None, 8765)


def test_controller_uses_same_host_state_path_and_sibling_machines(tmp_path):
    c = config(tmp_path)
    c["machine_image"] = "sha256:machine"
    args = installer.runtime_args(c, "sha256:server")
    state = str(Path(c["root"]) / "state")
    assert f"type=bind,src={state},dst={state}" in args
    assert "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock" in args
    assert f"HARNESS_HOME={state}" in args
    assert f"HARNESS_MACHINE_PREFIX={c['name']}-machine" in args
    assert "HARNESS_MACHINE_IMAGE=sha256:machine" in args
    assert "127.0.0.1:8765:8765" in args
    assert "--privileged" not in args


def test_https_does_not_publish_plaintext_api(tmp_path):
    c = dict(config(tmp_path), address="bots.example.com", machine_image="test")
    args = installer.runtime_args(c, "server")
    assert "-p" not in args
    assert "HARNESS_PUBLIC_URL=https://bots.example.com" in args


def test_configuration_preserves_identity_and_rejects_unregistered_state(tmp_path):
    c = config(tmp_path)
    root = Path(c["root"])
    (root / "install.json").write_text(json.dumps(c))
    assert installer.configuration(root, None, 8765) == c
    with pytest.raises(installer.InstallError, match="address retained"):
        installer.configuration(root, "bots.example.com", 8765)
    (root / "install.json").unlink()
    (root / "state").mkdir()
    with pytest.raises(installer.InstallError, match="unregistered state"):
        installer.configuration(root, None, 8765)


def test_foreign_container_is_refused_before_any_mutation(tmp_path, monkeypatch):
    c = config(tmp_path)
    monkeypatch.setattr(installer, "inspect", lambda *a: {"Config": {"Labels": {}}})
    monkeypatch.setattr(installer, "docker", lambda *a, **kw: pytest.fail("must not mutate"))
    with pytest.raises(installer.InstallError, match="another installation"):
        installer.install(c, tmp_path, "candidate")


def test_image_build_failure_keeps_running_server(tmp_path, monkeypatch):
    c = config(tmp_path)
    (tmp_path / "harness").mkdir()
    (tmp_path / "harness/version.py").write_text('__version__ = "1.2.3"')
    monkeypatch.setattr(
        installer,
        "owned",
        lambda kind, name, owner: (
            {"Id": "old"} if name == c["name"] and kind == "container" else None
        ),
    )
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(installer, "docker", docker)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(c, tmp_path, "candidate")
    assert len(calls) == 1 and calls[0][0] == "build"


def test_uninstall_retains_state_unless_explicitly_deleted(tmp_path, monkeypatch, capsys):
    c = config(tmp_path)
    state = Path(c["root"]) / "state"
    state.mkdir()
    marker = state / "keep"
    marker.write_text("persistent")
    monkeypatch.setattr(installer, "owned", lambda *a: None)
    monkeypatch.setattr(installer, "machines", lambda *a: [])
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(installer, "docker", docker)
    installer.uninstall(c, False)
    assert marker.read_text() == "persistent"
    assert not calls
    installer.uninstall(c, True)
    assert not state.exists()
    assert "Docker was retained" in capsys.readouterr().out


def test_uninstall_refuses_foreign_bot_before_stopping_server(tmp_path, monkeypatch):
    c = config(tmp_path)
    monkeypatch.setattr(installer, "owned", lambda *a: None)
    monkeypatch.setattr(installer, "inspect", lambda *a: {"Config": {"Labels": {}}})
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=c["name"] + "-machine-0\n")

    monkeypatch.setattr(installer, "docker", docker)
    with pytest.raises(installer.InstallError, match="Unowned bot"):
        installer.uninstall(c, True)
    assert calls == [("ps", "-a", "--format", "{{.Names}}")]


def test_container_help_does_not_require_docker():
    result = subprocess.run(
        ["bash", str(ROOT / "install.sh"), "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "--uninstall" in result.stdout and "macOS" in result.stdout


def test_delete_data_requires_uninstall(tmp_path, capsys):
    assert installer.main(["--root", str(tmp_path / "unused"), "--delete-data"]) == 1
    assert "--delete-data requires --uninstall" in capsys.readouterr().err
    assert not (tmp_path / "unused").exists()


def test_absent_network_is_distinct_from_unreachable_docker(monkeypatch):
    monkeypatch.setattr(
        installer,
        "docker",
        lambda *a, **kw: SimpleNamespace(
            returncode=1, stderr="Error response from daemon: network sample not found"
        ),
    )
    assert installer.inspect("network", "sample") is None
    monkeypatch.setattr(
        installer,
        "docker",
        lambda *a, **kw: SimpleNamespace(
            returncode=1, stderr="Cannot connect to the Docker daemon"
        ),
    )
    with pytest.raises(installer.InstallError, match="Cannot inspect"):
        installer.inspect("network", "sample")


def test_failed_update_restores_previous_server_and_retains_state(tmp_path, monkeypatch):
    c = dict(config(tmp_path), version="1.0.0", image="old-server", machine_image="old-machine")
    root = Path(c["root"])
    (root / "state").mkdir()
    (root / "state/keep").write_text("state")
    (root / "install.json").write_text(json.dumps(c))
    (tmp_path / "harness").mkdir()
    (tmp_path / "harness/version.py").write_text('__version__ = "1.2.3"')
    existing = {c["name"]}
    monkeypatch.setattr(
        installer,
        "owned",
        lambda kind, name, identity: (
            {"Id": name} if kind == "container" and name in existing else None
        ),
    )
    monkeypatch.setattr(installer, "inspect", lambda *a: {"Id": "candidate-machine"})
    monkeypatch.setattr(installer, "ensure_network", lambda *a: None)
    monkeypatch.setattr(installer, "ensure_proxy", lambda *a: None)
    monkeypatch.setattr(
        installer, "finish", lambda *a: (_ for _ in ()).throw(installer.InstallError("unhealthy"))
    )
    checked = []
    monkeypatch.setattr(installer, "ready", lambda config: checked.append(config["image"]))
    calls = []

    def docker(*args, **kw):
        calls.append(args)
        if args[0] == "rename":
            existing.remove(args[1])
            existing.add(args[2])
        if args[0] == "run":
            existing.add(args[args.index("--name") + 1])
        if args[0] == "rm":
            existing.remove(args[1])
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(installer, "docker", docker)
    with pytest.raises(installer.InstallError, match="unhealthy"):
        installer.install(c, tmp_path, "candidate-server")
    assert existing == {c["name"]}
    assert checked == ["old-server"]
    assert json.loads((root / "install.json").read_text()) == c
    assert (root / "state/keep").read_text() == "state"
    assert ("start", c["name"]) in calls


@pytest.mark.parametrize("preinstalled", [False, True])
def test_linux_bootstrap_installs_docker_only_if_missing(tmp_path, preinstalled):
    import os
    import shutil

    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    calls = tmp_path / "calls"
    root = tmp_path / "install"

    def executable(name, body):
        path = fakebin / name
        path.write_text(body)
        path.chmod(0o755)

    # An isolated PATH makes "Docker missing" a real command-discovery test.
    for name in ["bash", "mkdir", "mktemp", "chmod", "cat", "rm"]:
        (fakebin / name).symlink_to(shutil.which(name))
    executable("uname", '#!/bin/sh\ncase "$1" in -s) echo Linux;; -m) echo aarch64;; esac\n')
    executable("id", "#!/bin/sh\necho 0\n")
    executable("sh", '#!/bin/sh\nexec /bin/sh "$@"\n')
    docker_body = f"""#!{sys.executable}
import pathlib, sys
args = sys.argv[1:]
with open({str(calls)!r}, 'a') as log: log.write(' '.join(args)+'\\n')
if args[0] == 'info':
    if '--format' in args: print('linux')
elif args[0] == 'context': print('unix:///var/run/docker.sock')
elif args[0] == 'run' and 'python:3.12-slim' in args:
    assert 'checksum mismatch' in sys.stdin.read()
    stage = pathlib.Path(args[-1]); (stage/'release/deploy').mkdir(parents=True)
    (stage/'release/deploy/Dockerfile').write_text('FROM scratch')
elif args[0] == 'build':
    pathlib.Path(args[args.index('--iidfile')+1]).write_text('sha256:'+'a'*64)
"""
    payload = tmp_path / "docker-payload"
    payload.write_text(docker_body)
    if preinstalled:
        executable("docker", docker_body)
    executable(
        "curl",
        f'''#!{sys.executable}
import pathlib, sys
assert 'https://get.docker.com' in sys.argv
pathlib.Path({str(tmp_path / "docker-downloaded")!r}).touch()
output=pathlib.Path(sys.argv[sys.argv.index('-o')+1])
output.write_text('#!/bin/sh\\n/bin/cp "{payload}" "{fakebin / "docker"}"\\n/bin/chmod 755 "{fakebin / "docker"}"\\n')
''',
    )
    result = subprocess.run(
        ["/bin/bash"],
        input=(ROOT / "install.sh").read_text(),
        text=True,
        capture_output=True,
        env=dict(os.environ, PATH=str(fakebin), HOME=str(tmp_path), DOTOBOT_HOME=str(root)),
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "docker-downloaded").exists() is not preinstalled
    log = calls.read_text()
    assert "container_install.py --root" in log
    assert "dst=/var/run/docker.sock" in log
    assert not list(root.glob("download.*"))
    assert (root / "setup.log").stat().st_mode & 0o777 == 0o600
    assert "\x1b" not in result.stdout
    assert "5  Building your server" in result.stdout


def test_data_deletion_never_guesses_volume_ownership_from_prefix(tmp_path, monkeypatch):
    c = config(tmp_path)
    monkeypatch.setattr(installer, "owned", lambda *a: None)
    monkeypatch.setattr(installer, "machines", lambda *a: [])
    monkeypatch.setattr(
        installer, "docker", lambda *a, **kw: pytest.fail("No known volume can be deleted")
    )
    installer.uninstall(c, True)


def test_health_probe_executes_and_checks_release_version(tmp_path, monkeypatch):
    import io
    import urllib.request

    (tmp_path / "link-key").write_text("test-only-key")
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path))

    def response(request, **kwargs):
        assert request.get_header("Authorization") == "Bearer test-only-key"
        stream = io.BytesIO(b'{"version":"1.2.3"}')
        stream.status = 200
        return stream

    monkeypatch.setattr(urllib.request, "urlopen", response)

    def docker(*args, **kwargs):
        monkeypatch.setattr(sys, "argv", ["probe", args[5]])
        exec(compile(args[4], "health probe", "exec"), {})
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(installer, "docker", docker)
    installer.ready({"name": "controller", "version": "1.2.3"})
    with pytest.raises(AssertionError):
        installer.ready({"name": "controller", "version": "9.9.9"})


def test_controller_restart_starts_roster_and_quiesces_before_exit(monkeypatch):
    import container_entrypoint as entrypoint

    from harness.cli import build_parser

    events = []
    handlers = {}

    class Server:
        def __init__(self, command):
            args = build_parser().parse_args(command[3:])
            assert args.backend == "machines"
            assert args.up, "Controller restart must bring the persisted roster back up"
            events.append("start")

        def wait(self):
            handlers[entrypoint.signal.SIGTERM](None, None)
            handlers[entrypoint.signal.SIGTERM](None, None)
            return 0

        def terminate(self):
            events.append("terminate")

    def down(command, **kwargs):
        assert command[-1] == "down"
        events.append("sync-and-stop")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(entrypoint.subprocess, "Popen", Server)
    monkeypatch.setattr(entrypoint.subprocess, "run", down)
    monkeypatch.setattr(entrypoint.signal, "signal", lambda sig, fn: handlers.update({sig: fn}))
    assert entrypoint.main() == 0
    assert events == ["start", "sync-and-stop", "terminate"]


def test_result_panel_shows_one_private_code_after_health_check(tmp_path, monkeypatch, capsys):
    from harness.linking import decode_link_code

    c = dict(config(tmp_path), version="1.2.3")
    state = Path(c["root"]) / "state"
    state.mkdir()
    (state / "link-key").write_text("panel-test-key")
    checked = []
    monkeypatch.setattr(installer, "ready", lambda value: checked.append(value))
    installer.finish(c)
    out = capsys.readouterr().out
    assert checked == [c]
    codes = [line.strip() for line in out.splitlines() if line.strip().startswith("dotobot_")]
    assert len(codes) == 1
    assert decode_link_code(codes[0])["key"] == "panel-test-key"
    assert "panel-test-key" not in out and "harness_" not in out
    assert "5/5  Ready" in out and "YOUR PRIVATE LINK CODE" in out
    assert "dotobot://" not in out and "Press any key" not in out


def test_failed_health_never_prints_success_or_link_code(tmp_path, monkeypatch, capsys):
    c = dict(config(tmp_path), version="1.2.3")
    monkeypatch.setattr(
        installer, "ready", lambda _: (_ for _ in ()).throw(installer.InstallError("not healthy"))
    )
    with pytest.raises(installer.InstallError, match="not healthy"):
        installer.finish(c)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("exit_status", [0, 7])
def test_terminal_progress_preserves_piped_input_and_exit_status(tmp_path, exit_status):
    import os
    import pty
    import shlex

    script = (ROOT / "install.sh").read_text()
    function = script.split("dotobot_step() {", 1)[1].split("\n}\n", 1)[0]
    log = tmp_path / "build.log"
    command = (
        "dotobot_step() {"
        + function
        + "\n}\n"
        + "interactive=1\nsetup_log="
        + shlex.quote(str(log))
        + "\n"
        + f"dotobot_step 'Building' /bin/sh -c 'cat; exit {exit_status}' <<'INPUT'\n"
        + "piped release verifier\nINPUT\n"
    )
    master, slave = pty.openpty()
    try:
        result = subprocess.run(
            ["/bin/bash", "-c", command], stdout=slave, stderr=subprocess.PIPE, timeout=15
        )
    finally:
        os.close(slave)
        os.close(master)
    assert result.returncode == exit_status
    assert log.read_text() == "piped release verifier\n"
    if exit_status:
        assert str(log).encode() in result.stderr
