"""Public artifacts contain exactly reviewed files and cannot inherit private history."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tarfile
import tomllib
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import public_export as exporter  # noqa: E402


def test_actual_public_archive_excludes_private_and_operator_trees(tmp_path):
    archive = tmp_path / "harness.tar.gz"
    digest = exporter.make_archive(ROOT, archive)
    assert digest == hashlib.sha256(archive.read_bytes()).hexdigest()
    with tarfile.open(archive) as tar:
        names = tar.getnames()
        assert "harness/server.py" in names and "deploy/public_install.py" in names
        assert "install.sh" in names and "LICENSE" in names
        assert "deploy/smoke_machines.sh" not in names
        assert not any(
            n.startswith(
                (
                    "clients/",
                    "cloud/",
                    "website/",
                    ".git/",
                    "deploy/github-runner/",
                    "shared/",
                    "tests/",
                )
            )
            for n in names
        )
        readme = tar.extractfile("README.md").read().decode()
        assert readme == (ROOT / "deploy/public_README.md").read_text()
        assert "--ip YOUR_PUBLIC_IP" in readme and "A domain is optional" in readme
        bootstrap = tar.extractfile("install.sh").read().decode()
        assert "--ip ADDRESS" in bootstrap and "optional DNS name" in bootstrap
        installer = tar.extractfile("deploy/public_install.py").read().decode()
        assert "profile shortlived" in installer and "CADDY_PACKAGES" in installer
        metadata = tar.extractfile("pyproject.toml").read().decode()
        assert 'Repository = "https://github.com/slunkeh/dotobot-harness"' in metadata


def test_public_source_has_its_own_floor_and_no_private_content(tmp_path):
    output = tmp_path / "public"
    exporter.export_tree(ROOT, output)
    assert not (output / ".git").exists()
    assert not (output / "clients").exists() and not (output / "cloud").exists()
    assert (output / "conftest.py").read_bytes() == (
        ROOT / "deploy/public_conftest.py"
    ).read_bytes()
    assert (output / "tests/test_linking.py").is_file()
    assert not (output / "tests/test_cloud_registry.py").exists()
    assert (output / "NOTICE").read_bytes() == (ROOT / "NOTICE").read_bytes()
    assert (output / "docs/research-hermes.md").is_file()
    metadata = (output / "pyproject.toml").read_text()
    project = tomllib.loads(metadata)["project"]
    assert project["urls"] == {
        "Homepage": "https://dotobot.com",
        "Repository": "https://github.com/slunkeh/dotobot-harness",
    }
    assert project["authors"] == [{"name": "Dotobot contributors"}]
    assert "maintainers" not in project
    source_project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    source_repository = source_project["urls"]["Repository"]
    if source_repository != project["urls"]["Repository"]:
        for _, relative in exporter.public_files(ROOT):
            assert source_repository.encode() not in (output / relative).read_bytes(), relative
    with pytest.raises(ValueError, match="empty output"):
        exporter.export_tree(ROOT, output)


def test_public_jpeg_strips_metadata_and_preserves_color_tables_and_scans():
    def segment(marker, payload):
        return bytes([0xFF, marker]) + (len(payload) + 2).to_bytes(2, "big") + payload

    private = b"private fixture metadata"
    jfif_header = b"JFIF\0\x01\x01\0\0\x01\0\x01"
    jfif = segment(0xE0, jfif_header + b"\x01\x01\0\0\0" + private)
    icc = segment(0xE2, b"ICC_PROFILE\0\x01\x01color-profile")
    adobe_header = b"Adobe\0d\0\0\0\0\x01"
    adobe = segment(0xEE, adobe_header + private)
    tables = segment(0xDB, b"quantization-table") + segment(0xC4, b"huffman-table")
    scan_header = segment(0xDA, b"scan-header")
    first_scan = scan_header + b"\x03\xff\0\x42\xff\xd0\x08"
    second_scan = scan_header + b"\x07\xff\0\x14\xff\xd7\x21"
    metadata = b"".join(segment(marker, private) for marker in (0xE1, 0xE2, 0xE3, 0xEB, 0xED, 0xFE))
    original = (
        b"\xff\xd8"
        + metadata
        + jfif
        + icc
        + adobe
        + tables
        + first_scan
        + segment(0xE1, private)
        + second_scan
        + b"\xff\xd9"
        + private
    )
    expected = (
        b"\xff\xd8"
        + segment(0xE0, jfif_header + b"\0\0")
        + icc
        + segment(0xEE, adobe_header)
        + tables
        + first_scan
        + second_scan
        + b"\xff\xd9"
    )
    sanitized = exporter.public_jpeg(original)
    assert sanitized == expected
    assert private not in sanitized
    assert exporter.public_jpeg(sanitized) == sanitized


def test_public_wallpaper_is_sanitized_without_mutating_the_source(tmp_path):
    relative = "deploy/machine-wallpaper.jpg"
    source = ROOT / relative
    original = source.read_bytes()
    sanitized = exporter.public_content(source, relative)
    assert source.read_bytes() == original
    assert sanitized.startswith(b"\xff\xd8") and sanitized.endswith(b"\xff\xd9")
    assert b"Exif\0\0" not in sanitized and b"c2pa" not in sanitized
    public_asset = tmp_path / "wallpaper.jpg"
    public_asset.write_bytes(sanitized)
    assert exporter.public_content(public_asset, relative) == sanitized
    archive = tmp_path / "harness.tar.gz"
    exporter.make_archive(ROOT, archive)
    with tarfile.open(archive) as tar:
        assert tar.extractfile(relative).read() == sanitized


@pytest.mark.parametrize("malformed", [b"not jpeg", b"\xff\xd8\xff\xe1\0\x10short", b"\xff\xd8"])
def test_public_jpeg_refuses_invalid_or_truncated_files(malformed):
    with pytest.raises(ValueError, match="JPEG"):
        exporter.public_jpeg(malformed)


def test_git_add_after_import_and_local_state_keeps_only_public_sources(tmp_path):
    output = tmp_path / "public"
    exporter.export_tree(ROOT, output)
    subprocess.run(
        [sys.executable, "-c", "import harness, harness.linking"], cwd=output, check=True
    )
    for relative in (
        "shared/link-key",
        "credentials/provider-key",
        ".env",
        "state.sqlite",
        "link-key",
        "roster.json",
        ".pytest_cache/results",
        ".venv/bin/python",
    ):
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private local fixture\n")
    subprocess.run(["git", "init", "-q", str(output)], check=True)
    subprocess.run(["git", "-C", str(output), "add", "."], check=True)
    tracked = subprocess.check_output(["git", "-C", str(output), "ls-files"], text=True)
    assert set(tracked.splitlines()) == {relative for _, relative in exporter.public_files(ROOT)}


@pytest.mark.parametrize(
    "path",
    [
        "cloud/control/me.py",
        "clients/secret.swift",
        "../README.md",
        "deploy/github-runner/install.sh",
        "shared/link-key",
        "/tmp/secret",
        "deploy/mac_package.py",
        ".github/workflows/ci.yml",
        "deploy/public_ci.yml",
    ],
)
def test_manifest_cannot_reopen_private_paths(tmp_path, path):
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy/public-files.txt").write_text(path + "\n")
    with pytest.raises(ValueError, match="unapproved"):
        exporter.public_files(tmp_path)


def test_manifest_rejects_symlinks_and_unreviewed_runtime_modules(tmp_path):
    (tmp_path / "deploy").mkdir()
    (tmp_path / "harness").mkdir()
    (tmp_path / "deploy/public-files.txt").write_text("LICENSE\n")
    (tmp_path / "LICENSE").symlink_to(ROOT / "LICENSE")
    with pytest.raises(ValueError, match="symlink"):
        exporter.public_files(tmp_path)
    (tmp_path / "LICENSE").unlink()
    (tmp_path / "LICENSE").write_text("License")
    (tmp_path / "harness/new.py").write_text("new = True\n")
    with pytest.raises(ValueError, match="missing from public manifest"):
        exporter.public_files(tmp_path)


def test_parent_symlink_is_rejected_even_from_another_working_directory(tmp_path, monkeypatch):
    root = tmp_path / "source"
    (root / "deploy").mkdir(parents=True)
    private = root / "private-app"
    private.mkdir()
    (private / "secret.py").write_text("proprietary = True\n")
    (root / "harness").symlink_to(private, target_is_directory=True)
    (root / "deploy/public-files.txt").write_text("harness/secret.py\n")
    unrelated = tmp_path / "elsewhere"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    with pytest.raises(ValueError, match="symlink"):
        exporter.public_files(root)


def test_public_png_strips_metadata_and_preserves_image_chunks(tmp_path):
    def chunk(kind, data):
        return (
            len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")
        )

    pixels = chunk(b"IHDR", b"header") + chunk(b"sRGB", b"\0") + chunk(b"IDAT", b"pixels")
    end = chunk(b"IEND", b"")
    private = b"private fixture metadata"
    metadata = b"".join(chunk(kind, private) for kind in (b"eXIf", b"tEXt", b"iTXt", b"zTXt"))
    source = tmp_path / "terminal-icon.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + pixels + metadata + end + private)
    clean = exporter.public_content(source, "deploy/terminal-icon.png")
    assert clean == b"\x89PNG\r\n\x1a\n" + pixels + end
    assert private in source.read_bytes()
    source.write_bytes(clean)
    assert exporter.public_content(source, "deploy/terminal-icon.png") == clean


@pytest.mark.parametrize(
    "data", [b"not PNG", b"\x89PNG\r\n\x1a\n", b"\x89PNG\r\n\x1a\n\0\0\0\x20IDATshort"]
)
def test_public_png_refuses_invalid_or_truncated_files(tmp_path, data):
    source = tmp_path / "terminal-icon.png"
    source.write_bytes(data)
    with pytest.raises(ValueError, match="PNG"):
        exporter.public_content(source, "deploy/terminal-icon.png")
