import os

import pytest

from harness import computer_env as ce
from harness.paths import HarnessPaths


@pytest.mark.parametrize("mode", ["machine", "host", "dock"])
def test_chrome_profile_launch_has_no_debugging_or_test_mode_by_default(
    tmp_path, monkeypatch, mode
):
    import subprocess

    monkeypatch.delenv("HARNESS_CHROME_CDP", raising=False)
    monkeypatch.setattr(ce, "machine_browser", lambda: "google-chrome")
    monkeypatch.setattr(ce, "find_browser", lambda: "google-chrome")
    profile = tmp_path / "profile"
    if mode == "host":
        argv = ce.browser_command("https://accounts.google.com", profile, no_sandbox=True)
    else:
        argv = ce.machine_browser_command("https://accounts.google.com", profile)
    if mode == "dock":
        stub = tmp_path / "chrome-stub"
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        stub.chmod(0o755)
        wrapper = ce.write_chrome_wrapper(tmp_path, [str(stub), *argv[1:-1]], profile)
        result = subprocess.run(
            [str(wrapper), argv[-1]], capture_output=True, text=True, check=True
        )
        argv = result.stdout.splitlines()
    assert not any(arg.startswith("--remote-debugging") for arg in argv)
    assert "--test-type" not in argv
    assert "--force-renderer-accessibility" not in argv
    assert f"--user-data-dir={profile}" in argv
    assert argv[-1] == "https://accounts.google.com"


def test_machine_bringup_uses_bundled_wallpaper(tmp_path, monkeypatch):
    photo = tmp_path / "field.jpg"
    photo.write_bytes(b"jpg")
    launched: list[list[str]] = []
    monkeypatch.setattr(ce, "_BUNDLED_WALLPAPER", photo)
    monkeypatch.setenv("HARNESS_MACHINE", "1")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("HARNESS_WALLPAPER", raising=False)
    monkeypatch.setattr(ce, "find_browser", lambda: "/usr/bin/google-chrome")
    monkeypatch.setattr(ce, "find_file_manager", lambda: "/usr/bin/thunar")
    monkeypatch.setattr(ce, "find_terminal", lambda: "/usr/bin/xterm")
    monkeypatch.setattr(
        ce.shutil,
        "which",
        lambda name: "/usr/bin/" + name if name in {"feh", "openbox", "tint2"} else None,
    )
    monkeypatch.setattr(ce, "_spawn", lambda argv, env: launched.append(list(argv)) or True)
    monkeypatch.setattr(ce, "_running", lambda name: False)
    from harness.paths import HarnessPaths

    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    ce.bringup(paths, display=":0")
    wall = [argv for argv in launched if argv and argv[0] == "feh"]
    assert wall and wall[0][1:3] == ["--bg-fill", str(photo)]
    assert not any(
        "chrome" in argv[0] or argv[0].endswith("thunar") or argv[0].endswith("xterm")
        for argv in launched
    )


def test_product_computer_defaults_to_machine_look(monkeypatch):
    monkeypatch.delenv("HARNESS_DESKTOP_STYLE", raising=False)
    monkeypatch.delenv("HARNESS_MACHINE", raising=False)
    monkeypatch.delenv("HARNESS_MACHINE_NAME", raising=False)
    assert ce.product_computer() is True
    monkeypatch.setenv("HARNESS_DESKTOP_STYLE", "host")
    assert ce.product_computer() is False


def test_desktop_style_machine_matches_product_look(tmp_path, monkeypatch):
    """Tenant --desktop (no HARNESS_MACHINE) still gets the field + dock."""
    photo = tmp_path / "field.jpg"
    photo.write_bytes(b"jpg")
    launched: list[list[str]] = []
    monkeypatch.setattr(ce, "_BUNDLED_WALLPAPER", photo)
    monkeypatch.setenv("HARNESS_DESKTOP_STYLE", "machine")
    monkeypatch.delenv("HARNESS_MACHINE", raising=False)
    monkeypatch.delenv("HARNESS_MACHINE_NAME", raising=False)
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.delenv("HARNESS_WALLPAPER", raising=False)
    monkeypatch.setattr(ce, "find_browser", lambda: "/usr/bin/google-chrome")
    monkeypatch.setattr(ce, "find_file_manager", lambda: "/usr/bin/thunar")
    monkeypatch.setattr(ce, "find_terminal", lambda: "/usr/bin/xterm")
    monkeypatch.setattr(
        ce.shutil,
        "which",
        lambda name: "/usr/bin/" + name if name in {"feh", "openbox", "tint2"} else None,
    )
    monkeypatch.setattr(ce, "_spawn", lambda argv, env: launched.append(list(argv)) or True)
    monkeypatch.setattr(ce, "_running", lambda name: False)
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    ce.bringup(paths, display=":99")
    wall = [argv for argv in launched if argv and argv[0] == "feh"]
    assert wall and wall[0][1:3] == ["--bg-fill", str(photo)]
    rc = (paths.home / "desktop" / "tint2rc").read_text()
    assert "panel_items = L\n" in rc
    assert "panel_size = 210 62" in rc
    assert not any(argv[0].endswith("thunar") or argv[0].endswith("xterm") for argv in launched)


def test_icon_prefers_existing_pixmap(tmp_path):
    png = tmp_path / "chromium.png"
    png.write_bytes(b"png")
    assert ce._icon([str(png), "web-browser"]) == str(png)
    assert ce._icon(["missing-theme-name"]) == "missing-theme-name"


def test_write_launchers(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_FILE_MANAGER", raising=False)
    monkeypatch.delenv("HARNESS_TERMINAL", raising=False)
    monkeypatch.setattr(ce, "find_file_manager", lambda: None)
    monkeypatch.setattr(ce, "find_terminal", lambda: None)
    cfg = tmp_path / "desktop"
    files = ce.write_launchers(cfg)
    names = {f.name for f in files}
    assert names == {"chrome.desktop", "thunar.desktop", "xterm.desktop"}
    chrome = (cfg / "launchers" / "chrome.desktop").read_text()
    assert "Name=Chrome" in chrome
    assert "Exec=" in chrome
    assert "Icon=" in chrome
    assert any(name in chrome for name in ("google-chrome", "chromium", "web-browser"))
    thunar = (cfg / "launchers" / "thunar.desktop").read_text()
    assert "Name=Thunar" in thunar
    assert "Exec=thunar %U" in thunar
    xterm = (cfg / "launchers" / "xterm.desktop").read_text()
    assert "Name=xterm" in xterm
    assert "Exec=xterm %U" in xterm
    assert "terminal-icon.png" in xterm
    assert "Icon=xterm\n" not in xterm


def test_find_file_manager_prefers_thunar(monkeypatch):
    monkeypatch.delenv("HARNESS_FILE_MANAGER", raising=False)

    def fake_which(name):
        return {"thunar": "/usr/bin/thunar", "pcmanfm": "/usr/bin/pcmanfm"}.get(name)

    monkeypatch.setattr(ce.shutil, "which", fake_which)
    assert ce.find_file_manager() == "/usr/bin/thunar"


def test_find_terminal_prefers_xterm(monkeypatch):
    monkeypatch.delenv("HARNESS_TERMINAL", raising=False)

    def fake_which(name):
        return {
            "xterm": "/usr/bin/xterm",
            "xfce4-terminal": "/usr/bin/xfce4-terminal",
        }.get(name)

    monkeypatch.setattr(ce.shutil, "which", fake_which)
    assert ce.find_terminal() == "/usr/bin/xterm"


def test_find_file_manager_env_override(monkeypatch):
    monkeypatch.setenv("HARNESS_FILE_MANAGER", "/opt/my/thunar")
    assert ce.find_file_manager() == "/opt/my/thunar"


def test_find_terminal_env_override(monkeypatch):
    monkeypatch.setenv("HARNESS_TERMINAL", "/opt/my/xterm")
    assert ce.find_terminal() == "/opt/my/xterm"


def test_clear_stale_chrome_singleton_other_host(tmp_path, monkeypatch):
    profile = tmp_path / "harness-chrome"
    profile.mkdir()
    (profile / "SingletonLock").symlink_to("otherhost-99")
    (profile / "SingletonSocket").write_text("x")
    (profile / "Cookies").write_text("keep")
    monkeypatch.setattr(ce.socket, "gethostname", lambda: "thishost")
    assert ce.clear_stale_chrome_singleton(profile) is True
    assert not (profile / "SingletonLock").exists()
    assert not (profile / "SingletonSocket").exists()
    assert (profile / "Cookies").read_text() == "keep"


def test_clear_stale_chrome_singleton_keeps_live_lock(tmp_path, monkeypatch):
    profile = tmp_path / "harness-chrome"
    profile.mkdir()
    monkeypatch.setattr(ce.socket, "gethostname", lambda: "thishost")
    (profile / "SingletonLock").symlink_to(f"thishost-{os.getpid()}")
    assert ce.clear_stale_chrome_singleton(profile) is False
    assert (profile / "SingletonLock").is_symlink()


def test_write_tint2rc_dock_has_no_grey_plate(tmp_path):
    cfg = tmp_path / "desktop"
    rc = ce.write_tint2rc(cfg, ce.write_launchers(cfg), dock=True)
    text = rc.read_text()
    assert "panel_background_id = 0" in text
    assert "launcher_background_id = 0" in text
    assert "background_color" not in text
    assert "panel_items = L\n" in text


def test_machine_chrome_desktop_uses_lock_wrapper(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_MACHINE", "1")
    monkeypatch.setattr(ce, "find_browser", lambda: "/usr/bin/google-chrome")
    monkeypatch.setattr("pathlib.Path.home", staticmethod(lambda: tmp_path / "agent"))
    (tmp_path / "agent").mkdir()
    cfg = tmp_path / "desktop"
    files = ce.write_launchers(cfg)
    desktop = (cfg / "launchers" / "chrome.desktop").read_text()
    wrapper = cfg / "launch-chrome"
    assert wrapper.is_file()
    assert os.access(wrapper, os.X_OK)
    assert f"Exec={wrapper} %U" in desktop
    script = wrapper.read_text()
    assert "SingletonLock" in script
    assert "google-chrome" in script
    assert "--remote-debugging-port=0" in script
    assert "--remote-debugging-address=127.0.0.1" in script
    # the flags must be behind the runtime guard, never baked into the exec
    assert "HARNESS_CHROME_CDP" in script
    exec_line = next(line for line in script.splitlines() if line.startswith("exec "))
    assert "--remote-debugging" not in exec_line
    assert "$CDP_FLAGS" in exec_line
    assert files[0].name == "chrome.desktop"


def test_chrome_wrapper_gates_cdp_flags_at_runtime(tmp_path, monkeypatch):
    """The wrapper outlives its bake: HARNESS_CHROME_CDP is read when the
    dock click runs it, so a kill-switch flip needs no rebake."""
    import subprocess

    stub = tmp_path / "chrome-stub"
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    stub.chmod(0o755)
    wrapper = ce.write_chrome_wrapper(tmp_path, [str(stub), "--baked"], tmp_path / "profile")

    env = {k: v for k, v in os.environ.items() if k != "HARNESS_CHROME_CDP"}
    on = subprocess.run(
        [str(wrapper), "https://x"],
        capture_output=True,
        text=True,
        env={**env, "HARNESS_CHROME_CDP": "1"},
    )
    lines = on.stdout.splitlines()
    assert "--baked" in lines
    assert "--remote-debugging-port=0" in lines
    assert "--force-renderer-accessibility" in lines
    assert lines[-1] == "https://x"

    off = subprocess.run(
        [str(wrapper), "https://x"],
        capture_output=True,
        text=True,
        env={**env, "HARNESS_CHROME_CDP": "0"},
    )
    lines = off.stdout.splitlines()
    assert "--baked" in lines
    assert not any(a.startswith("--remote-debugging") for a in lines)
    assert "--force-renderer-accessibility" not in lines
    assert lines[-1] == "https://x"


def test_write_tint2rc_is_bottom_panel_with_launchers(tmp_path):
    cfg = tmp_path / "desktop"
    launchers = ce.write_launchers(cfg)
    rc = ce.write_tint2rc(cfg, launchers)
    text = rc.read_text()
    assert "panel_position = bottom center horizontal" in text
    assert text.count("launcher_item_app = ") == 3
    assert "panel_items = LTC" in text  # launcher + tasks + clock


def test_machine_browser_command_prefers_google_chrome(monkeypatch, tmp_path):
    """Host-side the command must stay a bare name.

    This argv is handed to `docker exec` inside the machine, so resolving it
    against the host's PATH pointed the machine at a path that need not exist
    there — breaking the browser on exactly those hosts that have Chrome.
    """
    monkeypatch.setenv("HARNESS_CHROME_CDP", "1")
    monkeypatch.delenv("HARNESS_BROWSER", raising=False)
    monkeypatch.delenv("HARNESS_MACHINE", raising=False)
    monkeypatch.setattr(ce, "find_browser", lambda: "/usr/bin/google-chrome")
    cmd = ce.machine_browser_command("https://www.google.com", tmp_path)
    assert cmd[0] == "google-chrome"
    assert "--no-sandbox" in cmd
    assert "--no-zygote" in cmd
    assert "--disable-seccomp-filter-sandbox" in cmd
    assert "--remote-debugging-port=0" in cmd
    assert "--remote-debugging-address=127.0.0.1" in cmd
    assert "--force-renderer-accessibility" in cmd
    assert cmd[-1] == "https://www.google.com"


def test_kill_switch_strips_cdp_flags_from_launch(monkeypatch, tmp_path):
    """HARNESS_CHROME_CDP=0 must close the debug port on relaunch, not just
    stop the client — otherwise the rollback story still needs a revert."""
    monkeypatch.setenv("HARNESS_CHROME_CDP", "0")
    monkeypatch.delenv("HARNESS_BROWSER", raising=False)
    monkeypatch.delenv("HARNESS_MACHINE", raising=False)
    monkeypatch.setattr(ce, "find_browser", lambda: "/usr/bin/google-chrome")
    cmd = ce.machine_browser_command("https://www.google.com", tmp_path)
    host_cmd = ce.browser_command("https://www.google.com", tmp_path, no_sandbox=True)
    for argv in (cmd, host_cmd):
        assert not any(a.startswith("--remote-debugging") for a in argv)
        assert "--force-renderer-accessibility" not in argv


def test_machine_browser_ignores_host_path_when_driving_a_machine(monkeypatch):
    """HARNESS_MACHINE_NAME means we are the host driving a machine."""
    monkeypatch.delenv("HARNESS_BROWSER", raising=False)
    monkeypatch.delenv("HARNESS_MACHINE", raising=False)
    monkeypatch.setenv("HARNESS_MACHINE_NAME", "harness-machine-0")
    monkeypatch.setattr(ce, "_which", lambda names: "/snap/bin/chromium")
    assert ce.machine_browser() == "google-chrome"


def test_machine_browser_probes_path_inside_the_machine(monkeypatch):
    """Inside the machine our PATH is the machine's PATH, so probing is right."""
    monkeypatch.delenv("HARNESS_BROWSER", raising=False)
    monkeypatch.setenv("HARNESS_MACHINE", "1")
    monkeypatch.setattr(ce, "_which", lambda names: "/usr/bin/chromium")
    assert ce.machine_browser() == "/usr/bin/chromium"


def test_machine_browser_honours_explicit_override(monkeypatch):
    monkeypatch.delenv("HARNESS_MACHINE", raising=False)
    monkeypatch.setenv("HARNESS_BROWSER", "/opt/custom/browser")
    assert ce.machine_browser() == "/opt/custom/browser"


def test_browser_command_chromium(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_CHROME_CDP", "1")
    monkeypatch.setattr(ce, "find_browser", lambda: "/usr/bin/chromium")
    cmd = ce.browser_command("https://www.google.com", tmp_path, no_sandbox=True)
    assert cmd[0] == "/usr/bin/chromium"
    assert any(a.startswith("--user-data-dir=") for a in cmd)
    assert "--no-sandbox" in cmd
    assert "--no-zygote" in cmd
    assert "--disable-seccomp-filter-sandbox" in cmd
    assert "--ozone-platform=x11" in cmd
    assert "--remote-debugging-port=0" in cmd
    assert "--remote-debugging-address=127.0.0.1" in cmd
    assert cmd[-1] == "https://www.google.com"


def test_browser_command_non_chromium(monkeypatch, tmp_path):
    monkeypatch.setattr(ce, "find_browser", lambda: "/usr/bin/epiphany-browser")
    cmd = ce.browser_command("https://www.google.com", tmp_path)
    assert cmd == ["/usr/bin/epiphany-browser", "https://www.google.com"]


def test_browser_command_none(monkeypatch, tmp_path):
    monkeypatch.setattr(ce, "find_browser", lambda: None)
    assert ce.browser_command("https://x", tmp_path) is None


def test_find_browser_env_override(monkeypatch):
    monkeypatch.setenv("HARNESS_BROWSER", "/opt/my/browser")
    assert ce.find_browser() == "/opt/my/browser"


def test_bringup_reports_error_without_display(monkeypatch, tmp_path):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([])
    report = ce.bringup(paths, display=None)
    assert report.get("error") == "no DISPLAY set"
