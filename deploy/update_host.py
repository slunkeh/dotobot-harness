#!/usr/bin/env python3
"""Host-side worker, independent of the controller being updated.

Run with a root-owned JSON config: root, home, manifest_url, service, image.
Docker mode additionally requires container and the persistent supervisor.
No command or artifact URL is accepted from an API request.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy import updater  # noqa: E402
from harness import update_state as state  # noqa: E402
from harness.fsutil import write_atomic  # noqa: E402
from harness.paths import HarnessPaths  # noqa: E402
from harness.runtime_identity import identity  # noqa: E402


class AppReleasePoller:
    """Refresh client pins independently of owner-requested runtime upgrades."""

    def __init__(self, config):
        self.config = config
        self.next_check = 0.0

    def poll(self, *, now=None):
        now = time.monotonic() if now is None else now
        if now < self.next_check:
            return
        # Back off on failures too; do not hammer an unavailable release feed.
        self.next_check = now + 300
        try:
            manifest = updater.load_manifest(self.config["manifest_url"])
            latest = manifest.get("latest_app_version")
            if latest is None:
                return  # Runtime-only feeds do not erase existing app pins.
            updater.parse_version(latest)
            if not updater.https_only(manifest.get("app_download_url")) or not updater._hex_digest(
                manifest.get("app_sha256")
            ):
                raise ValueError("App release requires an HTTPS download and SHA-256")
            updater.write_app_release(Path(self.config["home"]), manifest)
        except Exception as exc:
            from harness.redaction import scrub

            print(f"App release check failed: {scrub(str(exc))}", file=sys.stderr)


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True)


def version_tag(image, version):
    # Preserve an existing tag (and any registry port) without a second colon.
    return image + ("-" if ":" in image.rsplit("/", 1)[-1] else ":") + version


def restart(config, runner=run):
    if config.get("container"):
        # Supervisor owns this mailbox. Never restart/replace a container that
        # owns agents: their processes must survive controller-only changes.
        home = Path(config["home"])
        write_atomic(home / "controller-reload", str(time.time()))
    else:
        runner(["systemctl", "restart", config["service"]])


def supported(config, paths):
    if config.get("container"):
        supervisor = state.read_json(paths.home / "controller-supervisor.json")
        if time.time() - supervisor.get("time", 0) > 15:
            return (
                False,
                "This controller container needs the persistent-supervisor bridge migration before safe updates.",
            )
    return True, None


def probe_controller(config):
    paths = HarnessPaths.resolve(config["home"])
    info = state.read_json(paths.home / "serve.json")
    origin = config.get("health_url", f"http://127.0.0.1:{info.get('port', 8765)}")
    try:
        request = urllib.request.Request(
            origin.rstrip("/") + "/api/health",
            headers={"Authorization": f"Bearer {info.get('key', '')}"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response).get("version")
    except Exception:
        return None


def apply(config, *, runner=run, fetch=updater.fetch_release, health=None):
    paths = HarnessPaths.resolve(config["home"])
    root = Path(config["root"])
    available, reason = supported(config, paths)
    write_atomic(
        paths.home / "update-host.json",
        json.dumps(
            {
                "available": available,
                "reason": reason,
                "time": time.time(),
                "manifest_url": config["manifest_url"],
            }
        ),
    )
    if not available:
        return
    operation = state.read(paths)
    probe = health or (lambda: probe_controller(config))
    if operation.get("stage") == "installing_controller":
        with state.operation_lock(paths):
            if probe() == operation["target"]["version"]:
                operation["stage"] = "rolling"
            else:
                operation["stage"] = "needs_attention"
                operation["error"] = (
                    "Controller activation was interrupted. Verify the selected release before retrying."
                )
            state.save(paths, operation)
        return
    if operation.get("stage") != "preparing":
        return
    try:
        manifest = updater.load_manifest(config["manifest_url"])
        if manifest["version"] != operation["target"]["version"]:
            raise RuntimeError("Available release changed; check again before installing")
        previous = updater.current_version(root)
        if not previous or updater.parse_version(manifest["version"]) <= updater.parse_version(
            previous
        ):
            raise RuntimeError("Refusing an unreviewed downgrade or same-version replacement")
        tree = fetch(manifest, root)
        target_identity = identity(tree)
        declared = manifest.get("runtime_identity")
        if declared and declared != target_identity:
            raise RuntimeError("Release compatibility metadata does not match the verified archive")
        old_identity = identity((root / "current").resolve())
        if any(
            old_identity.get(key) is not None and old_identity[key] != target_identity[key]
            for key in ("protocol", "state")
        ):
            raise RuntimeError(
                "Release requires an incompatible state/protocol migration; fleet retained"
            )
        if config.get("container") and old_identity["controller"] != target_identity["controller"]:
            raise RuntimeError(
                "Controller base image changed; a stopped-fleet bridge migration is required"
            )
        # Missing metadata cannot authorize skipping a restart.
        operation["target"]["identity"] = target_identity if declared else None
        if declared:
            from harness.runtime_identity import compatible

            operation["bots"] = [
                row
                for row in operation["bots"]
                if not compatible(row.get("previous_identity"), declared)
            ]
        operation["previous_identity"] = old_identity
        operation["previous_version"] = previous
        machine_image = config.get("image", "agent-harness-machine")
        tag = version_tag(machine_image, manifest["version"])
        if not declared or old_identity["machine"] != target_identity["machine"]:
            runner(
                [
                    "docker",
                    "build",
                    "-t",
                    tag,
                    "-f",
                    str(tree / "deploy/Dockerfile.machine"),
                    str(tree),
                ]
            )
            runner(["docker", "image", "inspect", tag])
            # Versioned source and image exist before activation. The old image
            # remains tagged for recovery; never prune before fleet completion.
            # Persist the original immutable image ID before changing any alias.
            # A crash after alias activation must not relabel the new image as
            # the previous one on retry.
            if not operation.get("previous_machine_image"):
                inspected = runner(
                    ["docker", "image", "inspect", "--format", "{{.Id}}", machine_image]
                )
                operation["previous_machine_image"] = inspected.stdout.strip()
                if not operation["previous_machine_image"]:
                    raise RuntimeError("Cannot identify the previous machine image for rollback")
                with state.operation_lock(paths):
                    state.save(paths, operation)
            runner(
                [
                    "docker",
                    "tag",
                    operation["previous_machine_image"],
                    version_tag(machine_image, previous),
                ]
            )
            runner(["docker", "tag", tag, machine_image])
        with state.operation_lock(paths):
            operation["stage"] = "installing_controller"
            state.save(paths, operation)
        updater.flip_current(root, manifest["version"])
        restart(config, runner)
        if not updater.wait_healthy(manifest["version"], probe):
            # No fleet roll is allowed before this health gate. Restoring the
            # previous controller is safe only under the same state contract.
            if (
                declared
                and old_identity["state"] is not None
                and old_identity["state"] == target_identity["state"]
            ):
                updater.flip_current(root, previous)
                if not declared or old_identity["machine"] != target_identity["machine"]:
                    runner(["docker", "tag", version_tag(machine_image, previous), machine_image])
                restart(config, runner)
                if not updater.wait_healthy(previous, probe):
                    raise RuntimeError("Controller rollback needs manual recovery; data retained")
                operation["rollback"] = "controller_restored"
            raise RuntimeError("Target controller failed readiness; fleet update paused")
        with state.operation_lock(paths):
            operation["stage"] = "rolling"
            state.save(paths, operation)
        updater.write_app_release(paths.home, manifest)
    except Exception as exc:
        from harness.redaction import scrub

        with state.operation_lock(paths):
            operation["stage"] = "needs_attention"
            operation["error"] = scrub(str(exc))
            state.save(paths, operation)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    if args.config.stat().st_mode & 0o022 or args.config.stat().st_uid != 0:
        raise SystemExit("Updater config must be root-owned and not group/world writable")
    config = json.loads(args.config.read_text())
    paths = HarnessPaths.resolve(config["home"])
    paths.home.mkdir(parents=True, exist_ok=True)
    with (paths.home / "update-host.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        app_releases = AppReleasePoller(config)
        while True:
            apply(config)
            app_releases.poll()
            if not args.watch:
                break
            time.sleep(15)


if __name__ == "__main__":
    main()
