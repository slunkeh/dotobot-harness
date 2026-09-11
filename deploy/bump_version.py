#!/usr/bin/env python3
"""Compute and apply the next release version from a merge-commit message.

Run by the release job in .github/workflows/ci.yml on every push to main:

    git log -1 --format=%B | python3 deploy/bump_version.py --skip-website-only harness/version.py

Prints what the job should release and rewrites harness/version.py when a
bump is due:

- default bump is patch; a `release: minor` / `release: major` token
  anywhere in the message escalates (bracketed `[release: minor]` works, so
  a squash-merge PR title can carry it); `release: patch` is the explicit
  spelling of the default;
- `release: skip` opts the merge out of a release — prints `skip`, file
  untouched;
- `--skip-website-only` also skips when the changes since the current
  version's tag affect only website/, its workflow, or the root README;
- a subject that IS a release commit (`release: vX.Y.Z`) is the job's own
  bump landing back on main: prints the file's current version without
  rewriting, so a re-run republishes instead of double-bumping.

Stdlib only, pure functions take their inputs so tests inject them.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

_TOKEN = re.compile(r"\brelease:\s*(major|minor|patch|skip)\b", re.IGNORECASE)
_RELEASE_SUBJECT = re.compile(r"^release: v\d+\.\d+\.\d+$")
_VERSION_LINE = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
# What main() prints is interpolated into the release workflow (refnames,
# artifact paths, a shell step), so only these two shapes may ever leave it.
_PRINTABLE = re.compile(r"skip|[0-9]+\.[0-9]+\.[0-9]+")


def is_release_commit(message: str) -> bool:
    """True when the message subject is one of our own `release: vX.Y.Z` commits."""
    subject = message.strip().splitlines()[0] if message.strip() else ""
    return bool(_RELEASE_SUBJECT.match(subject))


def parse_bump(message: str) -> str | None:
    """Bump level for a merge-commit message; None means no release (skip)."""
    levels = {m.group(1).lower() for m in _TOKEN.finditer(message)}
    if "skip" in levels:
        return None
    for level in ("major", "minor"):
        if level in levels:
            return level
    return "patch"


def bump(version: str, level: str) -> str:
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"not a MAJOR.MINOR.PATCH version: {version!r}")
    major, minor, patch = (int(p) for p in parts)
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    if level == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"unknown bump level: {level!r}")


def read_version(path: Path) -> str:
    m = _VERSION_LINE.search(path.read_text(encoding="utf-8"))
    if not m:
        raise ValueError(f"no __version__ line in {path}")
    return checked(m.group(1))


def checked(output: str) -> str:
    """Refuse anything but `skip` or MAJOR.MINOR.PATCH before it is printed.

    On a `release: vX.Y.Z` head commit main() echoes the file's version
    without bumping, so a `__version__ = "1.2.3$(cmd)"` planted in
    harness/version.py would otherwise reach the workflow verbatim.
    """
    if not _PRINTABLE.fullmatch(output):  # fullmatch: `$` would admit a trailing newline
        raise ValueError(f"refusing to print a non-version: {output!r}")
    return output


def rewrite(path: Path, new_version: str) -> None:
    text = path.read_text(encoding="utf-8")
    replaced, count = _VERSION_LINE.subn(f'__version__ = "{new_version}"', text)
    if count != 1:
        raise ValueError(f"expected exactly one __version__ line in {path}, found {count}")
    path.write_text(replaced, encoding="utf-8")


def website_only_since_release(path: Path) -> bool:
    """Compare against the release tag, not just the newest merge.

    A website merge can supersede an app merge whose release is still queued.
    Include pending app changes, but honor merges explicitly marked release:
    skip. Otherwise this guard's own skipped merge would trigger a release on
    the next website update. Compare each merge with its first parent and
    disable rename detection so moving app code cannot hide its deletion.
    Missing history fails the job before either version file is touched.
    """
    root = path.resolve().parent.parent
    base = f"refs/tags/v{read_version(path)}"

    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.PIPE)

    git("merge-base", "--is-ancestor", base, "HEAD")
    commits = git("rev-list", "--first-parent", f"{base}..HEAD").decode("ascii").splitlines()
    for commit in commits:
        message = git("log", "-1", "--format=%B", commit).decode("utf-8", errors="replace")
        if parse_bump(message) is None:
            continue
        changed = git("diff", "--no-renames", "--name-only", "-z", f"{commit}^", commit, "--")
        if any(
            not (
                name.startswith(b"website/")
                or name in {b".github/workflows/website.yml", b"README.md"}
            )
            for name in changed.split(b"\0")
            if name
        ):
            return False
    return True


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-website-only", action="store_true")
    parser.add_argument("path", nargs="?", type=Path, default=Path("harness") / "version.py")
    args = parser.parse_args(argv[1:])
    path = args.path
    message = sys.stdin.read()
    try:
        if is_release_commit(message):
            print(checked(read_version(path)))
            return 0
        level = parse_bump(message)
        if level is None or (args.skip_website_only and website_only_since_release(path)):
            print(checked("skip"))
            return 0
        new_version = checked(bump(read_version(path), level))
        rewrite(path, new_version)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"bump_version: {exc}", file=sys.stderr)
        return 2
    print(new_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
