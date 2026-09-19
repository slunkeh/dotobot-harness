import json

from deploy import update_host
from harness import update_state
from harness.paths import HarnessPaths
from harness.runtime_identity import identity


def setup(tmp_path):
    root = tmp_path / "releases-root"
    for version in ["1.0.0", "1.1.0"]:
        (root / "releases" / version / "harness").mkdir(parents=True)
        (root / "releases" / version / "harness/version.py").write_text(
            f'__version__ = "{version}"\n'
        )
    (root / "current").symlink_to(root / "releases/1.0.0")
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.home.mkdir()
    update_state.save(
        paths, {"id": "op", "stage": "preparing", "bots": [], "target": {"version": "1.1.0"}}
    )
    return (
        {
            "root": str(root),
            "home": str(paths.home),
            "manifest_url": "https://example.test/manifest.json",
            "service": "test.service",
            "image": "test-image",
        },
        paths,
        root,
    )


def test_verified_controller_only_activation_no_image_build(tmp_path, monkeypatch):
    config, paths, root = setup(tmp_path)
    tree = root / "releases/1.1.0"
    monkeypatch.setattr(
        update_host.updater,
        "load_manifest",
        lambda _: {"version": "1.1.0", "runtime_identity": identity(tree)},
    )
    commands = []
    update_host.apply(config, runner=commands.append, fetch=lambda *a: tree, health=lambda: "1.1.0")
    assert commands == [["systemctl", "restart", "test.service"]]
    assert update_state.read(paths)["stage"] == "rolling"
    assert (root / "current").resolve() == tree


def test_metadata_mismatch_does_not_switch_or_restart(tmp_path, monkeypatch):
    config, paths, root = setup(tmp_path)
    monkeypatch.setattr(
        update_host.updater,
        "load_manifest",
        lambda _: {"version": "1.1.0", "runtime_identity": {"agent": "wrong"}},
    )
    commands = []
    update_host.apply(config, runner=commands.append, fetch=lambda *a: root / "releases/1.1.0")
    assert not commands
    assert update_state.read(paths)["stage"] == "needs_attention"
    assert (root / "current").resolve().name == "1.0.0"


def test_legacy_docker_is_blocked_without_touching_it(tmp_path):
    config, paths, _ = setup(tmp_path)
    config["container"] = "legacy"
    commands = []
    update_host.apply(config, runner=commands.append)
    assert not commands
    assert not json.loads((paths.home / "update-host.json").read_text())["available"]


def test_supervised_docker_reload_does_not_stop_container(tmp_path):
    config, paths, _ = setup(tmp_path)
    config["container"] = "supervised"
    commands = []
    update_host.restart(config, runner=commands.append)
    assert not commands
    assert (paths.home / "controller-reload").exists()


def test_interrupted_controller_activation_reconciles(tmp_path):
    config, paths, _ = setup(tmp_path)
    op = update_state.read(paths)
    op["stage"] = "installing_controller"
    update_state.save(paths, op)
    update_host.apply(config, health=lambda: "1.1.0")
    assert update_state.read(paths)["stage"] == "rolling"


def test_build_failure_keeps_selected_release(tmp_path, monkeypatch):
    config, paths, root = setup(tmp_path)
    monkeypatch.setattr(update_host.updater, "load_manifest", lambda _: {"version": "1.1.0"})

    def fail(args):
        raise RuntimeError("build failed")

    update_host.apply(config, runner=fail, fetch=lambda *a: root / "releases/1.1.0")
    assert (root / "current").resolve().name == "1.0.0"
    assert update_state.read(paths)["stage"] == "needs_attention"


def test_version_tags_support_existing_tags_and_registry_ports():
    assert update_host.version_tag("machine", "1.2.3") == "machine:1.2.3"
    assert (
        update_host.version_tag("machine:tenant-current", "1.2.3") == "machine:tenant-current-1.2.3"
    )
    assert (
        update_host.version_tag("registry:5000/machine", "1.2.3") == "registry:5000/machine:1.2.3"
    )
