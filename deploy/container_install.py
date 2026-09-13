#!/usr/bin/env python3
"""Manage a containerised harness and its sibling computers using local Docker."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from public_install import caddy_config, domain_name, public_ip, public_origin, write_private

LABEL = "com.dotobot.installation"
FORMAT = "dotobot-containers-v1"
CADDY_IMAGE = "caddy:2.11.4"


class InstallError(RuntimeError):
    pass


def docker(*args: str, check: bool = True, capture: bool = True):
    if capture:
        return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)
    # Build chatter stays out of the result panel; retain diagnostics privately.
    log = os.environ.get("DOTOBOT_SETUP_LOG")
    with open(log, "a+") if log else tempfile.TemporaryFile(mode="w+") as output:
        process = subprocess.Popen(["docker", *args], stdout=output, stderr=output, text=True)
        tick = 0
        try:
            while True:
                try:
                    status = process.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if os.environ.get("DOTOBOT_INSTALL_TTY") == "1":
                        marker = " " * (tick % 16) + "===="
                        print(f"\r  [{marker:<20}] Working...", end="", flush=True)
                        tick += 1
        except BaseException:
            process.terminate()
            process.wait()
            raise
        if os.environ.get("DOTOBOT_INSTALL_TTY") == "1":
            print("\r" + " " * 60 + "\r", end="", flush=True)
        if status and check:
            output.seek(0)
            print("\n".join(output.read().splitlines()[-20:]), file=sys.stderr)
            raise subprocess.CalledProcessError(status, ["docker", *args])
        return subprocess.CompletedProcess(["docker", *args], status, "", "")


def inspect(kind: str, name: str) -> dict | None:
    result = docker(kind, "inspect", name, check=False)
    if result.returncode:
        # A missing object must not be confused with an unavailable daemon.
        if "No such" not in result.stderr and f"network {name} not found" not in result.stderr:
            raise InstallError(f"Cannot inspect {kind} {name}: {result.stderr.strip()}")
        return None
    return json.loads(result.stdout)[0]


def labels(info: dict) -> dict:
    return info.get("Config", {}).get("Labels") or info.get("Labels") or {}


def owned(kind: str, name: str, identity: str) -> dict | None:
    info = inspect(kind, name)
    if info and labels(info).get(LABEL) != identity:
        raise InstallError(
            f"Existing {kind} {name} belongs to another installation; left unchanged."
        )
    return info


def configuration(root: Path, address: str | None, port: int | None) -> dict:
    if (
        not root.is_absolute()
        or root.is_symlink()
        or str(root) in ("/", "/root", "/home", "/Users")
    ):
        raise InstallError("Use a dedicated absolute Dotobot directory.")
    if any(c in str(root) for c in (",", ":", "\n", "\r")):
        raise InstallError("Dotobot directory cannot contain commas, colons or newlines.")
    record = root / "install.json"
    if record.exists():
        config = json.loads(record.read_text())
        if config.get("format") != FORMAT or config.get("root") != str(root):
            raise InstallError("Unrecognised installation record; nothing was changed.")
        if address and address != config["address"]:
            raise InstallError("Existing address retained. Reconfigure your proxy explicitly.")
        if port is not None and port != config["port"]:
            raise InstallError("Existing port retained; supply its original --port value.")
        return config
    if (root / "state").exists():
        raise InstallError("Existing unregistered state was left unchanged.")
    identity = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    return dict(
        format=FORMAT,
        root=str(root),
        identity=identity,
        name=f"dotobot-{identity}",
        address=address,
        port=port or 8765,
        socket=os.environ.get("DOTOBOT_DOCKER_SOCKET", "/var/run/docker.sock"),
    )


def runtime_args(config: dict, image: str) -> list[str]:
    root = Path(config["root"])
    state = root / "state"
    name = config["name"]
    args = [
        "run",
        "-d",
        "--name",
        name,
        "--label",
        f"{LABEL}={config['identity']}",
        "--restart",
        "unless-stopped",
        "--stop-timeout",
        "240",
        "--init",
        "--network",
        name,
        "--network-alias",
        "harness",
        "--mount",
        f"type=bind,src={state},dst={state}",
        "--mount",
        f"type=bind,src={config['socket']},dst=/var/run/docker.sock",
        "-e",
        f"HARNESS_HOME={state}",
        "-e",
        f"HARNESS_MACHINE_PREFIX={name}-machine",
        "-e",
        f"HARNESS_MACHINE_IMAGE={config['machine_image']}",
        "-e",
        "HARNESS_MACHINE_MEMORY=4g",
        "-e",
        "HARNESS_MACHINE_CPUS=2",
    ]
    if config["address"]:
        args += ["-e", f"HARNESS_PUBLIC_URL={public_origin(config['address'])}"]
    else:
        args += ["-p", f"127.0.0.1:{config['port']}:8765"]
    return args + [image]


def ready(config: dict, timeout: float = 120) -> None:
    # Runs inside the API container. Never put a bearer on the Docker command
    # line, in docker inspect, or in installer output before health succeeds.
    probe = """import json, os, pathlib, sys, urllib.request
key=(pathlib.Path(os.environ['HARNESS_HOME'])/'link-key').read_text().strip()
for path in ('health','bots'):
 r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8765/api/'+path,headers={'Authorization':'Bearer '+key}),timeout=3)
 assert r.status==200
 if path=='health': assert json.load(r)['version']==sys.argv[1]
"""
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        result = docker(
            "exec", config["name"], "python", "-c", probe, config["version"], check=False
        )
        if result.returncode == 0:
            return
        info = inspect("container", config["name"])
        if (
            not info
            or not info.get("State", {}).get("Running")
            or info.get("State", {}).get("Restarting")
        ):
            raise InstallError("The controller exited before becoming ready.")
        time.sleep(1)
    raise InstallError("API health check failed; inspect the container logs locally.")


def ensure_network(config: dict) -> None:
    if not owned("network", config["name"], config["identity"]):
        docker("network", "create", "--label", f"{LABEL}={config['identity']}", config["name"])


def ensure_proxy(config: dict) -> None:
    if not config["address"]:
        return
    name = config["name"] + "-https"
    if owned("container", name, config["identity"]):
        docker("start", name)
        return
    root = Path(config["root"])
    proxy = root / "Caddyfile"
    if not proxy.exists():
        write_private(
            proxy, caddy_config(config["address"]).replace("127.0.0.1:8765", "harness:8765"), 0o644
        )
    docker(
        "run",
        "-d",
        "--name",
        name,
        "--label",
        f"{LABEL}={config['identity']}",
        "--restart",
        "unless-stopped",
        "--network",
        config["name"],
        "-p",
        "80:80",
        "-p",
        "443:443",
        "-p",
        "443:443/udp",
        "--mount",
        f"type=bind,src={proxy},dst=/etc/caddy/Caddyfile,readonly",
        "--mount",
        f"type=bind,src={root / 'https'},dst=/data",
        CADDY_IMAGE,
    )


def finish(config: dict) -> None:
    ready(config)
    root = Path(config["root"])
    key = (root / "state/link-key").read_text().strip()
    if config["address"]:
        from public_install import wait_health

        wait_health(public_origin(config["address"]), key, config["version"])
    from harness.linking import link_code

    url = (
        public_origin(config["address"])
        if config["address"]
        else f"http://127.0.0.1:{config['port']}"
    )
    code = link_code(url, key)
    print("\n  [####################] 5/5  Ready\n")
    print("  +----------------------------------------------------------+")
    print("  |  DOTOBOT IS READY                                        |")
    print("  +----------------------------------------------------------+")
    print(f"\n  Version  {config['version']}\n  Server   {url}")
    print("\n  YOUR PRIVATE LINK CODE\n")
    print("  " + code)
    print("\n  Paste this into Dotobot > Your servers > Link a server.")
    print("  Add your first server, or another alongside your existing ones.")
    print("  Keep this code private: it grants access to your server.")
    if not config["address"]:
        print("\n  Local connection only. From another computer, use an SSH")
        print("  tunnel or configure HTTPS with --ip / --domain.")


def install(config: dict, source: Path, image: str) -> None:
    root, name = Path(config["root"]), config["name"]
    identity = config["identity"]
    old = owned("container", name, identity)
    previous = name + "-previous"
    if owned("container", previous, identity):
        raise InstallError(
            "A previous update needs recovery; resolve the previous container first."
        )
    owned("network", name, identity)
    owned("container", name + "-https", identity)
    version_file = (source / "harness/version.py").read_text()
    version = re.search(r'__version__\s*=\s*[\'"]([0-9]+\.[0-9]+\.[0-9]+)', version_file)
    if not version:
        raise InstallError("Release has no valid version.")
    candidate = dict(config, version=version[1], image=image)
    tag = f"dotobot-machine:{identity}-{version[1]}"
    # Build before changing the running server. Save immutable image IDs so a
    # later build cannot change the old container's machine-image selection.
    print("\n  [############--------] 3/5  Building your bot computer", flush=True)
    print("  The first build can take several minutes.", flush=True)
    docker(
        "build",
        "-t",
        tag,
        "-f",
        str(source / "deploy/Dockerfile.machine"),
        str(source),
        capture=False,
    )
    candidate["machine_image"] = inspect("image", tag)["Id"]
    if candidate["address"]:
        docker("pull", CADDY_IMAGE, capture=False)
    print("\n  [################----] 4/5  Starting and checking your server", flush=True)
    (root / "state").mkdir(exist_ok=True, mode=0o700)
    (root / "https").mkdir(exist_ok=True, mode=0o700)
    ensure_network(candidate)
    # Persist an identifiable partial installation before launching resources.
    if not (root / "install.json").exists():
        write_private(root / "install.json", json.dumps(candidate))
        write_private(root / "manager-image", image + "\n", 0o644)
    if old:
        docker("stop", "--time", "240", name)
        docker("rename", name, previous)
    try:
        docker(*runtime_args(candidate, image))
        ensure_proxy(candidate)
        finish(candidate)
    except Exception:
        if owned("container", name, identity):
            docker("stop", "--time", "240", name)
            docker("rm", name)
        if old:
            docker("rename", previous, name)
            docker("start", name)
            ready(config)
            print("Previous server restored; data was retained.", file=sys.stderr)
        raise
    write_private(root / "install.json", json.dumps(candidate))
    write_private(root / "manager-image", image + "\n", 0o644)
    if old:
        docker("rm", previous)


def machines(config: dict) -> list[dict]:
    prefix = config["name"] + "-machine-"
    result = docker("ps", "-a", "--format", "{{.Names}}")
    found = []
    for name in result.stdout.splitlines():
        if not name.startswith(prefix) or not name[len(prefix) :].isdigit():
            continue
        info = inspect("container", name)
        if labels(info).get("com.agent-harness.machine") != "1":
            raise InstallError(f"Unowned bot container {name} was left unchanged.")
        found.append(info)
    return found


def uninstall(config: dict, delete_data: bool) -> None:
    root, identity = Path(config["root"]), config["identity"]
    names = [config["name"], config["name"] + "-previous", config["name"] + "-https"]
    for name in names:
        owned("container", name, identity)
    bots = machines(config)  # All ownership checks precede the first stop.
    # Remember only volumes observed on our labelled computers. A name prefix
    # alone is never authority to delete someone else's Docker volume.
    prefix = config["name"] + "-machine"
    volumes = set(config.get("volumes", []))
    for bot in bots:
        bot_name = bot["Name"].lstrip("/")
        for mount in bot.get("Mounts", []):
            if mount.get("Type") == "volume" and (
                (
                    mount.get("Name") == bot_name + "-home"
                    and mount.get("Destination") == "/home/agent"
                )
                or (
                    mount.get("Name") == prefix + "-workspace"
                    and mount.get("Destination") == "/workspace"
                )
            ):
                volumes.add(mount["Name"])
    if any(
        not re.fullmatch(re.escape(prefix) + r"-(?:[0-9]+-home|workspace)", name)
        for name in volumes
    ):
        raise InstallError("Invalid saved volume inventory; nothing was removed.")
    if (root / "install.json").exists():
        write_private(root / "install.json", json.dumps(dict(config, volumes=sorted(volumes))))
    for name in names:
        if owned("container", name, identity):
            docker("stop", "--time", "240", name)
            docker("rm", name)
    for bot in bots:
        name = bot["Name"].lstrip("/")
        docker("stop", "--time", "60", name)
        docker("rm", name)
    if owned("network", config["name"], identity):
        docker("network", "rm", config["name"])
    if delete_data:
        for volume in sorted(volumes):
            if inspect("volume", volume):
                # Docker refuses volumes mounted by ANY other container.
                docker("volume", "rm", volume)
        for path in (root / "state", root / "https"):
            if path.is_symlink():
                raise InstallError("Refusing to delete symlinked state.")
            if path.exists():
                shutil.rmtree(path)
        (root / "Caddyfile").unlink(missing_ok=True)
        (root / "install.json").unlink(missing_ok=True)
        print("Dotobot removed, including its data. Docker was retained.")
    else:
        print(
            f"Dotobot stopped and removed. Data retained in {root}; rerun the installer to restore it."
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--image")
    address = parser.add_mutually_exclusive_group()
    address.add_argument("--ip", type=public_ip)
    address.add_argument("--domain", type=domain_name)
    parser.add_argument("--port", type=int)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--update", action="store_true")
    action.add_argument("--link", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    parser.add_argument("--delete-data", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.delete_data and not args.uninstall:
            raise InstallError("--delete-data requires --uninstall.")
        if args.port is not None and not 1 <= args.port <= 65535:
            raise InstallError("Port must be between 1 and 65535.")
        args.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (args.root / "installer.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            config = configuration(args.root, args.ip or args.domain, args.port)
            if args.link or args.uninstall or args.update:
                if not (args.root / "install.json").exists():
                    raise InstallError("No container installation exists here.")
            if args.link:
                finish(config)
            elif args.uninstall:
                uninstall(config, args.delete_data)
            else:
                if not args.source or not args.image:
                    raise InstallError("Verified release source and server image are required.")
                install(config, args.source, args.image)
        return 0
    except (InstallError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Setup incomplete: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
