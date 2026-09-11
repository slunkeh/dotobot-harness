"""Token, I/O, and prompt-assembly efficiency."""

from __future__ import annotations

from agent.history import (
    cap_tool_result,
    estimate_message_tokens,
    history_budget,
    prompt_tokens_from_usage,
    trim_loop_messages,
)
from agent.memory import Memory
from agent.messaging import Msg, read_inbox, send
from agent.runtime import (
    _CARDS_PROMPT,
    _COMPUTER_PROMPT,
    _CONNECTOR_PROMPT,
    _CREATE_BOT_PROMPT,
    _REPO_PROMPT,
    _ROUTINE_PROMPT,
    _TERMINAL_FIRST_PROMPT,
    Agent,
    _filter_repo_tools,
    _wants_computer,
)
from agent.soul import save_soul
from agent.streaming import StreamWriter
from agent.tools import default_tools
from connectors import mcp
from harness.control import Control
from harness.paths import HarnessPaths
from harness.roster import Bot
from isolation.base import BotHandle, Status
from isolation.machines import MachineBackend
from providers.base import Message, Provider


def _paths(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    return paths


def _agent(tmp_path):
    paths = _paths(tmp_path)
    agent = Agent(
        paths=paths,
        bot=Bot(name="atlas", role="assistant", personality="Stay terse.", provider="echo"),
        provider=Provider(model="m"),
        memory=Memory(paths=paths, bot="atlas"),
        control=Control(paths),
    )
    return agent, paths


def test_generic_turn_skips_feature_tutorials(tmp_path):
    agent, _ = _agent(tmp_path)
    prompt = agent.system_prompt("hello")
    assert _COMPUTER_PROMPT not in prompt
    assert _CREATE_BOT_PROMPT in prompt
    assert _ROUTINE_PROMPT not in prompt
    assert _CARDS_PROMPT not in prompt
    assert _CONNECTOR_PROMPT in prompt
    assert _TERMINAL_FIRST_PROMPT in prompt


def test_intent_prompts_attach_only_what_the_turn_needs(tmp_path):
    agent, _ = _agent(tmp_path)
    computer = agent.system_prompt("open chrome")
    assert _COMPUTER_PROMPT in computer
    assert _CREATE_BOT_PROMPT in computer
    create = agent.system_prompt("create a proposals assistant bot")
    assert _CREATE_BOT_PROMPT in create
    assert _COMPUTER_PROMPT not in create
    routine = agent.system_prompt("every morning check ahrefs")
    assert _ROUTINE_PROMPT in routine
    delay = agent.system_prompt("in an hour check staging only")
    assert _ROUTINE_PROMPT in delay
    cards = agent.system_prompt("show a table of linear tickets")
    assert _CARDS_PROMPT in cards
    repo = agent.system_prompt("check the latest proposal in the github repo")
    assert _REPO_PROMPT in repo
    assert _COMPUTER_PROMPT not in repo


def test_repo_job_hides_computer_when_github_tools_are_granted():
    tools = {
        "computer_open": object(),
        "computer_screenshot": object(),
        "run_command": object(),
        "github_list_repos": object(),
    }
    offered = _filter_repo_tools(tools, "clone example-owner/example-proposal-tool and report")
    assert "github_list_repos" in offered and "run_command" in offered
    assert "computer_open" not in offered
    kept = _filter_repo_tools(tools, "open chrome and look at github")
    assert "computer_open" in kept
    no_gh = _filter_repo_tools(
        {"computer_open": object(), "run_command": object()},
        "clone the repo",
    )
    assert "computer_open" in no_gh


def test_personality_is_in_the_system_prompt(tmp_path):
    agent, _ = _agent(tmp_path)
    roster = agent.bot.system_prompt()
    assert "Stay terse." in roster
    prompt = agent.system_prompt("hello")
    assert "Stay terse." in prompt
    # Seeded soul is a copy of personality — do not stack it again.
    assert prompt.count("Stay terse.") == 1
    assert "Your soul" not in prompt


def test_distinct_soul_stays_alongside_personality(tmp_path):
    agent, paths = _agent(tmp_path)
    save_soul(paths, "atlas", "I keep the garden.")
    prompt = agent.system_prompt("hello")
    assert "Stay terse." in prompt
    assert "I keep the garden." in prompt
    assert "Your soul" in prompt


def test_show_block_description_is_compact():
    spec = default_tools()["show_block"].spec
    assert "JSON tree" in spec.description
    assert '{"type":"column"' not in spec.description
    assert len(spec.description) < 700


def test_default_tools_is_cached():
    assert default_tools() is default_tools()


def test_history_budget_uses_provider_usage():
    provider = Provider(model="m", context_window=8_000)
    estimated = history_budget(provider, system="s" * 40, current="c" * 40)
    measured = history_budget(
        provider,
        system="s" * 40,
        current="c" * 40,
        usage={"input_tokens": 3_000},
        last_history_tokens=1_000,
    )
    assert prompt_tokens_from_usage({"prompt_tokens": 9}) == 9
    assert measured == 8_000 - 2_000 - 1_024 - 1_024
    assert estimated != measured


def test_cap_and_trim_tool_results():
    long = "x" * 20_000
    capped = cap_tool_result(long)
    assert capped.endswith("…(truncated)")
    assert len(capped) < 12_100
    messages = [
        Message(role="user", content="hi"),
        Message(role="tool", content=long, name="run_command", tool_call_id="1"),
        Message(role="tool", content="tiny", name="remember", tool_call_id="2"),
    ]
    trim_loop_messages(messages, budget=50)
    assert messages[1].content == "(earlier tool result omitted to stay in context)"
    assert messages[2].content == "tiny"


def test_estimate_message_tokens_counts_images():
    msg = Message(role="user", content="shot", images=[("image/png", b"\x00" * 7_500)])
    assert estimate_message_tokens(msg) >= 10


def test_mcp_result_cap_matches_shell():
    assert mcp._MAX_RESULT == 12_000
    blob = mcp._format_result({"content": [{"type": "text", "text": "z" * 20_000}]})
    assert blob.endswith("… (truncated)")
    assert len(blob) < 12_100


def test_session_records_cache_until_files_change(tmp_path):
    mem = Memory(paths=_paths(tmp_path), bot="atlas")
    mem.log_turn("s1", "in:user", "hello", peer="user")
    first = mem._session_records()
    second = mem._session_records()
    assert first is second
    mem.log_turn("s1", "out", "hi", peer="user")
    third = mem._session_records()
    assert third is not first
    assert len(third) == 2


def test_inbox_cache_skips_reread(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    reads = {"n": 0}
    original = Msg.from_file

    def counted(path):
        reads["n"] += 1
        return original(path)

    monkeypatch.setattr(Msg, "from_file", counted)
    send(paths, Msg(to="atlas", frm="user", text="one"))
    first = read_inbox(paths, "atlas")
    assert reads["n"] == 1
    second = read_inbox(paths, "atlas")
    assert reads["n"] == 1
    assert [m.text for _p, m in second] == [m.text for _p, m in first]
    send(paths, Msg(to="atlas", frm="user", text="two"))
    third = read_inbox(paths, "atlas")
    assert reads["n"] == 3
    assert len(third) == 2


def test_stream_writer_fsyncs_settling_events_not_every_delta(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    syncs = {"n": 0}
    monkeypatch.setattr(
        "agent.streaming.os.fsync", lambda _fd: syncs.__setitem__("n", syncs["n"] + 1)
    )
    w = StreamWriter(paths, "req")
    w.status("typing")
    w.delta("a")
    w.delta("b")
    w.final("ab", "atlas")
    assert syncs["n"] == 1  # only final


def test_machine_flush_skips_without_dirty_marker(tmp_path):
    paths = _paths(tmp_path)
    backend = MachineBackend(paths)
    handle = BotHandle(
        bot="atlas",
        backend="machines",
        pid=1,
        status=Status.RUNNING,
        meta={"machine": "harness-machine-0", "machine_id": 0},
    )
    backend.load = lambda _bot: handle
    called = []
    backend._sync_up = lambda *a, **k: called.append((a, k)) or {"copied": 1}
    assert backend.flush("atlas") is None
    assert called == []
    paths.run.mkdir(parents=True, exist_ok=True)
    paths.machine_dirty_file(0).write_text("1", encoding="utf-8")
    assert backend.flush("atlas") == {"copied": 1}
    assert called


def test_terminal_first_prompt_is_always_on(tmp_path):
    agent, _ = _agent(tmp_path)
    for text in (
        "hello",
        "list files in /tmp",
        "open chrome",
        "check the latest proposal in the github repo",
    ):
        prompt = agent.system_prompt(text)
        assert _TERMINAL_FIRST_PROMPT in prompt
        assert "Prefer the machine filesystem" in prompt
        assert "visit a website" in prompt
        assert "robots.txt, sitemap, and anything not needed visually should be curl" in prompt


def test_visit_site_attaches_computer_prompt(tmp_path):
    agent, _ = _agent(tmp_path)
    prompt = agent.system_prompt("visit https://example.com")
    assert _COMPUTER_PROMPT in prompt
    assert "open Chrome (computer_open app=browser)" in prompt
    assert "robots.txt, sitemap, and anything not needed visually should be curl" in prompt
    assert _wants_computer("visit https://example.com")
    assert _wants_computer("go to https://news.ycombinator.com")
    assert _wants_computer("visit example.com")
    assert _wants_computer("go to news.ycombinator.com")
    assert not _wants_computer("list env names")


def test_computer_prompt_groups_predictable_actions_and_matches_shared_control(tmp_path):
    agent, _ = _agent(tmp_path)
    prompt = agent.system_prompt("open chrome")
    assert "short group" in prompt
    assert "same response" in prompt
    assert "After any GUI action" not in prompt
    assert "takeover does not block" in prompt
    assert "GUI computer actions are refused until" not in prompt


def test_visit_intent_ignores_filenames_and_versions():
    for text in (
        "go to package.json",
        "browse config.yaml",
        "go to README.md",
        "visit app.py",
        "browse v1.2",
        "browse to package.json",
    ):
        assert not _wants_computer(text), text


def test_repo_job_still_hides_gui_when_user_did_not_ask_to_visit():
    tools = {
        "computer_open": object(),
        "computer_screenshot": object(),
        "run_command": object(),
        "github_list_repos": object(),
    }
    offered = _filter_repo_tools(tools, "clone example-owner/example-proposal-tool and report")
    assert "computer_open" not in offered
    kept = _filter_repo_tools(tools, "visit https://github.com/foo/bar")
    assert "computer_open" in kept
    file_job = _filter_repo_tools(tools, "clone the repo and go to package.json")
    assert "computer_open" not in file_job


def test_loop_trim_keeps_newest_unread_tool_result_when_history_exceeds_budget():
    messages = [
        Message(role="user", content="prior conversation " * 1000),
        Message(role="tool", content="old result", tool_call_id="old"),
        Message(role="tool", content="one Amazon order returned", tool_call_id="new"),
    ]
    trim_loop_messages(messages, budget=50)
    assert messages[-1].content == "one Amazon order returned"
    assert "omitted" in messages[1].content


def test_loop_trim_keeps_whole_latest_tool_batch():
    from providers.base import ToolCall

    messages = [
        Message(role="user", content="prior conversation " * 1000),
        Message(role="tool", content="old result", tool_call_id="old"),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="orders", name="run_command", arguments={}),
                ToolCall(id="catalog", name="run_command", arguments={}),
            ],
        ),
        Message(role="tool", content="order name and size", tool_call_id="orders"),
        Message(role="tool", content="matching image", tool_call_id="catalog"),
    ]
    trim_loop_messages(messages, budget=50)
    assert messages[-2].content == "order name and size"
    assert messages[-1].content == "matching image"
    assert "omitted" in messages[1].content
