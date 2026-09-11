"""A secure-input credential becomes usable by one bot's scripts, without SSH."""

import json
import stat

import pytest

from agent import govern, policy
from agent.memory import Memory
from agent.tools import ToolContext, default_tools
from harness.paths import HarnessPaths
from harness.secrets import delete_secret, set_secret


def setup_machine(tmp_path, monkeypatch, bot="atlas"):
    from harness import machine_secrets

    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([bot, "other"])
    paths.run_file(bot).write_text(json.dumps({"backend": "machines", "machine": "machine-0"}))
    directory = machine_secrets.prepare_directory(paths, bot)
    monkeypatch.setattr(machine_secrets, "mounted_for_bot", lambda *args: True)
    return paths, directory


def test_secure_input_to_script_file_without_transcript_value(tmp_path, monkeypatch):
    from harness import machine_secrets

    paths, directory = setup_machine(tmp_path, monkeypatch)
    ctx = ToolContext(paths=paths, bot="atlas", memory=Memory(paths=paths, bot="atlas"))
    set_secret("AMAZON_CLIENT_SECRET", "private-test-value", paths)
    out = default_tools()["use_secret_file"].handler(ctx, {"name": "AMAZON_CLIENT_SECRET"})
    assert "/run/harness/AMAZON_CLIENT_SECRET" in out
    assert "private-test-value" not in out
    assert (directory / "AMAZON_CLIENT_SECRET").read_text() == "private-test-value"
    assert stat.S_IMODE((directory / "AMAZON_CLIENT_SECRET").stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert not (machine_secrets.directory_for_bot(paths, "other") / "AMAZON_CLIENT_SECRET").exists()
    assert not list(paths.workspace.rglob("AMAZON_CLIENT_SECRET"))


def test_secret_rotation_and_removal_reach_previously_granted_files(tmp_path, monkeypatch):
    from harness import machine_secrets

    paths, directory = setup_machine(tmp_path, monkeypatch)
    set_secret("AMAZON_KEY", "first-value", paths)
    machine_secrets.grant(paths, "atlas", "AMAZON_KEY")
    set_secret("AMAZON_KEY", "replacement", paths)
    assert (directory / "AMAZON_KEY").read_text() == "replacement"
    assert delete_secret("AMAZON_KEY", paths)
    assert not (directory / "AMAZON_KEY").exists()


def test_missing_mount_is_actionable_and_does_not_stage_secret(tmp_path, monkeypatch):
    from harness import machine_secrets

    paths, directory = setup_machine(tmp_path, monkeypatch)
    monkeypatch.setattr(machine_secrets, "mounted_for_bot", lambda *args: False)
    set_secret("AMAZON_KEY", "private-value", paths)
    with pytest.raises(machine_secrets.MachineSecretError, match="Restart"):
        machine_secrets.grant(paths, "atlas", "AMAZON_KEY")
    assert not (directory / "AMAZON_KEY").exists()


@pytest.mark.parametrize("name", ["../serve.json", "/etc/passwd", ".hidden", "bad/name"])
def test_secret_paths_are_not_accepted(tmp_path, monkeypatch, name):
    from harness import machine_secrets

    paths, directory = setup_machine(tmp_path, monkeypatch)
    with pytest.raises(machine_secrets.MachineSecretError):
        machine_secrets.grant(paths, "atlas", name)
    assert list(directory.iterdir()) == []


def test_script_secret_use_is_governed_as_credential_use(tmp_path):
    paths = HarnessPaths.resolve(tmp_path)
    ctx = ToolContext(paths=paths, bot="atlas", memory=Memory(paths=paths, bot="atlas"))
    intent, target, _ = govern.classify("use_secret_file", {"name": "AMAZON_KEY"})
    assert intent == govern.INTENT_TYPE_SECRET
    assert target == "AMAZON_KEY"
    result = govern.govern(
        ctx,
        "use_secret_file",
        {"name": "AMAZON_KEY"},
        paths=paths,
        bot="atlas",
        policy=policy.parse({"deny": [{"intent": "type_secret"}]}),
    )
    assert result and result.startswith("error:")


def test_rotation_does_not_follow_grant_symlinks(tmp_path, monkeypatch):
    from harness import machine_secrets

    paths, directory = setup_machine(tmp_path, monkeypatch)
    elsewhere = tmp_path / "untouched"
    elsewhere.write_text("unchanged")
    (directory / "AMAZON_KEY").symlink_to(elsewhere)
    set_secret("AMAZON_KEY", "replacement", paths)
    assert elsewhere.read_text() == "unchanged"
    with pytest.raises(machine_secrets.MachineSecretError):
        machine_secrets.grant(paths, "atlas", "AMAZON_KEY")


def test_secret_card_uses_the_app_api_then_supplies_a_file(tmp_path, monkeypatch):
    import threading

    from agent import tools as agent_tools
    from harness import machine_secrets
    from harness.orchestrator import Orchestrator
    from harness.server import make_server

    roster = tmp_path / "roster.toml"
    roster.write_text('[[bots]]\nname="atlas"\nprovider="echo"\n')
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=roster, backend="process")
    orch.init()
    server = make_server(orch, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ctx = ToolContext(
            paths=orch.paths, bot="atlas", memory=Memory(paths=orch.paths, bot="atlas")
        )
        orch.paths.run_file("atlas").write_text(
            json.dumps({"backend": "machines", "machine": "fake-machine"})
        )
        monkeypatch.setattr(machine_secrets, "mounted_for_bot", lambda *a: True)

        def fill_card(_ctx, ready):
            result = agent_tools._api_json(
                orch.paths,
                "POST",
                "/api/secrets",
                {"name": "AMAZON_KEY", "value": "secure-card-test-value"},
            )
            assert isinstance(result, dict)
            return ready()

        monkeypatch.setattr(agent_tools, "_wait_for_human", fill_card)
        collected = default_tools()["request_secret"].handler(ctx, {"name": "AMAZON_KEY"})
        supplied = default_tools()["use_secret_file"].handler(ctx, {"name": "AMAZON_KEY"})
        assert collected.startswith("ok:") and supplied.startswith("ok:")
        assert "secure-card-test-value" not in collected + supplied
        assert (
            machine_secrets.directory_for_bot(orch.paths, "atlas") / "AMAZON_KEY"
        ).read_text() == "secure-card-test-value"
    finally:
        orch.paths.run_file("atlas").unlink(missing_ok=True)
        server.shutdown()
        server.server_close()
        orch.down()


def test_only_expected_readonly_mount_is_accepted(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from harness import machine_secrets
    from isolation import engine

    paths = HarnessPaths.resolve(tmp_path)
    directory = machine_secrets.prepare_directory(paths, "atlas")
    mount = {"Type": "bind", "Source": str(directory), "Destination": "/run/harness", "RW": False}
    monkeypatch.setattr(
        engine, "run", lambda *a: SimpleNamespace(returncode=0, stdout=json.dumps([mount]))
    )
    assert machine_secrets.mounted_for_bot(paths, "atlas", "machine-0")
    assert not machine_secrets.mounted_for_bot(paths, "other", "machine-0")
    mount["RW"] = True
    assert not machine_secrets.mounted_for_bot(paths, "atlas", "machine-0")


@pytest.mark.parametrize("rotate_during_tool", [False, True])
def test_persisted_script_key_is_scrubbed_before_the_next_provider_call(
    tmp_path, monkeypatch, rotate_during_tool
):
    from agent.runtime import build_agent
    from agent.tools import Tool
    from harness import machine_secrets, redaction
    from harness.roster import Bot
    from providers.base import Completion, Provider, ToolCall

    paths, directory = setup_machine(tmp_path, monkeypatch)
    value = "private-restarted-script-key"
    machine_secrets.stage(paths, "atlas", "AMAZON_KEY", value)
    redaction.registry().clear()  # a new agent process does not know old grants

    def script_output(ctx, args):
        if rotate_during_tool:
            # Another process rotates the mounted file during this tool call.
            machine_secrets.stage(paths, "atlas", "AMAZON_KEY", "private-rotated-script-key")
        return (directory / "AMAZON_KEY").read_text()

    original = default_tools()["run_command"]
    monkeypatch.setitem(default_tools(), "run_command", Tool(original.spec, script_output))

    class ScriptProvider(Provider):
        id = "scripted"
        called = False

        def complete(self, messages, **kwargs):
            if not self.called:
                self.called = True
                return Completion(tool_calls=[ToolCall("c1", "run_command", {"command": "check"})])
            results = [m.content for m in messages if m.role == "tool"]
            assert results and "hs-v1." in results[-1]
            assert not any("private-" in text for text in results)
            return Completion(text="done")

    try:
        agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0)
        agent.provider = ScriptProvider("scripted")
        assert agent._produce("user", "run the API script") == "done"
    finally:
        redaction.registry().clear()
