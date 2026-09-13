"""`harness` CLI — the self-hosted control plane entrypoint.

harness init                 create the shared directory layout
harness roster               list configured bots
harness up                   start all bots (--backend machines|process|container|vm)
harness machines             show the bot-machine pool (machines backend)
harness status               show bot pid/status
harness chat <bot> <text>    talk to a named bot, print its reply
harness chat <bot> <text> --stream   stream the reply as it is generated
harness send <from> <to> ... inject a bot-to-bot message
harness logs <bot|server> [-n N] [-f]   print a log (server: the harness's own); -f follows
harness reports [<id>] [--rm]  problem reports filed from the apps, with server context
harness down                 stop all bots
harness paths                print the resolved shared layout
harness settings             print every HARNESS_* setting and its default
harness export [--out F]     write this deployment's config (never secrets)
harness import <file>        add bots/skills/routines from a bundle (additive)
harness install              install host packages for computer use (ffmpeg, xdotool)
harness serve                run the HTTP+WS+SSE API (for the macOS app etc.)
harness link [--rotate]      re-print (or rotate) the pairing link code
harness channels             list user-facing channels
harness desktop              launch the Tkinter desktop app (local)
harness computer             bring up the preloaded desktop (openbox/tint2/browser)
harness skills [--bot NAME]  list the slash command/skill catalog
harness secret set <NAME>    store a secret (hidden prompt; answers a bot's request)
harness room list|create|chat   manage group chats
harness control <bot>        show human-in-the-loop control state
harness takeover <bot>       take manual control of a bot
harness return <bot>         return control to a bot
harness teach <bot> --step ... --save NAME   record + save a taught skill
harness audit [--json] [--fix]  blast-radius security audit (exit 1 on criticals)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from isolation import IsolationUnavailable

from .netguard import install_safe_redirects
from .orchestrator import Orchestrator
from .roster import RosterError
from .settings import SettingError, describe, validate_environment
from .statestore import SchemaTooNew


def _orch(args) -> Orchestrator:
    orch = Orchestrator.create(home=args.home, roster_path=args.roster, backend=args.backend)
    # Adopt the JSON store when the home already has one. It is the roster of
    # record — `serve` switches to it, bots added through the API persist
    # there, and spawned agents read it — so a CLI command reading roster.toml
    # instead was answering about a file the running harness had left behind.
    # `harness import` made that visible: it reported a bot added and `harness
    # roster` then listed only the seed file. Only adopted when the file exists;
    # this never creates one, so a home with no store behaves exactly as before.
    if (orch.paths.home / "roster.json").is_file():
        orch.use_json_store()
    return orch


def cmd_init(args) -> int:
    orch = _orch(args)
    orch.init()
    print(f"Initialized harness home at {orch.paths.home}")
    print(orch.paths.tree())
    return 0


def cmd_paths(args) -> int:
    orch = _orch(args)
    print(orch.paths.tree())
    return 0


def cmd_roster(args) -> int:
    orch = _orch(args)
    for bot in orch.bots():
        model = bot.model or "(provider default)"
        print(f"{bot.name:12} role={bot.role!r:24} provider={bot.provider:8} model={model}")
    return 0


def cmd_up(args) -> int:
    orch = _orch(args)
    handles = orch.up()
    for h in handles:
        print(f"started {h.bot:12} pid={h.pid} status={h.status.value} backend={h.backend}")
    if not handles:
        print("no bots in the roster. Add one with `harness serve` + the app, or edit the roster.")
        return 1
    print(f"\n{len(handles)} bot(s) up. Try: harness chat {handles[0].bot} 'hello'")
    return 0


def cmd_status(args) -> int:
    orch = _orch(args)
    for h in orch.status():
        print(f"{h.bot:12} status={h.status.value:8} pid={h.pid}")
    return 0


def cmd_chat(args) -> int:
    orch = _orch(args)
    if not args.stream:
        try:
            reply = orch.chat(args.bot, args.text, timeout=args.timeout)
        except RosterError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if reply is None:
            print(
                f"(no reply from {args.bot} within {args.timeout:.0f}s — is it up? `harness status`)"
            )
            return 1
        print(f"{reply.frm}: {reply.text}")
        return 0

    # streaming: render typing + deltas live
    try:
        _rid, reader = orch.chat_stream(args.bot, args.text)
    except RosterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    started = False
    got_final = False
    progress_seen: dict[str, dict[str, str]] = {}
    for ev in reader.events(timeout=args.timeout):
        if ev.type == "status" and ev.value:
            print(f"[{args.bot} is {ev.value}…]", flush=True)
        elif ev.type == "takeover":
            print(f"\n[⚠ {ev.bot} is stuck: {ev.reason}]", flush=True)
        elif ev.type == "secret_request":
            _answer_secret_request(orch, ev)
        elif ev.type == "choice":
            _answer_choice(orch, ev)
        elif ev.type == "card":
            _render_card(orch, ev, progress_seen)
        elif ev.type == "delta":
            if not started:
                print(f"{args.bot}: ", end="", flush=True)
                started = True
            print(ev.text or "", end="", flush=True)
        elif ev.type == "final":
            got_final = True
            if not started:
                print(f"{args.bot}: {ev.text}", end="")
            print(flush=True)
    if not got_final:
        print(f"\n(no reply from {args.bot} within {args.timeout:.0f}s — is it up?)")
        return 1
    return 0


def _answer_secret_request(orch, ev) -> None:
    """Terminal fallback for the in-chat secure input box."""
    from .secrets import set_secret

    name = ev.name or ""
    label = (ev.title or "").strip() or name
    print(f"\n[🔐 {label}: {ev.reason or ''}]", flush=True)
    if not sys.stdin.isatty():
        print(f"[non-interactive: run `harness secret set {name}` in another shell]", flush=True)
        return
    import getpass

    try:
        value = getpass.getpass(f"{name} (input hidden, empty to skip): ")
    except (EOFError, KeyboardInterrupt):
        value = ""
    if value.strip():
        set_secret(name, value, orch.paths)
        print("[stored — the bot can use it but never sees the value]", flush=True)


def _answer_choice(orch, ev) -> None:
    """Terminal fallback for the in-chat choice box."""
    from agent.streaming import write_answer

    options = ev.options or []
    print(f"\n[{ev.bot} asks] {ev.question}", flush=True)
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}", flush=True)
    if not sys.stdin.isatty():
        print("[non-interactive: no choice submitted]", flush=True)
        return
    try:
        raw = input("pick a number (empty to skip): ").strip()
    except (EOFError, KeyboardInterrupt):
        raw = ""
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        write_answer(orch.paths, ev.id or "", options[int(raw) - 1])


def _render_card(orch, ev, progress_seen: dict) -> None:
    """Terminal fallback for rich chat cards: one line per card."""
    card_type = ev.card_type or ""
    d = ev.payload or {}
    if card_type == "confirm":
        _answer_confirm(orch, ev)
        return
    if card_type == "progress":
        # Repeated ids are updates: print only the step transitions.
        last = progress_seen.get(ev.id or "", {})
        for step in d.get("steps") or []:
            label = str(step.get("label", ""))
            status = str(step.get("status", "pending"))
            if last.get(label) != status:
                mark = {"done": "✓", "active": "…", "error": "✗"}.get(status, "·")
                print(f"[progress] {mark} {label}", flush=True)
        progress_seen[ev.id or ""] = {
            str(s.get("label", "")): str(s.get("status", "pending")) for s in (d.get("steps") or [])
        }
        if d.get("state") in ("done", "error"):
            print(f"[progress] {d.get('title', '')}: {d.get('state')}", flush=True)
        return
    if card_type == "table":
        print(f"[card] table: {d.get('title') or ''} ({len(d.get('rows') or [])} rows)", flush=True)
        return
    if card_type == "chart":
        from agent.charts import chart_summary

        summary = chart_summary(d.get("chart") or {})
        print(f"[card] {d.get('title') or 'chart'}: {summary}", flush=True)
        return
    if card_type == "github_pull":
        line = f"PR {d.get('repo', '')}#{d.get('number', '?')} · {d.get('state', '')}"
    elif card_type == "github_issue":
        line = f"issue {d.get('repo', '')}#{d.get('number', '?')} · {d.get('state', '')}"
    elif card_type == "linear_issue":
        line = f"{d.get('identifier', '?')} · {d.get('status', '')}"
    elif card_type == "link":
        line = d.get("domain") or ""
    elif card_type == "file":
        line = f"file {d.get('name', '?')} ({d.get('size', 0)} bytes)"
    else:
        line = card_type
    parts = [line, d.get("title") or "", d.get("url") or ""]
    print(f"[card] {' · '.join(x for x in parts if x)}", flush=True)


def _answer_confirm(orch, ev) -> None:
    """Terminal fallback for the confirm card."""
    from agent.streaming import write_answer

    d = ev.payload or {}
    print(f"\n[{ev.bot} asks] {d.get('question', 'Proceed?')}", flush=True)
    if d.get("detail"):
        print(f"  {d['detail']}", flush=True)
    if not sys.stdin.isatty():
        print("[non-interactive: no confirmation submitted]", flush=True)
        return
    try:
        raw = input("confirm? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        raw = ""
    write_answer(orch.paths, ev.id or "", "confirm" if raw in ("y", "yes") else "cancel")


def cmd_secret(args) -> int:
    orch = _orch(args)
    if args.action == "set":
        import getpass

        from .secrets import set_secret

        try:
            value = getpass.getpass(f"{args.name} (input hidden): ")
        except (EOFError, KeyboardInterrupt):
            value = ""
        if not value.strip():
            print("error: empty value; nothing stored", file=sys.stderr)
            return 2
        set_secret(args.name, value, orch.paths)
        print(f"stored {args.name} (chmod 600; value never printed)")
        return 0
    print(f"error: unknown secret command {args.action!r}", file=sys.stderr)
    return 2


def cmd_install(args) -> int:
    from .hostdeps import install

    return install(desktop=args.desktop)


def cmd_serve(args) -> int:
    from .hostdeps import ensure
    from .server import serve

    if args.desktop:
        # Tenant / --desktop is the product computer (machine look),
        # not the old full-width host taskbar.
        os.environ.setdefault("HARNESS_DESKTOP_STYLE", "machine")
        os.environ.setdefault("HARNESS_BROWSER_NO_SANDBOX", "1")
    # Machine computers contain their own desktop tools. A containerised
    # controller must not install X11/ffmpeg packages on every startup.
    if args.backend != "machines" or args.desktop:
        ensure(desktop=args.desktop)
    return serve(
        home=args.home,
        roster_path=args.roster,
        backend=args.backend,
        host=args.host,
        port=args.port,
        start_bots=not args.no_up,
        desktop=args.desktop,
        public_url=getattr(args, "public_url", None),
    )


def cmd_computer(args) -> int:
    from .computer_env import bringup
    from .paths import HarnessPaths

    os.environ.setdefault("HARNESS_DESKTOP_STYLE", "machine")
    os.environ.setdefault("HARNESS_BROWSER_NO_SANDBOX", "1")
    paths = HarnessPaths.resolve(args.home)
    paths.ensure_layout([])
    report = bringup(paths, display=args.display, url=args.url, no_sandbox=args.no_sandbox)
    if report.get("error"):
        print(f"error: {report['error']}", file=sys.stderr)
        return 2
    print(f"desktop on {report['display']}")
    print(f"  taskbar: {', '.join(report['taskbar'])}")
    if report["started"]:
        print(f"  started: {', '.join(report['started'])}")
    if report["skipped"]:
        print(f"  skipped: {', '.join(report['skipped'])}")
    return 0


def cmd_link(args) -> int:
    from .linking import advertised_url, get_or_create_key, link_code
    from .paths import HarnessPaths

    paths = HarnessPaths.resolve(args.home)
    key = get_or_create_key(paths, rotate=args.rotate)
    url = advertised_url(args.host, args.port, getattr(args, "public_url", None), home=paths.home)
    print(f"Link code:   {link_code(url, key)}")
    print(f"URL:         {url}")
    print(f"Linking key: {key}")
    return 0


def cmd_channels(args) -> int:
    from channels.base import available_channels

    for name, status in available_channels().items():
        print(f"{name:10} {status}")
    return 0


def cmd_desktop(args) -> int:
    from channels.desktop import run_desktop

    try:
        return run_desktop(home=args.home, roster_path=args.roster, backend=args.backend)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def cmd_control(args) -> int:
    orch = _orch(args)
    state = orch.control.state(args.bot)
    print(f"{args.bot}: mode={state.mode} takeover_requested={state.takeover_requested}")
    if state.reason:
        print(f"  reason: {state.reason}")
    if state.holder:
        print(f"  held by: {state.holder}")
    if state.return_requested:
        why = f": {state.return_reason}" if state.return_reason else ""
        print(f"  {args.bot} is asking for the computer back{why}")
        print(f"  accept with `harness return {args.bot}`")
    if state.teach_steps:
        print(f"  teach steps: {len(state.teach_steps)}")
    return 0


def cmd_takeover(args) -> int:
    orch = _orch(args)
    orch.control.take_over(args.bot)
    print(f"you now control {args.bot} (bot paused). Run `harness return {args.bot}` when done.")
    return 0


def cmd_return(args) -> int:
    orch = _orch(args)
    orch.control.return_control(args.bot)
    print(f"control returned to {args.bot}")
    return 0


def cmd_teach(args) -> int:
    orch = _orch(args)
    ctrl = orch.control
    ctrl.start_teach(args.bot)
    for step in args.step or []:
        ctrl.record_step(args.bot, step)
    if args.save:
        _state, path = ctrl.save_teach(args.bot, args.save, args.description or "")
        print(f"taught {args.bot} skill '{args.save}' ({len(args.step or [])} steps) -> {path}")
    else:
        print(
            f"{args.bot} in teach mode with {len(args.step or [])} step(s); pass --save NAME to keep"
        )
    return 0


def cmd_skills(args) -> int:
    from agent.commands import catalog

    orch = _orch(args)
    bot = args.bot or (orch.roster.names()[0] if orch.roster.names() else "")
    if not bot:
        print("error: no bots in roster", file=sys.stderr)
        return 2
    for item in catalog(orch.paths, bot):
        print(f"/{item['name']:16} {item['kind']:8} {item['description']}")
    return 0


def cmd_room(args) -> int:
    orch = _orch(args)
    action = args.room_cmd
    if action == "list":
        rooms = orch.rooms()
        if not rooms:
            print("(no group chats)")
            return 0
        for room in rooms:
            print(f"{room.id:28} {room.title!r:24} members={','.join(room.members)}")
        return 0
    if action == "create":
        try:
            room = orch.create_room(args.title, args.member or [])
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"created {room.id} {room.title!r} members={','.join(room.members)}")
        return 0
    if action == "chat":
        from agent.streaming import multiplex

        try:
            turns = orch.dispatch_chat(args.text, room_id=args.id)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        readers = [(name, reader) for name, _rid, reader in turns]
        finals = 0
        for name, ev in multiplex(readers, timeout=args.timeout):
            if ev.type == "delta" and ev.text:
                print(f"{name}: {ev.text}", end="" if not ev.text.endswith("\n") else "\n")
            elif ev.type == "final":
                finals += 1
                print(flush=True)
        return 0 if finals else 1
    print(f"error: unknown room command {action!r}", file=sys.stderr)
    return 2


def cmd_send(args) -> int:
    orch = _orch(args)
    try:
        mid = orch.send(args.frm, args.to, args.text)
    except RosterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"sent {mid} : {args.frm} -> {args.to}")
    return 0


def cmd_logs(args) -> int:
    """Print a log: a bot's run/<bot>.log, or `server` for run/server/serve.log.

    Plain `harness logs <bot>` still prints the whole file. `-n N` prints the
    last N lines; `-f` keeps printing new lines (a rotated or truncated log
    is announced and read from its start) until Ctrl-C.
    """
    import threading

    from . import logstream

    orch = _orch(args)
    log = logstream.log_path_for(orch.paths, args.bot)
    if log is None:
        print(f"error: {args.bot!r} is not a log source (a bot name, or 'server')", file=sys.stderr)
        return 2
    follow = bool(getattr(args, "follow", False))
    lines = getattr(args, "lines", None)
    if not follow and lines is None:
        text = logstream.read_whole(log)
        if text is None:
            print(f"(no log for {args.bot} at {log})")
            return 1
        print(text, end="")
        return 0
    if not log.is_file() and not follow:
        print(f"(no log for {args.bot} at {log})")
        return 1
    page = logstream.tail(
        log,
        lines=logstream.DEFAULT_LINES if lines is None else lines,
        previous=logstream.previous_generation(log),
    )
    for line in page.lines:
        print(line)
    if not follow:
        return 0
    if not log.is_file():
        print(f"(waiting for {log})", file=sys.stderr)
    # `_stop` is a test seam: an Event a test sets to end the loop the way
    # Ctrl-C does for an operator.
    stop = getattr(args, "_stop", None) or threading.Event()
    try:
        for batch in logstream.follow(log, page.offset, stop, gen=page.gen):
            if not batch.exists:
                print(f"(log removed: {log})", file=sys.stderr, flush=True)
            elif batch.reset:
                print(f"(log restarted: {log})", file=sys.stderr, flush=True)
            for line in batch.lines:
                print(line, flush=True)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_reports(args) -> int:
    """List problem reports, print one in full, or remove one."""
    import json
    import time

    from . import reports

    orch = _orch(args)
    if args.id:
        if args.rm:
            if not reports.delete_report(orch.paths, args.id):
                print(f"(no report {args.id})", file=sys.stderr)
                return 1
            print(f"removed {args.id}")
            return 0
        record = reports.load_report(orch.paths, args.id)
        if record is None:
            print(f"(no report {args.id})", file=sys.stderr)
            return 1
        print(json.dumps(record, ensure_ascii=False, indent=1))
        return 0
    rows = reports.list_reports(orch.paths, limit=args.limit)
    if not rows:
        print(f"(no reports in {orch.paths.reports})")
        return 0
    for row in rows:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["ts"] or 0))
        who = f" bot={row['bot']}" if row["bot"] else ""
        app = f" app={row['app_version']}/{row['platform']}" if row["app_version"] else ""
        print(f"{row['id']}  {stamp}  {row['kind']}{who}{app}  {row['summary'][:80]}")
    return 0


def cmd_down(args) -> int:
    orch = _orch(args)
    orch.down()
    print("stopped all bots")
    return 0


def cmd_audit(args) -> int:
    import json

    from . import security_audit

    orch = _orch(args)
    try:
        bots = orch.roster.names()
    except RosterError:
        bots = []
    report = security_audit.run(orch.paths, bots, fix=args.fix)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(security_audit.render(report))
    return 1 if report.summary()[security_audit.SEV_CRITICAL] else 0


def cmd_machines(args) -> int:
    from isolation.machines import MachinePool

    orch = _orch(args)
    machines = MachinePool(orch.paths).machines()
    if not machines:
        print("(no machines yet — they are created as bots start under the machines backend)")
        return 0
    for m in machines:
        print(f"{m.name:22} state={m.state:5} bot={m.bot or '-':12} volume={m.volume}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="harness", description="Dotobot: self-hosted multi-bot agent harness"
    )
    p.add_argument("--roster", default="roster.toml", help="path to roster.toml")
    p.add_argument("--home", default=None, help="harness home (default $HARNESS_HOME or ./shared)")
    p.add_argument(
        "--backend",
        default="machines",
        help="isolation backend (machines|process|container|vm); machines is the "
        "default — each bot gets its own sandboxed computer (needs docker/podman "
        "and the agent-harness-machine image; use --backend process to opt out)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the shared directory layout").set_defaults(func=cmd_init)
    sub.add_parser("paths", help="print resolved shared layout").set_defaults(func=cmd_paths)
    sub.add_parser("settings", help="print every HARNESS_* setting and its default").set_defaults(
        func=cmd_settings
    )

    p_export = sub.add_parser("export", help="write this deployment's config (never secrets)")
    p_export.add_argument("--out", default="", help="file to write, or - for stdout (default)")
    p_export.set_defaults(func=cmd_export)

    p_import = sub.add_parser("import", help="add bots/skills/routines from a bundle")
    p_import.add_argument("file", help="bundle written by `harness export`")
    p_import.set_defaults(func=cmd_import)
    sub.add_parser("roster", help="list configured bots").set_defaults(func=cmd_roster)
    sub.add_parser("up", help="start all bots").set_defaults(func=cmd_up)
    sub.add_parser("status", help="show bot status").set_defaults(func=cmd_status)
    sub.add_parser("down", help="stop all bots").set_defaults(func=cmd_down)
    sub.add_parser("machines", help="show the bot-machine pool (machines backend)").set_defaults(
        func=cmd_machines
    )

    c = sub.add_parser("chat", help="talk to a named bot")
    c.add_argument("bot")
    c.add_argument("text")
    c.add_argument("--timeout", type=float, default=45.0)
    c.add_argument("--stream", action="store_true", help="stream the reply live")
    c.set_defaults(func=cmd_chat)

    ins = sub.add_parser(
        "install", help="install host packages for computer use (ffmpeg, xdotool, scrot, xclip)"
    )
    ins.add_argument(
        "--desktop", action="store_true", help="also install openbox/tint2/thunar/xterm/xvfb"
    )
    ins.set_defaults(func=cmd_install)

    sv = sub.add_parser("serve", help="run the API server (prints a linking key)")
    sv.add_argument("--host", default="0.0.0.0", help="bind host (default all interfaces)")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--public-url", help="HTTPS origin for link codes and OAuth callbacks")
    sv.add_argument("--up", action="store_true", help="(default) start bots before serving")
    sv.add_argument(
        "--no-up", dest="no_up", action="store_true", help="serve without starting bots"
    )
    sv.add_argument("--desktop", action="store_true", help="bring up the computer desktop first")
    sv.set_defaults(func=cmd_serve)

    cp = sub.add_parser("computer", help="bring up the desktop (Chrome / Thunar / xterm + Google)")
    cp.add_argument("--display", default=None, help="X display (default $DISPLAY)")
    cp.add_argument("--url", default="https://www.google.com")
    cp.add_argument("--no-sandbox", action="store_true", help="pass --no-sandbox to Chromium")
    cp.set_defaults(func=cmd_computer)

    lk = sub.add_parser("link", help="print the linking key/code for the app")
    lk.add_argument("--host", default="0.0.0.0")
    lk.add_argument("--port", type=int, default=8765)
    lk.add_argument("--public-url", help="HTTPS origin to include in the link code")
    lk.add_argument("--rotate", action="store_true", help="generate a new key")
    lk.set_defaults(func=cmd_link)

    sub.add_parser("channels", help="list user-facing channels").set_defaults(func=cmd_channels)
    sub.add_parser("desktop", help="launch the Tkinter desktop app").set_defaults(func=cmd_desktop)

    ct = sub.add_parser("control", help="show control state for a bot")
    ct.add_argument("bot")
    ct.set_defaults(func=cmd_control)

    to = sub.add_parser("takeover", help="take manual control of a bot")
    to.add_argument("bot")
    to.set_defaults(func=cmd_takeover)

    rt = sub.add_parser("return", help="return control to a bot")
    rt.add_argument("bot")
    rt.set_defaults(func=cmd_return)

    tc = sub.add_parser("teach", help="record and save a taught skill")
    tc.add_argument("bot")
    tc.add_argument("--step", action="append", help="a demonstrated step (repeatable)")
    tc.add_argument("--save", help="skill name to save the steps under")
    tc.add_argument("--description", default="")
    tc.set_defaults(func=cmd_teach)

    sec = sub.add_parser("secret", help="manage secrets (e.g. answer a bot's request)")
    sec.add_argument("action", choices=["set"], help="set: store a secret (prompted, hidden)")
    sec.add_argument("name", help="secret name, e.g. SMTP_PASSWORD")
    sec.set_defaults(func=cmd_secret)

    sk = sub.add_parser("skills", help="list / commands and skills for a bot")
    sk.add_argument("bot", nargs="?", default=None)
    sk.set_defaults(func=cmd_skills)

    rm = sub.add_parser("room", help="create and chat in a bot group")
    rm_sub = rm.add_subparsers(dest="room_cmd", required=True)
    rm_sub.add_parser("list", help="list group chats").set_defaults(func=cmd_room)
    rc = rm_sub.add_parser("create", help="create a group chat")
    rc.add_argument("--title", required=True)
    rc.add_argument("--member", action="append", help="bot name (repeat)")
    rc.set_defaults(func=cmd_room)
    rchat = rm_sub.add_parser("chat", help="send a message to a group")
    rchat.add_argument("id")
    rchat.add_argument("text")
    rchat.add_argument("--timeout", type=float, default=45.0)
    rchat.set_defaults(func=cmd_room)

    s = sub.add_parser("send", help="inject a bot-to-bot message")
    s.add_argument("frm", metavar="from")
    s.add_argument("to")
    s.add_argument("text")
    s.set_defaults(func=cmd_send)

    lg = sub.add_parser("logs", help="print a bot's log, or the server's own ('server')")
    lg.add_argument(
        "bot", metavar="source", help="a bot name, or 'server' for run/server/serve.log"
    )
    lg.add_argument("-n", "--lines", type=int, default=None, help="only the last N lines")
    lg.add_argument(
        "-f", "--follow", action="store_true", help="keep printing new lines until Ctrl-C"
    )
    lg.set_defaults(func=cmd_logs)

    rp = sub.add_parser("reports", help="problem reports filed from the apps (with context)")
    rp.add_argument("id", nargs="?", default=None, help="print this report in full")
    rp.add_argument("--rm", action="store_true", help="remove the named report")
    rp.add_argument("--limit", type=int, default=50, help="how many to list (newest first)")
    rp.set_defaults(func=cmd_reports)

    ad = sub.add_parser(
        "audit", help="blast-radius security audit of this home (exit 1 on criticals)"
    )
    ad.add_argument("--json", action="store_true", help="machine-readable output for CI")
    ad.add_argument(
        "--fix",
        action="store_true",
        help="apply the narrow fixes: chmod credentials, seed the suggested ask-policy",
    )
    ad.set_defaults(func=cmd_audit)

    return p


def cmd_settings(args) -> int:
    print(describe())
    return 0


def cmd_export(args) -> int:
    from . import bundle

    orch = _orch(args)
    text = bundle.dumps(bundle.export(orch.paths, orch.bots()))
    out = getattr(args, "out", "") or ""
    if out and out != "-":
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path} ({len(text)} bytes)")
        print("No credentials are in this file; a connector needs its secret supplied again.")
    else:
        print(text, end="")
    return 0


def cmd_import(args) -> int:
    from . import bundle

    orch = _orch(args)
    path = Path(args.file)
    try:
        data = bundle.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except bundle.BundleError as exc:
        print(f"error: {path}: {exc}", file=sys.stderr)
        return 2
    print(bundle.apply(orch.paths, data, orch).summary())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        # Before anything reads a setting. A value that is set and unusable
        # stops the harness here, naming itself — rather than being coerced
        # back to a default that the operator then spends an afternoon
        # failing to observe.
        validate_environment()
    except SettingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Every urlopen in this process (serve's connector/OAuth calls included)
    # gets the opener that drops Authorization/Cookie on a cross-host 3xx.
    install_safe_redirects()
    try:
        return args.func(args)
    except SchemaTooNew as exc:
        # Distinct exit code: a systemd unit sets
        # RestartPreventExitStatus for it instead of restart-looping into
        # the same refusal.
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except RosterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except IsolationUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
