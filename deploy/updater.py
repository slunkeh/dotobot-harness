#!/usr/bin/env python3
"""Fleet updater: pull-based, staged harness updates for managed hosts.

Runs from a systemd timer (see deploy/systemd/harness-updater.*). Each tick:

1. Fetch the release manifest (HARNESS_RELEASE_MANIFEST, an S3/HTTPS JSON):
     {"version": "0.3.0",
      "url": "https://…/harness-0.3.0.tar.gz",
      "sha256": "…",
      "rollout_percent": 100,
      "min_app_version": "0.2.0",
      "latest_app_version": "0.3.0",
      "app_download_url": "https://…/Dotobot.zip"}
2. Wave gate: sha256(host id) % 100 < rollout_percent — canary by editing one
   number in the manifest.
3. If the version differs from the running release: download, verify sha256,
   unpack to releases/<version>, byte-compile check.
4. Write $HARNESS_HOME/app_release.json (served by /api/health + WS hello).
5. Atomically flip the `current` symlink and restart the harness service.
   The harness shuts down gracefully (SIGTERM → client notice) and the new
   server adopts the running bots, then rolls them when idle.
6. Health-check the new version; on failure flip back, restart, and mark the
   release .failed so future ticks skip it.

Bots are NOT stopped by the updater. State ($HARNESS_HOME) never lives
inside a release directory.

Stdlib only, functions take their effects (restart/health) as parameters so
tests inject fakes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

KEEP_RELEASES = 3
HEALTH_TIMEOUT = 30.0
#: Largest manifest document the updater will read (it is a few hundred bytes).
MAX_MANIFEST_BYTES = 1 << 20
#: A release version is MAJOR.MINOR.PATCH and nothing else — it becomes a
#: directory name under releases/ and is compared numerically for the
#: downgrade floor.
_VERSION_RE = re.compile(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}")
#: Set to 1 to let the manifest and tarball come from file:// or http://
#: (tests, an air-gapped mirror). The fleet default is https only.
INSECURE_ENV = "HARNESS_RELEASE_ALLOW_INSECURE"


class UpdateError(RuntimeError):
    pass


def _insecure_allowed() -> bool:
    return os.environ.get(INSECURE_ENV, "").strip().lower() in {"1", "true", "yes"}


def check_url(url: str, what: str) -> None:
    """Refuse a fetch that TLS does not protect.

    The manifest's sha256 only proves the tarball matches the manifest, so
    the transport is the authenticity check: both documents must arrive
    over https unless the operator explicitly allowed otherwise.
    """
    scheme = urllib.parse.urlsplit(str(url)).scheme.lower()
    if scheme == "https":
        return
    if scheme in {"file", "http"} and _insecure_allowed():
        return
    raise UpdateError(f"{what} must be an https URL (got {url!r}); set {INSECURE_ENV}=1 to allow")


def parse_version(text: object) -> tuple[int, int, int]:
    """MAJOR.MINOR.PATCH as a comparable tuple; UpdateError for anything else."""
    if not isinstance(text, str) or _VERSION_RE.fullmatch(text) is None:
        raise UpdateError(f"not a MAJOR.MINOR.PATCH release version: {text!r}")
    major, minor, patch = text.split(".")
    return int(major), int(minor), int(patch)


def same_origin(a: str, b: str) -> bool:
    ua, ub = urllib.parse.urlsplit(a), urllib.parse.urlsplit(b)
    return (ua.scheme.lower(), ua.netloc.lower()) == (ub.scheme.lower(), ub.netloc.lower())


def load_manifest(url: str) -> dict:
    check_url(url, "HARNESS_RELEASE_MANIFEST")
    with urllib.request.urlopen(url, timeout=30) as r:
        raw = r.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise UpdateError("manifest is larger than any release manifest should be")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise UpdateError(f"manifest is not a JSON object: {data!r}")
    if not data.get("version") or not data.get("url") or not data.get("sha256"):
        raise UpdateError(f"manifest missing version/url/sha256: {data!r}")
    parse_version(data["version"])
    check_url(data["url"], "manifest url")
    if not same_origin(url, data["url"]):
        raise UpdateError(
            f"manifest url {data['url']!r} is not on the manifest's own origin ({url!r})"
        )
    return data


def host_id() -> str:
    """Stable identity for wave gating: EC2 instance-id file if present, else hostname."""
    for path in ("/var/lib/cloud/data/instance-id",):
        try:
            text = Path(path).read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
    return socket.gethostname()


def in_wave(identity: str, percent) -> bool:
    try:
        pct = int(percent)
    except (TypeError, ValueError):
        pct = 100
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return int(digest, 16) % 100 < pct


def release_version(release_dir: Path) -> str | None:
    """Read harness/version.py without importing the release's code."""
    try:
        text = (release_dir / "harness" / "version.py").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"__version__\s*=\s*[\"']([^\"']+)[\"']", text)
    return m.group(1) if m else None


def current_version(root: Path) -> str | None:
    return release_version(root / "current")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_release(tar: tarfile.TarFile, destination: Path) -> None:
    """Extract only source files/directories, including on Debian's Python 3.11.

    That stdlib does not provide extraction filters. Copying regular members
    explicitly also avoids archive ownership, special modes, and link targets.
    The destination is a fresh staging directory, never an installed home.
    """
    members = tar.getmembers()
    if len(members) > 10000 or sum(m.size for m in members) > 512 * 1048576:
        raise UpdateError("release archive exceeds its unpacking limit")
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isfile()):
            raise UpdateError("unsafe release archive member")
    destination.mkdir(parents=True, exist_ok=True)
    for member in members:
        target = destination / member.name
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(member.mode & 0o755)


def fetch_release(manifest: dict, root: Path) -> Path:
    """Download + verify + unpack releases/<version>; idempotent."""
    version = manifest["version"]
    parse_version(version)  # a path component below — never anything but N.N.N
    check_url(manifest["url"], "manifest url")
    releases = root / "releases"
    dest = releases / version
    if (releases / f"{version}.failed").exists():
        raise UpdateError(f"release {version} previously failed health check; skipping")
    if (dest / "harness" / "version.py").is_file():
        return dest
    releases.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=releases, prefix=f".tmp-{version}-") as tmp:
        tarball = Path(tmp) / "release.tar.gz"
        with urllib.request.urlopen(manifest["url"], timeout=300) as r, tarball.open("wb") as out:
            shutil.copyfileobj(r, out)
        got = _sha256(tarball)
        if got != manifest["sha256"]:
            raise UpdateError(f"sha256 mismatch for {version}: got {got}")
        unpack = Path(tmp) / "unpack"
        with tarfile.open(tarball) as tar:
            extract_release(tar, unpack)
        # tolerate an archive wrapped in a single top-level directory
        tree = unpack
        if release_version(tree) is None:
            entries = [d for d in unpack.iterdir() if d.is_dir()]
            if len(entries) == 1 and release_version(entries[0]) is not None:
                tree = entries[0]
        if release_version(tree) != version:
            raise UpdateError(f"archive for {version} carries version {release_version(tree)!r}")
        # macOS tar can leave AppleDouble ._* files; they are not Python.
        for junk in tree.rglob("._*"):
            junk.unlink(missing_ok=True)
        proc = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", "-x", r"/[.]_", str(tree)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise UpdateError(f"compileall failed for {version}: {proc.stderr[-500:]}")
        os.replace(tree, dest)
    return dest


def flip_current(root: Path, version: str) -> None:
    """Atomically point `current` at releases/<version>."""
    target = Path("releases") / version
    tmp = root / f".current-{version}"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(target)
    os.replace(tmp, root / "current")


def adopt_directory_current(root: Path) -> None:
    """Move a first-boot directory `current` into releases/<ver> + symlink.

    Early tenant cloud-init unpacked the tarball into `/opt/harness/current`
    as a real directory. `os.replace` of a symlink over that tree fails, so
    later updater ticks could never flip. Idempotent when `current` is
    already a symlink or missing.
    """
    current = root / "current"
    if current.is_symlink() or not current.exists():
        return
    if not current.is_dir():
        raise UpdateError(f"{current} exists and is not a directory or symlink")
    version = release_version(current)
    if not version:
        raise UpdateError("current is a directory with no harness/version.py; cannot migrate")
    dest = root / "releases" / version
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(current)
    else:
        current.rename(dest)
    flip_current(root, version)


def https_only(url: object) -> str | None:
    """`url` when it is https, else None — the app zip is installed as code."""
    if isinstance(url, str) and urllib.parse.urlsplit(url).scheme.lower() == "https":
        return url
    return None


def _hex_digest(value: object) -> str | None:
    """A 64-hex sha256 (lowercased) or None — never an arbitrary string."""
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return value.lower()
    return None


def write_app_release(home: Path, manifest: dict) -> None:
    pins = {
        "min_app_version": manifest.get("min_app_version"),
        "latest_app_version": manifest.get("latest_app_version"),
        # Clients download and run whatever this names; a plain-http pin
        # would hand a network attacker the Mac. Drop it rather than relay it.
        "app_download_url": https_only(manifest.get("app_download_url")),
        "app_sha256": _hex_digest(manifest.get("app_sha256")),
    }
    home.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=home, prefix=".app_release-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(pins, fh)
    os.replace(tmp, home / "app_release.json")


def systemctl_restart(unit: str = "harness") -> None:
    subprocess.run(["systemctl", "restart", unit], check=True)


#: `deploy/build_machine_image.sh` exits 3 when there is no container engine —
#: a process-backend host or a laptop — which is "nothing to build", not a
#: failure.
BUILD_SKIPPED = 3


def build_machine_image(root: Path, version: str, log=print) -> bool:
    """Rebuild the bot-machine image from the NEW release tree (machines
    backend). Runs between the symlink flip and the restart so the restarted
    harness recreates its machines on the new image (isolation/machines.py
    notices the image id changed, home volumes kept).

    Never raises: a failed build is logged and the release still restarts on
    the previous image — stale in-machine code beats a fleet that cannot
    update at all. No engine on the host is a quiet skip.
    """
    script = root / "releases" / version / "deploy" / "build_machine_image.sh"
    if not script.is_file():
        return False
    try:
        proc = subprocess.run(
            ["sh", str(script), str(script.parent.parent)],
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"updater: machine image build failed to run: {exc}")
        return False
    if proc.returncode == 0:
        log(f"updater: machine image rebuilt for {version}")
        return True
    if proc.returncode == BUILD_SKIPPED:
        return False
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
    log(f"updater: machine image build failed (rc={proc.returncode}): {' | '.join(tail)}")
    return False


def default_health(home: Path, port: int = 8765) -> str | None:
    """Version reported by the local harness, or None while it is unreachable."""
    try:
        info = json.loads((home / "serve.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        info = {}
    key = info.get("key", "")
    req = urllib.request.Request(
        f"http://127.0.0.1:{info.get('port', port)}/api/health",
        headers={"Authorization": f"Bearer {key}"} if key else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read().decode("utf-8")).get("version")
    except Exception:  # noqa: BLE001 - unreachable = not healthy yet
        return None


def wait_healthy(version: str, health, timeout: float = HEALTH_TIMEOUT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if health() == version:
            return True
        time.sleep(1.0)
    return False


def prune_releases(root: Path, keep: int = KEEP_RELEASES) -> None:
    releases = root / "releases"
    if not releases.is_dir():
        return
    current = os.path.realpath(root / "current")
    dirs = sorted(
        (d for d in releases.iterdir() if d.is_dir()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    for d in dirs[keep:]:
        if os.path.realpath(d) != current:
            shutil.rmtree(d, ignore_errors=True)


def apply_update(
    manifest: dict,
    *,
    root: Path,
    home: Path,
    restart=systemctl_restart,
    health=None,
    log=print,
    health_timeout: float = HEALTH_TIMEOUT,
    build_image=build_machine_image,
) -> bool:
    """One updater tick. Returns True when a new release is serving.

    Raises UpdateError when the update failed AND rollback was needed.
    """
    if health is None:
        health = lambda: default_health(home)  # noqa: E731

    version = manifest["version"]
    if not in_wave(host_id(), manifest.get("rollout_percent", 100)):
        log(f"updater: {version} not in rollout wave for this host")
        return False

    adopt_directory_current(root)

    # App pins are cheap and harmless — keep them fresh even without a
    # harness release change.
    write_app_release(home, manifest)

    previous = current_version(root)
    if previous == version:
        return False
    if previous and parse_version(version) < parse_version(previous):
        # Going backwards is a deliberate act (rollback.yml stamps
        # `rollback: true`); an unflagged older manifest is a stale or
        # hostile document and must not reinstall a superseded release.
        if not manifest.get("rollback"):
            raise UpdateError(
                f"refusing downgrade {previous} -> {version}: manifest is not marked rollback"
            )
        log(f"updater: rollback {previous} -> {version} (manifest marked rollback)")

    log(f"updater: {previous or 'none'} -> {version}")
    fetch_release(manifest, root)
    flip_current(root, version)
    # machines backend: the image carries /app, so a release is not fully
    # applied until the image is rebuilt; do it before the restart.
    build_image(root, version, log)
    restart()
    if wait_healthy(version, health, timeout=health_timeout):
        log(f"updater: {version} healthy")
        prune_releases(root)
        return True

    # Roll back: flip to the previous release and mark this one failed.
    (root / "releases" / f"{version}.failed").touch()
    if previous:
        log(f"updater: {version} failed health check; rolling back to {previous}")
        flip_current(root, previous)
        restart()
        if not wait_healthy(previous, health, timeout=health_timeout):
            raise UpdateError(f"rollback to {previous} also unhealthy — manual intervention")
    raise UpdateError(f"release {version} failed its health check")
    # CloudWatch/SNS hook: this non-zero exit shows up in the journal; alert on it.


def main() -> int:
    manifest_url = os.environ.get("HARNESS_RELEASE_MANIFEST")
    if not manifest_url:
        print("updater: HARNESS_RELEASE_MANIFEST not set", file=sys.stderr)
        return 2
    root = Path(os.environ.get("HARNESS_ROOT", "/opt/harness"))
    home = Path(os.environ.get("HARNESS_HOME", "/var/lib/harness/shared"))
    try:
        manifest = load_manifest(manifest_url)
        apply_update(manifest, root=root, home=home)
    except UpdateError as exc:
        print(f"updater: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
