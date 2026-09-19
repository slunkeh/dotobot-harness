#!/usr/bin/env python3
"""Public-IP or own-domain HTTPS installation and owner-controlled updates.

Only install.sh bootstraps OS prerequisites. This module is also the installed
`dotobot-server` command. It never adopts a managed tenant or edits its services.
"""

from __future__ import annotations

import argparse
import fcntl
import filecmp
import hashlib
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

import updater

DEFAULT_MANIFEST = "https://releases.dotobot.com/harness/manifest.json"
SERVICE = "dotobot-server.service"
TIMER = "dotobot-server-update.timer"
IMAGE = "dotobot-server-machine"
CADDY_VERSION = "2.11.4"
CADDY_PACKAGES = {
    "amd64": "c41708ffb4af9bc6d19f7d22a7a034804352a21ecc62e1d3dfe3d58e30b38a3e",
    "arm64": "aeab2e38bf77a0162611a1703a5e16c09475b000d41f7edaa9337734d16642fd",
}


class InstallError(RuntimeError):
    pass


class Layout:
    def __init__(self, prefix: Path = Path("/")):
        self.prefix = prefix
        self.root = prefix / "opt/harness"
        self.home = prefix / "var/lib/harness"
        self.config_dir = prefix / "etc/dotobot-server"
        self.config = self.config_dir / "install.json"
        self.units = prefix / "etc/systemd/system"
        self.system_caddy = prefix / "etc/caddy/Caddyfile"
        self.caddy = self.config_dir / "Caddyfile"
        self.wrapper = prefix / "usr/local/bin/dotobot-server"


def domain_name(value: str) -> str:
    value = value.strip().lower().rstrip(".")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise InstallError("Use a DNS domain name pointing to this server, not an IP address.")
    if (
        len(value) > 253
        or "." not in value
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in value.split(".")
        )
    ):
        raise InstallError(
            "Use a domain name such as bots.example.com (without https:// or a path)."
        )
    return value


def public_ip(value: str) -> str:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise InstallError("Use a public IPv4 or IPv6 address without a port or path.") from exc
    if not address.is_global or address.is_multicast or address.is_reserved or "%" in value:
        raise InstallError(
            "IP HTTPS needs a public Internet address reachable on ports 80 and 443."
        )
    return address.compressed


def server_address(value: str) -> str:
    try:
        ipaddress.ip_address(value.strip().removeprefix("[").removesuffix("]"))
    except ValueError:
        return domain_name(value)
    return public_ip(value)


def public_origin(address: str) -> str:
    return "https://" + (f"[{address}]" if ":" in address else address)


def caddy_config(address: str) -> str:
    issuer = ""
    options = ""
    try:
        ipaddress.ip_address(address)
    except ValueError:
        pass
    else:
        # Bare IP sites otherwise get Caddy's local CA, which app devices do
        # not trust. Public IP certificates require Let's Encrypt's profile.
        # IP clients omit SNI; the public address can differ from the local
        # interface behind port forwarding, so select its certificate explicitly.
        options = f"{{\n    default_sni {address}\n}}\n\n"
        issuer = """    tls {
        issuer acme https://acme-v02.api.letsencrypt.org/directory {
            profile shortlived
        }
    }
"""
    return (
        "# Dotobot HTTPS proxy; owner edits are retained.\n"
        + options
        + public_origin(address)
        + " {\n"
        + issuer
        + "    reverse_proxy 127.0.0.1:8765\n}\n"
    )


def platform_check() -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise InstallError("Run the Linux server installer as root using sudo.")
    values = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip('"')
    supported = (values.get("ID"), values.get("VERSION_ID")) in {
        ("ubuntu", "24.04"),
        ("debian", "12"),
    }
    if not supported or os.uname().machine not in {"x86_64", "aarch64", "arm64"}:
        raise InstallError("Supported server systems: Ubuntu 24.04 / Debian 12, amd64 or arm64.")


def run(args: list[str], **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def write_private(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            os.fchmod(out.fileno(), mode)
            out.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_new(path: Path, text: str, mode: int = 0o644) -> None:
    # User edits to an installed service or environment are retained on reruns.
    if not path.exists():
        write_private(path, text, mode)


def load_config(layout: Layout) -> dict | None:
    if not layout.config.exists():
        return None
    data = json.loads(layout.config.read_text())
    if data.get("installer") != "dotobot-self-hosted-v1":
        raise InstallError("Unrecognized installer record; existing files were left unchanged.")
    # Keep the original record key so existing domain installations need no migration.
    server_address(data["domain"])
    return data


def origin(url: str) -> tuple:
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment:
        raise InstallError("Release URLs must use HTTPS without credentials.")
    return p.scheme, p.netloc.lower()


class ReleaseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if origin(req.full_url) != origin(newurl):
            raise InstallError("Release redirect left its trusted origin.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class CaddyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"github.com", "release-assets.githubusercontent.com"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
        ):
            raise InstallError("Caddy package redirect left its official HTTPS release hosts.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def caddy_version_ok(value: str) -> bool:
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:\s|$)", value.strip())
    return bool(match and tuple(map(int, match.groups())) >= (2, 10, 0))


def ensure_caddy(*, runner=run) -> None:
    binary = shutil.which("caddy")
    if binary == "/usr/bin/caddy":
        installed = runner([binary, "version"], capture_output=True, text=True)
        if caddy_version_ok(installed.stdout):
            return
    architecture = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(os.uname().machine)
    if architecture not in CADDY_PACKAGES:
        raise InstallError("No verified Caddy package for this server architecture.")
    filename = f"caddy_{CADDY_VERSION}_linux_{architecture}.deb"
    url = f"https://github.com/caddyserver/caddy/releases/download/v{CADDY_VERSION}/{filename}"
    opener = urllib.request.build_opener(CaddyRedirects())
    with opener.open(url, timeout=60) as response:
        data = response.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024:
        raise InstallError("Caddy package exceeds its download limit.")
    if hashlib.sha256(data).hexdigest() != CADDY_PACKAGES[architecture]:
        raise InstallError("Caddy package checksum does not match the reviewed official release.")
    with tempfile.TemporaryDirectory(prefix="dotobot-caddy-") as temporary:
        directory = Path(temporary)
        directory.chmod(0o755)  # Permit apt's unprivileged package reader.
        package = directory / filename
        package.write_bytes(data)
        package.chmod(0o644)
        runner(
            [
                "apt-get",
                "install",
                "-y",
                "--no-install-recommends",
                "-o",
                "Dpkg::Options::=--force-confold",
                str(package),
            ]
        )
    installed = runner(["/usr/bin/caddy", "version"], capture_output=True, text=True)
    if not caddy_version_ok(installed.stdout):
        raise InstallError("Caddy 2.10 or newer is required for public-IP HTTPS.")


def download_release(manifest_url: str, root: Path) -> tuple[dict, Path]:
    """TLS-authenticated same-origin manifest/archive, then checksum verification."""
    origin(manifest_url)
    opener = urllib.request.build_opener(ReleaseRedirects())
    with opener.open(manifest_url, timeout=30) as response:
        raw = response.read(updater.MAX_MANIFEST_BYTES + 1)
    if len(raw) > updater.MAX_MANIFEST_BYTES:
        raise InstallError("Release manifest is too large.")
    manifest = json.loads(raw)
    updater.parse_version(manifest.get("version"))
    if not re.fullmatch(r"[a-fA-F0-9]{64}", str(manifest.get("sha256", ""))):
        raise InstallError("Invalid release checksum.")
    manifest["sha256"] = manifest["sha256"].lower()
    if origin(manifest["url"]) != origin(manifest_url):
        raise InstallError("Release archive must use the manifest origin.")
    # The existing unpacker validates the digest, extraction and source version.
    # Its network opener is scoped to this operation (the installer is single-threaded).
    previous = urllib.request._opener
    try:
        urllib.request.install_opener(opener)
        tree = updater.fetch_release(manifest, root)
    finally:
        urllib.request._opener = previous
    return manifest, tree


def check_conflicts(layout: Layout) -> None:
    occupied = [
        p
        for p in (
            layout.root / "current",
            layout.home,
            layout.caddy,
            layout.system_caddy,
            layout.units / SERVICE,
            layout.units / "harness.service",
            layout.wrapper,
        )
        if p.exists() or p.is_symlink()
    ]
    if occupied:
        raise InstallError(
            "Existing installation/proxy files were left unchanged: "
            + ", ".join(map(str, occupied))
        )
    for port in (80, 443, 8765):
        with socket.socket() as sock:
            try:
                sock.bind(("0.0.0.0", port))
            except OSError as exc:
                raise InstallError(
                    f"Port {port} is already in use. Existing services were left unchanged."
                ) from exc


def service_files(layout: Layout, config: dict) -> None:
    # Caddy runs as its own user and must traverse this installer-owned directory,
    # even when root invoked setup with umask 077. Secrets remain mode 0600.
    layout.config_dir.chmod(0o755)
    env_file = layout.config_dir / "server.env"
    write_new(
        env_file,
        (
            f"HARNESS_HOME={layout.home}\nPYTHONPATH={layout.root}/current\n"
            f"HARNESS_PUBLIC_URL={public_origin(config['domain'])}\n"
            f"HARNESS_MACHINE_IMAGE={IMAGE}\n"
            "HARNESS_MACHINE_MEMORY=4g\nHARNESS_MACHINE_CPUS=2\nHARNESS_BROWSER_IDLE_MINUTES=30\n"
        ),
        0o600,
    )
    write_new(layout.home / "public-url", public_origin(config["domain"]) + "\n", 0o600)
    write_new(
        layout.units / SERVICE,
        f"""[Unit]
Description=Dotobot self-hosted server
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
Type=simple
WorkingDirectory={layout.root}/current
EnvironmentFile={env_file}
ExecStart=/usr/bin/python3 -m harness serve --host 127.0.0.1 --port 8765 --up
Restart=on-failure
RestartSec=2
RestartPreventExitStatus=78
KillMode=process
TimeoutStopSec=60
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths={layout.home} {layout.root}

[Install]
WantedBy=multi-user.target
""",
    )
    write_new(
        layout.units / "dotobot-server-update.service",
        f"""[Unit]
Description=Update the self-hosted Dotobot server
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {layout.root}/current/deploy/public_install.py --update --quiet
TimeoutStartSec=3600
""",
    )
    write_new(
        layout.units / TIMER,
        """[Unit]
Description=Optional daily Dotobot server updates
[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true
[Install]
WantedBy=timers.target
""",
    )
    write_new(
        layout.units / "caddy.service.d/dotobot.conf",
        f"[Service]\nExecStart=\nExecStart=/usr/bin/caddy run --environ --config {layout.caddy}\nExecReload=\nExecReload=/usr/bin/caddy reload --config {layout.caddy} --force\n",
    )
    write_new(
        layout.wrapper,
        f"""#!/bin/sh
set -eu
export HARNESS_HOME={layout.home}
exec /usr/bin/python3 {layout.root}/current/deploy/public_install.py "$@"
""",
        0o755,
    )


def health(url: str, key: str, version: str) -> bool:
    """No redirects: a health probe never forwards the bearer to another URL."""
    p = urllib.parse.urlsplit(url)
    conn_type = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
    connection = conn_type(p.hostname, p.port, timeout=5)
    try:
        connection.request("GET", "/api/health", headers={"Authorization": f"Bearer {key}"})
        response = connection.getresponse()
        raw = response.read(1 << 20)
        body = json.loads(raw) if response.status == 200 else {}
        if body.get("ok") is not True or body.get("version") != version:
            return False
        # Also prove access to an authenticated resource; a future public health
        # endpoint must not make a bad/stale linking key appear ready.
        connection.request("GET", "/api/bots", headers={"Authorization": f"Bearer {key}"})
        response = connection.getresponse()
        response.read(1 << 20)
        return response.status == 200
    except (OSError, ValueError, http.client.HTTPException, ssl.SSLError):
        return False
    finally:
        connection.close()


def wait_health(url: str, key: str, version: str, *, timeout: float = 120) -> None:
    until = time.monotonic() + timeout
    while True:
        if health(url, key, version):
            return
        if time.monotonic() >= until:
            raise InstallError(
                f"Health check failed at {url}. Check DNS, inbound TCP 80/443 and the server/Caddy journals. No link code was printed."
            )
        time.sleep(2)


def install_release(layout: Layout, tree: Path, *, runner=run) -> str:
    version = updater.release_version(tree)
    if version is None:
        raise InstallError("Release has no valid harness version.")
    updater.parse_version(version)
    dest = layout.root / "releases" / version
    if dest.resolve() != tree.resolve():
        if dest.exists():
            expected = {p.relative_to(tree) for p in tree.rglob("*") if p.is_file()}
            if any(
                not (dest / p).is_file() or not filecmp.cmp(tree / p, dest / p, shallow=False)
                for p in expected
            ):
                raise InstallError(
                    f"A different release already exists at {dest}; refusing to overwrite it."
                )
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(tree, dest)
    # Build under a versioned tag before touching the active image or service.
    runner(
        [
            "docker",
            "build",
            "-t",
            f"{IMAGE}:{version}",
            "-f",
            str(dest / "deploy/Dockerfile.machine"),
            str(dest),
        ]
    )
    runner(
        ["docker", "image", "inspect", f"{IMAGE}:{version}"],
        stdout=subprocess.DEVNULL,
    )
    return version


def apply_release(
    layout: Layout, tree: Path, config: dict, *, runner=run, probe=wait_health
) -> str:
    previous = updater.current_version(layout.root) if config.get("ready") else None
    version = updater.release_version(tree)
    if previous and updater.parse_version(version) < updater.parse_version(previous):
        raise InstallError(
            "Refusing to downgrade a self-hosted server. Use a reviewed manual rollback."
        )
    version = install_release(layout, tree, runner=runner)
    old_image = None
    if previous:
        result = runner(
            ["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
        )
        old_image = result.stdout.strip()
    key = (layout.home / "link-key").read_text().strip()
    if previous and (tree / "harness/update_state.py").exists():
        # The first service bridge must also preserve the stopped roster. Old
        # controllers do not record explicit stop markers, so capture live
        # records before activating the bridge. Never infer all roster bots ran.
        # This installer can still be running from a pre-bridge release; do
        # not import new runtime modules before the new source is selected.
        journal = layout.home / "update-operation.json"
        with (layout.home / "update.lock").open("a") as bridge_lock:
            fcntl.flock(bridge_lock, fcntl.LOCK_EX)
            existing = json.loads(journal.read_text()) if journal.exists() else {}
            if existing and existing.get("stage") not in {"complete", "cancelled"}:
                raise InstallError("An existing fleet operation must be resolved before bridging")
            running = []
            for record in (layout.home / "run").glob("*.json"):
                data = json.loads(record.read_text())
                if not data.get("bot") or not data.get("pid"):
                    continue
                try:
                    os.kill(int(data["pid"]), 0)
                except ProcessLookupError:
                    continue
                running.append(data["bot"])
            write_private(journal, json.dumps({
                "id": "service-bridge", "stage": "rolling", "bots": [],
                "target": {"version": version}, "previous_version": previous,
                "running_before": running, "started_at": time.time(),
                "updated_at": time.time(),
            }))
    try:
        runner(["docker", "tag", f"{IMAGE}:{version}", IMAGE])
        updater.flip_current(layout.root, version)
        runner(["systemctl", "restart", SERVICE])
        probe("http://127.0.0.1:8765", key, version)
        probe(public_origin(config["domain"]), key, version)
    except Exception as failure:
        if previous:
            try:
                updater.flip_current(layout.root, previous)
                if old_image:
                    runner(["docker", "tag", old_image, IMAGE])
                runner(["systemctl", "restart", SERVICE])
                probe("http://127.0.0.1:8765", key, previous)
                probe(public_origin(config["domain"]), key, previous)
            except Exception as rollback_failure:
                raise InstallError(
                    f"Update failed ({failure}); the previous release was selected but failed "
                    f"readiness ({rollback_failure}). Manual recovery is required. Server data "
                    "was retained; a newer database schema may need a compatible release."
                ) from failure
        raise
    return version


def finish(layout: Layout, config: dict, args, *, runner=run, probe=wait_health) -> None:
    from update_service import install as install_update_worker
    install_update_worker(layout, config, write=write_private, runner=runner)
    version = updater.current_version(layout.root)
    if not version:
        raise InstallError("No installed release found.")
    key = (layout.home / "link-key").read_text().strip()
    if not args.quiet:
        probe(public_origin(config["domain"]), key, version)
    if args.auto_update is not None or not config.get("ready"):
        if args.auto_update is not None:
            config["auto_update"] = args.auto_update
        runner(["systemctl", "enable" if config.get("auto_update") else "disable", "--now", TIMER])
        config["ready"] = True
        write_private(layout.config, json.dumps(config, indent=2) + "\n")
    if args.quiet:
        return
    sys.path.insert(0, str(layout.root / "current"))
    from harness.linking import link_code

    print(f"Dotobot {version} is ready at {public_origin(config['domain'])}")
    code = link_code(public_origin(config["domain"]), key)
    print(f"Link code: {code}")
    print("Keep this code private. Paste it into Add server in the Dotobot app.")
    print(
        "Updates: "
        + (
            "automatic daily updates enabled"
            if config.get("auto_update")
            else "manual; run sudo dotobot-server --update"
        )
    )


def main(argv=None, *, layout: Layout | None = None, runner=run, probe=wait_health) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    address = parser.add_mutually_exclusive_group()
    address.add_argument("--domain", help="optional DNS name pointing to this server")
    address.add_argument("--ip", help="public IPv4 or IPv6 address; no domain needed")
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--link", action="store_true")
    policy = parser.add_mutually_exclusive_group()
    policy.add_argument("--auto-update", dest="auto_update", action="store_true")
    policy.add_argument("--no-auto-update", dest="auto_update", action="store_false")
    parser.set_defaults(auto_update=None)
    parser.add_argument("--quiet", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--release-tree", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--release-manifest", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--manifest-url", default=os.environ.get("HARNESS_RELEASE_MANIFEST"), help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    layout = layout or Layout()
    try:
        requested = (
            domain_name(args.domain)
            if args.domain is not None
            else public_ip(args.ip)
            if args.ip is not None
            else None
        )
        platform_check()
        layout.config_dir.mkdir(parents=True, exist_ok=True)
        with (layout.config_dir / "update.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InstallError("Another Dotobot install or update is running.") from exc
            config = load_config(layout)
            if config is None:
                if args.update or args.link:
                    raise InstallError(
                        "No self-hosted installer record found; no existing server was changed."
                    )
                check_conflicts(layout)
                value = requested
                if value is None:
                    with open("/dev/tty", "r+") as tty:
                        tty.write("Public IP address or optional domain pointing to this server: ")
                        tty.flush()
                        value = tty.readline().strip()
                config = {
                    "installer": "dotobot-self-hosted-v1",
                    "domain": server_address(value),
                    "manifest_url": args.manifest_url or DEFAULT_MANIFEST,
                    "auto_update": bool(args.auto_update),
                    "ready": False,
                }
                origin(config["manifest_url"])
                socket.getaddrinfo(config["domain"], 443, type=socket.SOCK_STREAM)
                # This marks our own partial installation before any package changes.
                # Reruns can resume failed downloads/builds without adopting other hosts.
                write_private(layout.config, json.dumps(config, indent=2) + "\n")
            if requested and requested != config["domain"]:
                raise InstallError(
                    "The installed address was retained. Changing it requires a reviewed proxy and connection migration."
                )
            if args.manifest_url and args.manifest_url != config["manifest_url"]:
                raise InstallError(
                    "The installed release source was retained; edit install.json explicitly to change trust."
                )
            if not config.get("ready"):
                # Minimal Debian images can enable AppArmor in the kernel but
                # omit its parser. Docker needs it to load the container profile.
                packages = ["python3", "curl", "ca-certificates", "apparmor"]
                # Reuse an existing engine, including Docker CE; installing the
                # distro package over it can remove conflicting owner packages.
                if shutil.which("docker") is None:
                    packages.append("docker.io")
                runner(["apt-get", "update"])
                runner(["apt-get", "install", "-y", "--no-install-recommends", *packages])
                ensure_caddy(runner=runner)
                runner(["systemctl", "enable", "--now", "docker"])
                runner(["docker", "info"], stdout=subprocess.DEVNULL)
                tree = args.release_tree
                if tree is None:
                    _, tree = download_release(config["manifest_url"], layout.root)
                elif args.release_manifest is None or updater.release_version(tree) != json.loads(
                    args.release_manifest.read_text()
                ).get("version"):
                    raise InstallError("Bootstrap archive version does not match its manifest.")
                version = install_release(layout, tree, runner=runner)
                updater.flip_current(layout.root, version)
                # Create before service launch: its first-run journal never sees a key.
                sys.path.insert(0, str(layout.root / "current"))
                from harness.linking import get_or_create_key
                from harness.paths import HarnessPaths

                get_or_create_key(HarnessPaths.resolve(layout.home))
                service_files(layout, config)
                write_new(
                    layout.caddy,
                    caddy_config(config["domain"]),
                )
                runner(
                    [
                        "/usr/bin/caddy",
                        "validate",
                        "--config",
                        str(layout.caddy),
                        "--adapter",
                        "caddyfile",
                    ]
                )
                runner(["docker", "tag", f"{IMAGE}:{version}", IMAGE])
                runner(["systemctl", "daemon-reload"])
                runner(["systemctl", "enable", SERVICE])
                runner(["systemctl", "restart", SERVICE])
                runner(["systemctl", "enable", "--now", "caddy"])
                runner(["systemctl", "restart", "caddy"])
                key = (layout.home / "link-key").read_text().strip()
                probe("http://127.0.0.1:8765", key, version)
                probe(public_origin(config["domain"]), key, version)
            elif args.update:
                worker = layout.config_dir / "update-host.json"
                if worker.exists():
                    manifest = updater.load_manifest(config["manifest_url"])
                    if manifest["version"] == updater.current_version(layout.root):
                        finish(layout, config, args, runner=runner, probe=probe)
                        return 0
                    key = (layout.home / "link-key").read_text().strip()
                    request = urllib.request.Request(
                        "http://127.0.0.1:8765/api/updates/start",
                        data=json.dumps({"version": manifest["version"]}).encode(),
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(request, timeout=60) as response:
                        operation = json.load(response)
                    print(f"Update {operation.get('id')}: {operation.get('stage')}; progress is available in the apps.")
                else:
                    # Bridge installation: preserve agents under KillMode=process.
                    _, tree = download_release(config["manifest_url"], layout.root)
                    if updater.release_version(tree) != updater.current_version(layout.root):
                        apply_release(layout, tree, config, runner=runner, probe=probe)
            finish(layout, config, args, runner=runner, probe=probe)
            return 0
    except (
        InstallError,
        updater.UpdateError,
        OSError,
        ValueError,
        KeyError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"Setup incomplete: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
