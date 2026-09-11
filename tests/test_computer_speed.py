"""Per-step cost of computer use: what a screenshot / click / display probe
may spend, and what the tool loop re-sends to the model each step.

Regression tests for the slowdown that followed the Chrome AX snapshot
landing on every screenshot: discovery and the full tree are cached,
the snapshot overlaps the capture under a wall budget, a proven display is
not re-probed per action, old screenshot frames leave the in-flight loop,
and the Anthropic provider marks prompt-cache breakpoints.
"""

from __future__ import annotations

import threading
import time

import pytest

from agent import computer as computer_mod
from agent.computer import HostComputer
from agent.history import (
    DEFAULT_LOOP_IMAGES,
    DROPPED_FRAME_NOTE,
    loop_image_limit,
    prune_loop_images,
)
from agent.memory import Memory
from agent.tools import ToolContext, default_tools
from harness import cdp
from harness.paths import HarnessPaths
from providers.base import Message
from tests.test_cdp import FakeChrome, _profile


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    monkeypatch.setenv("HARNESS_CHROME_CDP", "1")
    cdp.reset_caches()
    computer_mod.forget_display_ready()
    yield
    cdp.reset_caches()
    computer_mod.forget_display_ready()


# -- CDP discovery + tree caches --------------------------------------------


def test_snapshot_reuses_discovered_port(tmp_path, monkeypatch):
    probes: list[str] = []
    real = cdp._probe_port
    monkeypatch.setattr(cdp, "_probe_port", lambda m, u: probes.append(u or "") or real(m, u))
    fake = FakeChrome()
    fake.start()
    try:
        profile = _profile(tmp_path, fake.port)
        first = cdp.snapshot(user_data_dir=str(profile))
        second = cdp.snapshot(user_data_dir=str(profile))
    finally:
        fake.stop()
    assert first and second and 'button "Publish" #2' in second
    assert len(probes) == 1  # discovery ran once for two screenshots


def test_click_after_snapshot_reuses_the_tree(tmp_path):
    fake = FakeChrome()
    fake.start()
    try:
        profile = _profile(tmp_path, fake.port)
        assert cdp.snapshot(user_data_dir=str(profile))
        assert cdp.click_node("2", user_data_dir=str(profile)) is True
    finally:
        fake.stop()
    assert fake.calls.count("Accessibility.getFullAXTree") == 1
    assert fake.calls.count("Input.dispatchMouseEvent") == 2
    assert fake.box_ids == [102]


def test_click_refetches_when_the_cached_tree_is_stale(tmp_path, monkeypatch):
    fake = FakeChrome()
    fake.start()
    try:
        profile = _profile(tmp_path, fake.port)
        assert cdp.snapshot(user_data_dir=str(profile))
        monkeypatch.setattr(cdp, "_TREE_TTL", 0.0)
        assert cdp.click_node("2", user_data_dir=str(profile)) is True
    finally:
        fake.stop()
    assert fake.calls.count("Accessibility.getFullAXTree") == 2


def test_click_unknown_id_refetches_then_falls_open(tmp_path):
    fake = FakeChrome()
    fake.start()
    try:
        profile = _profile(tmp_path, fake.port)
        assert cdp.snapshot(user_data_dir=str(profile))
        assert cdp.click_node("999", user_data_dir=str(profile)) is False
    finally:
        fake.stop()
    # the cached tree missed, so a fresh one was fetched before giving up
    assert fake.calls.count("Accessibility.getFullAXTree") == 2
    assert "Input.dispatchMouseEvent" not in fake.calls


def test_cdp_failure_forgets_the_cached_port(tmp_path):
    fake = FakeChrome()
    fake.start()
    profile = _profile(tmp_path, fake.port)
    key = cdp._cache_key(None, str(profile))
    try:
        assert cdp.snapshot(user_data_dir=str(profile))
        assert cdp._cached_port(key) == fake.port
    finally:
        fake.stop()
    assert cdp.snapshot(user_data_dir=str(profile)) is None
    assert cdp._cached_port(key) is None
    assert cdp._cached_tree(key, "/devtools/page/1") is None


def test_snapshot_budget_knob(monkeypatch):
    monkeypatch.delenv("HARNESS_CHROME_CDP_BUDGET", raising=False)
    assert cdp.snapshot_budget() == cdp.DEFAULT_SNAPSHOT_BUDGET
    monkeypatch.setenv("HARNESS_CHROME_CDP_BUDGET", "0.5")
    assert cdp.snapshot_budget() == 0.5
    monkeypatch.setenv("HARNESS_CHROME_CDP_BUDGET", "nope")
    assert cdp.snapshot_budget() == cdp.DEFAULT_SNAPSHOT_BUDGET
    monkeypatch.setenv("HARNESS_CHROME_CDP_BUDGET", "-1")
    assert cdp.snapshot_budget() == cdp.DEFAULT_SNAPSHOT_BUDGET


# -- the screenshot tool ----------------------------------------------------


def _ctx(tmp_path, computer) -> ToolContext:
    paths = HarnessPaths(tmp_path)
    paths.ensure_layout(["atlas"])
    return ToolContext(
        paths=paths, bot="atlas", memory=Memory(paths=paths, bot="atlas"), computer=computer
    )


def test_screenshot_tool_drops_a_slow_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_CHROME_CDP_BUDGET", "0.05")
    released = threading.Event()

    class SlowChrome:
        def screenshot(self):
            return (b"\xff\xd8fakejpeg", "image/jpeg")

        def chrome_snapshot(self):
            released.wait(2)
            return 'Chrome AX (t):\n  button "Late" #2'

    ctx = _ctx(tmp_path, SlowChrome())
    started = time.monotonic()
    try:
        out = default_tools()["computer_screenshot"].handler(ctx, {})
    finally:
        released.set()
    assert time.monotonic() - started < 1.0
    assert out.startswith("ok: screenshot attached")
    assert "Chrome AX" not in out  # the frame went out without the tree
    assert ctx.images == [("image/jpeg", b"\xff\xd8fakejpeg")]


def test_screenshot_tool_overlaps_snapshot_with_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_CHROME_CDP_BUDGET", "2")
    marks: dict[str, float] = {}

    class Both:
        def screenshot(self):
            time.sleep(0.15)
            marks["capture_done"] = time.monotonic()
            return (b"\xff\xd8fakejpeg", "image/jpeg")

        def chrome_snapshot(self):
            marks["snapshot_started"] = time.monotonic()
            return 'Chrome AX (t):\n  button "Publish" #2'

    out = default_tools()["computer_screenshot"].handler(_ctx(tmp_path, Both()), {})
    assert 'button "Publish" #2' in out
    assert marks["snapshot_started"] < marks["capture_done"]


def test_screenshot_tool_swallows_a_raising_snapshot(tmp_path):
    class Raises:
        def screenshot(self):
            return (b"\xff\xd8fakejpeg", "image/jpeg")

        def chrome_snapshot(self):
            raise RuntimeError("cdp exploded")

    out = default_tools()["computer_screenshot"].handler(_ctx(tmp_path, Raises()), {})
    assert out.startswith("ok: screenshot attached")
    assert "Chrome AX" not in out


# -- the display probe ------------------------------------------------------


class _Probe:
    def __init__(self, codes: list[int]) -> None:
        self.codes = list(codes)
        self.calls = 0

    def __call__(self, argv, **kwargs):
        self.calls += 1
        code = self.codes.pop(0) if self.codes else 0

        class P:
            returncode = code

        return P()


def test_display_probe_is_cached_after_a_hit(monkeypatch):
    probe = _Probe([0])
    monkeypatch.setattr(computer_mod.subprocess, "run", probe)
    monkeypatch.setattr("harness.machine_view.exec_prefix", lambda m, **k: ["docker", "exec", m])
    host = HostComputer(machine="harness-machine-1")
    assert host.display_ready() is True
    assert host.display_ready() is True
    assert host.display_ready() is True
    assert probe.calls == 1


def test_display_probe_miss_is_not_cached(monkeypatch):
    probe = _Probe([1, 1, 0])
    monkeypatch.setattr(computer_mod.subprocess, "run", probe)
    monkeypatch.setattr("harness.machine_view.exec_prefix", lambda m, **k: ["docker", "exec", m])
    host = HostComputer(machine="harness-machine-2")
    assert host.display_ready() is False
    assert host.display_ready() is False
    assert host.display_ready() is True  # Xvfb came up: the miss was re-probed
    assert host.display_ready() is True  # ...and the hit is now cached
    assert probe.calls == 3


def test_display_probe_cache_is_per_machine(monkeypatch):
    probe = _Probe([0, 0])
    monkeypatch.setattr(computer_mod.subprocess, "run", probe)
    monkeypatch.setattr("harness.machine_view.exec_prefix", lambda m, **k: ["docker", "exec", m])
    assert HostComputer(machine="m-a").display_ready() is True
    assert HostComputer(machine="m-b").display_ready() is True
    assert probe.calls == 2
    computer_mod.forget_display_ready("m-a")
    assert HostComputer(machine="m-b").display_ready() is True
    assert HostComputer(machine="m-a").display_ready() is True
    assert probe.calls == 3


# -- frames in the tool loop ------------------------------------------------

_NOTE = "[computer screenshot]"


def _frame(n: int) -> Message:
    return Message(role="user", content=_NOTE, images=[("image/jpeg", bytes([n]) * 16)])


def test_prune_loop_images_keeps_the_newest_frames():
    attachment = Message(
        role="user", content="here is my photo", images=[("image/png", b"\x89PNG")]
    )
    messages = [
        Message(role="user", content="open reddit"),
        attachment,
        _frame(1),
        Message(role="assistant", content="clicking"),
        _frame(2),
        _frame(3),
        Message(role="tool", content="ok: click", tool_call_id="c1", name="computer_click"),
        _frame(4),
    ]
    assert prune_loop_images(messages, 2, frame_note=_NOTE) == 2
    assert messages[2].images is None and DROPPED_FRAME_NOTE in messages[2].content
    assert messages[4].images is None and DROPPED_FRAME_NOTE in messages[4].content
    assert messages[5].images and messages[5].content == _NOTE
    assert messages[7].images and messages[7].content == _NOTE
    # the user's own attachment is not a frame carrier and stays intact
    assert attachment.images == [("image/png", b"\x89PNG")]
    assert attachment.content == "here is my photo"
    # a second pass has nothing left to drop
    assert prune_loop_images(messages, 2, frame_note=_NOTE) == 0


def test_prune_loop_images_is_a_no_op_under_the_limit():
    messages = [_frame(1), _frame(2)]
    assert prune_loop_images(messages, 3, frame_note=_NOTE) == 0
    assert all(m.images for m in messages)


def test_loop_image_limit_knob(monkeypatch):
    monkeypatch.delenv("HARNESS_LOOP_IMAGES", raising=False)
    assert loop_image_limit() == DEFAULT_LOOP_IMAGES
    monkeypatch.setenv("HARNESS_LOOP_IMAGES", "5")
    assert loop_image_limit() == 5
    monkeypatch.setenv("HARNESS_LOOP_IMAGES", "0")
    assert loop_image_limit() == 1
    monkeypatch.setenv("HARNESS_LOOP_IMAGES", "many")
    assert loop_image_limit() == DEFAULT_LOOP_IMAGES


class _FrameCounter:
    """Scripted provider that records how many frames each step carried."""

    id = "scripted"
    model = "scripted-1"

    def __init__(self, script):
        self.script = list(script)
        self.frames_per_call: list[int] = []
        self.dropped_per_call: list[int] = []

    def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
        from providers.base import Completion

        self.frames_per_call.append(sum(len(m.images or []) for m in messages))
        self.dropped_per_call.append(
            sum(1 for m in messages if DROPPED_FRAME_NOTE in (m.content or ""))
        )
        return self.script.pop(0) if self.script else Completion(text="done")


def test_loop_drops_old_screenshot_frames_end_to_end(tmp_path, monkeypatch):
    from agent.runtime import build_agent
    from harness.roster import Bot
    from providers.base import Completion, ToolCall

    monkeypatch.setenv("HARNESS_LOOP_IMAGES", "2")
    monkeypatch.setattr(
        "agent.computer.HostComputer.screenshot", lambda self: (b"\xff\xd8fake", "image/jpeg")
    )
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0.0)
    agent.provider = _FrameCounter(
        [
            Completion(tool_calls=[ToolCall(id=f"s{i}", name="computer_screenshot", arguments={})])
            for i in range(5)
        ]
        + [Completion(text="done")]
    )
    assert agent._produce("user", "look at the screen") == "done"
    # step k sees min(k, 2) frames; the rest have been replaced by the note.
    # (The runtime may add a continue nudge after GUI work — one more call,
    # still capped at 2 frames.)
    assert agent.provider.frames_per_call[:6] == [0, 1, 2, 2, 2, 2]
    assert max(agent.provider.frames_per_call) == 2
    assert agent.provider.dropped_per_call[:6] == [0, 0, 0, 1, 2, 3]


# -- Anthropic prompt caching -----------------------------------------------


def _anthropic(**options):
    from providers.anthropic import AnthropicProvider
    from providers.base import Auth

    return AnthropicProvider(model="claude-sonnet-5", auth=Auth(api_key="k"), **options)


def test_anthropic_marks_tools_system_and_last_message():
    from providers.base import ToolSpec

    p = _anthropic()
    body = p._body(
        [
            Message(role="user", content="open reddit"),
            Message(role="assistant", content="ok"),
            Message(role="user", content="now click"),
        ],
        system="be quick",
        tools=[
            ToolSpec(name="a", description="first", parameters={"type": "object"}),
            ToolSpec(name="b", description="second", parameters={"type": "object"}),
        ],
        max_tokens=64,
        temperature=0.5,
        stream=False,
    )
    mark = {"type": "ephemeral"}
    assert "cache_control" not in body["tools"][0]
    assert body["tools"][1]["cache_control"] == mark
    assert body["system"] == [{"type": "text", "text": "be quick", "cache_control": mark}]
    # only the last message is marked; earlier ones keep their plain shape
    assert body["messages"][0] == {"role": "user", "content": "open reddit"}
    assert body["messages"][1] == {"role": "assistant", "content": "ok"}
    assert body["messages"][2]["content"] == [
        {"type": "text", "text": "now click", "cache_control": mark}
    ]


def test_anthropic_marks_the_last_tool_result_block_and_images():
    from providers.base import ToolCall

    p = _anthropic()
    body = p._body(
        [
            Message(role="user", content="look"),
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="t1", name="computer_screenshot", arguments={})],
            ),
            Message(role="tool", content="ok: shot", tool_call_id="t1", name="computer_screenshot"),
        ],
        system=None,
        tools=None,
        max_tokens=64,
        temperature=0.5,
        stream=False,
    )
    last = body["messages"][-1]["content"][-1]
    assert last["type"] == "tool_result"
    assert last["cache_control"] == {"type": "ephemeral"}
    assert "tools" not in body and "system" not in body

    body = p._body(
        [Message(role="user", content="[frames]", images=[("image/jpeg", b"\xff\xd8")])],
        system=None,
        tools=None,
        max_tokens=64,
        temperature=0.5,
        stream=False,
    )
    blocks = body["messages"][-1]["content"]
    assert blocks[0] == {"type": "text", "text": "[frames]"}
    assert blocks[-1]["type"] == "image"
    assert blocks[-1]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_prompt_cache_can_be_turned_off():
    from providers.base import ToolSpec

    p = _anthropic(prompt_cache=False)
    body = p._body(
        [Message(role="user", content="hi")],
        system="be nice",
        tools=[ToolSpec(name="a", description="d", parameters={"type": "object"})],
        max_tokens=64,
        temperature=0.5,
        stream=False,
    )
    assert body["system"] == "be nice"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert "cache_control" not in body["tools"][0]


def test_anthropic_oauth_marks_only_the_last_system_block():
    p = _anthropic(claude_oauth=True)
    body = p._body(
        [Message(role="user", content="hi")],
        system="be helpful",
        tools=None,
        max_tokens=64,
        temperature=0.5,
        stream=False,
    )
    assert isinstance(body["system"], list) and len(body["system"]) == 2
    assert "cache_control" not in body["system"][0]  # the Claude Code identity block
    assert body["system"][1]["text"] == "be helpful"
    assert body["system"][1]["cache_control"] == {"type": "ephemeral"}
