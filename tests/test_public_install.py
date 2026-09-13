"""Fresh installs/updates use disposable files and fake OS commands, never this host."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import public_install as installer  # noqa: E402


@pytest.fixture
def host(tmp_path, monkeypatch):
    layout = installer.Layout(tmp_path / "host")
    monkeypatch.setattr(installer, "platform_check", lambda: None)
    monkeypatch.setattr(installer, "ensure_caddy", lambda **kwargs: None)
    monkeypatch.setattr(installer.socket, "getaddrinfo", lambda *a, **kw: [(None,)])

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bind(self, addr):
            pass

    monkeypatch.setattr(installer.socket, "socket", lambda *a, **kw: Socket())
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv == ["systemctl", "restart", installer.SERVICE]:
            assert (layout.home / "link-key").is_file(), (
                "key must exist before the first journal entry"
            )
        return subprocess.CompletedProcess(argv, 0, stdout="sha256:old-image\n", stderr="")

    probes = []

    def probe(url, key, version):
        assert key and version
        probes.append((url, version))

    return layout, calls, probes, runner, probe


def release(tmp_path, version="1.0.0"):
    tree = tmp_path / ("release-" + version)
    (tree / "harness").mkdir(parents=True)
    (tree / "deploy").mkdir()
    (tree / "harness/version.py").write_text(f'__version__ = "{version}"\n')
    (tree / "deploy/Dockerfile.machine").write_text("FROM scratch\n")
    return tree


def initial(host, tmp_path, **kwargs):
    layout, calls, probes, runner, probe = host
    tree = release(tmp_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"version": "1.0.0"}))
    args = [
        "--ip" if kwargs.get("ip") else "--domain",
        kwargs.get("ip", "Bots.Example.com"),
        "--release-tree",
        str(tree),
        "--release-manifest",
        str(manifest),
    ]
    return installer.main(
        args, layout=layout, runner=kwargs.get("runner", runner), probe=kwargs.get("probe", probe)
    )


def test_fresh_install_is_private_persistent_and_manual(host, tmp_path, capsys):
    layout, calls, probes, _, _ = host
    assert initial(host, tmp_path) == 0
    config = json.loads(layout.config.read_text())
    assert config["auto_update"] is False and config["ready"] is True
    assert config["manifest_url"] == "https://releases.dotobot.com/harness/manifest.json"
    unit = (layout.units / installer.SERVICE).read_text()
    assert "--host 127.0.0.1" in unit and "--backend process" not in unit
    assert "HARNESS_HOME=" + str(layout.home) in (layout.config_dir / "server.env").read_text()
    assert (layout.home / "link-key").stat().st_mode & 0o777 == 0o600
    assert (layout.home / "public-url").read_text().strip() == "https://bots.example.com"
    assert ("https://bots.example.com", "1.0.0") in probes
    assert ["systemctl", "disable", "--now", installer.TIMER] in calls
    assert "Link code: harness_" in capsys.readouterr().out
    assert "HARNESS_HOME=" + str(layout.home) in layout.wrapper.read_text()


@pytest.mark.parametrize("address", ["8.8.8.8", "2606:4700:4700::1111"])
def test_ip_install_uses_public_acme_and_persists_a_reusable_link(host, tmp_path, capsys, address):
    from harness.linking import decode_link_code

    layout, _, probes, runner, probe = host
    expected = installer.public_origin(address)
    assert initial(host, tmp_path, ip=address) == 0
    assert (layout.home / "public-url").read_text().strip() == expected
    assert f"HARNESS_PUBLIC_URL={expected}\n" in (layout.config_dir / "server.env").read_text()
    assert expected in layout.caddy.read_text()
    assert f"default_sni {address}\n" in layout.caddy.read_text()
    assert "issuer acme https://acme-v02.api.letsencrypt.org/directory" in layout.caddy.read_text()
    assert "profile shortlived" in layout.caddy.read_text()
    assert (expected, "1.0.0") in probes
    code = next(
        line.removeprefix("Link code: ")
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("Link code: ")
    )
    assert decode_link_code(code) == {
        "url": expected,
        "key": (layout.home / "link-key").read_text().strip(),
    }
    before = {
        path: path.read_bytes() for path in (layout.config, layout.caddy, layout.home / "link-key")
    }
    assert installer.main(["--link"], layout=layout, runner=runner, probe=probe) == 0
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize(
    "address",
    [
        "",
        "127.0.0.1",
        "10.0.0.1",
        "169.254.1.1",
        "100.64.0.1",
        "224.0.0.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
        "0.0.0.0",
        "8.8.8.8:443",
        "8.8.8.8/path",
    ],
)
def test_ip_install_rejects_nonpublic_or_malformed_addresses_before_changes(host, address):
    layout, calls, _, runner, probe = host
    assert installer.main(["--ip", address], layout=layout, runner=runner, probe=probe) == 1
    assert not layout.config_dir.exists()
    assert calls == []


def test_existing_ip_install_refuses_address_changes(host, tmp_path):
    layout, calls, _, runner, probe = host
    assert initial(host, tmp_path, ip="8.8.8.8") == 0
    original = layout.config.read_bytes()
    calls.clear()
    assert installer.main(["--ip", "1.1.1.1"], layout=layout, runner=runner, probe=probe) == 1
    assert layout.config.read_bytes() == original
    assert calls == []


@pytest.mark.parametrize(("machine", "architecture"), [("x86_64", "amd64"), ("aarch64", "arm64")])
def test_caddy_official_package_is_verified_before_install(monkeypatch, machine, architecture):
    payload = b"reviewed official package fixture"
    monkeypatch.setattr(installer.shutil, "which", lambda _: None)
    monkeypatch.setattr(installer.os, "uname", lambda: SimpleNamespace(machine=machine))
    monkeypatch.setattr(
        installer, "CADDY_PACKAGES", {architecture: hashlib.sha256(payload).hexdigest()}
    )
    requested = []

    def fetch(url, **kwargs):
        requested.append(url)
        return io.BytesIO(payload)

    monkeypatch.setattr(
        installer.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=fetch)
    )
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["apt-get", "install"]:
            assert Path(argv[-1]).read_bytes() == payload
        return subprocess.CompletedProcess(argv, 0, stdout=f"v{installer.CADDY_VERSION} fixture\n")

    installer.ensure_caddy(runner=runner)
    assert requested == [
        f"https://github.com/caddyserver/caddy/releases/download/v{installer.CADDY_VERSION}/caddy_{installer.CADDY_VERSION}_linux_{architecture}.deb"
    ]
    assert calls[0][:2] == ["apt-get", "install"]
    assert calls[1] == ["/usr/bin/caddy", "version"]


def test_caddy_bad_checksum_never_installs_a_package(monkeypatch):
    monkeypatch.setattr(installer.shutil, "which", lambda _: None)
    monkeypatch.setattr(installer.os, "uname", lambda: SimpleNamespace(machine="x86_64"))
    monkeypatch.setattr(
        installer.urllib.request,
        "build_opener",
        lambda *args: SimpleNamespace(
            open=lambda *args, **kwargs: io.BytesIO(b"untrusted package")
        ),
    )
    calls = []
    with pytest.raises(installer.InstallError, match="checksum"):
        installer.ensure_caddy(runner=lambda *args, **kwargs: calls.append(args))
    assert calls == []


def test_modern_system_caddy_is_reused_without_download(monkeypatch):
    monkeypatch.setattr(installer.shutil, "which", lambda _: "/usr/bin/caddy")
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="v2.11.4 fixture\n")

    installer.ensure_caddy(runner=runner)
    assert calls == [["/usr/bin/caddy", "version"]]
    assert not installer.caddy_version_ok("v2.6.2")
    assert not installer.caddy_version_ok("v2.11.4-beta.1")
    assert installer.caddy_version_ok("v2.10.0")


def test_caddy_redirects_are_limited_to_official_https_asset_hosts():
    handler = installer.CaddyRedirects()
    request = installer.urllib.request.Request(
        "https://github.com/caddyserver/caddy/releases/download/package.deb"
    )
    for target in (
        "http://github.com/package.deb",
        "https://evil.example/package.deb",
        "https://user@github.com/package.deb",
        "https://github.com:8080/package.deb",
    ):
        with pytest.raises(installer.InstallError, match="official HTTPS"):
            handler.redirect_request(request, None, 302, "Found", {}, target)
    redirected = handler.redirect_request(
        request, None, 302, "Found", {}, "https://release-assets.githubusercontent.com/package.deb"
    )
    assert redirected.full_url == "https://release-assets.githubusercontent.com/package.deb"


def test_caddy_can_read_its_configuration_with_private_root_umask(host, tmp_path):
    layout, _, _, _, _ = host
    previous = os.umask(0o077)
    try:
        assert initial(host, tmp_path) == 0
    finally:
        os.umask(previous)
    assert layout.config_dir.stat().st_mode & 0o777 == 0o755
    assert layout.caddy.stat().st_mode & 0o777 == 0o644
    for private_file in (layout.config, layout.config_dir / "server.env"):
        assert private_file.stat().st_mode & 0o777 == 0o600


def test_existing_docker_engine_is_reused_without_replacing_its_package(
    host, tmp_path, monkeypatch
):
    layout, calls, _, _, _ = host
    monkeypatch.setattr(installer.shutil, "which", lambda command: "/usr/bin/docker")
    assert initial(host, tmp_path) == 0
    packages = next(call for call in calls if call[:2] == ["apt-get", "install"])
    assert "docker.io" not in packages
    assert (
        "HARNESS_MACHINE_IMAGE=dotobot-server-machine"
        in (layout.config_dir / "server.env").read_text()
    )
    assert all("agent-harness-machine" not in arg for call in calls for arg in call)


@pytest.mark.parametrize("existing_docker", [False, True])
def test_machine_build_has_apparmor_userspace_on_minimal_hosts(
    host, tmp_path, monkeypatch, existing_docker
):
    """An enforcing kernel still needs the parser omitted by minimal Debian images."""
    _, calls, _, runner, _ = host
    monkeypatch.setattr(
        installer.shutil,
        "which",
        lambda command: "/usr/bin/docker" if command == "docker" and existing_docker else None,
    )
    parser_installed = False

    def enforcing_host(argv, **kwargs):
        nonlocal parser_installed
        if argv[:2] == ["apt-get", "install"] and "apparmor" in argv:
            parser_installed = True
        if argv[:2] == ["docker", "build"] and not parser_installed:
            raise subprocess.CalledProcessError(
                1, argv, stderr="docker-default profile requires apparmor_parser"
            )
        return runner(argv, **kwargs)

    assert initial(host, tmp_path, runner=enforcing_host) == 0
    assert parser_installed
    packages = next(call for call in calls if call[:2] == ["apt-get", "install"])
    assert ("docker.io" in packages) is not existing_docker


def test_concurrent_install_cannot_create_or_replace_install_record(host, tmp_path, capsys):
    layout, calls, _, _, _ = host
    layout.config_dir.mkdir(parents=True)
    with (layout.config_dir / "update.lock").open("a") as lock:
        installer.fcntl.flock(lock, installer.fcntl.LOCK_EX | installer.fcntl.LOCK_NB)
        assert initial(host, tmp_path) == 1
    assert not layout.config.exists()
    assert calls == []
    assert "Another Dotobot install" in capsys.readouterr().err


def test_failed_https_does_not_print_code_or_mark_ready(host, tmp_path, capsys):
    layout, _, _, _, _ = host

    def fail(url, key, version):
        if url.startswith("https:"):
            raise installer.InstallError("DNS not ready")

    assert initial(host, tmp_path, probe=fail) == 1
    assert not json.loads(layout.config.read_text())["ready"]
    assert "harness_" not in capsys.readouterr().out


def test_rerun_preserves_settings_key_proxy_and_update_policy(host, tmp_path, capsys):
    layout, calls, _, runner, probe = host
    assert initial(host, tmp_path) == 0
    capsys.readouterr()
    key = (layout.home / "link-key").read_bytes()
    settings = layout.config_dir / "server.env"
    settings.write_text(settings.read_text() + "HARNESS_MACHINE_CPUS=3\n")
    layout.caddy.write_text(layout.caddy.read_text() + "# custom owner setting\n")
    before = {p: p.read_bytes() for p in (settings, layout.caddy, layout.config)}
    calls.clear()
    assert installer.main(["--link"], layout=layout, runner=runner, probe=probe) == 0
    assert calls == []
    assert (layout.home / "link-key").read_bytes() == key
    assert {p: p.read_bytes() for p in before} == before


def test_update_policy_requires_explicit_opt_in(host, tmp_path):
    layout, calls, _, runner, probe = host
    assert initial(host, tmp_path) == 0
    assert installer.main(["--auto-update"], layout=layout, runner=runner, probe=probe) == 0
    assert ["systemctl", "enable", "--now", installer.TIMER] in calls
    assert json.loads(layout.config.read_text())["auto_update"] is True
    assert installer.main(["--no-auto-update"], layout=layout, runner=runner, probe=probe) == 0
    assert json.loads(layout.config.read_text())["auto_update"] is False


@pytest.mark.parametrize("existing", ["proxy", "managed", "state"])
def test_existing_owner_files_prevent_adoption_before_commands(host, tmp_path, existing):
    layout, calls, _, _, _ = host
    path = {"proxy": layout.caddy, "managed": layout.root / "current", "state": layout.home}[
        existing
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("owner data")
    assert initial(host, tmp_path) == 1
    assert path.read_text() == "owner data" and calls == []


def test_failed_image_build_never_changes_active_release(host, tmp_path):
    layout, _, _, runner, probe = host
    assert initial(host, tmp_path) == 0
    new = release(tmp_path, "1.0.1")

    def fail(argv, **kwargs):
        if argv[:2] == ["docker", "build"]:
            raise subprocess.CalledProcessError(1, argv)
        return runner(argv, **kwargs)

    with pytest.raises(subprocess.CalledProcessError):
        installer.apply_release(
            layout, new, json.loads(layout.config.read_text()), runner=fail, probe=probe
        )
    assert installer.updater.current_version(layout.root) == "1.0.0"


def test_failed_update_restores_release_image_and_state(host, tmp_path):
    layout, calls, _, runner, _ = host
    assert initial(host, tmp_path) == 0
    key = (layout.home / "link-key").read_bytes()
    (layout.home / "roster.json").write_text('{"bots": []}')
    new = release(tmp_path, "1.0.1")

    checks = []

    def fail(url, key, version):
        checks.append((url, version))
        if version == "1.0.1":
            raise installer.InstallError("unhealthy")

    with pytest.raises(installer.InstallError):
        installer.apply_release(
            layout, new, json.loads(layout.config.read_text()), runner=runner, probe=fail
        )
    assert installer.updater.current_version(layout.root) == "1.0.0"
    assert ["docker", "tag", "sha256:old-image", installer.IMAGE] in calls
    assert (layout.home / "link-key").read_bytes() == key
    assert (layout.home / "roster.json").read_text() == '{"bots": []}'
    assert ("https://bots.example.com", "1.0.0") in checks


def test_failed_rollback_reports_manual_recovery_and_preserves_newer_state(host, tmp_path):
    layout, _, _, runner, _ = host
    assert initial(host, tmp_path) == 0
    state = layout.home / "state.sqlite"
    state.write_bytes(b"newer schema must not be replaced")
    new = release(tmp_path, "1.0.1")

    def fail(url, key, version):
        raise installer.InstallError("new schema refused")

    with pytest.raises(installer.InstallError, match="Manual recovery is required"):
        installer.apply_release(
            layout, new, json.loads(layout.config.read_text()), runner=runner, probe=fail
        )
    assert state.read_bytes() == b"newer schema must not be replaced"


def test_successful_update_changes_release_after_build(host, tmp_path):
    layout, calls, _, runner, probe = host
    assert initial(host, tmp_path) == 0
    calls.clear()
    new = release(tmp_path, "1.0.1")
    installer.apply_release(
        layout, new, json.loads(layout.config.read_text()), runner=runner, probe=probe
    )
    assert installer.updater.current_version(layout.root) == "1.0.1"
    assert calls[0][:2] == ["docker", "build"]
    assert calls[-1] == ["systemctl", "restart", installer.SERVICE]


@pytest.mark.parametrize(
    "domain",
    [
        "https://bots.example.com",
        "example.com/path",
        "127.0.0.1",
        "localhost",
        "x\n{ evil }",
        "*.example.com",
    ],
)
def test_domains_cannot_inject_proxy_or_unit_configuration(domain):
    with pytest.raises(installer.InstallError):
        installer.domain_name(domain)


def test_release_redirects_cannot_change_origin_or_downgrade():
    handler = installer.ReleaseRedirects()
    request = installer.urllib.request.Request("https://releases.example.com/manifest.json")
    for target in ("https://evil.example/file", "http://releases.example.com/file"):
        with pytest.raises(installer.InstallError):
            handler.redirect_request(request, None, 302, "Found", {}, target)


def test_bootstrap_rejects_bad_checksum_before_extracting(tmp_path, monkeypatch):
    script = (ROOT / "deploy/system_install.sh").read_text().split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    data = b"not-an-archive"
    manifest = {
        "version": "1.0.0",
        "sha256": "0" * 64,
        "url": "https://releases.example.com/release.tar.gz",
    }
    opener = SimpleNamespace(
        open=lambda url, **kw: io.BytesIO(
            json.dumps(manifest).encode() if url.endswith("json") else data
        )
    )
    monkeypatch.setattr(installer.urllib.request, "build_opener", lambda *a: opener)
    monkeypatch.setattr(
        sys, "argv", ["bootstrap", "https://releases.example.com/manifest.json", str(tmp_path)]
    )
    with pytest.raises(SystemExit, match="checksum mismatch"):
        exec(compile(script, "install.sh bootstrap", "exec"), {})
    assert not (tmp_path / "release").exists()


def test_bootstrap_extracts_without_tarfile_filter_support(tmp_path, monkeypatch):
    script = (ROOT / "deploy/system_install.sh").read_text().split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        content = b"# verified release installer\n"
        member = tarfile.TarInfo("deploy/public_install.py")
        member.mode, member.size = 0o7777, len(content)
        tar.addfile(member, io.BytesIO(content))
    data = raw.getvalue()
    manifest = {
        "version": "1.0.0",
        "sha256": hashlib.sha256(data).hexdigest(),
        "url": "https://releases.example.com/release.tar.gz",
    }
    opener = SimpleNamespace(
        open=lambda url, **kw: io.BytesIO(
            json.dumps(manifest).encode() if url.endswith("json") else data
        )
    )
    monkeypatch.setattr(installer.urllib.request, "build_opener", lambda *a: opener)
    monkeypatch.setattr(
        sys, "argv", ["bootstrap", "https://releases.example.com/manifest.json", str(tmp_path)]
    )

    def old_extractall(self, path=".", members=None, *, numeric_owner=False):
        raise AssertionError("bootstrap must not depend on stdlib extraction filters")

    monkeypatch.setattr(tarfile.TarFile, "extractall", old_extractall)
    exec(compile(script, "install.sh bootstrap", "exec"), {})
    installed = tmp_path / "release/deploy/public_install.py"
    assert installed.read_bytes() == content
    assert installed.stat().st_mode & 0o7777 == 0o755


def test_bootstrap_rejects_archive_path_escape(tmp_path, monkeypatch):
    script = (ROOT / "deploy/system_install.sh").read_text().split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        info = tarfile.TarInfo("../escaped")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    data = raw.getvalue()
    manifest = {
        "version": "1.0.0",
        "sha256": hashlib.sha256(data).hexdigest(),
        "url": "https://releases.example.com/release.tar.gz",
    }
    opener = SimpleNamespace(
        open=lambda url, **kw: io.BytesIO(
            json.dumps(manifest).encode() if url.endswith("json") else data
        )
    )
    monkeypatch.setattr(installer.urllib.request, "build_opener", lambda *a: opener)
    monkeypatch.setattr(
        sys, "argv", ["bootstrap", "https://releases.example.com/manifest.json", str(tmp_path)]
    )
    with pytest.raises(SystemExit, match="Unsafe release"):
        exec(compile(script, "install.sh bootstrap", "exec"), {})
    assert not (tmp_path / "escaped").exists()


def test_failed_first_build_can_resume_without_overwriting_proxy(
    host, tmp_path, monkeypatch, capsys
):
    layout, _, _, runner, probe = host

    def failed_build(argv, **kwargs):
        if argv[:2] == ["docker", "build"]:
            raise subprocess.CalledProcessError(1, argv)
        return runner(argv, **kwargs)

    assert initial(host, tmp_path, runner=failed_build) == 1
    assert not json.loads(layout.config.read_text())["ready"]
    assert "Link code" not in capsys.readouterr().out
    # Package installation may already have created its default Caddyfile.
    layout.system_caddy.parent.mkdir(parents=True)
    layout.system_caddy.write_text("# package default stays untouched\n")
    tree = tmp_path / "release-1.0.0"
    monkeypatch.setattr(installer, "download_release", lambda *a: ({"version": "1.0.0"}, tree))
    assert installer.main([], layout=layout, runner=runner, probe=probe) == 0
    assert json.loads(layout.config.read_text())["ready"]
    assert layout.system_caddy.read_text() == "# package default stays untouched\n"


def test_retry_after_https_failure_restarts_running_service_on_new_release(
    host, tmp_path, monkeypatch
):
    layout, calls, _, runner, _ = host
    running_version = None

    def track_running(argv, **kwargs):
        nonlocal running_version
        if argv == ["systemctl", "restart", installer.SERVICE]:
            running_version = installer.updater.current_version(layout.root)
        return runner(argv, **kwargs)

    def first_probe(url, key, version):
        assert running_version == version
        if url.startswith("https:"):
            raise installer.InstallError("DNS not ready")

    assert initial(host, tmp_path, runner=track_running, probe=first_probe) == 1
    new = release(tmp_path, "1.0.1")
    monkeypatch.setattr(installer, "download_release", lambda *a: ({"version": "1.0.1"}, new))
    calls.clear()

    def ready(url, key, version):
        assert running_version == version == "1.0.1"

    assert installer.main([], layout=layout, runner=track_running, probe=ready) == 0
    assert ["systemctl", "restart", installer.SERVICE] in calls


def test_health_requires_authenticated_resource_even_if_health_is_public(monkeypatch):
    requests = []

    class Response:
        def __init__(self, status, body):
            self.status, self.body = status, body

        def read(self, limit):
            return json.dumps(self.body).encode()

    class Connection:
        def __init__(self, *a, **kw):
            pass

        def request(self, method, path, headers):
            requests.append((path, headers["Authorization"]))

        def getresponse(self):
            path, auth = requests[-1]
            if path == "/api/health":
                return Response(200, {"ok": True, "version": "1.0.0"})
            return Response(200 if auth == "Bearer correct" else 401, [])

        def close(self):
            pass

    monkeypatch.setattr(installer.http.client, "HTTPSConnection", Connection)
    assert not installer.health("https://owned.example.com", "wrong", "1.0.0")
    assert installer.health("https://owned.example.com", "correct", "1.0.0")
    assert ("/api/bots", "Bearer wrong") in requests


@pytest.mark.parametrize("piped", [True, False])
def test_bootstrap_requests_sudo_and_preserves_payload_and_arguments(tmp_path, piped):
    commands = tmp_path / "bin"
    commands.mkdir()
    capture = tmp_path / "sudo.json"
    for name, body in {
        "uname": "#!/bin/sh\necho Linux\n",
        "id": "#!/bin/sh\necho 1000\n",
        "sudo": (
            f"#!{sys.executable}\nimport json, sys\n"
            f"open({str(capture)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
            "sys.exit(23)\n"
        ),
    }.items():
        path = commands / name
        path.write_text(body)
        path.chmod(0o755)
    env = dict(os.environ, PATH=f"{commands}:/usr/bin:/bin", HOME=str(tmp_path))
    env.pop("SUDO_USER", None)
    env["HARNESS_RELEASE_MANIFEST"] = "https://releases.example.com/custom.json"
    env["HARNESS_INSTALL_DIR"] = str(tmp_path / "source checkout")
    args = ["--domain", "bots.example.com", "--no-auto-update"]
    command = ["/bin/bash", "-s", "--", *args] if piped else [
        "/bin/bash", str(ROOT / "deploy/system_install.sh"), *args
    ]
    result = subprocess.run(
        command, input=(ROOT / "deploy/system_install.sh").read_text() if piped else None,
        env=env, text=True, capture_output=True,
    )
    assert capture.exists(), result.stderr
    assert result.returncode == 23  # A denied/failed sudo must stop installation.
    elevated = json.loads(capture.read_text())
    assert elevated[:2] == ["/bin/bash", "-c"]
    assert elevated[4:6] == [env["HARNESS_RELEASE_MANIFEST"], env["HARNESS_INSTALL_DIR"]]
    assert elevated[6:] == args
    # Replay only --help: proves the complete script survived the pipe and is
    # valid after serialization, without invoking real sudo or host operations.
    replay = subprocess.run(
        [*elevated[:6], "--help"], env=env, text=True, capture_output=True,
    )
    assert replay.returncode == 0, replay.stderr
    assert "curl -fsSL https://dotobot.com/install.sh | bash" in replay.stdout
    assert "Release checksum mismatch" in elevated[2]


def test_bootstrap_help_needs_no_sudo():
    result = subprocess.run(
        ["/bin/bash", str(ROOT / "deploy/system_install.sh"), "--help"],
        text=True, capture_output=True,
    )
    assert result.returncode == 0
    assert "| bash" in result.stdout
