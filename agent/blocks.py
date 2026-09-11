"""Blocks: declarative mini-app surfaces the agent (or a user) can show.

A block is a small UI the bot renders inside the app — a form, a checklist,
a dashboard — described as a JSON tree of a fixed widget vocabulary and drawn
natively by the client. Blocks target one of three surfaces:

    chat    an inline card in the conversation (like the choice box)
    flyout  the right-hand inspector panel (like the screen card)
    pane    a full-width takeover of the content area (like the computer view)

Two halves live here:

* Instances — one shown block, durable JSON under `blocks-state/<id>.json`.
  Unlike `prompts/` (deleted on answer), settled instances are kept so cards
  survive an app reload; old settled ones are pruned. The user's submit comes
  back over the answers bus (`write_answer`) when the tool is waiting, or as
  an inbox message / handler call when it is not.

* Definitions — installed blocks, one directory per block (mirroring skills):

      shared/blocks/<name>/BLOCK.md      frontmatter: name, description,
                                         surfaces, schema_version; body is
                                         guidance for the bot
      .../view.json                      optional default view tree
      .../handler.py                     optional hooks:
                                             render(state) -> view
                                             on_action(action, values, state)
                                                 -> {state, view?, settle?, message?}
      memory/<bot>/blocks/<name>/        private, same layout

  Handlers run in the server process — the same trust level as connectors and
  skills. Never pass secrets through block state.
"""

from __future__ import annotations

import importlib.util
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.paths import HarnessPaths

from .charts import validate_chart

SCHEMA_VERSION = 1
RENDERER = "native-v1"
SURFACES = ("chat", "flyout", "pane")

#: instances kept after settling (newest first); older ones are pruned
_SETTLED_KEEP = 100

_MAX_NODES = 200
_MAX_DEPTH = 8

#: node type -> required props (validated shallowly; clients ignore extras)
NODE_TYPES: dict[str, tuple[str, ...]] = {
    "column": (),
    "row": (),
    "heading": ("text",),
    "text": ("text",),
    "image": ("url",),
    "divider": (),
    "progress": ("value",),
    "key_value": ("items",),
    "list": ("items",),
    "table": ("columns", "rows"),
    "chart": ("kind", "series"),
    "button": ("label", "action"),
    "text_input": ("name",),
    "select": ("name", "options"),
}

_ACTION_KINDS = ("submit", "action", "open_url")


def validate_view(node: Any) -> str | None:
    """Return an error string for a bad view tree, or None when it is valid.

    Errors are worded for the model so it can correct the tree and retry.
    """
    names: set[str] = set()
    count = 0

    def walk(n: Any, depth: int) -> str | None:
        nonlocal count
        count += 1
        if count > _MAX_NODES:
            return f"error: view has more than {_MAX_NODES} nodes"
        if depth > _MAX_DEPTH:
            return f"error: view nests deeper than {_MAX_DEPTH} levels"
        if not isinstance(n, dict):
            return "error: every view node must be an object with a 'type'"
        kind = n.get("type")
        if kind not in NODE_TYPES:
            known = ", ".join(sorted(NODE_TYPES))
            return f"error: unknown node type {kind!r}; use one of: {known}"
        for prop in NODE_TYPES[kind]:
            if prop not in n:
                return f"error: {kind} node needs {prop!r}"
        if kind == "chart":
            err = validate_chart(n)
            if err:
                return err
        if kind in ("text_input", "select"):
            name = str(n.get("name", ""))
            if not name:
                return f"error: {kind} needs a non-empty 'name'"
            if name in names:
                return f"error: duplicate input name {name!r}; names must be unique"
            names.add(name)
        if kind == "button":
            action = n.get("action")
            if not isinstance(action, dict) or action.get("kind") not in _ACTION_KINDS:
                kinds = " | ".join(_ACTION_KINDS)
                return f"error: button action must be an object with kind: {kinds}"
            if action.get("kind") == "action" and not str(action.get("id", "")).strip():
                return "error: action buttons need an 'id'"
            if action.get("kind") == "open_url" and not str(action.get("url", "")).strip():
                return "error: open_url buttons need a 'url'"
        for child in n.get("children") or []:
            err = walk(child, depth + 1)
            if err:
                return err
        return None

    return walk(node, 1)


# -- instances -------------------------------------------------------------

_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")


def new_block_id() -> str:
    return "blk_" + uuid.uuid4().hex[:8]


def _instance_file(paths: HarnessPaths, block_id: str) -> Path | None:
    safe = _SAFE_ID.sub("", block_id or "")
    if not safe:
        return None
    return paths.blocks_state / f"{safe}.json"


def write_block(paths: HarnessPaths, inst: dict[str, Any]) -> str | None:
    """Persist a block instance (idempotent by id); prune old settled ones."""
    bid = str(inst.get("id") or "") or new_block_id()
    path = _instance_file(paths, bid)
    if path is None:
        return None
    inst = {**inst, "id": bid}
    inst.setdefault("schema_version", SCHEMA_VERSION)
    inst.setdefault("renderer", RENDERER)
    inst.setdefault("status", "open")
    inst.setdefault("ts", time.time())
    inst["updated"] = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(inst, ensure_ascii=False), encoding="utf-8")
    _prune_settled(paths)
    return bid


def read_block(paths: HarnessPaths, block_id: str) -> dict[str, Any] | None:
    path = _instance_file(paths, block_id)
    if path is None or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def list_blocks(
    paths: HarnessPaths, bot: str | None = None, status: str | None = None
) -> list[dict[str, Any]]:
    if not paths.blocks_state.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in paths.blocks_state.glob("*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if bot and str(row.get("bot") or "") != bot:
            continue
        if status and str(row.get("status") or "") != status:
            continue
        out.append(row)
    out.sort(key=lambda r: r.get("ts", 0.0))
    return out


def settle_block(
    paths: HarnessPaths, block_id: str, result: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    inst = read_block(paths, block_id)
    if inst is None:
        return None
    inst["status"] = "settled"
    if result is not None:
        inst["result"] = result
    write_block(paths, inst)
    return inst


def _prune_settled(paths: HarnessPaths) -> None:
    settled = [r for r in list_blocks(paths, status="settled")]
    for row in settled[: max(0, len(settled) - _SETTLED_KEEP)]:
        path = _instance_file(paths, str(row.get("id") or ""))
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass


# -- definitions (installed block directories) -----------------------------

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


@dataclass
class BlockDef:
    name: str
    description: str
    surfaces: list[str]
    schema_version: int
    body: str
    source: str  # "shared" | "private"
    path: Path  # the block directory
    view_template: dict[str, Any] | None = None
    has_handler: bool = False
    _extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dir(cls, block_dir: Path, source: str) -> BlockDef:
        raw = (block_dir / "BLOCK.md").read_text(encoding="utf-8")
        meta: dict[str, str] = {}
        body = raw
        m = _FRONTMATTER.match(raw)
        if m:
            for line in m.group(1).splitlines():
                if ":" in line:
                    key, _, val = line.partition(":")
                    meta[key.strip().lower()] = val.strip()
            body = m.group(2).strip()
        surfaces = [
            s.strip() for s in meta.get("surfaces", "chat").split(",") if s.strip() in SURFACES
        ] or ["chat"]
        template: dict[str, Any] | None = None
        view_path = block_dir / "view.json"
        if view_path.is_file():
            try:
                template = json.loads(view_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                template = None
        try:
            version = int(meta.get("schema_version", str(SCHEMA_VERSION)))
        except ValueError:
            version = SCHEMA_VERSION
        return cls(
            name=meta.get("name", block_dir.name),
            description=meta.get("description", ""),
            surfaces=surfaces,
            schema_version=version,
            body=body,
            source=source,
            path=block_dir,
            view_template=template,
            has_handler=(block_dir / "handler.py").is_file(),
        )


def _load_from(root: Path, source: str) -> list[BlockDef]:
    defs: list[BlockDef] = []
    if not root.is_dir():
        return defs
    for block_md in sorted(root.glob("*/BLOCK.md")):
        try:
            d = BlockDef.from_dir(block_md.parent, source)
        except OSError:
            continue
        if d.schema_version > SCHEMA_VERSION:
            continue  # authored for a newer harness; skip rather than misrender
        defs.append(d)
    return defs


def load_block_defs(paths: HarnessPaths, bot: str) -> list[BlockDef]:
    """Shared blocks first, then this bot's private blocks."""
    private_root = paths.bot_memory(bot) / "blocks"
    return _load_from(paths.blocks, "shared") + _load_from(private_root, "private")


def find_block_def(paths: HarnessPaths, bot: str, name: str) -> BlockDef | None:
    for d in load_block_defs(paths, bot):
        if d.name == name:
            return d
    return None


def blocks_prompt(paths: HarnessPaths, bot: str) -> str:
    """Render a compact installed-blocks index for the system prompt."""
    defs = load_block_defs(paths, bot)
    if not defs:
        return ""
    lines = [
        "Installed blocks (pass block_type to show_block to use one). "
        "Only show a block when the user asked for that UI this turn — "
        "do not re-open a scratch pad (notes, checklists) on unrelated chats:"
    ]
    for d in defs:
        surfaces = "/".join(d.surfaces)
        lines.append(f"- {d.name} ({d.source}, {surfaces}): {d.description}")
    return "\n".join(lines)


def catalog(paths: HarnessPaths, bot: str) -> list[dict[str, Any]]:
    """Installed-block metadata for clients (a future Manage gallery)."""
    return [
        {
            "name": d.name,
            "description": d.description,
            "surfaces": d.surfaces,
            "schema_version": d.schema_version,
            "source": d.source,
            "has_handler": d.has_handler,
        }
        for d in load_block_defs(paths, bot)
    ]


_DEFAULT_NOTES_MD = """\
---
name: notes
description: A tiny shared note pad rendered as a block (example)
surfaces: chat, flyout
schema_version: 1
---
Show this when the user wants a quick scratch list they can add to from the
chat. Items live in the block's state via handler.py — no bot turn needed.
"""

_DEFAULT_NOTES_HANDLER = '''\
"""Example block handler. Runs in the harness server process when a user
presses a button on a `notes` block (see agent/blocks.py)."""


def _view(items):
    children = [
        {"type": "heading", "text": "Notes", "level": 3},
        {"type": "list", "items": items} if items
        else {"type": "text", "text": "Nothing yet.", "muted": True},
        {"type": "row", "spacing": 8, "children": [
            {"type": "text_input", "name": "note", "placeholder": "Add a note"},
            {"type": "button", "label": "Add", "style": "primary",
             "action": {"kind": "action", "id": "add"}},
        ]},
    ]
    return {"type": "column", "children": children}


def render(state):
    return _view(state.get("items", []))


def on_action(action, values, state):
    items = list(state.get("items", []))
    note = (values.get("note") or "").strip()
    if action == "add" and note:
        items.append(note)
    return {"state": {"items": items}, "view": _view(items), "settle": False}
'''


def ensure_default_blocks(paths: HarnessPaths) -> None:
    """Seed one example block so `show_block` has something installed to use."""
    paths.blocks.mkdir(parents=True, exist_ok=True)
    if any(paths.blocks.glob("*/BLOCK.md")):
        return
    dest = paths.blocks / "notes"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "BLOCK.md").write_text(_DEFAULT_NOTES_MD, encoding="utf-8")
    (dest / "handler.py").write_text(_DEFAULT_NOTES_HANDLER, encoding="utf-8")


def run_handler(block_def: BlockDef, hook: str, /, **kwargs: Any) -> Any:
    """Call `hook` in the block's handler.py; exceptions become error strings."""
    handler_path = block_def.path / "handler.py"
    if not handler_path.is_file():
        return f"error: block {block_def.name!r} has no handler.py"
    try:
        spec = importlib.util.spec_from_file_location(
            f"harness_block_{_SAFE_ID.sub('_', block_def.name)}", handler_path
        )
        if spec is None or spec.loader is None:
            return f"error: cannot load handler for block {block_def.name!r}"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fn = getattr(module, hook, None)
        if fn is None:
            return f"error: block {block_def.name!r} handler has no {hook}()"
        return fn(**kwargs)
    except Exception as exc:  # noqa: BLE001 - a block bug must not kill the server
        return f"error: block {block_def.name!r} {hook} failed: {exc}"
