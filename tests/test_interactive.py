"""Interactive chat blocks: secure secret input + choice box."""

from __future__ import annotations

import threading
import time

from agent.memory import Memory
from agent.streaming import StreamReader, StreamWriter, list_prompts, read_answer, write_answer
from agent.tools import ToolContext, default_tools
from harness.paths import HarnessPaths
from harness.secrets import set_secret
from providers.base import Message, ToolSpec
from providers.echo import EchoProvider


def _paths(tmp_path) -> HarnessPaths:
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas"])
    return p


def _ctx(paths, writer=None, timeout=1.0) -> ToolContext:
    return ToolContext(
        paths=paths,
        bot="atlas",
        memory=Memory(paths=paths, bot="atlas"),
        writer=writer,
        user_input_timeout=timeout,
    )


# -- answers bus -----------------------------------------------------------
def test_answer_roundtrip_and_consume(tmp_path):
    paths = _paths(tmp_path)
    assert write_answer(paths, "abc123", "blue")
    assert read_answer(paths, "abc123") == "blue"
    assert read_answer(paths, "abc123") is None  # consumed


def test_answer_ids_are_sanitized(tmp_path):
    paths = _paths(tmp_path)
    assert not write_answer(paths, "../../etc/passwd", "x") or (
        not (paths.home.parent / "etc").exists()
    )
    # a traversal-y id either fails or is reduced to a safe name inside answers/
    for f in paths.answers.glob("*"):
        assert f.parent == paths.answers


# -- request_secret --------------------------------------------------------
def test_request_secret_emits_event_and_unblocks_on_store(tmp_path):
    paths = _paths(tmp_path)
    writer = StreamWriter(paths, "r-secret")
    ctx = _ctx(paths, writer=writer, timeout=5.0)
    tools = default_tools()

    def provide():
        time.sleep(0.4)
        set_secret("DEMO_TOKEN", "s3cret-value", paths)

    t = threading.Thread(target=provide)
    t.start()
    out = tools["request_secret"].handler(ctx, {"name": "DEMO_TOKEN", "reason": "deploy"})
    t.join()

    assert out.startswith("ok:")
    assert "s3cret-value" not in out  # value never enters the transcript
    events = [e for e in StreamReader(paths, "r-secret")._read_new()]
    ev = next(e for e in events if e.type == "secret_request")
    assert ev.bot == "atlas"
    assert ev.name == "DEMO_TOKEN"
    assert ev.title == "Demo token"
    assert "deploy" in (ev.reason or "")
    assert "s3cret-value" not in (ev.reason or "")
    assert list_prompts(paths) == []  # cleared once stored


def test_request_secret_uses_explicit_title(tmp_path):
    paths = _paths(tmp_path)
    writer = StreamWriter(paths, "r-title")
    ctx = _ctx(paths, writer=writer, timeout=5.0)

    def provide():
        time.sleep(0.4)
        set_secret("SMTP_PASSWORD", "not-a-real-password", paths)

    t = threading.Thread(target=provide)
    t.start()
    out = default_tools()["request_secret"].handler(
        ctx,
        {
            "name": "SMTP_PASSWORD",
            "title": "SMTP password for mail.example.com",
            "reason": "Used only to send mail from this bot. Never shown in chat.",
        },
    )
    t.join()
    assert out.startswith("ok:")
    assert "not-a-real-password" not in out
    ev = next(e for e in StreamReader(paths, "r-title")._read_new() if e.type == "secret_request")
    assert ev.title == "SMTP password for mail.example.com"
    assert "Never shown" in (ev.reason or "")


def test_request_secret_short_circuits_when_available(tmp_path):
    paths = _paths(tmp_path)
    set_secret("EXISTING", "v", paths)
    writer = StreamWriter(paths, "r-have")
    out = default_tools()["request_secret"].handler(
        _ctx(paths, writer=writer), {"name": "EXISTING"}
    )
    assert out.startswith("ok:")
    assert StreamReader(paths, "r-have")._read_new() == []  # no prompt shown


def test_request_secret_times_out_with_cli_hint(tmp_path):
    paths = _paths(tmp_path)
    out = default_tools()["request_secret"].handler(
        _ctx(paths, writer=StreamWriter(paths, "r-to"), timeout=0.3), {"name": "NEVER_SET"}
    )
    assert out.startswith("error:")
    assert "harness secret set NEVER_SET" in out


# -- ask_user_choice -------------------------------------------------------
def test_choice_emits_event_and_returns_pick(tmp_path):
    paths = _paths(tmp_path)
    writer = StreamWriter(paths, "r-choice")
    ctx = _ctx(paths, writer=writer, timeout=5.0)
    tools = default_tools()
    picked: dict = {}

    def answer():
        deadline = time.time() + 4
        while time.time() < deadline:
            events = StreamReader(paths, "r-choice")._read_new()
            ev = next((e for e in events if e.type == "choice"), None)
            if ev:
                picked["ev"] = ev
                write_answer(paths, ev.id, ev.options[1])
                return
            time.sleep(0.05)

    t = threading.Thread(target=answer)
    t.start()
    out = tools["ask_user_choice"].handler(
        ctx, {"question": "Deploy where?", "options": ["staging", "production"]}
    )
    t.join()

    assert out == "user chose: production"
    ev = picked["ev"]
    assert ev.question == "Deploy where?"
    assert ev.options == ["staging", "production"]
    assert ev.bot == "atlas"
    assert read_answer(paths, ev.id) is None  # consumed by the tool


def test_choice_requires_two_options(tmp_path):
    paths = _paths(tmp_path)
    assert (
        default_tools()["ask_user_choice"]
        .handler(
            _ctx(paths, writer=StreamWriter(paths, "r-x")), {"question": "q", "options": ["only"]}
        )
        .startswith("error:")
    )


def test_secret_prompt_is_listed_until_stored(tmp_path):
    paths = _paths(tmp_path)
    writer = StreamWriter(paths, "r-open")
    ctx = _ctx(paths, writer=writer, timeout=5.0)

    def run():
        default_tools()["request_secret"].handler(ctx, {"name": "WAIT_TOKEN", "reason": "later"})

    t = threading.Thread(target=run)
    t.start()
    deadline = time.time() + 3
    while time.time() < deadline and not list_prompts(paths, "atlas"):
        time.sleep(0.05)
    rows = list_prompts(paths, "atlas")
    assert rows and rows[0]["name"] == "WAIT_TOKEN"
    assert rows[0]["title"] == "Wait token"
    set_secret("WAIT_TOKEN", "v", paths)
    t.join()
    assert list_prompts(paths) == []


def test_choice_times_out(tmp_path):
    paths = _paths(tmp_path)
    out = default_tools()["ask_user_choice"].handler(
        _ctx(paths, writer=StreamWriter(paths, "r-slow"), timeout=0.3),
        {"question": "q", "options": ["a", "b"]},
    )
    assert out.startswith("error:")


def test_choice_preempts_when_user_sends_a_chat(tmp_path):
    from agent import messaging

    paths = _paths(tmp_path)
    writer = StreamWriter(paths, "r-follow")
    ctx = _ctx(paths, writer=writer, timeout=5.0)
    ctx.turn_id = "r-follow"

    def follow():
        deadline = time.time() + 4
        while time.time() < deadline:
            if any(e.type == "choice" for e in StreamReader(paths, "r-follow")._read_new()):
                messaging.send(
                    paths,
                    messaging.Msg(to="atlas", frm="user", text="I mean the project name"),
                )
                return
            time.sleep(0.05)

    t = threading.Thread(target=follow)
    t.start()
    out = default_tools()["ask_user_choice"].handler(
        ctx, {"question": "How to name it?", "options": ["Keep ALT", "Rename"]}
    )
    t.join()
    assert out.startswith("interrupted:")
    pending = [m.text for m in messaging.pending(paths, "atlas")]
    assert "I mean the project name" in pending


# -- echo triggers (keyless end-to-end path) -------------------------------
def test_echo_emits_request_secret_call():
    p = EchoProvider()
    tools = [ToolSpec(name="request_secret", description="", parameters={})]
    out = p.complete(
        [Message(role="user", content="I need secret GITHUB_TOKEN to push")], tools=tools
    )
    assert out.tool_calls
    assert out.tool_calls[0].name == "request_secret"
    assert out.tool_calls[0].arguments["name"] == "GITHUB_TOKEN"


def test_echo_emits_choice_call():
    p = EchoProvider()
    tools = [ToolSpec(name="ask_user_choice", description="", parameters={})]
    out = p.complete(
        [Message(role="user", content="Which color? choose: red | blue | green")], tools=tools
    )
    assert out.tool_calls
    call = out.tool_calls[0]
    assert call.name == "ask_user_choice"
    assert call.arguments["options"] == ["red", "blue", "green"]
    assert "color" in call.arguments["question"].lower()


# -- server endpoints ------------------------------------------------------
def _server(tmp_path):
    import threading as _threading

    from harness.orchestrator import Orchestrator
    from harness.server import make_server

    rp = tmp_path / "roster.toml"
    rp.write_text(
        '[[bots]]\nname = "atlas"\nrole = "helper"\nprovider = "echo"\n', encoding="utf-8"
    )
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    httpd = make_server(orch, "127.0.0.1", 0)
    port = httpd.server_address[1]
    _threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, orch, f"http://127.0.0.1:{port}"


def _post(url, payload):
    import json as _json
    import urllib.request

    req = urllib.request.Request(
        url,
        data=_json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return _json.loads(r.read().decode())


def test_post_secret_stores_without_echoing(tmp_path):
    import json as _json

    httpd, orch, base = _server(tmp_path)
    try:
        out = _post(f"{base}/api/secrets", {"name": "MY_TOKEN", "value": "tok-123"})
        assert out["configured"] is True
        assert "tok-123" not in _json.dumps(out)
        assert (orch.paths.credentials / "MY_TOKEN").read_text() == "tok-123"
    finally:
        httpd.shutdown()
        orch.down()


def test_prompt_snapshot_can_include_explicit_resolutions(tmp_path):
    """A returning client can reconcile answered cards without inventing a pick."""
    import json
    import urllib.request

    from agent.streaming import resolve_prompt, write_prompt

    httpd, orch, base = _server(tmp_path)
    try:
        for prompt_id, bot in [("open", "atlas"), ("answered", "atlas"), ("other", "nova")]:
            write_prompt(
                orch.paths,
                {
                    "id": prompt_id,
                    "type": "choice",
                    "bot": bot,
                    "question": "Which platform?",
                    "options": ["X", "Reddit"],
                },
            )
        resolve_prompt(orch.paths, "answered", {"state": "answered", "responded_value": "Reddit"})
        with urllib.request.urlopen(f"{base}/api/prompts?bot=atlas", timeout=5) as response:
            assert [row["id"] for row in json.load(response)] == ["open"]
        with urllib.request.urlopen(
            f"{base}/api/prompts?bot=atlas&include_resolved=true", timeout=5
        ) as response:
            rows = {row["id"]: row for row in json.load(response)}
        assert set(rows) == {"open", "answered"}
        assert "resolution" not in rows["open"]
        assert rows["answered"]["resolution"]["responded_value"] == "Reddit"
    finally:
        httpd.shutdown()
        httpd.server_close()
        orch.down()


def test_post_answer_without_prompt_does_not_write_orphan_answer(tmp_path):
    import urllib.error

    import pytest as _pytest

    httpd, orch, base = _server(tmp_path)
    try:
        with _pytest.raises(urllib.error.HTTPError) as error:
            _post(f"{base}/api/answers", {"id": "deadbeef1234", "value": "staging"})
        assert error.value.code == 410
        assert read_answer(orch.paths, "deadbeef1234") is None
        with _pytest.raises(urllib.error.HTTPError):
            _post(f"{base}/api/answers", {"id": "", "value": "x"})
    finally:
        httpd.shutdown()
        orch.down()


def test_post_answer_without_prompt_never_becomes_chat(tmp_path):
    import urllib.error

    import pytest

    from agent import messaging

    httpd, orch, base = _server(tmp_path)
    try:
        for _ in range(2):
            with pytest.raises(urllib.error.HTTPError) as error:
                _post(
                    f"{base}/api/answers",
                    {"id": "deadbeef1234", "value": "I mean the project name", "bot": "atlas"},
                )
            assert error.value.code == 410
        assert messaging.pending(orch.paths, "atlas") == []
        assert read_answer(orch.paths, "deadbeef1234") is None
    finally:
        httpd.shutdown()
        orch.down()


def test_post_answer_parked_when_prompt_is_open(tmp_path):
    from agent import messaging
    from agent.history import user_thread
    from agent.streaming import write_prompt

    httpd, orch, base = _server(tmp_path)
    try:
        write_prompt(
            orch.paths,
            {
                "id": "abc123def456",
                "type": "choice",
                "bot": "atlas",
                "question": "Name?",
                "options": ["Keep ALT", "Rename"],
            },
        )
        out = _post(
            f"{base}/api/answers",
            {"id": "abc123def456", "value": "Rename", "bot": "atlas"},
        )
        assert out["ok"] is True
        assert out.get("parked") is True
        assert read_answer(orch.paths, "abc123def456") == "Rename"
        assert messaging.pending(orch.paths, "atlas") == []
        hist = user_thread(Memory(paths=orch.paths, bot="atlas"), peer="user")
        cards = [r for r in hist if r.get("type") == "card"]
        assert len(cards) == 1
        assert cards[0]["card_type"] == "choice"
        assert cards[0]["payload"]["question"] == "Name?"
        assert cards[0]["payload"]["options"] == ["Keep ALT", "Rename"]
        assert cards[0]["resolution"]["state"] == "answered"
        assert cards[0]["resolution"]["responded_value"] == "Rename"
    finally:
        httpd.shutdown()
        orch.down()


def test_choice_end_to_end_through_live_bot_and_sse(tmp_path):
    """echo bot asks a choice; user answers over HTTP; the reply completes.

    Since the wait-for-choice change, the SSE stream parks (closes without a
    "timed out" error) once the choice is offered; the answer then unblocks
    the bot and its final lands in the request's stream file.
    """
    import json as _json
    import urllib.request

    httpd, orch, base = _server(tmp_path)
    try:
        orch.up()
        deadline = time.time() + 10
        while time.time() < deadline and not all(
            h.status.value == "running" for h in orch.status()
        ):
            time.sleep(0.2)

        req = urllib.request.Request(
            f"{base}/api/chat",
            data=_json.dumps(
                {"bot": "atlas", "text": "Deploy target? choose: staging | prod"}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        events: list[dict] = []
        with urllib.request.urlopen(req, timeout=60) as r:
            for raw in r:
                line = raw.decode().strip()
                if not line.startswith("data:"):
                    continue
                events.append(_json.loads(line[len("data:") :]))

        choice = next(e for e in events if e.get("type") == "choice")
        assert choice["options"] == ["staging", "prod"]
        assert choice["question"].startswith("Deploy target")
        # parked on the user: the stream closed with no error/final frames
        assert not [e for e in events if e.get("type") in ("error", "final")]

        _post(f"{base}/api/answers", {"id": choice["id"], "value": "prod"})
        reader = StreamReader(orch.paths, choice["request_id"])
        final = ""
        for ev in reader.events(timeout=30):
            if ev.type == "final":
                final = ev.text or ""
        assert "prod" in final
    finally:
        httpd.shutdown()
        orch.down()
