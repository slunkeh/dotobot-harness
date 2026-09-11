"""Browser profile split: shared login jar, isolated sessions."""

from __future__ import annotations

from agent.browser import SHARED_PROFILE_FILES, ensure_profile_split, plan_profile_split
from agent.computer import HostComputer
from harness import computer_env as ce
from harness.paths import HarnessPaths


def _paths(tmp_path) -> HarnessPaths:
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas", "nova"])
    return p


def test_ensure_profile_split_creates_jar_symlinks(tmp_path):
    paths = _paths(tmp_path)
    plan = plan_profile_split(paths, "atlas")
    ensure_profile_split(plan)

    default = plan.private_user_data_dir / "Default"
    assert default.is_dir()
    for name in SHARED_PROFILE_FILES:
        link = default / name
        assert link.is_symlink()
        assert link.resolve() == (paths.browser_cookies / name).resolve()


def test_ensure_profile_split_is_idempotent_and_shares_across_bots(tmp_path):
    paths = _paths(tmp_path)
    for bot in ("atlas", "nova"):
        plan = plan_profile_split(paths, bot)
        ensure_profile_split(plan)
        ensure_profile_split(plan)  # second run must not fail or duplicate

    # a login written via atlas's Cookies link lands in the jar and is
    # readable through nova's link
    (plan_profile_split(paths, "atlas").private_user_data_dir / "Default" / "Cookies").write_text(
        "session=abc", encoding="utf-8"
    )
    via_nova = plan_profile_split(paths, "nova").private_user_data_dir / "Default" / "Cookies"
    assert via_nova.read_text(encoding="utf-8") == "session=abc"
    assert (paths.browser_cookies / "Cookies").read_text(encoding="utf-8") == "session=abc"


def test_ensure_profile_split_adopts_existing_profile_file(tmp_path):
    paths = _paths(tmp_path)
    plan = plan_profile_split(paths, "atlas")
    default = plan.private_user_data_dir / "Default"
    default.mkdir(parents=True)
    (default / "Cookies").write_text("already-logged-in", encoding="utf-8")

    ensure_profile_split(plan)
    assert (default / "Cookies").is_symlink()
    assert (paths.browser_cookies / "Cookies").read_text(encoding="utf-8") == "already-logged-in"


def test_ensure_profile_split_never_clobbers_divergent_file(tmp_path):
    paths = _paths(tmp_path)
    paths.browser_cookies.mkdir(parents=True, exist_ok=True)
    (paths.browser_cookies / "Cookies").write_text("jar-state", encoding="utf-8")
    plan = plan_profile_split(paths, "atlas")
    default = plan.private_user_data_dir / "Default"
    default.mkdir(parents=True)
    (default / "Cookies").write_text("local-divergent", encoding="utf-8")

    ensure_profile_split(plan)
    assert not (default / "Cookies").is_symlink()
    assert (default / "Cookies").read_text(encoding="utf-8") == "local-divergent"
    assert (paths.browser_cookies / "Cookies").read_text(encoding="utf-8") == "jar-state"


def test_desktop_bringup_uses_split_profile(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    launched: list[list[str]] = []
    monkeypatch.setenv("DISPLAY", ":99")
    # The machine look ships the dock alone and leaves Chrome to the bot or
    # the user, so only the host desktop opens a browser at bringup — which
    # is the launch whose profile this test is about.
    monkeypatch.setenv("HARNESS_DESKTOP_STYLE", "host")
    monkeypatch.setattr(ce, "find_browser", lambda: "/usr/bin/chromium")
    monkeypatch.setattr(ce, "find_file_manager", lambda: None)
    monkeypatch.setattr(ce, "find_terminal", lambda: None)
    monkeypatch.setattr(ce.shutil, "which", lambda name: None)
    monkeypatch.setattr(ce, "_spawn", lambda argv, env: launched.append(argv) or True)
    monkeypatch.setattr(ce, "_running", lambda name: False)

    ce.bringup(paths, display=":99")

    desktop_profile = str(paths.browser_sessions / "desktop")
    browser_cmds = [argv for argv in launched if argv and "chromium" in argv[0]]
    assert browser_cmds, launched
    assert any(a == f"--user-data-dir={desktop_profile}" for a in browser_cmds[0])
    # the split materialized for the desktop identity
    assert (paths.browser_sessions / "desktop" / "Default" / "Cookies").is_symlink()


def test_host_computer_opens_browser_with_bot_profile(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    launched: list[list[str]] = []
    monkeypatch.setattr("agent.computer.computer_env.find_browser", lambda: "/usr/bin/chromium")
    monkeypatch.setattr(ce, "find_browser", lambda: "/usr/bin/chromium")
    monkeypatch.setattr(
        "agent.computer._launch", lambda argv: launched.append(argv) or "ok: launched"
    )

    out = HostComputer(paths=paths, bot="atlas").act("open", app="browser")
    assert out.startswith("ok:")
    bot_profile = str(paths.bot_session("atlas"))
    assert any(a == f"--user-data-dir={bot_profile}" for a in launched[0])
    assert (paths.bot_session("atlas") / "Default" / "Cookies").is_symlink()


def test_host_computer_without_paths_keeps_bare_launch(tmp_path, monkeypatch):
    launched: list[list[str]] = []
    monkeypatch.setattr("agent.computer.computer_env.find_browser", lambda: "/usr/bin/chromium")
    monkeypatch.setattr(
        "agent.computer._launch", lambda argv: launched.append(argv) or "ok: launched"
    )
    HostComputer().act("open", app="browser")
    assert launched == [["/usr/bin/chromium"]]


# -- private_browser: opt out of the shared jar -----------------------------


def test_private_browser_plan_has_no_jar_symlinks(tmp_path):
    """A `private_browser` bot's Chrome starts with an empty jar: no login
    symlinks materialize and the shared jar is never touched, so an injected
    page in its browser has no ambient logins to spend."""
    paths = _paths(tmp_path)
    plan = plan_profile_split(paths, "atlas", shared_logins=False)
    assert plan.symlink_map() == {}
    ensure_profile_split(plan)

    default = plan.private_user_data_dir / "Default"
    assert default.is_dir()
    for name in SHARED_PROFILE_FILES:
        assert not (default / name).exists()
    assert list(paths.browser_cookies.iterdir()) == []  # nothing written to the jar


def test_flipping_private_browser_removes_existing_jar_links(tmp_path):
    """A bot that shared before the flag flipped has jar symlinks from
    earlier launches. The next private launch must remove them — otherwise
    `private_browser = true` on an existing bot still spends shared logins."""
    paths = _paths(tmp_path)
    shared = plan_profile_split(paths, "atlas")
    ensure_profile_split(shared)
    default = shared.private_user_data_dir / "Default"
    (default / "Cookies").write_text("session=abc", encoding="utf-8")  # via the link

    private = plan_profile_split(paths, "atlas", shared_logins=False)
    ensure_profile_split(private)
    for name in SHARED_PROFILE_FILES:
        assert not (default / name).is_symlink(), name
    assert not (default / "Cookies").exists()
    # the jar's own file is untouched
    assert (paths.browser_cookies / "Cookies").read_text(encoding="utf-8") == "session=abc"


def test_private_flip_leaves_divergent_real_files_alone(tmp_path):
    paths = _paths(tmp_path)
    plan = plan_profile_split(paths, "atlas", shared_logins=False)
    default = plan.private_user_data_dir / "Default"
    default.mkdir(parents=True)
    (default / "Cookies").write_text("my-own-logins", encoding="utf-8")  # real file

    ensure_profile_split(plan)
    assert (default / "Cookies").read_text(encoding="utf-8") == "my-own-logins"


def test_private_browser_bot_does_not_see_shared_logins(tmp_path):
    paths = _paths(tmp_path)
    shared = plan_profile_split(paths, "nova")
    ensure_profile_split(shared)
    (shared.private_user_data_dir / "Default" / "Cookies").write_text(
        "session=abc", encoding="utf-8"
    )

    private = plan_profile_split(paths, "atlas", shared_logins=False)
    ensure_profile_split(private)
    assert not (private.private_user_data_dir / "Default" / "Cookies").exists()
    # and the shared bot still shares
    assert (paths.browser_cookies / "Cookies").read_text(encoding="utf-8") == "session=abc"


def test_host_computer_honors_private_browser(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    launched: list[list[str]] = []
    monkeypatch.setattr("agent.computer.computer_env.find_browser", lambda: "/usr/bin/chromium")
    monkeypatch.setattr(
        "agent.computer._launch", lambda argv: launched.append(argv) or "ok: launched"
    )

    out = HostComputer(paths=paths, bot="atlas", private_browser=True).act("open", app="browser")
    assert out.startswith("ok:")
    bot_profile = str(paths.bot_session("atlas"))
    assert any(a == f"--user-data-dir={bot_profile}" for a in launched[0])
    assert not (paths.bot_session("atlas") / "Default" / "Cookies").exists()


def test_private_browser_survives_orchestrator_create_and_update(tmp_path, monkeypatch):
    """On a live home `roster.json` is the roster of record, so the flag must
    ride add_bot / update_bot and land in the JSON store — a `roster.toml`
    edit alone does not reach an existing deployment."""
    import json

    from harness.orchestrator import Orchestrator

    rp = tmp_path / "roster.toml"
    rp.write_text('[[bots]]\nname = "atlas"\nprovider = "echo"\n', encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    restarts: list[str] = []
    monkeypatch.setattr(orch, "restart", lambda name: restarts.append(name))

    bot = orch.add_bot(name="scout", provider="echo", private_browser=True, start=False)
    assert bot.private_browser is True
    store = json.loads((orch.paths.home / "roster.json").read_text(encoding="utf-8"))
    assert next(b for b in store["bots"] if b["name"] == "scout")["private_browser"] is True

    # update flips it, persists it, and restarts (the agent read it at spawn)
    orch.update_bot("scout", private_browser=False)
    assert orch.roster.get("scout").private_browser is False
    assert restarts == ["scout"]


# -- machine mode (machines backend): real profile, no symlink jar ----------


def test_machine_agent_opens_browser_inside_machine(tmp_path, monkeypatch):
    """HARNESS_MACHINE_NAME routes the launch into the machine with the
    machine's real Chrome profile — the symlink split must not materialize."""
    monkeypatch.delenv("HARNESS_CHROME_CDP", raising=False)
    paths = _paths(tmp_path)
    monkeypatch.setenv("HARNESS_MACHINE_NAME", "harness-machine-0")
    launched: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        "harness.machine_view.launch",
        lambda machine, argv: launched.append((machine, argv)) or True,
    )

    out = HostComputer(paths=paths, bot="atlas").act("open", app="browser")
    assert out.startswith("ok:")
    machine, argv = launched[0]
    assert machine == "harness-machine-0"
    # live argv, not the baked launch-chrome wrapper — that wrapper is
    # written once at bringup and would ignore a later kill-switch flip
    assert argv[0] == "google-chrome"
    assert argv[-1] == "https://www.google.com"
    assert not any(arg.startswith("--remote-debugging") for arg in argv)
    assert "--test-type" not in argv
    # no split, no jar links: login sharing is the machine-state sync's job
    assert not (paths.bot_session("atlas") / "Default" / "Cookies").exists()


def test_machine_open_browser_honours_cdp_kill_switch(tmp_path, monkeypatch):
    """Flip plus relaunch must not keep --remote-debugging-port on the
    primary machine path (live argv, not the baked wrapper)."""
    paths = _paths(tmp_path)
    monkeypatch.setenv("HARNESS_MACHINE_NAME", "harness-machine-0")
    monkeypatch.setenv("HARNESS_CHROME_CDP", "0")
    launched: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        "harness.machine_view.launch",
        lambda machine, argv: launched.append((machine, argv)) or True,
    )

    out = HostComputer(paths=paths, bot="atlas").act("open", app="browser")
    assert out.startswith("ok:")
    assert len(launched) == 1
    argv = launched[0][1]
    assert argv[0] == "google-chrome"
    assert not any(a.startswith("--remote-debugging") for a in argv)
    assert "--force-renderer-accessibility" not in argv
    assert not str(argv[0]).endswith("launch-chrome")


def test_machine_bringup_uses_real_profile_and_dock(tmp_path, monkeypatch):
    """Inside a machine (HARNESS_MACHINE=1) the desktop is the 3-icon dock.
    Chrome / Thunar / xterm are not auto-opened — the dock launches them."""
    paths = _paths(tmp_path)
    fake_home = tmp_path / "agent-home"
    fake_home.mkdir()
    launched: list[list[str]] = []
    monkeypatch.setenv("HARNESS_MACHINE", "1")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: fake_home))
    monkeypatch.setattr(ce, "find_browser", lambda: "/usr/bin/google-chrome")
    monkeypatch.setattr(ce, "find_file_manager", lambda: "/usr/bin/thunar")
    monkeypatch.setattr(ce, "find_terminal", lambda: "/usr/bin/xterm")
    monkeypatch.setattr(ce.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(ce, "_spawn", lambda argv, env: launched.append(argv) or True)
    monkeypatch.setattr(ce, "_running", lambda name: False)

    report = ce.bringup(paths, display=":0")

    apps = [argv[0] for argv in launched if argv]
    assert not any("chrome" in a or a.endswith("thunar") or a.endswith("xterm") for a in apps), (
        launched
    )
    assert not (paths.browser_sessions / "desktop" / "Default" / "Cookies").exists()
    rc = (paths.home / "desktop" / "tint2rc").read_text(encoding="utf-8")
    assert "panel_items = L\n" in rc
    assert "panel_position = bottom center horizontal" in rc
    assert "taskbar_name" not in rc
    desktop = (paths.home / "desktop" / "launchers" / "chrome.desktop").read_text()
    assert "Name=Chrome" in desktop
    assert report["taskbar"] == ["Chrome", "Thunar", "xterm"]
