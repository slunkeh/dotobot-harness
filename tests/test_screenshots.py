"""Screenshot staging store + retention sweep."""

import time

from harness.paths import HarnessPaths
from harness.screenshots import (
    DEFAULT_TTL_HOURS,
    ScreenshotStore,
    sweep_interval,
    ttl_seconds,
)


def store(tmp_path) -> ScreenshotStore:
    return ScreenshotStore(HarnessPaths.resolve(tmp_path / "home"))


def test_layout_creates_screenshot_dir(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout()
    assert paths.screenshots.is_dir()
    assert paths.screenshots == paths.home / "tmp" / "screenshots"


def test_stage_writes_into_the_store(tmp_path):
    s = store(tmp_path)
    path = s.stage(b"\x89PNG", label="desktop check")
    assert path.parent == s.root
    assert path.suffix == ".png"
    assert "desktop-check" in path.name
    assert path.read_bytes() == b"\x89PNG"


def test_stage_names_are_unique(tmp_path):
    s = store(tmp_path)
    assert s.stage(b"a") != s.stage(b"a")


def test_sweep_removes_only_expired_files(tmp_path):
    s = store(tmp_path)
    old = s.stage(b"old")
    fresh = s.stage(b"fresh")
    expired = time.time() - ttl_seconds() - 60
    import os

    os.utime(old, (expired, expired))
    assert s.sweep() == 1
    assert not old.exists()
    assert fresh.exists()


def test_read_refreshes_mtime_so_sends_survive_the_sweep(tmp_path):
    s = store(tmp_path)
    path = s.stage(b"frame")
    expired = time.time() - ttl_seconds() - 60
    import os

    os.utime(path, (expired, expired))
    assert s.read(path) == b"frame"  # touch + read: back inside the window
    assert s.sweep() == 0
    assert path.exists()


def test_ttl_env_override(monkeypatch):
    monkeypatch.setenv("HARNESS_SCREENSHOT_TTL_HOURS", "1.5")
    assert ttl_seconds() == 1.5 * 3600
    monkeypatch.setenv("HARNESS_SCREENSHOT_TTL_HOURS", "not-a-number")
    assert ttl_seconds() == DEFAULT_TTL_HOURS * 3600


def test_sweep_interval_env_override(monkeypatch):
    monkeypatch.setenv("HARNESS_SCREENSHOT_SWEEP_INTERVAL", "0")
    assert sweep_interval() == 0
    monkeypatch.delenv("HARNESS_SCREENSHOT_SWEEP_INTERVAL")
    assert sweep_interval() == 3600


def test_sweep_missing_dir_is_a_noop(tmp_path):
    assert store(tmp_path).sweep() == 0
