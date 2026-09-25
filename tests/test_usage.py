"""Usage ledger, readiness probe, and their API surface."""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from agent.memory import Memory
from agent.runtime import Agent
from harness.control import Control
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.readiness import find_cli, provider_readiness
from harness.roster import Bot
from harness.server import make_server
from harness.usage import add_usage, empty_usage, record_usage, rollup, usage_file
from providers.base import Completion, Provider, ToolCall
from providers.echo import EchoProvider

# -- ledger + rollup -------------------------------------------------------


def test_record_and_rollup_aggregates_per_provider_and_model(tmp_path):
    paths = HarnessPaths.resolve(tmp_path)
    record_usage(
        paths,
        "claude",
        "sonnet",
        requests=2,
        tokens={"input_tokens": 10, "output_tokens": 5},
        now=100.0,
    )
    record_usage(
        paths,
        "claude",
        "sonnet",
        tokens={"input_tokens": 1, "cache_read_tokens": 7, "cache_write_tokens": 2},
        now=200.0,
    )
    record_usage(paths, "claude", "haiku", tokens={"output_tokens": 4}, now=150.0)
    record_usage(paths, "grok", "grok-4", tokens={"output_tokens": 3}, now=175.0)

    agg = rollup(paths)
    assert agg["kind"] == "activity"
    assert agg["totals"]["requests"] == 5
    assert agg["totals"]["input_tokens"] == 11
    assert agg["totals"]["output_tokens"] == 12
    assert agg["totals"]["cache_read_tokens"] == 7
    assert agg["totals"]["cache_write_tokens"] == 2
    assert agg["totals"]["last_used_at"] == "1970-01-01T00:03:20Z"  # ts=200

    claude = agg["providers"]["claude"]
    assert claude["requests"] == 4
    assert claude["last_used_at"] == "1970-01-01T00:03:20Z"
    assert agg["providers"]["grok"]["output_tokens"] == 3

    models = {(m["provider"], m["model"]): m for m in agg["models"]}
    assert models[("claude", "sonnet")]["requests"] == 3
    assert models[("claude", "sonnet")]["input_tokens"] == 11
    assert models[("claude", "haiku")]["output_tokens"] == 4
    # the ledger is append-only JSONL, one record per logical turn
    lines = usage_file(paths).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    assert all(json.loads(line)["provider"] for line in lines)


def test_add_usage_normalizes_vendor_shapes():
    totals: dict[str, int] = {}
    # Anthropic-shaped usage block
    add_usage(
        totals,
        {
            "input_tokens": 5,
            "output_tokens": 2,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 1,
        },
    )
    # OpenAI-compatible usage block
    add_usage(
        totals,
        {
            "prompt_tokens": 7,
            "completion_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": 2},
        },
    )
    assert totals == {
        "input_tokens": 12,
        "output_tokens": 6,
        "cache_read_tokens": 5,
        "cache_write_tokens": 1,
    }


def test_rollup_skips_corrupt_lines_and_zeroes_bad_counts(tmp_path):
    paths = HarnessPaths.resolve(tmp_path)
    path = usage_file(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    good = {"ts": 50.0, "provider": "grok", "model": "grok-4", "requests": 1, "input_tokens": 9}
    lines = [
        json.dumps(good),
        "{not json at all",
        json.dumps(["a", "list", "record"]),
        json.dumps({"ts": 60.0, "model": "orphan", "requests": 1}),  # no provider
        json.dumps(
            {
                "ts": "yesterday",  # bad timestamp -> ignored for last_used_at
                "provider": "grok",
                "model": "grok-4",
                "requests": -4,  # negative -> 0
                "input_tokens": "lots",  # not a number -> 0
                "output_tokens": 2**60,  # beyond safe int -> 0
                "cache_read_tokens": True,  # bool -> 0
                "cache_write_tokens": 3.0,  # integral float -> 3
            }
        ),
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    agg = rollup(paths)
    assert list(agg["providers"]) == ["grok"]
    grok = agg["providers"]["grok"]
    assert grok["requests"] == 1
    assert grok["input_tokens"] == 9
    assert grok["output_tokens"] == 0
    assert grok["cache_read_tokens"] == 0
    assert grok["cache_write_tokens"] == 3
    assert grok["last_used_at"] == "1970-01-01T00:00:50Z"


def test_rollup_streams_usage_ledger_instead_of_reading_whole_file(tmp_path, monkeypatch):
    paths = HarnessPaths.resolve(tmp_path)
    record_usage(paths, "grok", "grok-4", tokens={"input_tokens": 9}, now=50.0)
    path = usage_file(paths)

    original = type(path).read_text

    def refuse_read_text(self, *args, **kwargs):
        if self == path:
            raise AssertionError("usage rollup should stream the JSONL ledger")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "read_text", refuse_read_text)
    assert rollup(paths)["providers"]["grok"]["input_tokens"] == 9


def test_record_usage_sanitizes_on_write(tmp_path):
    paths = HarnessPaths.resolve(tmp_path)
    record_usage(
        paths,
        "grok",
        "grok-4",
        requests=-2,
        tokens={"input_tokens": -5, "output_tokens": "nope", "cache_read_tokens": 4},
        now=10.0,
    )
    (line,) = usage_file(paths).read_text(encoding="utf-8").splitlines()
    rec = json.loads(line)
    assert rec["requests"] == 0
    assert rec["input_tokens"] == 0
    assert rec["output_tokens"] == 0
    assert rec["cache_read_tokens"] == 4


# -- agent runtime recording ----------------------------------------------


class ToolLoopProvider(Provider):
    """Two-step provider: a (bogus) tool call, then a final text, with usage."""

    id = "fake"

    def __init__(self) -> None:
        super().__init__(model="fake-1")
        self.turns = 0

    def complete(self, messages, *, system=None, tools=None, max_tokens=1024, temperature=0.7):
        self.turns += 1
        if self.turns == 1:
            return Completion(
                tool_calls=[ToolCall(id="t1", name="no_such_tool", arguments={})],
                finish_reason="tool_use",
                usage={"input_tokens": 10, "output_tokens": 1},
            )
        return Completion(
            text="done",
            finish_reason="stop",
            usage={"input_tokens": 20, "output_tokens": 2, "cache_read_input_tokens": 5},
        )


def _agent(tmp_path, provider, **kwargs):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    bot = Bot(name="atlas", role="an assistant", provider="echo")
    agent = Agent(
        paths=paths,
        bot=bot,
        provider=provider,
        memory=Memory(paths=paths, bot="atlas"),
        control=Control(paths),
        stream_delay=0.0,
        **kwargs,
    )
    return agent, paths


def test_turn_sums_tool_loop_steps_into_one_usage_record(tmp_path):
    seen: list[tuple] = []
    agent, paths = _agent(
        tmp_path,
        ToolLoopProvider(),
        on_usage=lambda provider, model, requests, tokens: seen.append(
            (provider, model, requests, tokens)
        ),
    )
    assert agent._produce("user", "go") == "done"
    assert seen == [
        (
            "fake",
            "fake-1",
            2,
            {
                "input_tokens": 30,
                "output_tokens": 3,
                "cache_read_tokens": 5,
                "cache_write_tokens": 0,
            },
        )
    ]
    # the custom sink replaced the ledger write
    assert not usage_file(paths).exists()


def test_turn_appends_one_ledger_record_by_default(tmp_path):
    agent, paths = _agent(tmp_path, EchoProvider())
    agent._produce("user", "hello")
    agent._produce("user", "again")
    records = [
        json.loads(line) for line in usage_file(paths).read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 2
    assert records[0]["provider"] == "echo"
    assert records[0]["model"] == "echo-1"
    assert records[0]["requests"] == 1
    assert records[1]["requests"] == 2  # relation check plus the follow-up response
    assert rollup(paths)["providers"]["echo"]["requests"] == 3


@pytest.mark.parametrize("relation", ['{"relation":"same_task"}', '{"relation":"new_task"}', "invalid"])
def test_continuity_usage_accumulates_in_the_followup_turn(tmp_path, relation):
    class FollowupProvider(Provider):
        id = "fake"

        def complete(self, messages, *, system=None, **kwargs):
            if (system or "").startswith("Classify whether"):
                return Completion(text=relation, usage={"input_tokens": 11, "output_tokens": 1})
            return Completion(text="The brief is ready.", usage={"input_tokens": 20, "output_tokens": 2})

    seen = []
    agent, _ = _agent(tmp_path, FollowupProvider(model="fake-1"),
                      on_usage=lambda *args: seen.append(args))
    agent._produce("user", "Find the brief")
    agent._produce("user", "What did you find?")
    assert len(seen) == 2
    assert seen[1] == ("fake", "fake-1", 2, {
        "input_tokens": 31, "output_tokens": 3, "cache_read_tokens": 0, "cache_write_tokens": 0,
    })


def test_usage_callback_failure_never_fails_the_turn(tmp_path):
    def boom(provider, model, requests, tokens):
        raise RuntimeError("disk full")

    agent, paths = _agent(tmp_path, EchoProvider(), on_usage=boom)
    reply = agent._produce("user", "hello")
    assert "hello" in reply  # the turn still completed normally
    assert not usage_file(paths).exists()


def test_default_ledger_write_failure_is_swallowed(tmp_path, monkeypatch):
    agent, _ = _agent(tmp_path, EchoProvider())
    monkeypatch.setattr(
        "agent.runtime.record_usage",
        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs")),
    )
    assert "hello" in agent._produce("user", "hello")


# -- readiness probe -------------------------------------------------------


def _make_exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    """Isolate HOME/$PATH and drop any real provider credentials from env."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(tmp_path / "pathbin"))
    for var in (
        "CODEX_PATH",
        "CLAUDE_CODE_PATH",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "XAI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    return home


def test_find_cli_search_order(tmp_path, clean_env, monkeypatch):
    home = clean_env
    kwargs = {"env_var": "CODEX_PATH", "tool_homes": ("~/.codex/bin",)}
    assert find_cli("codex", **kwargs) is None

    path_hit = tmp_path / "pathbin" / "codex"
    _make_exe(path_hit)
    assert find_cli("codex", **kwargs) == str(path_hit)

    tool_home_hit = home / ".codex" / "bin" / "codex"
    _make_exe(tool_home_hit)  # tool home beats $PATH
    assert find_cli("codex", **kwargs) == str(tool_home_hit)

    local_bin_hit = home / ".local" / "bin" / "codex"
    _make_exe(local_bin_hit)  # ~/.local/bin beats the tool home
    assert find_cli("codex", **kwargs) == str(local_bin_hit)

    override = tmp_path / "custom" / "my-codex"
    _make_exe(override)  # the env override beats everything
    monkeypatch.setenv("CODEX_PATH", str(override))
    assert find_cli("codex", **kwargs) == str(override)


def test_provider_readiness_statuses(tmp_path, clean_env, monkeypatch):
    home = clean_env
    paths = HarnessPaths.resolve(tmp_path / "harness")

    # nothing anywhere: CLI providers read as not installed
    assert provider_readiness("codex", paths) == {
        "installed": False,
        "authenticated": False,
        "path": None,
        "status": "Not installed",
    }

    # a credentials file counts as signed in by existence alone (never read)
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    codex = provider_readiness("codex", paths)
    assert codex["authenticated"] is True
    assert codex["status"] == "Ready"

    # CLI installed but no credentials -> needs sign-in
    claude_cli = home / ".local" / "bin" / "claude"
    _make_exe(claude_cli)
    claude = provider_readiness("claude", paths)
    assert claude == {
        "installed": True,
        "authenticated": False,
        "path": str(claude_cli),
        "status": "Needs sign-in",
    }
    # an env key flips it to ready
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert provider_readiness("claude", paths)["status"] == "Ready"

    # API-only provider: nothing to install, auth via key/OAuth store
    grok = provider_readiness("grok", paths)
    assert grok["installed"] is True
    assert grok["path"] is None
    assert grok["status"] == "Needs sign-in"
    (paths.credentials).mkdir(parents=True, exist_ok=True)
    (paths.credentials / "XAI_API_KEY").write_text("xai-test", encoding="utf-8")
    assert provider_readiness("grok", paths)["status"] == "Ready"

    # echo remains a test adapter (hidden from the UI catalog)
    assert provider_readiness("echo", paths)["status"] == "Ready"


# -- API surface -----------------------------------------------------------

ROSTER = """
[[bots]]
name = "atlas"
role = "a terse research assistant"
provider = "echo"
"""


@pytest.fixture
def server(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    orch.use_json_store()
    httpd = make_server(orch, "127.0.0.1", 0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", orch
    finally:
        httpd.shutdown()
        orch.down()


def _req(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())


def test_api_providers_carry_usage_and_readiness(server):
    base, orch = server
    record_usage(orch.paths, "grok", "grok-4", requests=2, tokens={"input_tokens": 9}, now=42.0)

    providers = {p["id"]: p for p in _req(f"{base}/api/providers")}
    grok = providers["grok"]
    assert grok["usage"]["requests"] == 2
    assert grok["usage"]["input_tokens"] == 9
    assert grok["usage"]["last_used_at"] == "1970-01-01T00:00:42Z"
    # an unused provider still shows a zeroed activity block
    assert providers["claude"]["usage"] == empty_usage()
    for p in providers.values():
        readiness = p["readiness"]
        assert set(readiness) == {"installed", "authenticated", "path", "status"}
        assert readiness["status"] in {"Ready", "Needs sign-in", "Not installed"}
    assert "echo" not in providers
    assert "deepseek" in providers
    assert "minimax" in providers
    assert providers["claude"]["oauth_implemented"] is True
    assert providers["codex"]["oauth_implemented"] is True


def test_api_usage_returns_the_rollup(server):
    base, orch = server
    record_usage(orch.paths, "grok", "grok-4", tokens={"output_tokens": 3}, now=7.0)
    record_usage(orch.paths, "claude", "sonnet", tokens={"input_tokens": 5}, now=8.0)

    agg = _req(f"{base}/api/usage")
    assert agg["kind"] == "activity"
    assert agg["totals"]["requests"] == 2
    assert agg["providers"]["grok"]["output_tokens"] == 3
    assert agg["providers"]["claude"]["input_tokens"] == 5
    assert [(m["provider"], m["model"]) for m in agg["models"]] == [
        ("claude", "sonnet"),
        ("grok", "grok-4"),
    ]
