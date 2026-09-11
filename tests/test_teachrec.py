"""Teach a task: host-owned demonstration capture."""

from __future__ import annotations

import pytest

from agent.learn_demo import LEARN_CHAT, SKILL_ID
from agent.skills import default_skill_names, ensure_default_skills, load_skills
from harness import teachrec
from harness.control import Control
from harness.paths import HarnessPaths

# 1x1 PNG
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf"
    b"\xc0\x00\x00\x00\x03\x00\x01\xc4\xae\x08\xa5\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _paths(tmp_path):
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas"])
    return p


def _no_ffmpeg(monkeypatch):
    monkeypatch.setattr(teachrec, "_start_ffmpeg", lambda live, paths: None)


def _shot(tmp_path, monkeypatch):
    png = tmp_path / "dot.png"
    png.write_bytes(_PNG)
    monkeypatch.setenv("HARNESS_SCREENSHOT_CMD", f"cat {png}")
    monkeypatch.setenv("HARNESS_TEACH_SAMPLE_SECS", "0.05")


def test_start_stop_stages_stills(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _no_ffmpeg(monkeypatch)
    _shot(tmp_path, monkeypatch)
    try:
        started = teachrec.start(paths, "atlas")
        assert started["mode"] == "teach"
        assert started["teach_recording"] is True
        assert Control(paths).state("atlas").teach_recording
        assert teachrec.is_recording(paths, "atlas")
        status = teachrec.status(paths, "atlas")
        assert status is not None
        assert status["session_id"] == started["session_id"]

        teachrec.note_input(paths, "atlas", {"action": "click", "x": 0.4, "y": 0.5})
        result = teachrec.stop(paths, "atlas")
    finally:
        teachrec.abort(paths, "atlas")

    assert not teachrec.is_recording(paths, "atlas")
    assert Control(paths).state("atlas").mode == "bot"
    assert Control(paths).state("atlas").teach_recording is False
    names = [a["name"] for a in result.attachments]
    assert any(n.endswith(".png") or n.endswith(".jpg") for n in names)
    assert any(n.endswith(".json") for n in names)
    assert not result.unusable
    video = paths.home / "teach-sessions" / "atlas" / f"teach-{result.session_id}" / "demo.mp4"
    assert not video.is_file()


def test_double_start_is_busy(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _no_ffmpeg(monkeypatch)
    _shot(tmp_path, monkeypatch)
    try:
        teachrec.start(paths, "atlas")
        with pytest.raises(teachrec.TeachBusy):
            teachrec.start(paths, "atlas")
    finally:
        teachrec.abort(paths, "atlas")
        Control(paths).cancel_teach("atlas")


def test_stop_without_start_is_idle(tmp_path):
    paths = _paths(tmp_path)
    with pytest.raises(teachrec.TeachIdle):
        teachrec.stop(paths, "atlas")


def test_no_display_is_unavailable(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _no_ffmpeg(monkeypatch)
    monkeypatch.delenv("HARNESS_SCREENSHOT_CMD", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(teachrec, "_capture", lambda paths, bot: None)
    with pytest.raises(teachrec.TeachUnavailable):
        teachrec.start(paths, "atlas")


def test_learn_skill_is_seeded(tmp_path):
    assert SKILL_ID in default_skill_names()
    paths = _paths(tmp_path)
    ensure_default_skills(paths)
    skills = load_skills(paths, "atlas")
    found = next(s for s in skills if s.skill_id == SKILL_ID)
    assert "propose_skill" in found.body
    assert "edit" in found.body.lower()
    assert "save it as a skill" in found.body.lower()
    assert "kebab-case" in found.body
    assert "watchVideo" not in found.body
    assert "queue_dir" not in found.body
    assert "SIGINT" not in found.body
    assert LEARN_CHAT.endswith(f"/{SKILL_ID}")
