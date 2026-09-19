#!/usr/bin/env python3
"""Build a clean harness source tree or runtime archive from an explicit allowlist.

No git history, private apps, account backend, website, operator setup, or local
state is copied. Publishing the resulting tree is a separate maintainer action.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import tarfile
import zlib
from pathlib import Path, PurePosixPath

RUNTIME_PACKAGES = {"harness", "agent", "providers", "connectors", "channels", "isolation"}
PUBLIC_DEPLOY = {
    "Dockerfile",
    "Dockerfile.machine",
    "build_machine_image.sh",
    "firewall-machines.sh",
    "machine_rm.py",
    "machine-wallpaper.jpg",
    "terminal-icon.png",
    "updater.py",
    "update_host.py",
    "update_service.py",
    "controller_supervisor.py",
    "bump_version.py",
    "release_manifest.py",
    "make_release.sh",
    "public_install.py",
    "container_install.py",
    "container_entrypoint.py",
    "system_install.sh",
    "public_export.py",
    "public-files.txt",
    "public_README.md",
    "public_conftest.py",
    "public_SECURITY.md",
    "public_gitignore",
}
ROOT_FILES = {
    "install.sh",
    "pyproject.toml",
    "LICENSE",
    "NOTICE",
    "README.md",
    "SECURITY.md",
    "conftest.py",
    ".dockerignore",
    ".gitignore",
    "roster.toml",
    "roster.example.toml",
    "policy.example.toml",
}


def permitted(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts or PurePosixPath(path).is_absolute() or ".." in parts:
        return False
    if len(parts) == 1:
        return parts[0] in ROOT_FILES
    if parts[0] in RUNTIME_PACKAGES:
        return len(parts) == 2 and parts[1].endswith(".py")
    if parts[0] == "tests":
        return len(parts) == 2 and parts[1].endswith(".py")
    if parts[0] == "deploy":
        return len(parts) == 2 and parts[1] in PUBLIC_DEPLOY
    return path == "docs/research-hermes.md"


def public_jpeg(data: bytes) -> bytes:
    """Remove identifying metadata without decoding or recompressing JPEG pixels.

    Keep only the JFIF display header, ICC color profiles and Adobe color transform
    among application markers. Copy every image table and entropy-coded scan
    unchanged, including progressive scans and their byte stuffing/restart markers.
    """
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("Public wallpaper must be a JPEG")
    result = bytearray(data[:2])
    offset = 2
    while offset < len(data):
        start = offset
        if data[offset] != 0xFF:
            raise ValueError("Invalid public JPEG marker")
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset == len(data):
            break
        marker = data[offset]
        offset += 1
        if marker == 0xD9:
            return bytes(result + b"\xff\xd9")  # Exclude any trailing metadata.
        if marker in {0, 0xD8} or 0xD0 <= marker <= 0xD7:
            raise ValueError("Unexpected public JPEG marker")
        if marker == 1:
            result.extend(data[start:offset])
            continue
        size = int.from_bytes(data[offset : offset + 2], "big")
        if size < 2 or offset + size > len(data):
            raise ValueError("Truncated public JPEG segment")
        payload = data[offset + 2 : offset + size]
        offset += size
        if 0xE0 <= marker <= 0xEF:
            if marker == 0xE0 and payload.startswith(b"JFIF\0") and len(payload) >= 14:
                # Thumbnail pixels and arbitrary extra payloads are not needed
                # to display the main image. Keep its density/aspect information.
                result.extend(b"\xff\xe0\x00\x10" + payload[:12] + b"\0\0")
            elif marker == 0xE2 and payload.startswith(b"ICC_PROFILE\0"):
                result.extend(data[start:offset])
            elif marker == 0xEE and payload.startswith(b"Adobe") and len(payload) >= 12:
                result.extend(b"\xff\xee\x00\x0e" + payload[:12])
        elif marker != 0xFE:  # JPEG comments have no display semantics.
            result.extend(data[start:offset])
        if marker == 0xDA:
            scan_end = offset
            while True:
                boundary = data.find(b"\xff", scan_end)
                if boundary < 0:
                    raise ValueError("Unterminated public JPEG scan")
                following = boundary + 1
                while following < len(data) and data[following] == 0xFF:
                    following += 1
                if following == len(data):
                    raise ValueError("Truncated public JPEG scan marker")
                following_marker = data[following]
                if following_marker == 0 or 0xD0 <= following_marker <= 0xD7:
                    scan_end = following + 1
                    continue
                result.extend(data[offset:boundary])
                offset = boundary
                break
    raise ValueError("Public JPEG has no end marker")


def public_png(data: bytes) -> bytes:
    """Keep only pixel/display chunks; discard EXIF, text and trailing metadata."""
    signature = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(signature):
        raise ValueError("Public icon must be a PNG")
    keep = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"cHRM", b"gAMA", b"sRGB", b"iCCP"}
    result = bytearray(signature)
    offset = len(signature)
    while offset + 12 <= len(data):
        size = int.from_bytes(data[offset : offset + 4], "big")
        end = offset + size + 12
        if end > len(data):
            raise ValueError("Truncated public PNG chunk")
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 4 : end - 4]
        if zlib.crc32(payload) != int.from_bytes(data[end - 4 : end], "big"):
            raise ValueError("Invalid public PNG checksum")
        if kind in keep:
            result.extend(data[offset:end])
        if kind == b"IEND":
            return bytes(result)
        offset = end
    raise ValueError("Public PNG has no end marker")


def public_content(source: Path, destination: str) -> bytes:
    data = source.read_bytes()
    if destination == "deploy/machine-wallpaper.jpg":
        return public_jpeg(data)
    if destination == "deploy/terminal-icon.png":
        return public_png(data)
    # The private publication policy contains identifying source fragments, so
    # it deliberately never enters the public allowlist. Public re-exports are
    # already sanitized and have no policy file to apply.
    policy = Path(__file__).with_name("public-sanitizations.json")
    if policy.exists():
        rules = json.loads(policy.read_text())
        text = data.decode("utf-8")
        for rule in rules.get("*", []) + rules.get(destination, []):
            text = re.sub(rule["pattern"], rule["replacement"], text)
        data = text.encode("utf-8")
    if destination == "pyproject.toml":
        data = re.sub(
            rb'Repository\s*=\s*"[^"]*"',
            b'Repository = "https://github.com/slunkeh/dotobot-harness"',
            data,
        )
        data = re.sub(
            rb"^authors\s*=.*$",
            b'authors = [{ name = "Dotobot contributors" }]',
            data,
            flags=re.MULTILINE,
        )
    return data


def public_files(root: Path, *, runtime: bool = False) -> list[tuple[Path, str]]:
    manifest = root / "deploy/public-files.txt"
    result = []
    seen = set()
    for line in manifest.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        columns = line.split()
        if len(columns) not in (1, 2):
            raise ValueError(f"Invalid public file entry: {line}")
        source, destination = columns[0], columns[-1]
        if not permitted(source) or not permitted(destination):
            raise ValueError(f"Private or unapproved path in public manifest: {line}")
        if destination in seen:
            raise ValueError(f"Duplicate public output: {destination}")
        seen.add(destination)
        path = root / source
        if path.is_symlink() or any(
            (root / p).is_symlink() for p in path.relative_to(root).parents if p != Path(".")
        ):
            raise ValueError(f"Public source must not be a symlink: {source}")
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Missing or escaped public source: {source}")
        if runtime and (
            destination.startswith(("tests/", ".github/")) or destination == "conftest.py"
        ):
            continue
        result.append((path, destination))
    declared = {path.relative_to(root).as_posix() for path, _ in result}
    # New runtime modules must be deliberately reviewed for publication.
    runtime_files = {
        p.relative_to(root).as_posix()
        for name in RUNTIME_PACKAGES
        for p in (root / name).glob("*.py")
    }
    missing = runtime_files - declared
    if missing:
        raise ValueError(
            "Runtime modules missing from public manifest: " + ", ".join(sorted(missing))
        )
    return result


def export_tree(root: Path, destination: Path) -> None:
    files = public_files(root)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(
            "Source export requires an empty output directory; existing files were preserved."
        )
    destination.mkdir(parents=True, exist_ok=True)
    for source, relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(public_content(source, relative))
        target.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)


def make_archive(root: Path, destination: Path) -> str:
    files = public_files(root, runtime=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as archive:
        for source, relative in files:
            data = public_content(source, relative)
            info = tarfile.TarInfo(relative)
            info.size = len(data)
            info.mode = 0o755 if source.stat().st_mode & 0o111 else 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument(
        "--source", type=Path, help="empty local directory; no history or remote created"
    )
    output.add_argument("--archive", type=Path, help="runtime tar.gz output")
    args = parser.parse_args(argv)
    if args.source:
        export_tree(args.root, args.source)
        print(f"Public harness source prepared at {args.source}; nothing published.")
    else:
        print(make_archive(args.root, args.archive))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
