"""Tool-gate hardening: five doors that were beside the gate, not behind it.

* `ask_human` was dispatched above `govern()` — no audit row, and a `deny`
  on it did nothing while control still flipped to takeover-requested.
* `write_stdin` handed the shell guard one call's bytes, so `rm -rf` in one
  call and ` /\\n` in the next was never seen as one command.
* `message_agent` with no roster in the home turned the model's raw string
  into an inbox path (`../x`, `/abs`).
* a tool an MCP server serves under a curated type (`linear_save_issue`)
  inherited "absent from the write list means read".
* a background shell was spawned with the full parent environment, provider
  keys and `HARNESS_TOKEN` included, while foreground `run_command` scrubbed.
"""

from __future__ import annotations

import os
import time

import agent.govern as govern
from agent import terminals as terminals_mod
from agent import tools as tools_mod
from agent.memory import Memory
from agent.runtime import build_agent
from agent.streaming import StreamReader, StreamWriter
from agent.terminals import ProcessTerminals
from agent.tools import ToolContext, default_tools
from connectors.effects import EFFECT_READ, EFFECT_WRITE, classify
from harness import audit
from harness.control import Control
from harness.paths import HarnessPaths
from harness.roster import Bot

BOT = "atlas"


def _paths(tmp_path) -> HarnessPaths:
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout([BOT])
    return paths


def _ctx(paths) -> ToolContext:
    return ToolContext(paths=paths, bot=BOT, memory=Memory(paths=paths, bot=BOT))


def _wait_for(cond, timeout=8.0, step=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


# -- ask_human goes through the gate ---------------------------------------


def test_ask_human_gets_an_audit_row_and_still_requests_takeover(tmp_path):
    """No policy: the stuck semantics are unchanged (takeover event, control
    state flipped) and the trail now has a `tool.decided` row for it."""
    paths = _paths(tmp_path)
    agent = build_agent(paths, Bot(name=BOT, role="terse", provider="echo"), stream_delay=0.0)
    final = agent._produce("user", "I'm stuck, take over please", writer=StreamWriter(paths, "r1"))

    events = list(StreamReader(paths, "r1").events(timeout=2.0))
    assert any(e.type == "takeover" for e in events)
    assert "take over" in final.lower()
    assert Control(paths).state(BOT).takeover_requested
    rows = [r for r in audit.read(paths, BOT) if r.get("tool") == "ask_human"]
    assert rows, "ask_human ran without a tool.decided row"
    assert rows[0]["event"] == "tool.decided"
    assert rows[0]["intent"] == govern.INTENT_UI
    assert rows[0]["decision"] == "allow"


def test_a_deny_rule_on_ask_human_holds(tmp_path):
    """`deny = ["ask_human"]` used to be ignored: the handler ran above the
    gate and control flipped to takeover-requested with nothing recorded."""
    paths = _paths(tmp_path)
    (paths.home / "policy.toml").write_text('deny = ["ask_human"]\n', encoding="utf-8")
    agent = build_agent(paths, Bot(name=BOT, role="terse", provider="echo"), stream_delay=0.0)
    final = agent._produce("user", "I'm stuck, take over please", writer=StreamWriter(paths, "r2"))

    events = list(StreamReader(paths, "r2").events(timeout=2.0))
    assert not any(e.type == "takeover" for e in events)
    assert not Control(paths).state(BOT).takeover_requested
    assert "i'm stuck" not in final.lower()
    assert "refused" in final.lower()
    rows = [r for r in audit.read(paths, BOT) if r.get("tool") == "ask_human"]
    assert rows and rows[0]["decision"] == "refuse"


# -- write_stdin: a line split across calls is one command ------------------


def _cat_shell(ctx, monkeypatch) -> int:
    monkeypatch.delenv("HARNESS_MACHINE_NAME", raising=False)
    out = default_tools()["run_command_background"].handler(ctx, {"command": "cat"})
    assert out.startswith("ok: background shell ")
    return int(out.split("background shell ")[1].split()[0])


def test_rm_rf_split_across_two_writes_is_refused(tmp_path, monkeypatch):
    """`rm -rf` then ` /\\n`: each chunk passes the guard on its own; the
    shell receives one line. The second write must be refused, and a
    refusal leaves the shell's pending line as it is, so retrying the tail
    is refused again."""
    paths = _paths(tmp_path)
    ctx = _ctx(paths)
    tools = default_tools()
    shell_id = _cat_shell(ctx, monkeypatch)
    try:
        first = {"shell_id": shell_id, "chars": "rm -rf"}
        assert govern.govern(ctx, "write_stdin", first, paths=paths, bot=BOT) is None
        assert tools["write_stdin"].handler(ctx, first).startswith("ok:")

        second = {"shell_id": shell_id, "chars": " /\n"}
        refusal = govern.govern(ctx, "write_stdin", second, paths=paths, bot=BOT)
        assert refusal is not None and refusal.startswith("error: write_stdin refused")
        # nothing was written, so the shell still holds `rm -rf`: same answer
        assert govern.govern(ctx, "write_stdin", second, paths=paths, bot=BOT) is not None
        rows = [r for r in audit.read(paths, BOT) if r.get("tool") == "write_stdin"]
        assert rows[0]["decision"] == "refuse" and rows[0]["source"] == "shell-guard"
    finally:
        tools["stop_terminal"].handler(ctx, {"shell_id": shell_id})


def test_ordinary_split_lines_still_go_through(tmp_path, monkeypatch):
    """A harmless line typed in pieces is not refused, a newline clears the
    tail, and stop_terminal forgets it."""
    paths = _paths(tmp_path)
    ctx = _ctx(paths)
    tools = default_tools()
    shell_id = _cat_shell(ctx, monkeypatch)
    try:
        for chars in ("echo ", "hello", "\n"):
            call = {"shell_id": shell_id, "chars": chars}
            assert govern.govern(ctx, "write_stdin", call, paths=paths, bot=BOT) is None
            assert tools["write_stdin"].handler(ctx, call).startswith("ok:")
        assert terminals_mod.pending_stdin(paths, BOT, shell_id) == ""
        assert tools["write_stdin"].handler(ctx, {"shell_id": shell_id, "chars": "rm -rf"})
        assert terminals_mod.pending_stdin(paths, BOT, shell_id) == "rm -rf"
    finally:
        tools["stop_terminal"].handler(ctx, {"shell_id": shell_id})
    assert terminals_mod.pending_stdin(paths, BOT, shell_id) == ""


def test_the_pending_line_is_bounded_and_never_raises(tmp_path):
    paths = _paths(tmp_path)
    terminals_mod.note_stdin(paths, BOT, 7, "x" * (terminals_mod.STDIN_TAIL_LIMIT + 50))
    assert len(terminals_mod.pending_stdin(paths, BOT, 7)) == terminals_mod.STDIN_TAIL_LIMIT
    terminals_mod.forget_stdin(paths, BOT, 7)
    assert terminals_mod.pending_stdin(paths, BOT, "not-an-id") == ""
    terminals_mod.note_stdin(paths, BOT, None, "rm -rf")  # no crash
    assert (
        govern.govern(_ctx(paths), "write_stdin", {"chars": "ls\n"}, paths=paths, bot=BOT) is None
    )


# -- message_agent: a target is a bot name, never a path ------------------


def test_message_agent_refuses_a_path_shaped_target_without_a_roster(tmp_path):
    """A `harness up --roster` home has no roster.json, and the raw string
    used to become `messages/<to>/inbox` — `../x` and `/abs` escaped it."""
    paths = _paths(tmp_path)
    ctx = _ctx(paths)
    outside = tmp_path / "outside"
    for to in ("../x", str(outside), "a/b", "..", "nova\x00"):
        result = tools_mod._message_agent(ctx, {"to": to, "text": "hi"})
        assert result.startswith("error:"), to
        assert "ask_user_choice" in result
    assert not (paths.messages.parent / "x").exists()
    assert not outside.exists()
    assert sorted(p.name for p in paths.messages.iterdir()) == [BOT, "user"]
    # an ordinary colleague name still resolves without a roster
    assert tools_mod._resolve_colleague(ctx, "nova") == ("nova", "")


# -- an MCP-served tool on a curated type is not a curated tool -------------


def test_mcp_served_linear_tool_off_the_curated_list_is_a_write():
    """Linear binds its MCP server's own tools/list under `linear_*`; those
    names were never reviewed, so they get the unreviewed-server rule."""
    for name in ("linear_save_issue", "linear_merge_diff", "linear_delete_attachment"):
        assert classify(name) == EFFECT_WRITE, name
        intent, target, askable = govern.classify(name, {}, {name})
        assert intent == govern.INTENT_WRITE_TOOL
        assert target == name and askable is True
    # a positively-read verb from the same server is still a read
    assert classify("linear_get_document") == EFFECT_READ
    assert govern.classify("linear_list_documents", {}, {"linear_list_documents"})[0] == (
        govern.INTENT_READ_TOOL
    )


def test_shipped_curated_reads_keep_their_curated_classification():
    """The shipped runtime's names — including the multi-account form — still
    use the write list, so no existing read starts asking."""
    for name in ("linear_get_issue", "linear_work_search_issues", "github_list_pulls"):
        assert classify(name) == EFFECT_READ, name
    assert classify("linear_work_attach_files") == EFFECT_WRITE


# -- background shells get the scrubbed environment -------------------------


def test_background_shell_does_not_inherit_provider_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_MACHINE_NAME", raising=False)
    names = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "HARNESS_TOKEN")
    for name in names:
        monkeypatch.setenv(name, f"leak-{name}")
    manager = ProcessTerminals(tmp_path / "terminals", tmp_path / "work", update_interval=0.05)
    info = manager.spawn("env")
    term = manager.terminal_path(info.shell_id)
    assert _wait_for(lambda: "exit_code: 0" in term.read_text(encoding="utf-8"))
    body = term.read_text(encoding="utf-8")
    for name in names:
        assert name not in body, f"{name} reached the background shell"
        assert f"leak-{name}" not in body
    assert "PATH=" in body
    assert os.environ["HARNESS_TOKEN"] == "leak-HARNESS_TOKEN"  # the parent still has it
