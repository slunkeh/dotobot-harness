import subprocess
import sys

import pytest

from agent.computer import GatedComputer, HostComputer
from agent.memory import Memory
from agent.runtime import (
    _CLARIFY_PROMPT,
    _COMPUTER_PROMPT,
    _CONSULT_PROMPT,
    _RELAY_PROMPT,
    _WORKSPACE_PROMPT,
    Agent,
)
from agent.tools import ToolContext, default_tools
from harness.control import Control
from harness.paths import HarnessPaths
from harness.roster import Bot
from providers.echo import EchoProvider
from tests.fakes import LoggingComputer


def test_visual_computer_use_without_chrome_debugging(tmp_path, monkeypatch):
    from harness import cdp, hostinput, machine_view

    monkeypatch.delenv("HARNESS_CHROME_CDP", raising=False)
    monkeypatch.setattr(cdp, "_probe_port", lambda *a: pytest.fail("debugging must stay off"))
    monkeypatch.setattr(hostinput, "flush", lambda machine: True)
    monkeypatch.setattr(hostinput, "_pos", lambda *a, **k: (200, 300))
    calls = []
    for action in ("click", "type_text", "key", "scroll"):

        def record(*args, _action=action, **kwargs):
            calls.append((_action, args, kwargs))
            return True

        monkeypatch.setattr(hostinput, action, record)
    frame = (b"\xff\xd8fakejpeg", "image/jpeg")
    monkeypatch.setattr(machine_view, "capture_for_model", lambda machine: frame)
    computer = HostComputer(machine="harness-machine-7")
    monkeypatch.setattr(computer, "display_ready", lambda: True)
    assert computer.chrome_snapshot() is None
    paths = HarnessPaths(tmp_path)
    paths.ensure_layout(["atlas"])
    ctx = ToolContext(
        paths=paths, bot="atlas", memory=Memory(paths=paths, bot="atlas"), computer=computer
    )
    actions = [
        ("computer_click", {"node": "stale-node", "x": 0.2, "y": 0.3}),
        ("computer_type", {"text": "search"}),
        ("computer_key", {"key": "Return"}),
        ("computer_scroll", {"amount": 2}),
        ("computer_screenshot", {}),
    ]
    for tool, args in actions:
        assert default_tools()[tool].handler(ctx, args).startswith("ok:")
    assert [call[0] for call in calls] == ["click", "type_text", "key", "scroll"]
    assert all(call[2]["machine"] == "harness-machine-7" for call in calls)
    assert ctx.images == [(frame[1], frame[0])]


def test_screenshot_waits_for_queued_machine_input(tmp_path, monkeypatch):
    from harness import hostinput, machine_view

    marker = tmp_path / "input-finished"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; sys.stdin.read(); Path(sys.argv[1]).write_text('finished')",
            str(marker),
        ],
        stdin=subprocess.PIPE,
        text=True,
    )
    session = hostinput._Session()
    session._proc = proc
    machine = "harness-machine-9"
    monkeypatch.setattr(hostinput, "_machine_sessions", {machine: session})

    def capture(target):
        assert target == machine
        assert marker.read_text() == "finished"
        assert proc.poll() == 0
        return b"fakejpeg", "image/jpeg"

    monkeypatch.setattr(machine_view, "capture_for_model", capture)
    try:
        assert proc.stdin is not None
        proc.stdin.write("type completed before screenshot\n")
        proc.stdin.flush()
        assert HostComputer(machine=machine).screenshot() == (b"fakejpeg", "image/jpeg")
    finally:
        session.close()


@pytest.mark.parametrize("observation", ["screenshot", "chrome_snapshot"])
def test_computer_observations_stop_when_input_cannot_finish(monkeypatch, observation):
    from harness import hostinput

    monkeypatch.setattr(hostinput, "flush", lambda machine: False)
    monkeypatch.setattr(
        "harness.machine_view.capture_for_model", lambda *a: pytest.fail("input did not finish")
    )
    monkeypatch.setattr("harness.cdp.snapshot", lambda **k: pytest.fail("input did not finish"))
    assert getattr(HostComputer(machine="harness-machine-9"), observation)() is None


@pytest.mark.parametrize(
    "name,action,args",
    [
        ("computer_move", "move", {"x": 0.2, "y": 0.3}),
        ("computer_drag", "drag", {"path": [{"x": 0.1, "y": 0.2}, {"x": 0.6, "y": 0.7}]}),
        ("computer_click", "click", {"x": 0.2, "y": 0.3, "clicks": 2}),
        ("computer_scroll", "scroll", {"x": 0.2, "y": 0.3, "amount": 12, "axis": "horizontal"}),
    ],
)
def test_shared_computer_gestures_reach_the_bot_machine(tmp_path, monkeypatch, name, action, args):
    from harness import hostinput

    calls = []
    machine = "harness-machine-7"
    monkeypatch.setattr(hostinput, "_pos", lambda *a, **k: (100, 200))
    monkeypatch.setattr(hostinput, action, lambda *a, **k: calls.append((a, k)) or True)
    computer = HostComputer(machine=machine)
    monkeypatch.setattr(computer, "display_ready", lambda: True)
    paths = HarnessPaths(tmp_path)
    ctx = ToolContext(
        paths=paths, bot="atlas", memory=Memory(paths=paths, bot="atlas"), computer=computer
    )
    out = default_tools()[name].handler(ctx, args)
    assert out.startswith("ok:")
    assert len(calls) == 1
    assert calls[0][1]["machine"] == machine
    if action == "scroll":
        assert calls[0] == (
            (12,),
            {"machine": machine, "nx": 0.2, "ny": 0.3, "axis": "horizontal", "wait": True},
        )
    elif action == "drag":
        assert calls[0][0] == ([(0.1, 0.2), (0.6, 0.7)],)
    elif action == "click":
        assert calls[0][1]["clicks"] == 2


@pytest.mark.parametrize(
    "action,args",
    [
        ("move", {"x": 0.5}),
        ("move", {"x": float("inf"), "y": 0.5}),
        ("drag", {"path": [{"x": 0.1, "y": 0.2}]}),
        ("drag", {"path": [{"x": 0.1, "y": 0.2}, {"x": "bad", "y": 0.2}]}),
        ("scroll", {"amount": 21}),
        ("scroll", {"amount": 1, "x": 0.5}),
        ("scroll", {"amount": 1, "axis": "diagonal"}),
        ("click", {"x": 0.2, "y": 0.3, "clicks": 3}),
        ("click", {"node": "12", "clicks": 2}),
    ],
)
def test_invalid_computer_gesture_is_an_actionable_error(monkeypatch, action, args):
    from harness import hostinput

    monkeypatch.setattr(
        hostinput, action, lambda *a, **k: pytest.fail("invalid action must not reach input")
    )
    assert HostComputer().act(action, **args).startswith("error:")


def test_double_click_does_not_silently_become_single_ax_click(monkeypatch):
    monkeypatch.setattr(
        "harness.cdp.click_node",
        lambda *a, **k: pytest.fail("AX only supports a single left click"),
    )
    monkeypatch.setattr("harness.hostinput.click", lambda *a, **k: k["clicks"] == 2)
    monkeypatch.setattr("harness.hostinput._pos", lambda *a, **k: (100, 200))
    assert HostComputer().act("click", node="12", x=0.1, y=0.2, clicks=2).startswith("ok:")


@pytest.mark.parametrize("finished", [False, True])
def test_ax_click_waits_for_prior_queued_input(monkeypatch, finished):
    calls = []
    machine = "harness-machine-7"

    def flush(target):
        assert target == machine
        calls.append("input finished" if finished else "input pending")
        return finished

    def click_node(node, **kwargs):
        assert calls == ["input finished"]
        assert kwargs["machine"] == machine
        calls.append("clicked node")
        return True

    monkeypatch.setattr("harness.hostinput.flush", flush)
    monkeypatch.setattr("harness.cdp.click_node", click_node)
    result = HostComputer(machine=machine).act("click", node="12")
    assert result.startswith("ok:" if finished else "error:")
    assert calls[-1] == ("clicked node" if finished else "input pending")


@pytest.mark.parametrize("finished", [False, True])
def test_typing_drains_previous_paste_before_replacing_clipboard(monkeypatch, finished):
    calls = []
    machine = "harness-machine-7"

    def flush(target):
        assert target == machine
        calls.append("previous paste finished" if finished else "previous paste pending")
        return finished

    def type_text(text, **kwargs):
        assert calls == ["previous paste finished"]
        assert kwargs["machine"] == machine
        assert text == "second field"
        calls.append("replace clipboard and type")
        return True

    monkeypatch.setattr("harness.hostinput.flush", flush)
    monkeypatch.setattr("harness.hostinput.type_text", type_text)
    result = HostComputer(machine=machine).act("type", text="second field")
    assert result.startswith("ok:" if finished else "error:")
    assert calls[-1] == ("replace clipboard and type" if finished else "previous paste pending")


def test_host_computer_opens_file_manager(monkeypatch):
    launched: list[list[str]] = []
    monkeypatch.setattr("agent.computer.computer_env.find_file_manager", lambda: "/usr/bin/thunar")
    monkeypatch.setattr(
        "agent.computer._launch", lambda argv: launched.append(argv) or f"ok: launched {argv[0]}"
    )
    out = HostComputer().act("open", app="files")
    assert "thunar" in out
    assert launched == [["/usr/bin/thunar"]]


def test_gated_computer_runs_during_takeover(tmp_path):
    """Human and bot share the desktop — takeover no longer refuses GUI tools."""
    paths = HarnessPaths(tmp_path)
    paths.ensure_layout(["atlas"])
    control = Control(paths)
    control.take_over("atlas")
    gated = GatedComputer(LoggingComputer(), control, "atlas")
    assert gated.act("open", app="files").startswith("ok:")


def test_any_takeover_leaves_other_bots_free(tmp_path):
    """Shared computer: a human driving via bot A does not pause bot B."""
    paths = HarnessPaths(tmp_path)
    paths.ensure_layout(["atlas", "nova"])
    control = Control(paths)
    nova = GatedComputer(LoggingComputer(), control, "nova")

    control.take_over("atlas")
    assert nova.act("open", app="files").startswith("ok:")

    control.return_control("atlas")
    assert nova.act("open", app="files").startswith("ok:")


def test_per_bot_display_still_runs_under_another_hold(tmp_path, monkeypatch):
    """Per-bot desktops keep working regardless of another bot's control mode."""
    paths = HarnessPaths(tmp_path)
    paths.ensure_layout(["atlas", "nova"])
    control = Control(paths)
    control.take_over("atlas")

    monkeypatch.setenv("HARNESS_SHARED_DISPLAY", "0")
    nova = GatedComputer(LoggingComputer(), control, "nova")
    assert nova.act("open", app="files").startswith("ok:")

    atlas = GatedComputer(LoggingComputer(), control, "atlas")
    assert atlas.act("open", app="files").startswith("ok:")

    monkeypatch.delenv("HARNESS_SHARED_DISPLAY")
    scoped = GatedComputer(LoggingComputer(), control, "nova", shared_display=False)
    assert scoped.act("open", app="files").startswith("ok:")


def test_teach_mode_runs_actions_instead_of_recording(tmp_path):
    paths = HarnessPaths(tmp_path)
    paths.ensure_layout(["atlas", "nova"])
    control = Control(paths)
    control.start_teach("atlas")

    atlas = GatedComputer(LoggingComputer(), control, "atlas")
    assert atlas.act("open", app="files").startswith("ok:")

    nova = GatedComputer(LoggingComputer(), control, "nova")
    assert nova.act("click", x=0.5, y=0.5).startswith("ok:")


def test_computer_open_tool_uses_host(monkeypatch, tmp_path):
    monkeypatch.setattr("agent.computer.computer_env.find_file_manager", lambda: "/usr/bin/thunar")
    monkeypatch.setattr("agent.computer._launch", lambda argv: f"ok: launched {argv[0]}")
    paths = HarnessPaths(tmp_path)
    paths.ensure_layout(["atlas"])
    ctx = ToolContext(
        paths=paths,
        bot="atlas",
        memory=Memory(paths=paths, bot="atlas"),
        computer=GatedComputer(HostComputer(), Control(paths), "atlas"),
    )
    tools = default_tools()
    assert "computer_open" in tools
    out = tools["computer_open"].handler(ctx, {"app": "file browser"})
    assert "thunar" in out


def test_computer_open_runs_during_takeover(tmp_path):
    """A GUI action during takeover is not refused."""
    paths = HarnessPaths(tmp_path)
    paths.ensure_layout(["atlas"])
    control = Control(paths)
    control.take_over("atlas")
    inner = LoggingComputer()
    ctx = ToolContext(
        paths=paths,
        bot="atlas",
        memory=Memory(paths=paths, bot="atlas"),
        control=control,
        computer=GatedComputer(inner, control, "atlas"),
    )
    out = default_tools()["computer_open"].handler(ctx, {"app": "files"})
    assert out.startswith("ok:")
    assert inner.actions


def test_system_prompt_tells_bot_it_can_use_computer(tmp_path):
    paths = HarnessPaths(tmp_path)
    paths.ensure_layout(["atlas"])
    bot = Bot(name="atlas", role="assistant", provider="echo")
    agent = Agent(
        paths=paths,
        bot=bot,
        provider=EchoProvider(),
        memory=Memory(paths=paths, bot="atlas"),
        control=Control(paths),
    )
    prompt = agent.system_prompt("open files")
    assert "computer_open" in prompt
    assert _COMPUTER_PROMPT in prompt
    assert "Desktop, Downloads, Chrome profile" in prompt or "survive a restart" in prompt
    assert _CLARIFY_PROMPT in prompt
    assert "ask_user_choice" in prompt
    assert "run_command" in prompt
    assert "Fall back to curl via run_command only if the browser" in prompt
    assert "visit a website" in prompt
    assert "robots.txt, sitemap, and anything not needed visually should be curl" in prompt
    assert _RELAY_PROMPT in prompt
    assert "message_agent" in prompt
    consult = agent.system_prompt("list env names", consult=True)
    assert _CONSULT_PROMPT in consult
    assert _COMPUTER_PROMPT not in consult


def test_machine_bots_are_told_what_the_shared_workspace_is(tmp_path, monkeypatch):
    """On the machines backend (HARNESS_MACHINE_NAME set) the prompt spells out
    the handoff contract: private computer, /workspace shared, path + message,
    no secrets there. Process-backend bots have no such directory."""
    paths = HarnessPaths(tmp_path)
    paths.ensure_layout(["atlas"])
    bot = Bot(name="atlas", role="assistant", provider="echo")
    agent = Agent(
        paths=paths,
        bot=bot,
        provider=EchoProvider(),
        memory=Memory(paths=paths, bot="atlas"),
        control=Control(paths),
    )
    monkeypatch.delenv("HARNESS_MACHINE_NAME", raising=False)
    assert _WORKSPACE_PROMPT not in agent.system_prompt("hello")
    monkeypatch.setenv("HARNESS_MACHINE_NAME", "harness-machine-0")
    monkeypatch.delenv("HARNESS_MACHINE_WORKSPACE", raising=False)
    prompt = agent.system_prompt("hello")
    assert _WORKSPACE_PROMPT in prompt
    # an opted-out machine (no shared volume) must not describe its private
    # /workspace as the handoff surface
    monkeypatch.setenv("HARNESS_MACHINE_WORKSPACE", "0")
    assert _WORKSPACE_PROMPT not in agent.system_prompt("hello")
    monkeypatch.setenv("HARNESS_MACHINE_WORKSPACE", "1")
    assert _WORKSPACE_PROMPT in agent.system_prompt("hello")
    assert "/workspace/<project>/" in prompt
    assert "message_agent" in _WORKSPACE_PROMPT
    assert "never onto the shared disk" in _WORKSPACE_PROMPT
    assert "/workspace" not in _COMPUTER_PROMPT, "process-backend bots have no /workspace"


def test_run_command_returns_stdout(tmp_path):
    paths = HarnessPaths(tmp_path)
    paths.ensure_layout(["atlas"])
    ctx = ToolContext(paths=paths, bot="atlas", memory=Memory(paths=paths, bot="atlas"))
    out = default_tools()["run_command"].handler(ctx, {"command": "echo hello-harness"})
    assert "hello-harness" in out
    assert "exit 0" in out


def test_run_command_requires_command(tmp_path):
    paths = HarnessPaths(tmp_path)
    paths.ensure_layout(["atlas"])
    ctx = ToolContext(paths=paths, bot="atlas", memory=Memory(paths=paths, bot="atlas"))
    out = default_tools()["run_command"].handler(ctx, {})
    assert out.startswith("error:")


def test_computer_screenshot_attaches_image(tmp_path):
    class FakeComputer:
        def act(self, action: str, **params) -> str:
            return "ok"

        def screenshot(self):
            return (b"\xff\xd8fakejpeg", "image/jpeg")

    paths = HarnessPaths(tmp_path)
    paths.ensure_layout(["atlas"])
    ctx = ToolContext(
        paths=paths,
        bot="atlas",
        memory=Memory(paths=paths, bot="atlas"),
        computer=FakeComputer(),
    )
    tools = default_tools()
    assert "computer_screenshot" in tools
    out = tools["computer_screenshot"].handler(ctx, {})
    assert out.startswith("ok:")
    assert ctx.images == [("image/jpeg", b"\xff\xd8fakejpeg")]


def test_computer_screenshot_runs_during_takeover(tmp_path):
    class FakeComputer:
        def act(self, action: str, **params) -> str:
            return "ok"

        def screenshot(self):
            return (b"\xff\xd8fakejpeg", "image/jpeg")

    paths = HarnessPaths(tmp_path)
    paths.ensure_layout(["atlas"])
    control = Control(paths)
    control.take_over("atlas")
    ctx = ToolContext(
        paths=paths,
        bot="atlas",
        memory=Memory(paths=paths, bot="atlas"),
        computer=GatedComputer(FakeComputer(), control, "atlas"),
    )
    out = default_tools()["computer_screenshot"].handler(ctx, {})
    assert out.startswith("ok:")
    assert ctx.images == [("image/jpeg", b"\xff\xd8fakejpeg")]


def test_computer_tools_stop_when_display_is_down(tmp_path):
    class DownComputer:
        def display_ready(self) -> bool:
            return False

        def act(self, action: str, **params) -> str:
            raise AssertionError("GUI must not run without a display")

        def screenshot(self):
            raise AssertionError("screenshot must not run without a display")

    paths = HarnessPaths(tmp_path)
    paths.ensure_layout(["atlas"])
    ctx = ToolContext(
        paths=paths,
        bot="atlas",
        memory=Memory(paths=paths, bot="atlas"),
        computer=DownComputer(),
    )
    tools = default_tools()
    for name, args in (
        ("computer_open", {"app": "files"}),
        ("computer_move", {"x": 0.2, "y": 0.3}),
        ("computer_drag", {"path": [{"x": 0.1, "y": 0.2}, {"x": 0.6, "y": 0.7}]}),
        ("computer_click", {"x": 0.2, "y": 0.3, "clicks": 2}),
        ("computer_scroll", {"x": 0.2, "y": 0.3, "amount": 12, "axis": "horizontal"}),
    ):
        out = tools[name].handler(ctx, args)
        assert out.startswith("error: display unavailable")
        assert "Do not retry" in out
    shot = tools["computer_screenshot"].handler(ctx, {})
    assert shot.startswith("error: display unavailable")
    assert ctx.images == []


def test_system_prompt_tells_bot_to_screenshot(tmp_path):
    paths = HarnessPaths(tmp_path)
    paths.ensure_layout(["atlas"])
    bot = Bot(name="atlas", role="assistant", provider="echo")
    agent = Agent(
        paths=paths,
        bot=bot,
        provider=EchoProvider(),
        memory=Memory(paths=paths, bot="atlas"),
        control=Control(paths),
    )
    prompt = agent.system_prompt("open chrome")
    assert "computer_screenshot" in prompt
    assert "cannot see the screen" in prompt
