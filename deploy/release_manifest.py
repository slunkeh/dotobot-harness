#!/usr/bin/env python3
"""Build the fleet release manifest deploy/updater.py converges on.

One builder shared by cloud/scripts/publish_release.sh (cut a new release)
and .github/workflows/rollback.yml (repoint the fleet at an old one), so the
pin rules cannot drift between them:

- Mac-app pins (min_app_version / latest_app_version / app_download_url)
  carry over from the previous manifest unless this cut ships an app zip —
  a server-only publish must not nag clients to update an app that was not
  rebuilt.
- With --app-zip and --app-version, latest_app_version uses the app version;
  the download URL points at that zip. --app-sha256 pins the zip's digest
  (app_sha256) so the Mac installer can verify what it downloaded.

CLI (prints the manifest JSON to stdout):

    python3 deploy/release_manifest.py --version 0.2.6 --sha256 <hex> \
        --base https://…/harness [--rollout 100] \
        [--prev-url https://…/manifest.json] [--app-zip --app-version 1.2.3]

Stdlib only; a previous manifest that cannot be fetched counts as absent.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request


def fetch_manifest(url: str, timeout: float = 15.0) -> dict:
    """The manifest currently served at url, or {} when unreachable/invalid."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - absent previous manifest is a normal first cut
        return {}
    return data if isinstance(data, dict) else {}


def build_manifest(
    version: str,
    base_url: str,
    sha256: str,
    rollout_percent: int = 100,
    prev: dict | None = None,
    app_zip: bool = False,
    rollback: bool = False,
    app_sha256: str | None = None,
    app_version: str | None = None,
) -> dict:
    prev = prev or {}
    latest_app = prev.get("latest_app_version") or None
    app_url = prev.get("app_download_url") or None
    app_digest = prev.get("app_sha256") or None
    min_app = prev.get("min_app_version") or None
    if app_zip:
        if not app_version:
            raise ValueError(
                "an app zip needs its own app_version; the harness version is not an app version"
            )
        app = apply_app_zip(prev, app_version, base_url, app_sha256)
        latest_app = app["latest_app_version"]
        app_url = app["app_download_url"]
        app_digest = app["app_sha256"]
    manifest = {
        "version": version,
        "url": f"{base_url}/harness-{version}.tar.gz",
        "sha256": sha256,
        "rollout_percent": int(rollout_percent),
        "min_app_version": min_app,
        "latest_app_version": latest_app,
        "app_download_url": app_url,
        "app_sha256": app_digest,
    }
    if rollback:
        # The updater refuses an older version unless the manifest says the
        # move backwards is deliberate (deploy/updater.py apply_update).
        manifest["rollback"] = True
    return manifest


def apply_app_zip(
    manifest: dict, version: str, base_url: str, app_sha256: str | None = None
) -> dict:
    """Keep the live harness tarball; point Mac/iOS clients at this app zip."""
    previous = manifest.get("latest_app_version")
    for value in (version, previous):
        if value is not None and (
            not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value)
        ):
            raise ValueError("app versions must use MAJOR.MINOR.PATCH")
    if previous and tuple(map(int, version.split("."))) < tuple(map(int, previous.split("."))):
        raise ValueError("refusing to replace a newer app release with an older one")
    out = dict(manifest)
    base = base_url.rstrip("/")
    out["latest_app_version"] = version
    out["app_download_url"] = f"{base}/Dotobot-{version}.zip"
    out["app_sha256"] = app_sha256 or None
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True)
    parser.add_argument("--sha256", default="", help="required unless --pins-only")
    parser.add_argument("--base", required=True, help="public base URL of the harness/ prefix")
    parser.add_argument("--rollout", type=int, default=100)
    parser.add_argument("--prev-url", default="", help="URL of the currently served manifest")
    parser.add_argument("--app-zip", action="store_true", help="this cut ships a Mac app zip")
    parser.add_argument("--app-version", help="app-owned version, required with --app-zip")
    parser.add_argument(
        "--app-sha256", default="", help="sha256 of the Mac app zip (with --app-zip / --pins-only)"
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="mark this manifest as a deliberate move to an older version",
    )
    parser.add_argument(
        "--pins-only",
        action="store_true",
        help="rewrite only latest_app_version / app_download_url on the live manifest",
    )
    args = parser.parse_args(argv)
    base = args.base.rstrip("/")

    if args.pins_only:
        if not args.prev_url:
            print("--pins-only needs --prev-url", file=sys.stderr)
            return 2
        prev = fetch_manifest(args.prev_url)
        if not prev.get("version"):
            print("no live harness manifest to pin", file=sys.stderr)
            return 1
        try:
            result = apply_app_zip(prev, args.version, base, app_sha256=args.app_sha256 or None)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if not args.sha256:
        parser.error("--sha256 is required unless --pins-only")
    if args.app_zip and not args.app_version:
        parser.error(
            "--app-version is required with --app-zip; never use the harness version implicitly"
        )

    prev = fetch_manifest(args.prev_url) if args.prev_url else {}
    manifest = build_manifest(
        args.version,
        base,
        args.sha256,
        rollout_percent=args.rollout,
        prev=prev,
        app_zip=args.app_zip,
        rollback=args.rollback,
        app_sha256=args.app_sha256 or None,
        app_version=args.app_version,
    )
    json.dump(manifest, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
