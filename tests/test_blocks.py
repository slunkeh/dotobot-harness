"""Blocks: declarative UI surfaces (show_block / update_block / block_actions)."""

from __future__ import annotations

import json
import threading
import time

from agent.blocks import (
    BlockDef,
    blocks_prompt,
    find_block_def,
    list_blocks,
    load_block_defs,
    read_block,
    validate_view,
    write_block,
)
from agent.memory import Memory
from agent.streaming import (
    StreamEvent,
    StreamReader,
    StreamWriter,
    multiplex,
    read_answer,
    write_answer,
)
from agent.tools import ToolContext, default_tools
from harness.paths import HarnessPaths


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


_FORM = {
    "type": "column",
    "children": [
        {"type": "heading", "text": "Deploy", "level": 2},
        {"type": "text_input", "name": "env", "label": "Environment"},
        {"type": "button", "label": "Go", "action": {"kind": "submit"}, "style": "primary"},
    ],
}


# -- validate_view ---------------------------------------------------------
def test_validate_view_accepts_a_form():
    assert validate_view(_FORM) is None


def test_validate_view_rejects_unknown_type():
    err = validate_view({"type": "carousel"})
    assert err and err.startswith("error:") and "carousel" in err


def test_validate_view_rejects_duplicate_input_names():
    err = validate_view(
        {
            "type": "column",
            "children": [
                {"type": "text_input", "name": "env"},
                {"type": "select", "name": "env", "options": ["a"]},
            ],
        }
    )
    assert err and "duplicate" in err


def test_validate_view_rejects_bad_button_action():
    err = validate_view({"type": "button", "label": "x", "action": {"kind": "detonate"}})
    assert err and "kind" in err
    err = validate_view({"type": "button", "label": "x", "action": {"kind": "action"}})
    assert err and "'id'" in err


def test_validate_view_rejects_deep_nesting():
    node = {"type": "text", "text": "leaf"}
    for _ in range(10):
        node = {"type": "column", "children": [node]}
    err = validate_view(node)
    assert err and "deeper" in err


def test_validate_view_accepts_a_chart_node():
    view = {
        "type": "column",
        "children": [
            {"type": "heading", "text": "Traffic", "level": 3},
            {
                "type": "chart",
                "kind": "line",
                "labels": ["Mon", "Tue"],
                "series": [{"name": "visits", "values": [1, 2]}],
            },
        ],
    }
    assert validate_view(view) is None


def test_validate_view_reports_chart_errors():
    err = validate_view({"type": "chart", "kind": "line"})
    assert err and "'series'" in err
    err = validate_view({"type": "chart", "kind": "spiral", "series": [{"values": [1]}]})
    assert err and "unknown chart kind" in err


# -- instance store --------------------------------------------------------
def test_block_store_roundtrip_and_filtering(tmp_path):
    paths = _paths(tmp_path)
    bid = write_block(paths, {"bot": "atlas", "surface": "chat", "title": "t", "view": _FORM})
    assert bid and bid.startswith("blk_")
    inst = read_block(paths, bid)
    assert inst["status"] == "open"
    assert inst["renderer"] == "native-v1"
    assert list_blocks(paths, "atlas", "open")[0]["id"] == bid
    assert list_blocks(paths, "someone-else") == []
    assert read_block(paths, "../../etc/passwd") is None


# -- show_block (blocking) -------------------------------------------------
def test_show_block_wait_returns_submitted_values(tmp_path):
    paths = _paths(tmp_path)
    writer = StreamWriter(paths, "r-block")
    ctx = _ctx(paths, writer=writer, timeout=5.0)
    tools = default_tools()
    seen: dict = {}

    def submit():
        deadline = time.time() + 4
        while time.time() < deadline:
            ev = next(
                (e for e in StreamReader(paths, "r-block")._read_new() if e.type == "block"),
                None,
            )
            if ev:
                seen["ev"] = ev
                write_answer(
                    paths, ev.id, json.dumps({"action": "submit", "values": {"env": "staging"}})
                )
                return
            time.sleep(0.05)

    t = threading.Thread(target=submit)
    t.start()
    out = tools["show_block"].handler(ctx, {"title": "Deploy", "view": _FORM, "wait": True})
    t.join()

    assert out.startswith("user submitted:")
    assert "staging" in out
    ev = seen["ev"]
    assert ev.bot == "atlas"
    assert ev.surface == "chat"
    assert ev.title == "Deploy"
    assert ev.blocking is True
    assert ev.view["type"] == "column"
    assert read_answer(paths, ev.id) is None  # consumed by the tool
    assert read_block(paths, ev.id)["status"] == "settled"  # kept, not deleted


def test_show_block_wait_times_out_and_settles(tmp_path):
    paths = _paths(tmp_path)
    out = default_tools()["show_block"].handler(
        _ctx(paths, writer=StreamWriter(paths, "r-slow"), timeout=0.3),
        {"title": "t", "view": _FORM, "wait": True},
    )
    assert out.startswith("error:")
    assert list_blocks(paths, "atlas", "open") == []


def test_show_block_rejects_bad_view_and_surface(tmp_path):
    paths = _paths(tmp_path)
    tools = default_tools()
    ctx = _ctx(paths, writer=StreamWriter(paths, "r-bad"))
    assert tools["show_block"].handler(ctx, {"title": "t"}).startswith("error:")
    assert (
        tools["show_block"]
        .handler(ctx, {"title": "t", "view": {"type": "nope"}})
        .startswith("error:")
    )
    assert (
        tools["show_block"]
        .handler(ctx, {"title": "t", "view": _FORM, "surface": "hologram"})
        .startswith("error:")
    )


def test_show_block_refused_for_colleagues(tmp_path):
    paths = _paths(tmp_path)
    ctx = _ctx(paths, writer=StreamWriter(paths, "r-consult"))
    ctx.sender = "hermes"
    out = default_tools()["show_block"].handler(ctx, {"title": "t", "view": _FORM})
    assert out.startswith("error:")


# -- show_block (non-blocking) + update_block ------------------------------
def test_show_block_nonblocking_returns_and_updates(tmp_path):
    paths = _paths(tmp_path)
    writer = StreamWriter(paths, "r-live")
    ctx = _ctx(paths, writer=writer)
    tools = default_tools()
    out = tools["show_block"].handler(
        ctx,
        {
            "title": "Progress",
            "surface": "flyout",
            "view": {"type": "progress", "value": 0.2, "label": "working"},
        },
    )
    assert out.startswith("ok: block blk_")
    bid = out.split()[2]
    out = tools["update_block"].handler(
        ctx,
        {"block_id": bid, "view": {"type": "progress", "value": 1.0}, "settle": True},
    )
    assert out.startswith("ok:")
    inst = read_block(paths, bid)
    assert inst["status"] == "settled"
    assert inst["view"]["value"] == 1.0
    events = [e for e in StreamReader(paths, "r-live")._read_new() if e.type == "block"]
    assert [e.update for e in events] == [False, True]
    assert events[0].surface == "flyout"
    assert events[0].blocking is False


# -- turn settling ---------------------------------------------------------
def _mux_types(paths, rid):
    reader = StreamReader(paths, rid)
    return [ev.type for _, ev in multiplex([("atlas", reader)], timeout=0.3)]


def test_blocking_block_settles_the_stream_wait(tmp_path):
    paths = _paths(tmp_path)
    w = StreamWriter(paths, "r-park")
    w.block(
        "atlas", {"id": "blk_1", "surface": "chat", "title": "t", "view": _FORM, "blocking": True}
    )
    start = time.time()
    assert _mux_types(paths, "r-park") == ["block"]
    assert time.time() - start < 0.25  # settled immediately, no idle timeout


def test_nonblocking_block_keeps_waiting_for_final(tmp_path):
    paths = _paths(tmp_path)
    w = StreamWriter(paths, "r-flow")
    w.block(
        "atlas", {"id": "blk_2", "surface": "chat", "title": "t", "view": _FORM, "blocking": False}
    )
    w.final("done", "atlas")
    # final is preceded by its closing full-content upsert
    assert _mux_types(paths, "r-flow") == ["block", "message", "final"]


def test_asdict_event_roundtrips_block_fields():
    from harness.server import asdict_event

    ev = StreamEvent.from_dict(
        {
            "type": "block",
            "id": "blk_3",
            "bot": "atlas",
            "block_type": "adhoc",
            "surface": "pane",
            "title": "Dash",
            "view": _FORM,
            "state": {"n": 1},
            "blocking": True,
            "update": False,
        }
    )
    d = asdict_event(ev)
    assert d["surface"] == "pane"
    assert d["title"] == "Dash"
    assert d["view"] == _FORM
    assert d["state"] == {"n": 1}
    assert d["blocking"] is True
    assert d["update"] is False
    assert d["block_type"] == "adhoc"


# -- definitions -----------------------------------------------------------
_BLOCK_MD = """---
name: deploy-status
description: Live deploy checklist
surfaces: chat, flyout
schema_version: 1
---
Show this when the user asks about a deployment.
"""


def _install(paths, root, name="deploy-status", md=_BLOCK_MD, handler=None, view=None):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "BLOCK.md").write_text(md, encoding="utf-8")
    if view is not None:
        (d / "view.json").write_text(json.dumps(view), encoding="utf-8")
    if handler is not None:
        (d / "handler.py").write_text(handler, encoding="utf-8")
    return d


def test_block_defs_discovery_shared_then_private(tmp_path):
    paths = _paths(tmp_path)
    _install(paths, paths.blocks, view=_FORM)
    _install(
        paths,
        paths.bot_memory("atlas") / "blocks",
        name="scratch",
        md="---\nname: scratch\ndescription: private pad\nsurfaces: pane\n---\nbody",
    )
    defs = load_block_defs(paths, "atlas")
    # ensure_layout seeds the "notes" example block alongside the installed ones
    assert [(d.name, d.source) for d in defs] == [
        ("deploy-status", "shared"),
        ("notes", "shared"),
        ("scratch", "private"),
    ]
    assert defs[0].surfaces == ["chat", "flyout"]
    assert defs[0].view_template == _FORM
    assert defs[2].surfaces == ["pane"]
    prompt = blocks_prompt(paths, "atlas")
    assert "- deploy-status (shared" in prompt
    assert "- scratch (private" in prompt
    other = blocks_prompt(paths, "other-bot")
    assert "- scratch (" not in other  # private stays private
    assert all(d.name != "scratch" for d in load_block_defs(paths, "other-bot"))


def test_newer_schema_defs_are_skipped(tmp_path):
    paths = _paths(tmp_path)
    _install(
        paths,
        paths.blocks,
        name="future",
        md="---\nname: future\ndescription: x\nschema_version: 99\n---\nbody",
    )
    assert find_block_def(paths, "atlas", "future") is None


def test_show_block_uses_installed_view_template(tmp_path):
    paths = _paths(tmp_path)
    _install(paths, paths.blocks, view=_FORM)
    writer = StreamWriter(paths, "r-tpl")
    out = default_tools()["show_block"].handler(
        _ctx(paths, writer=writer),
        {"title": "Deploy", "block_type": "deploy-status"},
    )
    assert out.startswith("ok:")
    ev = next(e for e in StreamReader(paths, "r-tpl")._read_new() if e.type == "block")
    assert ev.view == _FORM
    assert ev.block_type == "deploy-status"


_HANDLER = """
def render(state):
    return {"type": "list", "items": state.get("items", [])}

def on_action(action, values, state):
    items = state.get("items", []) + [values.get("note", "")]
    return {
        "state": {"items": items},
        "view": {"type": "list", "items": items},
        "settle": action == "finish",
    }
"""


def test_show_block_renders_via_handler(tmp_path):
    paths = _paths(tmp_path)
    _install(
        paths,
        paths.blocks,
        name="notes",
        md="---\nname: notes\ndescription: n\n---\nbody",
        handler=_HANDLER,
    )
    writer = StreamWriter(paths, "r-h")
    out = default_tools()["show_block"].handler(
        _ctx(paths, writer=writer),
        {"title": "Notes", "block_type": "notes", "state": {"items": ["a"]}},
    )
    assert out.startswith("ok:")
    ev = next(e for e in StreamReader(paths, "r-h")._read_new() if e.type == "block")
    assert ev.view == {"type": "list", "items": ["a"]}


def test_run_handler_errors_are_strings(tmp_path):
    paths = _paths(tmp_path)
    d = _install(
        paths,
        paths.blocks,
        name="broken",
        md="---\nname: broken\ndescription: b\n---\nbody",
        handler="def render(state):\n    raise RuntimeError('boom')\n",
    )
    from agent.blocks import run_handler

    block_def = BlockDef.from_dir(d, "shared")
    out = run_handler(block_def, "render", state={})
    assert isinstance(out, str) and out.startswith("error:") and "boom" in out
    assert run_handler(block_def, "on_action", action="x", values={}, state={}).startswith(
        "error:"
    )  # missing hook


# -- server routing --------------------------------------------------------
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
    import urllib.request

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _get(url):
    import urllib.request

    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())


def test_blocks_endpoint_hydrates_open_instances(tmp_path):
    httpd, orch, base = _server(tmp_path)
    try:
        bid = write_block(
            orch.paths, {"bot": "atlas", "surface": "chat", "title": "t", "view": _FORM}
        )
        rows = _get(f"{base}/api/blocks?bot=atlas&status=open")
        assert [r["id"] for r in rows] == [bid]
        assert _get(f"{base}/api/blocks?bot=nobody") == []
    finally:
        httpd.shutdown()
        orch.down()


def test_block_action_discard_broadcasts_cleared(tmp_path):
    httpd, orch, base = _server(tmp_path)
    frames: list[dict] = []
    orig = orch.ws_hub.broadcast

    def capture(frame):
        frames.append(frame)
        orig(frame)

    orch.ws_hub.broadcast = capture  # type: ignore[method-assign]
    try:
        bid = write_block(
            orch.paths,
            {
                "bot": "atlas",
                "block_type": "skill_draft",
                "surface": "chat",
                "title": "Save as /x",
                "view": _FORM,
                "blocking": True,
            },
        )
        out = _post(
            f"{base}/api/block_actions",
            {"block_id": bid, "action": "discard", "values": {}},
        )
        assert out["ok"] is True
        assert out["status"] == "settled"
        cleared = [f for f in frames if f.get("type") == "block" and f.get("id") == bid]
        assert cleared, frames
        assert cleared[-1]["mutation"] == "cleared"
        assert cleared[-1]["status"] == "settled"
        row = read_block(orch.paths, bid)
        assert row is not None
        assert row["status"] == "settled"
        assert row.get("result", {}).get("action") == "discard"
    finally:
        orch.ws_hub.broadcast = orig  # type: ignore[method-assign]
        httpd.shutdown()
        orch.down()


def test_block_action_routes_to_parked_tool(tmp_path):
    httpd, orch, base = _server(tmp_path)
    try:
        bid = write_block(
            orch.paths,
            {"bot": "atlas", "surface": "chat", "title": "t", "view": _FORM, "blocking": True},
        )
        out = _post(
            f"{base}/api/block_actions",
            {"block_id": bid, "action": "submit", "values": {"env": "prod"}},
        )
        assert out["ok"] is True
        answer = json.loads(read_answer(orch.paths, bid))
        assert answer == {"action": "submit", "values": {"env": "prod"}}
    finally:
        httpd.shutdown()
        orch.down()


def test_block_action_unknown_block_is_404(tmp_path):
    import urllib.error

    import pytest

    httpd, orch, base = _server(tmp_path)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{base}/api/block_actions", {"block_id": "blk_missing", "action": "submit"})
        assert exc.value.code == 404
    finally:
        httpd.shutdown()
        orch.down()


def test_block_action_routes_to_handler(tmp_path):
    httpd, orch, base = _server(tmp_path)
    try:
        _install(
            orch.paths,
            orch.paths.blocks,
            name="notes",
            md="---\nname: notes\ndescription: n\n---\nbody",
            handler=_HANDLER,
        )
        bid = write_block(
            orch.paths,
            {
                "bot": "atlas",
                "block_type": "notes",
                "surface": "chat",
                "title": "Notes",
                "view": {"type": "list", "items": []},
                "state": {"items": []},
            },
        )
        out = _post(
            f"{base}/api/block_actions",
            {"block_id": bid, "action": "add", "values": {"note": "ship it"}},
        )
        assert out["ok"] is True and out["status"] == "open"
        inst = read_block(orch.paths, bid)
        assert inst["state"]["items"] == ["ship it"]
        assert inst["view"]["items"] == ["ship it"]
        out = _post(
            f"{base}/api/block_actions",
            {"block_id": bid, "action": "finish", "values": {"note": "done"}},
        )
        assert out["status"] == "settled"
    finally:
        httpd.shutdown()
        orch.down()


def test_block_action_handler_error_is_json_not_500(tmp_path):
    httpd, orch, base = _server(tmp_path)
    try:
        _install(
            orch.paths,
            orch.paths.blocks,
            name="broken",
            md="---\nname: broken\ndescription: b\n---\nbody",
            handler="def on_action(action, values, state):\n    raise ValueError('nope')\n",
        )
        bid = write_block(
            orch.paths,
            {
                "bot": "atlas",
                "block_type": "broken",
                "surface": "chat",
                "title": "t",
                "view": _FORM,
            },
        )
        out = _post(f"{base}/api/block_actions", {"block_id": bid, "action": "x"})
        assert out["error"].startswith("error:") and "nope" in out["error"]
    finally:
        httpd.shutdown()
        orch.down()


def test_default_notes_block_seeds_and_round_trips(tmp_path):
    """The seeded example block renders and its handler appends notes."""
    from agent.blocks import find_block_def, run_handler

    paths = _paths(tmp_path)
    d = find_block_def(paths, "atlas", "notes")
    assert d is not None and d.has_handler
    view = run_handler(d, "render", state={})
    assert validate_view(view) is None
    out = run_handler(d, "on_action", action="add", values={"note": "ship it"}, state={})
    assert out["state"]["items"] == ["ship it"]
    assert validate_view(out["view"]) is None


def test_block_catalog_endpoint(tmp_path):
    httpd, orch, base = _server(tmp_path)
    try:
        rows = _get(f"{base}/api/blocks/catalog?bot=atlas")
        assert any(r["name"] == "notes" and r["has_handler"] for r in rows)
        assert all(
            {"name", "description", "surfaces", "schema_version", "source"} <= set(r) for r in rows
        )
    finally:
        httpd.shutdown()
        orch.down()


def test_block_action_falls_back_to_bot_inbox(tmp_path):
    httpd, orch, base = _server(tmp_path)
    try:
        bid = write_block(
            orch.paths,
            {"bot": "atlas", "surface": "chat", "title": "Adhoc", "view": _FORM},
        )
        out = _post(
            f"{base}/api/block_actions",
            {"block_id": bid, "action": "refresh", "values": {}},
        )
        assert out["ok"] is True
        deadline = time.time() + 3
        texts = []
        while time.time() < deadline:
            texts = [
                json.loads(f.read_text(encoding="utf-8")).get("text", "")
                for f in orch.paths.inbox("atlas").glob("*.json")
            ]
            if texts:
                break
            time.sleep(0.05)
        assert any(t.startswith("[block action] Adhoc") and "refresh" in t for t in texts)
    finally:
        httpd.shutdown()
        orch.down()
