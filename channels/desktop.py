"""Desktop app channel (Tkinter) — the initial first-party interface.

A local desktop window (no Telegram/Slack/Discord needed) that:

* streams replies token-by-token with a "typing…" indicator,
* lets the user **Take over** at any time and **Return control** when done,
* surfaces a banner when a bot is **stuck and asks the human to take over**,
* supports **Teach a task**: demonstrate steps, save them as a reusable skill.

The window is a thin view over ``DesktopViewModel`` (all logic lives there and
is unit-tested). Tkinter is stdlib, so this adds no pip dependency; it needs the
system ``python3-tk`` package to run a GUI.
"""

from __future__ import annotations

import queue
import threading

from harness.orchestrator import Orchestrator

from .viewmodel import DesktopViewModel


def _require_tk():
    try:
        import tkinter as tk  # noqa: F401
        from tkinter import messagebox, scrolledtext, simpledialog  # noqa: F401

        return tk, scrolledtext, simpledialog, messagebox
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "Tkinter is not available. Install the system package 'python3-tk' "
            "to run the desktop app (e.g. `sudo apt-get install -y python3-tk`)."
        ) from exc


class DesktopApp:  # pragma: no cover - GUI glue, exercised via manual/computer-use test
    def __init__(self, orch: Orchestrator, bot: str | None = None) -> None:
        tk, scrolledtext, simpledialog, messagebox = _require_tk()
        self.tk = tk
        self.simpledialog = simpledialog
        self.messagebox = messagebox
        self.vm = DesktopViewModel(orch, bot)
        self.events: queue.Queue = queue.Queue()
        self._assistant_open = False

        self.root = tk.Tk()
        self.root.title("Dotobot — desktop")
        self.root.geometry("760x640")

        # Header: bot / group picker + mode status
        header = tk.Frame(self.root, padx=10, pady=8)
        header.pack(fill="x")
        tk.Label(header, text="Chat:").pack(side="left")
        self.bot_var = tk.StringVar(value=self.vm.bot)
        names = self.vm.bots() or [""]
        tk.OptionMenu(header, self.bot_var, *names, command=self._on_bot).pack(side="left")
        tk.Button(header, text="New group", command=self._new_group).pack(side="left", padx=6)
        self.status_var = tk.StringVar(value="mode: bot")
        tk.Label(header, textvariable=self.status_var, fg="#555").pack(side="right")

        # Takeover banner (shown when the bot asks for help)
        self.banner = tk.Frame(self.root, bg="#ffe8b3", padx=10, pady=6)
        self.banner_var = tk.StringVar(value="")
        tk.Label(self.banner, textvariable=self.banner_var, bg="#ffe8b3", fg="#7a4f01").pack(
            side="left"
        )
        tk.Button(self.banner, text="Take over", command=self._take_over).pack(side="right")

        # Transcript
        self.transcript = scrolledtext.ScrolledText(self.root, wrap="word", state="disabled")
        self.transcript.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        from harness.colors import bot_color

        for name in self.vm.bots():
            self.transcript.tag_configure(f"mention-{name}", foreground=bot_color(name))

        self.typing_var = tk.StringVar(value="")
        tk.Label(self.root, textvariable=self.typing_var, fg="#888", anchor="w").pack(
            fill="x", padx=12
        )

        # Completer expands *up* from the composer (@ mentions, / skills)
        self._pick_kind = None
        self._pick_items: list[tuple[str, str]] = []
        self.completer = tk.Listbox(self.root, height=6, activestyle="dotbox")
        self.completer.bind("<Double-Button-1>", lambda _e: self._accept_pick())

        # Input + controls
        entry_row = tk.Frame(self.root, padx=10, pady=8)
        entry_row.pack(fill="x")
        self.entry = tk.Entry(entry_row)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", self._on_return)
        self.entry.bind("<KeyRelease>", self._on_key)
        self.entry.bind("<Up>", self._pick_move)
        self.entry.bind("<Down>", self._pick_move)
        self.entry.bind("<Escape>", lambda _e: self._hide_completer())
        tk.Button(entry_row, text="Send", command=self._send).pack(side="left", padx=(6, 0))

        controls = tk.Frame(self.root, padx=10, pady=6)
        controls.pack(fill="x", pady=(0, 10))
        tk.Button(controls, text="Take over", command=self._take_over).pack(side="left")
        tk.Button(controls, text="Return control", command=self._return_control).pack(
            side="left", padx=6
        )
        tk.Button(controls, text="Teach a task", command=self._teach).pack(side="left")

        self._refresh_control()
        self.root.after(120, self._pump)
        self.root.after(600, self._poll_control)

    # -- transcript helpers ----------------------------------------------
    def _append(self, text: str) -> None:
        self.transcript.configure(state="normal")
        start = self.transcript.index("end-1c")
        self.transcript.insert("end", text)
        import re

        for match in re.finditer(r"@([A-Za-z0-9_-]+)", text):
            name = match.group(1)
            canon = next((n for n in self.vm.bots() if n.lower() == name.lower()), None)
            if not canon:
                continue
            a = f"{start}+{match.start()}c"
            b = f"{start}+{match.end()}c"
            self.transcript.tag_add(f"mention-{canon}", a, b)
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    # -- @ / completer (opens upward) -------------------------------------
    def _on_key(self, _event=None) -> None:
        text = self.entry.get()
        at = self._trigger(text, "@")
        slash = self._trigger(text, "/")
        if at is not None:
            items = [(n, n) for n in self.vm.mention_candidates(at)]
            self._show_completer("mention", items)
        elif slash is not None:
            items = [
                (f"/{i['name']}", i.get("description") or i["name"])
                for i in self.vm.slash_candidates(slash)
            ]
            self._show_completer("slash", items)
        else:
            self._hide_completer()

    def _trigger(self, text: str, token: str) -> str | None:
        """Return the query after the last `token` if that token starts a word at the end."""
        idx = text.rfind(token)
        if idx < 0:
            return None
        if idx > 0 and not text[idx - 1].isspace():
            return None
        query = text[idx + 1 :]
        if any(ch.isspace() for ch in query):
            return None
        return query

    def _show_completer(self, kind: str, items: list[tuple[str, str]]) -> None:
        self._pick_kind = kind
        self._pick_items = items
        self.completer.delete(0, "end")
        for value, label in items:
            self.completer.insert("end", f"{value}  {label}" if label != value else value)
        if items:
            self.completer.selection_clear(0, "end")
            self.completer.selection_set(0)
            self.completer.activate(0)
            # Pack just above the composer (the last packed widget before controls).
            if not self.completer.winfo_ismapped():
                self.completer.pack(fill="x", padx=10, before=self.entry.master)
        else:
            self._hide_completer()

    def _hide_completer(self, _event=None):
        self._pick_kind = None
        self._pick_items = []
        self.completer.pack_forget()
        return "break"

    def _pick_move(self, event):
        if not self._pick_items:
            return None
        size = len(self._pick_items)
        cur = self.completer.curselection()
        idx = int(cur[0]) if cur else 0
        idx = (idx - 1) % size if event.keysym == "Up" else (idx + 1) % size
        self.completer.selection_clear(0, "end")
        self.completer.selection_set(idx)
        self.completer.activate(idx)
        self.completer.see(idx)
        return "break"

    def _on_return(self, _event=None):
        if self._pick_items:
            self._accept_pick()
            return "break"
        self._send()
        return "break"

    def _accept_pick(self) -> None:
        if not self._pick_items:
            return
        cur = self.completer.curselection()
        idx = int(cur[0]) if cur else 0
        value, _label = self._pick_items[idx]
        text = self.entry.get()
        token = "@" if self._pick_kind == "mention" else "/"
        cut = text.rfind(token)
        insert = value if value.startswith(token) else token + value
        self.entry.delete(0, "end")
        self.entry.insert(0, text[:cut] + insert + " ")
        self._hide_completer()

    # -- actions ----------------------------------------------------------
    def _on_bot(self, name: str) -> None:
        if name.startswith("group:"):
            self.vm.set_room(name.split(":", 1)[1])
        else:
            self.vm.set_bot(name)
        self._refresh_control()

    def _new_group(self) -> None:
        title = self.simpledialog.askstring("New group", "Group title:")
        if not title:
            return
        raw = self.simpledialog.askstring(
            "New group", f"Members (comma-separated). Bots: {', '.join(self.vm.bots())}"
        )
        if not raw:
            return
        members = [m.strip() for m in raw.split(",") if m.strip()]
        try:
            room = self.vm.create_room(title, members)
        except Exception as exc:
            self.messagebox.showerror("Group", str(exc))
            return
        self.vm.set_room(room.id)
        self.bot_var.set(f"group:{room.id}")
        self._append(f"[group {room.title} with {', '.join(room.members)}]\n")

    def _send(self) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self._append(f"\nyou: {text}\n")
        threading.Thread(target=self._worker, args=(text,), daemon=True).start()

    def _worker(self, text: str) -> None:
        try:
            for ev in self.vm.stream(text):
                self.events.put(ev)
        except Exception as exc:  # surface errors in the UI
            self.events.put(_ErrorEvent(str(exc)))

    def _pump(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                self._handle_event(ev)
        except queue.Empty:
            pass
        self.root.after(80, self._pump)

    def _handle_event(self, ev) -> None:
        if isinstance(ev, _ErrorEvent):
            self.typing_var.set("")
            self._append(f"[error] {ev.message}\n")
            return
        if ev.type == "status":
            who = ev.bot or self.vm.bot
            self.typing_var.set(f"{who} is {ev.value}…" if ev.value else "")
        elif ev.type == "delta":
            if not self._assistant_open:
                self._append(f"{ev.bot or self.vm.bot}: ")
                self._assistant_open = True
            self._append(ev.text or "")
        elif ev.type == "takeover":
            self._show_banner(ev.reason or "The bot needs help.")
        elif ev.type == "card":
            self._append(f"{_card_line(ev)}\n")
        elif ev.type == "final":
            self.typing_var.set("")
            self._append("\n")
            self._assistant_open = False
            self._refresh_control()

    def _show_banner(self, reason: str) -> None:
        self.banner_var.set(f"⚠ {self.vm.bot} is stuck: {reason}")
        self.banner.pack(fill="x", after=self.root.winfo_children()[0])

    def _hide_banner(self) -> None:
        self.banner.pack_forget()

    def _take_over(self) -> None:
        self.vm.take_over()
        self._hide_banner()
        self._append(f"[you took control of {self.vm.bot}]\n")
        self._refresh_control()

    def _return_control(self) -> None:
        self.vm.return_control()
        self._append(f"[you returned control to {self.vm.bot}]\n")
        self._refresh_control()

    def _teach(self) -> None:
        self.vm.start_teach()
        self._refresh_control()
        self._append(f"[teaching {self.vm.bot} — add steps]\n")
        while True:
            step = self.simpledialog.askstring("Teach a task", "Next step (blank to finish):")
            if not step:
                break
            self.vm.record_step(step)
            self._append(f"  · {step}\n")
        name = self.simpledialog.askstring("Save task", "Name this skill:")
        if name:
            path = self.vm.save_teach(name)
            self._append(f"[saved skill '{name}' -> {path}]\n")
            self.messagebox.showinfo("Skill saved", f"Saved '{name}'.")
        else:
            self.vm.cancel_teach()
            self._append("[teach cancelled]\n")
        self._refresh_control()

    # -- control status ---------------------------------------------------
    def _refresh_control(self) -> None:
        state = self.vm.control_state()
        self.status_var.set(f"mode: {state.mode}")

    def _poll_control(self) -> None:
        state = self.vm.control_state()
        if state.takeover_requested and state.mode == "bot":
            self._show_banner(state.reason or "The bot needs help.")
        self.root.after(600, self._poll_control)

    def run(self) -> int:
        self.root.mainloop()
        return 0


class _ErrorEvent:
    def __init__(self, message: str) -> None:
        self.message = message


def _card_line(ev) -> str:
    """One text line per rich card; the Tk channel has no card widgets.

    A confirm card can't be answered here — point at the Mac app / CLI (the
    24h tool timeout leaves plenty of room to answer elsewhere).
    """
    card_type = ev.card_type or ""
    d = ev.payload or {}
    if card_type == "confirm":
        return (
            f"[{ev.bot} asks] {d.get('question', 'Proceed?')} "
            "(answer in the Mac app or `harness chat --stream`)"
        )
    if card_type == "routine":
        return f"[routine] {d.get('title') or 'Routine'}"
    if card_type == "progress":
        steps = d.get("steps") or []
        done = sum(1 for s in steps if s.get("status") == "done")
        return f"[progress] {d.get('title', '')} ({done}/{len(steps)} steps)"
    if card_type == "table":
        return f"[card] table: {d.get('title') or ''} ({len(d.get('rows') or [])} rows)"
    if card_type == "chart":
        from agent.charts import chart_summary

        return f"[card] {d.get('title') or 'chart'}: {chart_summary(d.get('chart') or {})}"
    if card_type == "github_pull":
        head = f"PR {d.get('repo', '')}#{d.get('number', '?')} · {d.get('state', '')}"
    elif card_type == "github_issue":
        head = f"issue {d.get('repo', '')}#{d.get('number', '?')} · {d.get('state', '')}"
    elif card_type == "linear_issue":
        head = f"{d.get('identifier', '?')} · {d.get('status', '')}"
    elif card_type == "link":
        head = d.get("domain") or ""
    elif card_type == "file":
        head = f"file {d.get('name', '?')}"
    else:
        head = card_type
    parts = [head, d.get("title") or "", d.get("url") or ""]
    return f"[card] {' · '.join(x for x in parts if x)}"


def run_desktop(
    *, home: str | None = None, roster_path: str = "roster.toml", backend: str = "process"
) -> int:  # pragma: no cover - GUI entrypoint
    orch = Orchestrator.create(home=home, roster_path=roster_path, backend=backend)
    orch.init()
    return DesktopApp(orch).run()


def main() -> int:  # pragma: no cover - GUI entrypoint
    import argparse

    p = argparse.ArgumentParser(prog="python -m channels.desktop")
    p.add_argument("--roster", default="roster.toml")
    p.add_argument("--home", default=None)
    p.add_argument("--backend", default="process")
    args = p.parse_args()
    return run_desktop(home=args.home, roster_path=args.roster, backend=args.backend)


if __name__ == "__main__":
    raise SystemExit(main())
