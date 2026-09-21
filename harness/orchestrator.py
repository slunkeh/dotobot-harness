"""Orchestrator: roster, spawn/stop, message routing.

Self-hosted control plane. Owns the roster, the shared directory contract, and
an isolation backend. Talks to bots only through the file-based message bus, so
the same API works whether bots are processes (now) or containers/VMs (later).
"""

from __future__ import annotations

import re
import shutil
import sys
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from agent import messaging, obligations
from agent.commands import parse_slash
from agent.history import thread_root_id
from agent.mentions import has_everyone, leading_mention, resolve_mentions
from agent.streaming import StreamReader, StreamWriter
from isolation import BotHandle, Status, get_backend

from .control import Control
from .fsutil import write_atomic
from .paths import HarnessPaths
from .prefs import resolve_llm
from .rooms import Room, create_room, get_room, list_rooms
from .rooms import append_message as append_room_message
from .roster import (
    Bot,
    Roster,
    RosterError,
    bot_slug,
    caveman_flag,
    load_roster,
    save_roster,
    valid_bot_name,
)
from .routines import RoutineError, add_routine, list_routines

MAX_BOTS_AND_GROUPS = 25


@dataclass
class Orchestrator:
    paths: HarnessPaths
    roster: Roster
    roster_path: Path
    backend_name: str = "process"
    #: One long-lived Memory per bot (see memory_for): its record cache only
    #: re-parses session logs when their stamp changes, which per-request
    #: construction never benefited from.
    _memories: dict = field(default_factory=dict, repr=False)
    _memory_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    #: Cached isolation backend (see `backend`): constructing one per access
    #: rebuilt it — MachinePool included — once per bot per status() pass.
    _backend_cache: object = field(default=None, repr=False)
    _backend_cache_key: str = field(default="", repr=False)

    on_restart_changed: Callable[[], None] | None = field(default=None, repr=False)

    _starting: dict[str, object] = field(default_factory=dict, repr=False)
    _restarts: dict[str, tuple[Status, Future]] = field(default_factory=dict, repr=False)
    _restart_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def create(
        cls,
        *,
        home: str | Path | None = None,
        roster_path: str | Path = "roster.toml",
        backend: str = "process",
    ) -> Orchestrator:
        paths = HarnessPaths.resolve(home)
        rp = Path(roster_path)
        roster = load_roster(rp)
        return cls(paths=paths, roster=roster, roster_path=rp, backend_name=backend)

    @property
    def backend(self):
        # Keyed on backend_name so tests (and future switches) that reassign
        # it still get the right backend.
        if self._backend_cache is None or self._backend_cache_key != self.backend_name:
            self._backend_cache = get_backend(self.backend_name, self.paths)
            self._backend_cache_key = self.backend_name
        return self._backend_cache

    def memory_for(self, bot: str):
        """Shared per-bot Memory for server request threads. The stamp check
        in Memory keeps it fresh even though the bot agent writes the session
        files from another process; sharing one instance is what lets the
        parsed-record cache actually hit between requests. A rare concurrent
        cold parse is benign — the cache assignment is atomic."""
        from agent.memory import Memory

        with self._memory_lock:
            mem = self._memories.get(bot)
            if mem is None:
                mem = Memory(paths=self.paths, bot=bot, embedder=self._embedder_for(bot))
                self._memories[bot] = mem
            return mem

    def _embedder_for(self, name: str):
        """The bot's semantic-recall route for server-side writes.

        Facts remembered over the API and turns logged by serve must embed
        like the agent's own writes, or those rows never enter cosine
        ranking. Unknown bots and unstartable routes resolve to None —
        keyword-only, never an error.
        """
        from agent.embeddings import resolve_embedder

        try:
            bot = self.roster.get(name)
        except RosterError:
            return None
        return resolve_embedder(bot, self.paths)

    @property
    def control(self) -> Control:
        return Control(self.paths)

    # -- lifecycle --------------------------------------------------------
    def init(self) -> None:
        self.paths.ensure_layout(self.roster.names())
        # warn about a network home and fold any legacy session
        # JSONL into state.sqlite (idempotent; a no-op home costs one stat).
        # A schema newer than this code raises SchemaTooNew, which the CLI
        # maps to its distinct exit code.
        from .statestore import boot_state

        boot_state(self.paths)

    def use_json_store(self) -> Path:
        """Switch to a writable JSON roster store (seeded from the current roster).

        This makes the roster UI-manageable: bots added/edited/removed through the
        API persist here and are what spawned agents read.
        """
        store = self.paths.home / "roster.json"
        self.paths.home.mkdir(parents=True, exist_ok=True)
        if not store.is_file():
            save_roster(store, self.roster)
        seeds = self.roster
        self.roster = load_roster(store)
        self.roster_path = store
        self._backfill_seed_bots(seeds, store)
        return store

    def _backfill_seed_bots(self, seeds: Roster, store: Path) -> None:
        """Offer new seed-roster bots to an existing store, once each.

        The store is seeded from the TOML roster only when it doesn't exist, so
        a bot added to `roster.toml` later never reaches an existing home. Fix:
        append any seed bot the store has never been offered. A marker file
        records offered names so a deliberately deleted bot stays deleted.
        """
        marker = self.paths.home / ".seeded-bots"
        offered = set(marker.read_text(encoding="utf-8").split()) if marker.is_file() else set()
        existing = set(self.roster.names())
        added = False
        for bot in seeds:
            if bot.name in offered or bot.name in existing:
                continue
            self.roster.bots.append(bot)
            added = True
        if added:
            save_roster(store, self.roster)
        all_offered = offered | {b.name for b in seeds}
        if all_offered != offered:
            marker.write_text("\n".join(sorted(all_offered)) + "\n", encoding="utf-8")

    def _agent_argv(self, name: str) -> list[str]:
        return [
            "-m",
            "agent",
            "--bot",
            name,
            "--roster",
            str(self.roster_path),
            "--home",
            str(self.paths.home),
        ]

    @contextmanager
    def _lifecycle_lock(self, name: str):
        """One stop/spawn transaction per bot, across HTTP threads and CLIs.

        The lock file is permanent: unlinking it would let a new caller lock
        a different inode while an existing waiter still holds the old one.
        This is independent of the state store and the agent's recovery sweep.
        """
        import fcntl

        if not valid_bot_name(name):
            raise RosterError("invalid bot name")
        self.paths.run.mkdir(parents=True, exist_ok=True)
        with (self.paths.run / f"{name}.lifecycle.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _start_bot(self, name: str) -> BotHandle:
        with self._lifecycle_lock(name):
            (self.paths.run / f"{name}.stopped").unlink(missing_ok=True)
            handle = self.backend.spawn(name, self._agent_argv(name))
            self._clear_startup_error(name)
            return handle

    def is_starting(self, name: str) -> bool:
        """Whether creation or a restart is still preparing this bot."""
        return name in self._starting or name in self._restarts

    def _startup_error_path(self, name: str) -> Path:
        return self.paths.run / f"{name}.startup-error"

    def _clear_startup_error(self, name: str) -> None:
        self._startup_error_path(name).unlink(missing_ok=True)

    def _record_startup_error(self, name: str, exc: Exception) -> str:
        from .redaction import scrub

        error = scrub(str(exc))
        try:
            write_atomic(self._startup_error_path(name), error)
        except OSError:
            pass
        print(f"bot {name} startup failed: {error}", file=sys.stderr, flush=True)
        return error

    def start_created_bot(
        self, name: str, *, welcome: bool = True, on_finished: Callable[[], None] | None = None
    ) -> dict:
        """Prepare a persisted bot on a worker, without holding its HTTP response.

        Persist the welcome before spawning so a server restart during machine
        provisioning can still deliver it. The roster remains the boot work list.
        The lifecycle lock and creation token prevent a deleted/replaced bot
        from being resurrected by a worker that had not started yet.
        """
        bot = self.roster.get(name)
        token = object()
        self._starting[name] = token
        self._clear_startup_error(name)

        def work() -> None:
            try:
                with self._lifecycle_lock(name):
                    try:
                        if self._starting.get(name) is not token or name not in self.roster.names():
                            return
                        self.backend.spawn(name, self._agent_argv(name))
                        self._clear_startup_error(name)
                    except Exception as exc:
                        self._record_startup_error(name, exc)
                    finally:
                        if self._starting.get(name) is token:
                            self._starting.pop(name, None)
            except Exception as exc:
                # Failure to enter the lock is terminal too: no worker is
                # left to finish provisioning or keep queued chats alive.
                if self._starting.get(name) is token:
                    self._record_startup_error(name, exc)
                    self._starting.pop(name, None)
            finally:
                if on_finished is not None:
                    try:
                        on_finished()
                    except Exception:
                        pass  # a disconnected observer must not fail the startup

        try:
            if welcome:
                self._queue_welcome(bot)
            threading.Thread(target=work, name=f"create-bot-{name}", daemon=True).start()
        except Exception as exc:
            self._starting.pop(name, None)
            return {"status": "stopped", "startup_error": self._record_startup_error(name, exc)}
        if self._starting.get(name) is token:
            return {"status": "starting"}
        try:
            error = self._startup_error_path(name).read_text(encoding="utf-8")
        except OSError:
            error = ""
        return {"status": "stopped", "startup_error": error} if error else {"status": "running"}

    def _queue_welcome(self, bot: Bot) -> None:
        from .welcome import queue_welcome, welcome_prompt

        queue_welcome(
            self.paths,
            bot.name,
            welcome_prompt(
                title=bot.display_name(), name=bot.name, role=bot.role, description=bot.personality
            ),
        )

    def _stop_bot(self, name: str) -> None:
        with self._lifecycle_lock(name):
            write_atomic(self.paths.run / f"{name}.stopped", "1")
            self._starting.pop(name, None)
            with self._restart_lock:
                self._restarts.pop(name, None)
            handle = self._handle(name)
            if handle:
                self.backend.stop(handle)

    def up(self) -> list[BotHandle]:
        self.init()
        return [self._start_bot(bot.name) for bot in self.roster]

    def down(self) -> None:
        for bot in self.roster:
            self._stop_bot(bot.name)

    def restart(
        self,
        name: str,
        *,
        wait: bool = True,
        updating: bool = False,
        on_finished: Callable[[], None] | None = None,
    ) -> BotHandle | None:
        """Coalesce restarts and expose preparation before stopping the process.

        HTTP callers use wait=False: state sync and container startup can take
        minutes. CLI callers and the rolling updater can await the same result.
        """
        self.roster.get(name)
        with self._restart_lock:
            existing = self._restarts.get(name)
            if existing is None:
                future = Future()
                self._restarts[name] = (Status.UPDATING if updating else Status.RESTARTING, future)
            else:
                future = existing[1]

        def notify(callback) -> None:
            if callback is not None:
                try:
                    callback()
                except Exception:
                    pass  # an observer must not fail the restart

        # Register outside the lock: an already-complete future invokes inline.
        # Every caller is observed, including HTTP callers joining an idle roll.
        if on_finished is not None:
            future.add_done_callback(lambda _: notify(on_finished))
        if existing is not None:
            return future.result() if wait else None
        future.add_done_callback(lambda _: notify(self.on_restart_changed))

        def work() -> None:
            result = None
            error = None
            try:
                notify(self.on_restart_changed)
                with self._lifecycle_lock(name):
                    # A stop or delete may have won while this worker waited.
                    if self._restarts.get(name, (None, None))[1] is not future:
                        return
                    self.roster.get(name)
                    (self.paths.run / f"{name}.stopped").unlink(missing_ok=True)
                    self._starting.pop(name, None)
                    handle = self._handle(name)
                    if handle:
                        self.backend.stop(handle)
                    result = self.backend.spawn(name, self._agent_argv(name))
                    self._clear_startup_error(name)
            except Exception as exc:
                error = exc
                self._record_startup_error(name, exc)
            finally:
                with self._restart_lock:
                    if self._restarts.get(name, (None, None))[1] is future:
                        self._restarts.pop(name, None)
                if error is None:
                    future.set_result(result)
                else:
                    future.set_exception(error)

        if wait:
            work()
            return future.result()
        try:
            threading.Thread(target=work, name=f"restart-bot-{name}", daemon=True).start()
        except Exception as exc:
            with self._restart_lock:
                self._restarts.pop(name, None)
            self._record_startup_error(name, exc)
            future.set_exception(exc)
            raise
        return None

    # -- roster management (UI-driven) ------------------------------------
    def _persist(self) -> None:
        if self.roster_path.suffix != ".json":
            self.use_json_store()
        save_roster(self.roster_path, self.roster)

    def _bot_group_count(self) -> int:
        return len(self.roster.bots) + len(list_rooms(self.paths))

    def _ensure_bot_group_capacity(self) -> None:
        if self._bot_group_count() >= MAX_BOTS_AND_GROUPS:
            raise RosterError(
                f"Dotobot supports up to {MAX_BOTS_AND_GROUPS} bots and groups combined"
            )

    def add_bot(self, *, start: bool = True, welcome: bool = True, **fields) -> Bot:
        """Register a bot and, by default, start it and queue its welcome turn.

        `start=False` registers without spawning, for callers that are writing
        configuration rather than bringing a deployment up — `harness import`
        is one. Importing a roster should not require a container engine, and
        on the machines backend spawning provisions a machine, so an import on
        a laptop with no Docker used to fail per bot.

        A started bot also gets one `origin="welcome"` inbox message
        (`harness/welcome.py`) so it opens the chat itself from its name and
        description. `welcome=False` skips it — recipe install sends its own
        after seeding, and a client may pass `"welcome": false` on POST.
        """
        name = bot_slug(str(fields.get("name", "")).strip())
        with self._lifecycle_lock(name):
            bot = self._register_bot(**fields)
        if start:
            self._start_bot(bot.name)
            if welcome:
                self._queue_welcome(bot)
        return bot

    def _register_bot(self, **fields) -> Bot:
        raw = str(fields.get("name", "")).strip()
        if not raw:
            raise RosterError("bot needs a 'name'")
        # Never fall back to the raw string: `bot_slug("..")` is "", and the
        # name becomes a path component under the home for every per-bot
        # directory, so an unsluggable name is refused, not used verbatim.
        name = bot_slug(raw)
        if not name or not valid_bot_name(name):
            raise RosterError("bot name must contain a letter or digit")
        title = str(fields.get("title") or "").strip()
        if not title and name != raw:
            title = raw
        if not title or title == name:
            title = (
                " ".join(part[:1].upper() + part[1:] for part in name.split("-") if part) or name
            )
        if name in self.roster.names():
            raise RosterError(f"bot {name!r} already exists")
        self._ensure_bot_group_capacity()
        personality = str(fields.get("personality") or "").strip()
        provider, model, reasoning = resolve_llm(
            self.paths,
            provider=str(fields.get("provider") or ""),
            model=str(fields.get("model") or ""),
            reasoning=str(fields.get("reasoning") or ""),
        )
        from .roster import voice_override

        bot = Bot(
            name=name,
            role=fields.get("role", ""),
            personality=personality,
            provider=provider,
            model=model or None,
            reasoning=reasoning,
            embeddings=str(fields.get("embeddings") or "").strip(),
            auth_ref=fields.get("auth_ref") or None,
            avatar=fields.get("avatar", "robot"),
            title=title,
            color=str(fields.get("color") or ""),
            dreaming=bool(fields.get("dreaming", fields.get("idle_think", False))),
            private_browser=bool(fields.get("private_browser", False)),
            caveman=caveman_flag(fields.get("caveman")),
            voice_provider=voice_override(fields.get("voice_provider")),
            elevenlabs_voice_id=voice_override(fields.get("elevenlabs_voice_id"), voice=True),
        )
        if not bot.personality:
            bot.personality = bot.display_name()
        from .bot_cleanup import cancel

        cancel(self.paths, name, check_only=True)
        self.roster.bots.append(bot)
        self._persist()
        cancel(self.paths, name)
        self.init()
        from agent.soul import load_soul

        load_soul(self.paths, bot.name, personality=bot.personality)
        try:
            from .connectors import Connectors

            Connectors(self.paths).grant_bot(bot.name, self.roster.names())
        except Exception:
            pass
        return bot

    def update_bot(self, name: str, **fields) -> Bot:
        from .roster import voice_override

        overrides = {
            key: voice_override(fields[key], voice=key == "elevenlabs_voice_id")
            for key in ("voice_provider", "elevenlabs_voice_id")
            if key in fields
        }
        bot = self.roster.get(name)
        old_personality = (bot.personality or "").strip()
        for key, value in overrides.items():
            setattr(bot, key, value)
        for key in (
            "role",
            "personality",
            "provider",
            "model",
            "reasoning",
            "embeddings",
            "auth_ref",
            "avatar",
            "title",
            "color",
        ):
            if key in fields and fields[key] is not None:
                value = fields[key]
                if key == "model":
                    value = str(value or "").strip() or None
                if key in ("reasoning", "embeddings"):
                    value = str(value or "").strip()
                setattr(bot, key, value)
        # `dreaming` is canonical; `idle_think` is the first release's spelling
        # and still arrives from older app builds.
        for key in ("dreaming", "idle_think"):
            if key in fields and fields[key] is not None:
                bot.dreaming = bool(fields[key])
                break
        if "private_browser" in fields and fields["private_browser"] is not None:
            bot.private_browser = bool(fields["private_browser"])
        # `caveman` is a tri-state: an explicit null means "follow the
        # account again", so unlike the fields above None is a value here.
        # Live profile field — the agent adopts it next turn, no restart.
        if "caveman" in fields:
            bot.caveman = caveman_flag(fields["caveman"])
        if "blocked" in fields and fields["blocked"] is not None:
            bot.blocked = bool(fields["blocked"])
        self._persist()
        self._reseed_soul(name, old_personality, (bot.personality or "").strip())
        # The cached server-side Memory holds the pre-update embedding route.
        with self._memory_lock:
            self._memories.pop(name, None)
        # Title / role / description are roster labels. Restarting the process
        # on every settings keystroke drops in-flight work; only restart when
        # the running identity (provider / model / avatar) actually changes.
        # `private_browser` restarts too: the agent process read the flag at
        # spawn, and an already-running Chrome keeps its old jar links.
        # `embeddings` restarts too: the agent resolves its embedding route
        # once at spawn (build_agent), like the provider identity.
        if any(
            k in fields and fields[k] is not None
            for k in ("provider", "model", "reasoning", "embeddings", "auth_ref", "private_browser")
        ):
            self.restart(name)
        return bot

    def _reseed_soul(self, name: str, old: str, new: str) -> None:
        """Keep a never-edited soul in step with a rewritten personality.

        The soul file is seeded from the roster personality the first time
        it is read and is the bot's own after that. A soul that is still
        that verbatim seed is a copy, not an identity: leaving it behind
        when update_bot (or Settings) rewrites the instructions would keep
        the old text in every prompt as the bot's "soul". An edited soul is
        never touched.
        """
        if old == new:
            return
        from agent.soul import save_soul, soul_path

        path = soul_path(self.paths, name)
        if not path.is_file():
            return
        try:
            current = path.read_text(encoding="utf-8").strip()
        except OSError:
            return
        if current != old:
            return
        if new:
            save_soul(self.paths, name, new)
        else:
            path.unlink(missing_ok=True)

    def remove_bot(self, name: str) -> dict:
        from .bot_cleanup import job_path, schedule_shutdown, start_shutdown

        with self._lifecycle_lock(name):
            bot = self.roster.get(name)
            self._starting.pop(name, None)
            with self._restart_lock:
                self._restarts.pop(name, None)
            try:
                job = schedule_shutdown(self, name)
                self.roster.bots.remove(bot)
                self._persist()
            except Exception:
                # A failed deletion must not leave a live roster entry barred
                # from restarting by its preparatory cleanup job.
                self.roster = load_roster(self.roster_path)
                if name in self.roster.names():
                    job_path(self.paths, name).unlink(missing_ok=True)
                raise
        with self._memory_lock:
            self._memories.pop(name, None)
        start_shutdown(self, name)
        return job

    def _handle(self, name: str) -> BotHandle | None:
        backend = self.backend
        if hasattr(backend, "load"):
            return backend.load(name)  # type: ignore[attr-defined]
        return None

    def status(self) -> list[BotHandle]:
        out = []
        for bot in self.roster:
            restart = self._restarts.get(bot.name)
            if restart or self.is_starting(bot.name):
                handle = BotHandle(
                    bot=bot.name,
                    backend=self.backend_name,
                    status=restart[0] if restart else Status.STARTING,
                )
            else:
                handle = self._handle(bot.name) or BotHandle(
                    bot=bot.name, backend=self.backend_name, status=Status.STOPPED
                )
                try:
                    error = self._startup_error_path(bot.name).read_text(encoding="utf-8")
                except OSError:
                    error = ""
                if error and handle.status != Status.RUNNING:
                    handle.meta["startup_error"] = error
            out.append(handle)
        return out

    # -- messaging --------------------------------------------------------
    def chat(self, name: str, text: str, *, timeout: float = 45.0) -> messaging.Msg | None:
        """Send a message from 'user' to a named bot and wait for the reply."""
        self.roster.get(name)  # validate name
        msg = messaging.Msg(to=name, frm="user", text=text)
        messaging.send(self.paths, msg)
        return messaging.wait_for_reply(self.paths, "user", msg.id, timeout=timeout)

    def chat_stream(
        self,
        name: str,
        text: str,
        *,
        frm: str = "user",
        attachments: list[dict] | None = None,
        origin: str | None = None,
        task_scope: dict | None = None,
    ) -> tuple[str, StreamReader]:
        """Send a message and return (request_id, StreamReader) for live events."""
        self.roster.get(name)
        msg = messaging.Msg(
            to=name, frm=frm, text=text, attachments=attachments or [], origin=origin
        )
        # Server-owned sends expose the request id as their bubble/card id.
        # Keep that identity on both the live relay and persisted history.
        msg.message_id = msg.id
        if task_scope is not None:
            from .taskscope import begin_task

            begin_task(
                self.paths,
                name,
                str(task_scope["conversation"]),
                text="",
                input_id=msg.id,
                trusted_user=False,
                inherited_scope=task_scope,
            )
        messaging.send(self.paths, msg)
        if frm == "user":
            obligations.record_send(self.paths, name, msg.id)
        return msg.id, StreamReader(self.paths, msg.id)

    def recipients_for(
        self,
        text: str,
        *,
        bot: str | None = None,
        room: Room | None = None,
    ) -> list[str]:
        """Who should answer this turn (each uses their own memory/soul).

        A blocked bot is never among them: it stays on the roster (memory,
        settings, history) but gets no turn until the owner unblocks it.
        """
        known = self.roster.names()
        mentions = resolve_mentions(text, known)
        blocked = self.blocked_names()
        if blocked:
            recipients = self._recipients_unfiltered(text, known, mentions, bot=bot, room=room)
            return [r for r in recipients if r not in blocked]
        return self._recipients_unfiltered(text, known, mentions, bot=bot, room=room)

    def blocked_names(self) -> set[str]:
        """Roster bots the owner blocked (guideline 1.2). Every path that
        hands a bot a turn — chat routing, the routine and dream ticks, a
        routine test run — consults this, not just `recipients_for`."""
        return {b.name for b in self.roster.bots if getattr(b, "blocked", False)}

    def is_blocked(self, name: str) -> bool:
        return name in self.blocked_names()

    def _recipients_unfiltered(
        self,
        text: str,
        known: list[str],
        mentions: list[str],
        *,
        bot: str | None,
        room: Room | None,
    ) -> list[str]:
        if room is not None:
            members = [m for m in room.members if m in known]
            if has_everyone(text):
                return members
            chosen = [m for m in mentions if m in members]
            if chosen:
                return chosen
            # The user owns the room. An un-addressed group message reaches
            # every member; ownership does not select a default bot.
            return members
        if not bot:
            return mentions
        self.roster.get(bot)
        lead = leading_mention(text)
        lead_canon = next((n for n in known if n.lower() == (lead or "").lower()), None)
        if lead_canon and lead_canon != bot:
            return mentions or [lead_canon]
        extra = [m for m in mentions if m != bot]
        return [bot, *extra]

    def dispatch_chat(
        self,
        text: str,
        *,
        bot: str | None = None,
        room_id: str | None = None,
        frm: str = "user",
        attachments: list[dict] | None = None,
        quote: dict | None = None,
        thread_id: str | None = None,
        message_id: str | None = None,
        voice_call_id: str | None = None,
    ) -> list[tuple[str, str, StreamReader]]:
        """Fan a user message out to one or more bots.

        Returns a list of `(bot_name, request_id, StreamReader)`. Each bot is
        addressed independently so it loads its own soul and memory.
        """
        room: Room | None = None
        if room_id:
            room = get_room(self.paths, room_id)
        cmd = parse_slash(text)
        if cmd and room is not None and cmd.name in {"stop", "queue"}:
            # A busy-session command in a group applies to every member —
            # it never becomes a model turn that writes "Stopped." N times
            # into the transcript. The command line itself is kept.
            acks: list[tuple[str, str, StreamReader]] = []
            for member in room.members:
                if member not in self.roster.names():
                    continue
                ack = self._session_command(member, cmd, text=text, room=room.id)
                if ack is not None:
                    acks.extend(ack)
            if acks:
                append_room_message(self.paths, room.id, frm=frm, text=text, message_id=message_id)
                return acks
            if cmd.name == "queue" and cmd.rest:
                text = cmd.rest
                cmd = None
        elif cmd and bot and cmd.name in {"stop", "queue"}:
            ack = self._session_command(bot, cmd, text=text, message_id=message_id)
            if ack is not None:
                return ack
            if cmd.name == "queue" and cmd.rest:
                text = cmd.rest
                cmd = None
        recipients = (
            [bot] if voice_call_id and bot else self.recipients_for(text, bot=bot, room=room)
        )
        if room is not None and frm != "user" and frm in recipients:
            # A bot posting into a group (`message_room`) never answers its
            # own line; if it was the only addressee, the rest of the room is.
            recipients = [r for r in recipients if r != frm] or [
                m for m in room.members if m != frm and m in self.roster.names()
            ]
        if not recipients:
            raise RosterError("no bots to send to")
        mentions = resolve_mentions(text, self.roster.names())
        skill = (
            None
            if (cmd is None)
            else (None if cmd.name in {"memory", "remember", "soul", "skills"} else cmd.name)
        )
        if room is not None:
            # Rooms keep their own transcript; side threads are 1:1 only.
            thread_id = None
            append_room_message(
                self.paths,
                room.id,
                frm=frm,
                text=text,
                mentions=mentions,
                attachments=attachments,
                message_id=message_id,
                quote=quote,
            )
        turns: list[tuple[str, str, StreamReader]] = []
        for name in recipients:
            self.roster.get(name)
            resolved_thread = thread_id
            if thread_id and room is None:
                resolved_thread = thread_root_id(self.memory_for(name), thread_id) or thread_id
            msg = messaging.Msg(
                to=name,
                frm=frm,
                text=_deliver_text(name, text),
                attachments=attachments or [],
                room=room.id if room else None,
                skill=skill,
                mentions=mentions,
                quote=quote,
                thread_id=resolved_thread,
                message_id=message_id,
                origin="voice" if voice_call_id else None,
                voice_call_id=voice_call_id,
            )
            messaging.send(self.paths, msg)
            if frm == "user":
                # The bot now owes the user a visible reply; the
                # obligation coalesces if one is already open for this bot.
                obligations.record_send(self.paths, name, msg.id)
            turns.append((name, msg.id, StreamReader(self.paths, msg.id)))
        return turns

    def rooms(self) -> list[Room]:
        return list_rooms(self.paths)

    def create_room(
        self,
        title: str,
        members: list[str],
        owner: str | None = None,
        description: str = "",
    ) -> Room:
        known = set(self.roster.names())
        unknown = [m for m in members if m not in known]
        if unknown:
            raise RosterError(f"unknown bots: {', '.join(unknown)}")
        self._ensure_bot_group_capacity()
        return create_room(self.paths, title, members, owner=owner, description=description)

    def duplicate_bot(self, source: str, *, name: str | None = None, start: bool = True) -> Bot:
        """Copy profile, skills, routines, avatar. Not history or facts."""
        src = self.roster.get(source)
        new_name = bot_slug(str(name or "").strip()) or _copy_name(self.roster.names(), src.name)
        if new_name in self.roster.names():
            raise RosterError(f"bot {new_name!r} already exists")
        # Read before creating the destination. Reuse this snapshot so a
        # damaged source cannot leave a partial copy on the roster.
        try:
            source_routines = list_routines(self.paths, src.name)
        except RoutineError as exc:
            raise RosterError(f"cannot duplicate {src.name!r}: {exc}") from exc
        fields = src.to_dict()
        fields["name"] = new_name
        if not str(fields.get("title") or "").strip() or fields.get("title") == src.name:
            fields["title"] = src.display_name() + " copy"
        bot = self.add_bot(start=False, **fields)
        _copy_bot_state(self.paths, src.name, bot.name, source_routines=source_routines)
        if start:
            self._start_bot(bot.name)
        return bot

    def _ack_turn(
        self, bot: str, text: str, *, room: str | None = None
    ) -> list[tuple[str, str, StreamReader]]:
        rid = uuid.uuid4().hex
        writer = StreamWriter(self.paths, rid, room=room)
        writer.status("typing")
        writer.final(text, bot)
        return [(bot, rid, StreamReader(self.paths, rid))]

    def _log_user_command(self, bot: str, text: str, *, message_id: str | None = None) -> None:
        """Record a slash command on the 1:1 thread without queuing a turn.

        `/stop` skips the inbox so it can interrupt the in-flight turn, but
        the user still said it — if it never hits the session log, a history
        reload after `Stopped.` shoves the live `/stop` bubble *below* the
        reply.
        """
        line = (text or "").strip()
        if not line:
            return
        mem = self.memory_for(bot)
        mem.log_turn(mem.latest_session_id(), "in:user", line, peer="user", message_id=message_id)

    def _session_command(
        self,
        bot: str,
        cmd,
        text: str = "",
        *,
        room: str | None = None,
        message_id: str | None = None,
    ) -> list[tuple[str, str, StreamReader]] | None:
        """Hermes busy-session commands: /stop and /queue, never go through the inbox.

        In a room (`room=`) the command line is kept by the caller on the
        room transcript, not on each member's 1:1 log.
        """
        self.roster.get(bot)
        if cmd.name == "stop":
            if room is None:
                self._log_user_command(bot, text or "/stop", message_id=message_id)
            self.control.request_stop(bot)
            return self._ack_turn(bot, "Stopped.", room=room)
        rest = (cmd.rest or "").strip()
        if rest and not rest.startswith("clear") and not rest.startswith("drop"):
            return None
        if room is None:
            self._log_user_command(bot, text or f"/queue {rest}".strip(), message_id=message_id)
        if rest == "clear":
            n = messaging.drop_pending(self.paths, bot)
            return self._ack_turn(bot, f"cleared {n} queued message(s).", room=room)
        if rest.startswith("drop"):
            n = messaging.drop_pending(self.paths, bot, count=1)
            return self._ack_turn(bot, f"dropped {n} queued message(s).", room=room)
        items = messaging.pending(self.paths, bot)
        if not items:
            return self._ack_turn(bot, "queue is empty.", room=room)
        lines = [f"queued ({len(items)}):"]
        lines.extend(f"{i + 1}. {m.text[:80]}" for i, m in enumerate(items[:10]))
        return self._ack_turn(bot, "\n".join(lines), room=room)

    def send(self, frm: str, to: str, text: str) -> str:
        """Inject a bot-to-bot message (attribution preserved). Fire and forget."""
        self.roster.get(to)
        msg = messaging.Msg(to=to, frm=frm, text=text)
        messaging.send(self.paths, msg)
        return msg.id

    def bots(self) -> list[Bot]:
        return list(self.roster)


def _deliver_text(bot: str, text: str) -> str:
    """Strip a leading @bot so the named bot answers instead of re-handing off."""
    lead = leading_mention(text)
    if not lead or lead.lower() != bot.lower():
        return text
    return re.sub(rf"^\s*@{re.escape(lead)}\s+", "", text, count=1, flags=re.IGNORECASE)


def _copy_name(taken: list[str], source: str) -> str:
    have = {n.lower() for n in taken}
    base = f"{source}-copy"
    if base.lower() not in have:
        return base
    n = 2
    while f"{source}-copy-{n}".lower() in have:
        n += 1
    return f"{source}-copy-{n}"


def _copy_bot_state(
    paths: HarnessPaths, source: str, dest: str, *, source_routines: list[dict]
) -> None:
    """Profile-adjacent files only: soul, private skills, enablement, routines."""
    from agent.soul import load_soul, save_soul

    save_soul(paths, dest, load_soul(paths, source))
    src_skills = paths.bot_memory(source) / "skills"
    dst_skills = paths.bot_memory(dest) / "skills"
    if src_skills.is_dir():
        if dst_skills.exists():
            shutil.rmtree(dst_skills)
        shutil.copytree(src_skills, dst_skills)
    disabled = paths.bot_memory(source) / "disabled-workflows.json"
    if disabled.is_file():
        dest_dis = paths.bot_memory(dest) / "disabled-workflows.json"
        dest_dis.write_text(disabled.read_text(encoding="utf-8"), encoding="utf-8")
    for row in source_routines:
        add_routine(
            paths,
            dest,
            title=str(row.get("title") or ""),
            prompt=str(row.get("prompt") or ""),
            when=str(row.get("cron") or ""),
            enabled=bool(row.get("enabled", False)),
        )
