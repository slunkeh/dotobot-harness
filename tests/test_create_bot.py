"""Bots can create roster peers, after asking clarifying questions."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from agent.memory import Memory
from agent.runtime import _CLARIFY_PROMPT, _CREATE_BOT_PROMPT, Agent
from agent.tools import ToolContext, _bot_slug, default_tools
from harness.control import Control
from harness.orchestrator import Orchestrator
from harness.paths import HarnessPaths
from harness.roster import Bot
from harness.server import make_server
from isolation import BotHandle, IsolationUnavailable, Status
from providers.base import Message, ToolSpec
from providers.echo import EchoProvider


def _paths(tmp_path) -> HarnessPaths:
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas"])
    return p


def _ctx(paths) -> ToolContext:
    return ToolContext(paths=paths, bot="atlas", memory=Memory(paths=paths, bot="atlas"))


def _echo_tools():
    return [
        ToolSpec(name="ask_user_choice", description="", parameters={}),
        ToolSpec(name="create_bot", description="", parameters={}),
    ]


def test_create_bot_is_a_default_tool():
    assert "create_bot" in default_tools()


def test_system_prompt_tells_bot_to_ask_before_creating(tmp_path):
    paths = HarnessPaths(tmp_path)
    paths.ensure_layout(["atlas"])
    agent = Agent(
        paths=paths,
        bot=Bot(name="atlas", role="assistant", provider="echo"),
        provider=EchoProvider(),
        memory=Memory(paths=paths, bot="atlas"),
        control=Control(paths),
    )
    prompt = agent.system_prompt("create a bot")
    assert _CREATE_BOT_PROMPT in prompt
    assert _CLARIFY_PROMPT in prompt
    assert "ask_user_choice" in prompt
    assert "create_bot" in prompt
    assert "never invent" in prompt
    assert "unless create_bot returned ok" in prompt
    assert "account default" in prompt
    assert "ask_user_choice with grok" not in prompt


def test_create_bot_refuses_missing_name(tmp_path):
    paths = _paths(tmp_path)
    out = default_tools()["create_bot"].handler(_ctx(paths), {"role": "helper"})
    assert out.startswith("error:")
    assert "ask_user_choice" in out


def test_bot_slug_hyphenates_display_names():
    assert _bot_slug("Research Assistant") == "research-assistant"
    assert _bot_slug("helper") == "helper"
    assert _bot_slug("Nova_1") == "Nova_1"
    assert _bot_slug("  ") == ""


def test_create_bot_omits_provider_when_unset(tmp_path):
    paths = _paths(tmp_path)
    out = default_tools()["create_bot"].handler(_ctx(paths), {"name": "helper"})
    assert "serve.json" in out


def test_create_bot_errors_without_serve_json(tmp_path):
    paths = _paths(tmp_path)
    out = default_tools()["create_bot"].handler(_ctx(paths), {"name": "helper", "provider": "echo"})
    assert "serve.json" in out


def test_echo_asks_before_creating_unnamed_bot():
    p = EchoProvider()
    out = p.complete([Message(role="user", content="please create a bot")], tools=_echo_tools())
    assert out.tool_calls
    call = out.tool_calls[0]
    assert call.name == "ask_user_choice"
    assert "name" in call.arguments["question"].lower()
    assert len(call.arguments["options"]) >= 2


def test_echo_creates_when_name_is_given():
    p = EchoProvider()
    out = p.complete(
        [Message(role="user", content="create a bot named researcher")], tools=_echo_tools()
    )
    assert out.tool_calls
    call = out.tool_calls[0]
    assert call.name == "create_bot"
    assert call.arguments["name"] == "researcher"


def test_echo_creates_after_user_picks_a_name():
    p = EchoProvider()
    out = p.complete(
        [
            Message(role="user", content="create a new bot"),
            Message(role="tool", content="user chose: writer", name="ask_user_choice"),
        ],
        tools=_echo_tools(),
    )
    assert out.tool_calls
    call = out.tool_calls[0]
    assert call.name == "create_bot"
    assert call.arguments["name"] == "writer"


def test_echo_still_wraps_unrelated_tool_results():
    p = EchoProvider(persona="atlas")
    out = p.complete(
        [
            Message(role="user", content="@nova hi"),
            Message(role="tool", content="nova replied: hey", name="message_agent"),
        ],
        tools=_echo_tools(),
    )
    assert not out.tool_calls
    assert "nova: hey" in out.text
    assert "relayed reply" not in out.text


def test_create_bot_posts_through_serve_json(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(
        '[[bots]]\nname = "atlas"\nrole = "helper"\nprovider = "echo"\n', encoding="utf-8"
    )
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    httpd = make_server(orch, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        info = json.loads((orch.paths.home / "serve.json").read_text(encoding="utf-8"))
        assert info["url"].startswith("http://127.0.0.1:")
        ctx = ToolContext(
            paths=orch.paths, bot="atlas", memory=Memory(paths=orch.paths, bot="atlas")
        )
        out = default_tools()["create_bot"].handler(
            ctx,
            {
                "name": "zephyr",
                "role": "a tester",
                "provider": "echo",
                "personality": "short answers",
            },
        )
        assert out.startswith("ok:"), out
        assert "zephyr" in out
        assert "zephyr" in orch.roster.names()
        store = json.loads((orch.paths.home / "roster.json").read_text(encoding="utf-8"))
        assert any(b["name"] == "zephyr" for b in store["bots"])
        out = default_tools()["create_bot"].handler(
            ctx,
            {"name": "Research Assistant", "provider": "echo"},
        )
        assert out.startswith("ok:"), out
        assert "research-assistant" in orch.roster.names()
        peer = orch.roster.get("research-assistant")
        assert peer.title == "Research Assistant"
    finally:
        httpd.shutdown()
        orch.down()


def test_create_bot_uses_account_default_when_provider_omitted(tmp_path):
    from harness.prefs import set_llm_defaults

    rp = tmp_path / "roster.toml"
    rp.write_text(
        '[[bots]]\nname = "atlas"\nrole = "helper"\nprovider = "echo"\n', encoding="utf-8"
    )
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    set_llm_defaults(orch.paths, provider="grok", model="grok-4.6")
    httpd = make_server(orch, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        ctx = ToolContext(
            paths=orch.paths, bot="atlas", memory=Memory(paths=orch.paths, bot="atlas")
        )
        out = default_tools()["create_bot"].handler(ctx, {"name": "scout", "role": "a tester"})
        assert out.startswith("ok:"), out
        assert "provider=grok" in out
        scout = orch.roster.get("scout")
        assert scout.provider == "grok"
        assert scout.model == "grok-4.6"
        named = default_tools()["create_bot"].handler(
            ctx, {"name": "echo-peer", "provider": "echo"}
        )
        assert named.startswith("ok:"), named
        peer = orch.roster.get("echo-peer")
        assert peer.provider == "echo"
        assert peer.model is None
    finally:
        httpd.shutdown()
        orch.down()


@pytest.fixture
def creation_server(tmp_path, monkeypatch):
    rp = tmp_path / "roster.toml"
    rp.write_text('[[bots]]\nname = "atlas"\nprovider = "echo"\n', encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp)
    orch.init()
    handles = {}
    monkeypatch.setattr(orch.backend, "load", handles.get)
    monkeypatch.setattr(orch.backend, "stop", lambda h: handles.pop(h.bot, None))
    httpd = make_server(orch, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield orch, handles, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        orch.down()


def _created_row(base, name):
    with urllib.request.urlopen(f"{base}/api/bots", timeout=2) as response:
        return next(row for row in json.load(response) if row["name"] == name)


def test_creation_confirms_persisted_bot_while_machine_start_is_blocked(
    creation_server, monkeypatch
):
    """The API/tool must not wait for the fleet flush and machine clone-down."""
    from agent import messaging

    orch, handles, base = creation_server
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    results = []

    def spawn(name, argv):
        entered.set()
        assert release.wait(5)
        handles[name] = BotHandle(bot=name, backend="process", status=Status.RUNNING)
        return handles[name]

    def create():
        results.append(
            default_tools()["create_bot"].handler(_ctx(orch.paths), {"name": "Community Helper"})
        )
        returned.set()

    monkeypatch.setattr(orch.backend, "spawn", spawn)
    caller = threading.Thread(target=create)
    caller.start()
    try:
        assert entered.wait(2)
        assert returned.wait(1), "creation response waited for machine provisioning"
        assert results[0].startswith("ok: created bot 'community-helper'"), results
        assert "starting" in results[0]
        assert _created_row(base, "community-helper")["status"] == "starting"
        stored = json.loads((orch.paths.home / "roster.json").read_text())
        assert any(bot["name"] == "community-helper" for bot in stored["bots"])
        welcomes = messaging.pending(orch.paths, "community-helper")
        assert len(welcomes) == 1 and welcomes[0].origin == "welcome"
    finally:
        release.set()
        caller.join(3)
    deadline = time.monotonic() + 2
    while (
        _created_row(base, "community-helper")["status"] == "starting"
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    assert _created_row(base, "community-helper")["status"] == "running"


def test_creation_start_failure_is_visible_and_restart_can_recover(creation_server, monkeypatch):
    from agent import messaging

    orch, handles, base = creation_server

    def fail(name, argv):
        raise IsolationUnavailable("machine image is unavailable")

    monkeypatch.setattr(orch.backend, "spawn", fail)
    result = default_tools()["create_bot"].handler(_ctx(orch.paths), {"name": "scout"})
    assert result.startswith("ok: created bot 'scout'"), result
    deadline = time.monotonic() + 2
    while _created_row(base, "scout")["status"] == "starting" and time.monotonic() < deadline:
        time.sleep(0.01)
    row = _created_row(base, "scout")
    assert row["status"] == "stopped"
    assert row["startup_error"] == "machine image is unavailable"
    assert len(messaging.pending(orch.paths, "scout")) == 1

    def recover(name, argv):
        handles[name] = BotHandle(bot=name, backend="process", status=Status.RUNNING)
        return handles[name]

    monkeypatch.setattr(orch.backend, "spawn", recover)
    orch.restart("scout")
    row = _created_row(base, "scout")
    assert row["status"] == "running"
    assert not row.get("startup_error")
    assert len(messaging.pending(orch.paths, "scout")) == 1


@pytest.mark.parametrize("replace", [False, True])
def test_deleting_before_creation_worker_starts_cannot_resurrect_bot(
    creation_server, monkeypatch, replace
):
    from contextlib import contextmanager

    orch, _, _ = creation_server
    waiting, release, finished = threading.Event(), threading.Event(), threading.Event()
    lifecycle_lock = orch._lifecycle_lock
    spawned = []

    @contextmanager
    def delayed_lock(name):
        if threading.current_thread().name == "create-bot-scout":
            waiting.set()
            assert release.wait(5)
        with lifecycle_lock(name):
            yield

    monkeypatch.setattr(orch, "_lifecycle_lock", delayed_lock)
    monkeypatch.setattr(orch.backend, "spawn", lambda name, argv: spawned.append(name))
    orch.add_bot(name="scout", start=False)
    orch.start_created_bot("scout", welcome=False, on_finished=finished.set)
    try:
        assert waiting.wait(2)
        orch.remove_bot("scout")
        from harness.bot_cleanup import shutdown

        shutdown(orch, "scout")
        if replace:
            orch.add_bot(name="scout", personality="replacement", start=False)
    finally:
        release.set()
    assert finished.wait(2)
    assert spawned == []
    assert ("scout" in orch.roster.names()) is replace
    assert all(handle.status != Status.STARTING for handle in orch.status())


def test_creation_start_error_survives_control_plane_reload(creation_server, monkeypatch):
    orch, handles, base = creation_server
    finished = threading.Event()

    def fail(name, argv):
        raise IsolationUnavailable("container startup failed")

    monkeypatch.setattr(orch.backend, "spawn", fail)
    orch.add_bot(name="scout", start=False)
    orch.start_created_bot("scout", on_finished=finished.set)
    assert finished.wait(2)
    restarted = Orchestrator.create(home=orch.paths.home, roster_path=orch.roster_path)
    monkeypatch.setattr(restarted.backend, "load", handles.get)
    row = next(h for h in restarted.status() if h.bot == "scout")
    assert row.status == Status.STOPPED
    assert row.meta["startup_error"] == "container startup failed"


@pytest.mark.parametrize("action", ["stop", "update"])
def test_pending_creation_respects_stop_and_preserves_profile_edits(
    creation_server, monkeypatch, action
):
    from contextlib import contextmanager

    orch, handles, _ = creation_server
    waiting, release, finished = threading.Event(), threading.Event(), threading.Event()
    lifecycle_lock = orch._lifecycle_lock
    spawned = []

    @contextmanager
    def delayed_lock(name):
        if threading.current_thread().name == "create-bot-scout":
            waiting.set()
            assert release.wait(5)
        with lifecycle_lock(name):
            yield

    def spawn(name, argv):
        spawned.append(orch.roster.get(name).title)
        handles[name] = BotHandle(bot=name, backend="process", status=Status.RUNNING)
        return handles[name]

    monkeypatch.setattr(orch, "_lifecycle_lock", delayed_lock)
    monkeypatch.setattr(orch.backend, "spawn", spawn)
    orch.add_bot(name="scout", start=False)
    orch.start_created_bot("scout", welcome=False, on_finished=finished.set)
    try:
        assert waiting.wait(2)
        if action == "stop":
            orch.down()
        else:
            orch.update_bot("scout", title="Research Scout")
            orch.use_json_store()  # reloading settings must not cancel this creation
    finally:
        release.set()
    assert finished.wait(2)
    assert spawned == ([] if action == "stop" else ["Research Scout"])
    assert all(handle.status != Status.STARTING for handle in orch.status())


def test_creation_lock_failure_is_terminal_and_visible(creation_server, monkeypatch):
    from contextlib import contextmanager

    orch, _, base = creation_server
    finished = threading.Event()
    lock = orch._lifecycle_lock

    @contextmanager
    def failing_lock(name):
        if threading.current_thread().name == "create-bot-scout":
            raise OSError("could not open the lifecycle lock")
        with lock(name):
            yield

    monkeypatch.setattr(orch, "_lifecycle_lock", failing_lock)
    orch.add_bot(name="scout", start=False)
    orch.start_created_bot("scout", on_finished=finished.set)
    assert finished.wait(2)
    assert not orch.is_starting("scout")
    row = _created_row(base, "scout")
    assert row["status"] == "stopped"
    assert row["startup_error"] == "could not open the lifecycle lock"


def test_concurrent_delete_before_start_registration_returns_http_conflict(
    creation_server, monkeypatch
):
    from agent import messaging

    orch, _, base = creation_server
    start_created = orch.start_created_bot

    def deleted_before_start(name, **kwargs):
        orch.remove_bot(name)
        return start_created(name, **kwargs)

    monkeypatch.setattr(orch, "start_created_bot", deleted_before_start)
    request = urllib.request.Request(
        f"{base}/api/bots",
        data=b'{"name":"scout"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as failed:
        urllib.request.urlopen(request, timeout=2)
    assert failed.value.code == 409
    assert "No bot named" in json.load(failed.value)["error"]
    assert not orch.is_starting("scout")
    assert "scout" not in orch.roster.names()
    assert messaging.pending(orch.paths, "scout") == []


def _paused_creation(tmp_path, monkeypatch):
    from contextlib import contextmanager

    rp = tmp_path / "roster.toml"
    rp.write_text("bots = []\n", encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp)
    orch.init()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    lock = orch._lifecycle_lock
    handles, spawned = {}, []

    @contextmanager
    def paused_lock(name):
        if threading.current_thread().name == "create-bot-scout":
            entered.set()
            assert release.wait(5)
        with lock(name):
            yield

    def spawn(name, argv):
        spawned.append(name)
        handles[name] = BotHandle(bot=name, backend="process", status=Status.RUNNING)
        return handles[name]

    monkeypatch.setattr(orch, "_lifecycle_lock", paused_lock)
    monkeypatch.setattr(orch.backend, "load", handles.get)
    monkeypatch.setattr(orch.backend, "spawn", spawn)
    monkeypatch.setattr(orch.backend, "stop", lambda handle: handles.pop(handle.bot, None))
    orch.add_bot(name="scout", start=False)
    orch.start_created_bot("scout", on_finished=finished.set)
    assert entered.wait(2)
    return orch, release, finished, spawned


def test_restart_cancels_pending_creation_worker_and_preserves_welcome(tmp_path, monkeypatch):
    from agent import messaging

    orch, release, finished, spawned = _paused_creation(tmp_path, monkeypatch)
    welcome = messaging.pending(orch.paths, "scout")[0]
    user = messaging.Msg(to="scout", frm="user", text="Start my research")
    messaging.send(orch.paths, user)
    try:
        handle = orch.restart("scout")
        assert handle.status == Status.RUNNING
        assert not orch.is_starting("scout"), "restart left the old creation worker pending"
        assert next(h for h in orch.status() if h.bot == "scout").status == Status.RUNNING
    finally:
        release.set()
        assert finished.wait(2)
    assert spawned == ["scout"], "the canceled creation worker spawned after restart"
    assert {m.id for m in messaging.pending(orch.paths, "scout")} == {welcome.id, user.id}


def test_delete_cancels_only_old_welcome_before_same_name_recreation(tmp_path, monkeypatch):
    from agent import messaging

    orch, release, finished, spawned = _paused_creation(tmp_path, monkeypatch)
    old_welcome = messaging.pending(orch.paths, "scout")[0]
    # Text alone never makes a user request disposable; other queue lanes and
    # already-produced replies retain their existing archive/retention policy.
    kept = [
        messaging.Msg(to="scout", frm="user", text=old_welcome.text),
        messaging.Msg(to="scout", frm="user", text="Scheduled work", origin="routine"),
        messaging.Msg(to="scout", frm="atlas", text="Peer request"),
        messaging.Msg(to="scout", frm="scout", text="Reply", reply_to="earlier", origin="welcome"),
    ]
    for msg in kept:
        messaging.send(orch.paths, msg)
    try:
        orch.remove_bot("scout")
        from harness.bot_cleanup import shutdown

        shutdown(orch, "scout")
        assert old_welcome.id not in {m.id for _, m in messaging.read_inbox(orch.paths, "scout")}, (
            "deletion left the canceled creation's welcome queued"
        )
        assert {m.id for _, m in messaging.read_inbox(orch.paths, "scout")} == {m.id for m in kept}
        archived = [
            messaging.Msg.from_file(p) for p in orch.paths.processed("scout").glob("*.json")
        ]
        assert {m.id for m in archived} == {old_welcome.id}
        orch.add_bot(name="scout", title="Replacement", provider="echo")
    finally:
        release.set()
        assert finished.wait(2)
    assert spawned == ["scout"]
    welcomes = [m for m in messaging.pending(orch.paths, "scout") if m.origin == "welcome"]
    assert len(welcomes) == 1 and welcomes[0].id != old_welcome.id
    assert "Replacement" in welcomes[0].text
    assert {m.id for _, m in messaging.read_inbox(orch.paths, "scout")} == {
        *(m.id for m in kept),
        welcomes[0].id,
    }


def test_stop_cancels_restart_worker_before_it_spawns(tmp_path, monkeypatch):
    rp = tmp_path / "roster.toml"
    rp.write_text('[[bots]]\nname="atlas"\nrole="helper"\nprovider="echo"\n')
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp)
    orch.init()
    workers, spawned = [], []

    class DeferredThread:
        def __init__(self, *, target, **kwargs):
            workers.append(target)

        def start(self):
            pass

    monkeypatch.setattr(threading, "Thread", DeferredThread)
    monkeypatch.setattr(orch.backend, "spawn", lambda name, argv: spawned.append(name))
    orch.restart("atlas", wait=False)
    assert orch.is_starting("atlas")
    orch._stop_bot("atlas")
    workers[0]()
    assert not orch.is_starting("atlas")
    assert spawned == []
