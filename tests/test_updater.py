"""Fleet updater: wave gating, verify, symlink flip, health check, rollback."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import updater  # noqa: E402  (deploy/updater.py is not a package)


@pytest.fixture(autouse=True)
def _local_manifests_allowed(monkeypatch):
    """These tests serve manifests and tarballs from file:// URLs, which the
    fleet default refuses (https only); opt in the way an air-gapped mirror
    would. `test_https_is_required_by_default` clears it again."""
    monkeypatch.setenv(updater.INSECURE_ENV, "1")


def _make_release_tarball(tmp_path: Path, version: str) -> tuple[Path, str]:
    tree = tmp_path / f"tree-{version}"
    (tree / "harness").mkdir(parents=True)
    (tree / "harness" / "version.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (tree / "harness" / "__init__.py").write_text("", encoding="utf-8")
    tarball = tmp_path / f"harness-{version}.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(tree, arcname=".")
    return tarball, hashlib.sha256(tarball.read_bytes()).hexdigest()


def _manifest(tarball: Path, sha: str, version: str, **extra) -> dict:
    m = {"version": version, "url": tarball.as_uri(), "sha256": sha, "rollout_percent": 100}
    m.update(extra)
    return m


def test_download_extracts_on_python_without_tarfile_filters(tmp_path, monkeypatch):
    def old_extractall(self, path=".", members=None, *, numeric_owner=False):
        raise AssertionError("release extraction must not depend on stdlib filters")

    monkeypatch.setattr(tarfile.TarFile, "extractall", old_extractall)
    archive, digest = _make_release_tarball(tmp_path, "1.2.3")
    tree = updater.fetch_release(_manifest(archive, digest, "1.2.3"), tmp_path / "host")
    assert updater.release_version(tree) == "1.2.3"


@pytest.mark.parametrize("kind", ["traversal", "symlink", "hardlink"])
def test_source_extraction_rejects_escape_and_link_members(tmp_path, kind):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as tar:
        member = tarfile.TarInfo("../escaped" if kind == "traversal" else "link")
        if kind == "symlink":
            member.type, member.linkname = tarfile.SYMTYPE, "../escaped"
        elif kind == "hardlink":
            member.type, member.linkname = tarfile.LNKTYPE, "../escaped"
        tar.addfile(member)
    data.seek(0)
    with tarfile.open(fileobj=data) as tar, pytest.raises(updater.UpdateError, match="unsafe"):
        updater.extract_release(tar, tmp_path / "out")
    assert not (tmp_path / "escaped").exists()


def _install(root: Path, version: str) -> None:
    """Simulate an existing installed release."""
    rel = root / "releases" / version
    (rel / "harness").mkdir(parents=True)
    (rel / "harness" / "version.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    updater.flip_current(root, version)


def test_wave_gate_is_deterministic_and_monotonic():
    assert not updater.in_wave("host-a", 0)
    assert updater.in_wave("host-a", 100)
    hits = [h for h in (f"host-{i}" for i in range(50)) if updater.in_wave(h, 30)]
    for h in hits:
        assert updater.in_wave(h, 60)  # once in a wave, larger waves keep you


def test_release_version_parses():
    assert updater.release_version(Path("/nonexistent")) is None


def test_fetch_release_strips_macos_appledouble(tmp_path):
    tree = tmp_path / "tree-1.0.0"
    (tree / "harness").mkdir(parents=True)
    (tree / "harness" / "version.py").write_text('__version__ = "1.0.0"\n', encoding="utf-8")
    (tree / "harness" / "__init__.py").write_text("", encoding="utf-8")
    (tree / "providers").mkdir()
    (tree / "providers" / "._echo.py").write_bytes(b"\x00Mac OS X")
    tarball = tmp_path / "h.tgz"
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(tree, arcname=".")
    sha = hashlib.sha256(tarball.read_bytes()).hexdigest()
    dest = updater.fetch_release(_manifest(tarball, sha, "1.0.0"), tmp_path / "root")
    assert not list(dest.rglob("._*"))


def test_fetch_release_rejects_bad_sha(tmp_path):
    tarball, _sha = _make_release_tarball(tmp_path, "0.9.0")
    root = tmp_path / "root"
    with pytest.raises(updater.UpdateError, match="sha256 mismatch"):
        updater.fetch_release(_manifest(tarball, "0" * 64, "0.9.0"), root)


def test_apply_update_flips_restarts_and_pins(tmp_path):
    root, home = tmp_path / "root", tmp_path / "home"
    _install(root, "0.8.0")
    tarball, sha = _make_release_tarball(tmp_path, "0.9.0")
    manifest = _manifest(
        tarball, sha, "0.9.0", latest_app_version="1.2.0", app_download_url="https://x/app.zip"
    )

    restarts = []
    ok = updater.apply_update(
        manifest,
        root=root,
        home=home,
        restart=lambda: restarts.append(1),
        health=lambda: "0.9.0",
        log=lambda *_: None,
        health_timeout=2,
    )
    assert ok
    assert restarts == [1]
    assert updater.current_version(root) == "0.9.0"
    pins = json.loads((home / "app_release.json").read_text())
    assert pins["latest_app_version"] == "1.2.0"
    assert pins["min_app_version"] is None


def test_apply_update_rolls_back_on_failed_health(tmp_path):
    root, home = tmp_path / "root", tmp_path / "home"
    _install(root, "0.8.0")
    tarball, sha = _make_release_tarball(tmp_path, "0.9.0")

    versions = {"current": "0.8.0"}

    def restart():
        versions["current"] = updater.current_version(root)

    with pytest.raises(updater.UpdateError, match="failed its health check"):
        updater.apply_update(
            _manifest(tarball, sha, "0.9.0"),
            root=root,
            home=home,
            restart=restart,
            health=lambda: versions["current"] if versions["current"] == "0.8.0" else None,
            log=lambda *_: None,
            health_timeout=2,
        )
    # rolled back and marked failed
    assert updater.current_version(root) == "0.8.0"
    assert (root / "releases" / "0.9.0.failed").exists()

    # future ticks skip the failed release outright
    with pytest.raises(updater.UpdateError, match="previously failed"):
        updater.fetch_release(_manifest(tarball, sha, "0.9.0"), root)


def test_same_version_is_a_noop_but_refreshes_pins(tmp_path):
    root, home = tmp_path / "root", tmp_path / "home"
    _install(root, "0.9.0")
    tarball, sha = _make_release_tarball(tmp_path, "0.9.0")
    restarts = []
    ok = updater.apply_update(
        _manifest(tarball, sha, "0.9.0", latest_app_version="9.9.9"),
        root=root,
        home=home,
        restart=lambda: restarts.append(1),
        health=lambda: "0.9.0",
        log=lambda *_: None,
    )
    assert not ok
    assert restarts == []
    assert json.loads((home / "app_release.json").read_text())["latest_app_version"] == "9.9.9"


def test_out_of_wave_host_does_nothing(tmp_path, monkeypatch):
    root, home = tmp_path / "root", tmp_path / "home"
    _install(root, "0.8.0")
    tarball, sha = _make_release_tarball(tmp_path, "0.9.0")
    monkeypatch.setattr(updater, "host_id", lambda: "some-host")
    ok = updater.apply_update(
        _manifest(tarball, sha, "0.9.0", rollout_percent=0),
        root=root,
        home=home,
        restart=lambda: (_ for _ in ()).throw(AssertionError("must not restart")),
        health=lambda: None,
        log=lambda *_: None,
    )
    assert not ok
    assert updater.current_version(root) == "0.8.0"


def test_prune_keeps_current_and_newest(tmp_path):
    root = tmp_path / "root"
    for i, v in enumerate(["0.1.0", "0.2.0", "0.3.0", "0.4.0", "0.5.0"]):
        _install(root, v)
        os.utime(root / "releases" / v, (i, i))
    updater.flip_current(root, "0.5.0")
    updater.prune_releases(root, keep=2)
    left = sorted(d.name for d in (root / "releases").iterdir() if d.is_dir())
    assert "0.5.0" in left and "0.4.0" in left
    assert "0.1.0" not in left


def test_adopts_directory_current_then_flips(tmp_path):
    """First-boot used to unpack into `current` as a real directory."""
    root, home = tmp_path / "root", tmp_path / "home"
    current = root / "current"
    (current / "harness").mkdir(parents=True)
    (current / "harness" / "version.py").write_text('__version__ = "0.8.0"\n', encoding="utf-8")
    assert not current.is_symlink()
    tarball, sha = _make_release_tarball(tmp_path, "0.9.0")
    restarts = []
    ok = updater.apply_update(
        _manifest(tarball, sha, "0.9.0"),
        root=root,
        home=home,
        restart=lambda: restarts.append(1),
        health=lambda: "0.9.0",
        log=lambda *_: None,
        health_timeout=2,
    )
    assert ok
    assert restarts == [1]
    assert (root / "current").is_symlink()
    assert updater.current_version(root) == "0.9.0"
    assert (root / "releases" / "0.8.0" / "harness" / "version.py").is_file()


def test_adopt_is_noop_when_current_is_already_a_symlink(tmp_path):
    root = tmp_path / "root"
    _install(root, "0.8.0")
    updater.adopt_directory_current(root)
    assert (root / "current").is_symlink()
    assert updater.current_version(root) == "0.8.0"


# -- transport, version and downgrade checks (security audit) ---------------


def test_https_is_required_by_default(tmp_path, monkeypatch):
    """The sha256 in the manifest only proves the tarball matches the
    manifest; TLS is the authenticity check, so neither document may arrive
    over file:// or http:// unless the operator opted in."""
    monkeypatch.delenv(updater.INSECURE_ENV, raising=False)
    tarball, sha = _make_release_tarball(tmp_path, "0.9.0")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(tarball, sha, "0.9.0")), encoding="utf-8")
    with pytest.raises(updater.UpdateError, match="https"):
        updater.load_manifest(manifest_path.as_uri())
    with pytest.raises(updater.UpdateError, match="https"):
        updater.load_manifest("http://releases.example.com/harness/manifest.json")
    with pytest.raises(updater.UpdateError, match="https"):
        updater.fetch_release(_manifest(tarball, sha, "0.9.0"), tmp_path / "root")


def test_manifest_url_must_share_the_manifests_origin(tmp_path):
    """A manifest naming a tarball on another host is not a release; it is
    somebody pointing every tenant at a download they control."""
    tarball, sha = _make_release_tarball(tmp_path, "0.9.0")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"version": "0.9.0", "url": "https://evil.example/x.tar.gz", "sha256": sha}),
        encoding="utf-8",
    )
    with pytest.raises(updater.UpdateError, match="origin"):
        updater.load_manifest(manifest_path.as_uri())


@pytest.mark.parametrize("bad", ["../0.9.0", "0.9", "v0.9.0", "0.9.0/../..", "", "1e3"])
def test_release_version_must_be_major_minor_patch(tmp_path, bad):
    """The version becomes releases/<version> on disk and is compared for the
    downgrade floor, so it is exactly N.N.N or refused."""
    tarball, sha = _make_release_tarball(tmp_path, "0.9.0")
    with pytest.raises(updater.UpdateError):
        updater.fetch_release(_manifest(tarball, sha, bad), tmp_path / "root")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(tarball, sha, bad)), encoding="utf-8")
    with pytest.raises(updater.UpdateError):
        updater.load_manifest(manifest_path.as_uri())


def test_unflagged_downgrade_is_refused(tmp_path):
    """A manifest naming an older version than the one serving reinstalls
    a superseded (possibly vulnerable) release; only a manifest that says
    the move backwards is deliberate may do that."""
    root = tmp_path / "root"
    _install(root, "0.9.0")
    tarball, sha = _make_release_tarball(tmp_path, "0.8.0")
    restarts: list[str] = []
    with pytest.raises(updater.UpdateError, match="downgrade"):
        updater.apply_update(
            _manifest(tarball, sha, "0.8.0"),
            root=root,
            home=tmp_path / "home",
            restart=lambda: restarts.append("restart"),
            health=lambda: "0.8.0",
        )
    assert restarts == []
    assert updater.current_version(root) == "0.9.0"
    assert not (root / "releases" / "0.8.0").exists()


def test_flagged_rollback_may_go_backwards(tmp_path):
    root = tmp_path / "root"
    _install(root, "0.9.0")
    tarball, sha = _make_release_tarball(tmp_path, "0.8.0")
    ok = updater.apply_update(
        _manifest(tarball, sha, "0.8.0", rollback=True),
        root=root,
        home=tmp_path / "home",
        restart=lambda: None,
        health=lambda: "0.8.0",
        health_timeout=2,
    )
    assert ok is True
    assert updater.current_version(root) == "0.8.0"


def test_plain_http_app_download_url_is_not_relayed(tmp_path):
    """Clients install whatever app_download_url names as code, so the
    updater drops a non-https pin instead of handing it to every Mac."""
    home = tmp_path / "home"
    updater.write_app_release(
        home, {"latest_app_version": "1.2.0", "app_download_url": "http://x/app.zip"}
    )
    pins = json.loads((home / "app_release.json").read_text(encoding="utf-8"))
    assert pins["latest_app_version"] == "1.2.0"
    assert pins["app_download_url"] is None
    updater.write_app_release(home, {"app_download_url": "https://x/app.zip"})
    pins = json.loads((home / "app_release.json").read_text(encoding="utf-8"))
    assert pins["app_download_url"] == "https://x/app.zip"


def test_app_digest_pin_is_relayed_only_as_a_real_sha256(tmp_path):
    home = tmp_path / "home"
    updater.write_app_release(
        home, {"app_download_url": "https://x/app.zip", "app_sha256": "AB" * 32}
    )
    pins = json.loads((home / "app_release.json").read_text(encoding="utf-8"))
    assert pins["app_sha256"] == "ab" * 32
    updater.write_app_release(home, {"app_download_url": "https://x/app.zip", "app_sha256": "nope"})
    pins = json.loads((home / "app_release.json").read_text(encoding="utf-8"))
    assert pins["app_sha256"] is None


# --- machines backend: the image is rebuilt between the flip and the restart


def test_apply_update_rebuilds_the_machine_image_before_restart(tmp_path, monkeypatch):
    root, home = tmp_path / "root", tmp_path / "home"
    _install(root, "0.8.0")
    tarball, sha = _make_release_tarball(tmp_path, "0.9.0")
    manifest = _manifest(tarball, sha, "0.9.0")
    order: list[str] = []

    def build(r, version, log):
        assert r == root and version == "0.9.0"
        assert updater.current_version(root) == "0.9.0", "built from the flipped tree"
        order.append("build")
        return True

    ok = updater.apply_update(
        manifest,
        root=root,
        home=home,
        restart=lambda: order.append("restart"),
        health=lambda: "0.9.0",
        log=lambda *_: None,
        health_timeout=2,
        build_image=build,
    )
    assert ok
    assert order == ["build", "restart"]


def _release_with_build_script(root: Path, version: str, body: str) -> None:
    rel = root / "releases" / version
    (rel / "deploy").mkdir(parents=True, exist_ok=True)
    script = rel / "deploy" / "build_machine_image.sh"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")


def test_build_machine_image_reports_each_outcome(tmp_path):
    root = tmp_path / "root"
    _install(root, "1.0.0")
    logs: list[str] = []
    # no script in the release (older tree): nothing to do
    assert updater.build_machine_image(root, "1.0.0", logs.append) is False
    assert logs == []
    # exit 3 = no engine here: quiet skip
    _release_with_build_script(root, "1.0.0", "exit 3\n")
    assert updater.build_machine_image(root, "1.0.0", logs.append) is False
    assert logs == []
    # real failure: logged, never raised (the release still restarts)
    _release_with_build_script(root, "1.0.0", "echo boom >&2; exit 1\n")
    assert updater.build_machine_image(root, "1.0.0", logs.append) is False
    assert logs and "failed" in logs[-1] and "boom" in logs[-1]
    # success, invoked with the release tree as the build root
    _release_with_build_script(root, "1.0.0", 'echo "root=$1"; exit 0\n')
    assert updater.build_machine_image(root, "1.0.0", logs.append) is True
    assert "rebuilt" in logs[-1]
