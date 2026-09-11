import pytest

from harness.control import MODE_BOT, MODE_TAKEOVER, MODE_TEACH, Control, ControlDenied
from harness.paths import HarnessPaths


def _control(tmp_path):
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas"])
    return Control(p), p


def test_default_state_is_bot(tmp_path):
    ctrl, _ = _control(tmp_path)
    state = ctrl.state("atlas")
    assert state.mode == MODE_BOT
    assert not state.paused
    assert not state.takeover_requested


def test_request_and_take_over(tmp_path):
    ctrl, _ = _control(tmp_path)
    ctrl.request_takeover("atlas", "the login page changed")
    s = ctrl.state("atlas")
    assert s.takeover_requested
    assert s.reason == "the login page changed"
    assert s.mode == MODE_BOT  # still bot until the human acts

    ctrl.take_over("atlas")
    s = ctrl.state("atlas")
    assert s.mode == MODE_TAKEOVER
    assert s.paused
    assert not s.takeover_requested


def test_stop_and_busy_flags(tmp_path):
    ctrl, _ = _control(tmp_path)
    assert not ctrl.stop_requested("atlas")
    ctrl.request_stop("atlas")
    assert ctrl.stop_requested("atlas")
    assert ctrl.consume_stop("atlas")
    assert not ctrl.stop_requested("atlas")
    ctrl.set_busy("atlas", "rid")
    assert ctrl.is_busy("atlas")
    ctrl.clear_busy("atlas")
    assert not ctrl.is_busy("atlas")
    ctrl.set_busy("atlas", "dream-rid", frm="user", preview="[Dreaming — quiet]", origin="dream")
    info = ctrl._busy_info("atlas")
    assert info is not None
    assert info["origin"] == "dream"
    assert info["preview"].startswith("[Dreaming")
    ctrl.clear_busy("atlas")


def test_return_control(tmp_path):
    ctrl, _ = _control(tmp_path)
    ctrl.take_over("atlas")
    ctrl.return_control("atlas")
    s = ctrl.state("atlas")
    assert s.mode == MODE_BOT
    assert not s.paused


def test_teach_recording_flag_clears_on_cancel(tmp_path):
    ctrl, _ = _control(tmp_path)
    ctrl.start_teach("atlas", recording=True)
    s = ctrl.state("atlas")
    assert s.mode == MODE_TEACH
    assert s.teach_recording
    assert s.teach_started is not None
    ctrl.cancel_teach("atlas")
    s = ctrl.state("atlas")
    assert s.mode == MODE_BOT
    assert not s.teach_recording
    assert s.teach_started is None


def test_teach_saves_skill(tmp_path):
    ctrl, paths = _control(tmp_path)
    ctrl.start_teach("atlas")
    assert ctrl.state("atlas").mode == MODE_TEACH
    ctrl.record_step("atlas", "open the invoices page")
    ctrl.record_step("atlas", "download this month's PDF")
    _state, path = ctrl.save_teach("atlas", "download-invoice", "grab the monthly invoice")

    assert path.endswith("SKILL.md")
    body = open(path, encoding="utf-8").read()
    assert "open the invoices page" in body
    assert "download this month's PDF" in body
    # exited teach mode and the skill is now loadable
    assert ctrl.state("atlas").mode == MODE_BOT
    from agent.skills import load_skills

    assert any(s.name == "download-invoice" for s in load_skills(paths, "atlas"))


def test_events_are_logged(tmp_path):
    ctrl, _ = _control(tmp_path)
    ctrl.request_takeover("atlas", "stuck")
    ctrl.take_over("atlas")
    ctrl.return_control("atlas")
    kinds = [e["event"] for e in ctrl.events("atlas")]
    assert kinds == ["request_takeover", "take_over", "return_control"]


def test_holder_recorded_and_cleared(tmp_path):
    ctrl, _ = _control(tmp_path)
    ctrl.take_over("atlas", holder="alex")
    assert ctrl.state("atlas").holder == "alex"
    ctrl.return_control("atlas", by="alex")
    assert ctrl.state("atlas").holder is None
    assert ctrl.state("atlas").mode == MODE_BOT


def test_unidentified_caller_is_the_owner(tmp_path):
    ctrl, _ = _control(tmp_path)
    ctrl.take_over("atlas", holder="alex")
    # CLI / unidentified clients pass no identity and are always allowed
    assert ctrl.can_return("atlas", None)
    ctrl.return_control("atlas")
    assert not ctrl.state("atlas").paused


def test_only_the_holder_can_return_control(tmp_path):
    ctrl, _ = _control(tmp_path)
    ctrl.take_over("atlas", holder="alex")
    assert not ctrl.can_return("atlas", "someone-else")
    with pytest.raises(ControlDenied):
        ctrl.return_control("atlas", by="someone-else")
    assert ctrl.state("atlas").mode == MODE_TAKEOVER


def test_request_return_and_accept(tmp_path):
    ctrl, _ = _control(tmp_path)
    ctrl.take_over("atlas")
    ctrl.request_return("atlas", "I still have steps left", "card-1")
    s = ctrl.state("atlas")
    assert s.return_requested
    assert s.return_reason == "I still have steps left"
    assert s.return_request_id == "card-1"
    assert s.mode == MODE_TAKEOVER  # still the human's until they accept

    ctrl.return_control("atlas")
    s = ctrl.state("atlas")
    assert s.mode == MODE_BOT
    assert not s.return_requested
    assert s.return_request_id is None


def test_decline_return_keeps_the_human_in_control(tmp_path):
    ctrl, _ = _control(tmp_path)
    ctrl.take_over("atlas")
    ctrl.request_return("atlas", "need the browser", "card-2")
    ctrl.decline_return("atlas")
    s = ctrl.state("atlas")
    assert s.mode == MODE_TAKEOVER
    assert s.paused
    assert not s.return_requested
    assert s.return_reason is None


def test_teach_and_takeover_clear_a_pending_return(tmp_path):
    ctrl, _ = _control(tmp_path)
    ctrl.take_over("atlas")
    ctrl.request_return("atlas", "need the browser", "card-3")
    ctrl.take_over("atlas", holder="alex")
    assert not ctrl.state("atlas").return_requested
    ctrl.request_return("atlas", "again", "card-4")
    ctrl.cancel_teach("atlas")
    assert not ctrl.state("atlas").return_requested
