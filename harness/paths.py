"""Shared host directory contract.

This layout is backend-agnostic on purpose: the process prototype uses it as
plain directories today, and containers/VMs will bind-mount / virtio-fs the
same tree later without a fork.

Shared (machine-level, visible to every bot):
    shared/credentials/            provider keys / tokens (chmod 600, gitignored)
    shared/browser/cookies/        shared cookie + login jar (Google login, etc.)
    shared/workspace/              shared scratch / files
    shared/tmp/screenshots/        screenshot staging (expiring; see screenshots.py)
    shared/messages/<name>/inbox/  file-based message bus (incl. the "user")
    shared/skills/                 shared SKILL.md skills
    shared/rooms/                  group-chat metadata + transcripts
    shared/run/                    orchestrator pid/status files

Private (per bot):
    shared/browser/sessions/<bot>/ session, cache, open tabs/windows
    shared/memory/<bot>/           facts, soul, session recall, private skills
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOME_ENV = "HARNESS_HOME"


def _make_private_dir(path: Path) -> None:
    """chmod 0700 an existing directory, best-effort (non-posix, or a home
    some other uid owns, just keeps whatever mode it has)."""
    if not path.is_dir():
        return
    try:
        path.chmod(0o700)
    except OSError:  # pragma: no cover - non-posix / not ours
        pass


def default_home() -> Path:
    """Resolve the shared root: $HARNESS_HOME or ./shared under the repo/cwd."""
    env = os.environ.get(DEFAULT_HOME_ENV)
    if env:
        return Path(env).expanduser().resolve()
    return (Path.cwd() / "shared").resolve()


@dataclass(frozen=True)
class HarnessPaths:
    """Resolved paths for one harness home."""

    home: Path

    @classmethod
    def resolve(cls, home: Path | str | None = None) -> HarnessPaths:
        root = Path(home).expanduser().resolve() if home else default_home()
        return cls(home=root)

    # -- shared -----------------------------------------------------------
    @property
    def credentials(self) -> Path:
        return self.home / "credentials"

    @property
    def browser_cookies(self) -> Path:
        return self.home / "browser" / "cookies"

    @property
    def browser_sessions(self) -> Path:
        return self.home / "browser" / "sessions"

    @property
    def workspace(self) -> Path:
        return self.home / "workspace"

    @property
    def uploads(self) -> Path:
        return self.workspace / "uploads"

    @property
    def screenshots(self) -> Path:
        """Screenshot staging area: the one temp folder harness
        screenshots land in before being sent anywhere. Files expire after
        the retention window and are purged by the sweeper — see
        `harness/screenshots.py` for the contract."""
        return self.home / "tmp" / "screenshots"

    @property
    def reports(self) -> Path:
        """Problem reports (`harness/reports.py`): a flagged message or an app
        error, with the server's context attached, one JSON file each."""
        return self.home / "reports"

    @property
    def state_db(self) -> Path:
        """SQLite state store: transactional runtime state — session
        transcripts, prompt resolutions and turn admission claims / recovery
        markers in `harness/statestore.py`, plus the delivery
        intent/receipt ledger (`harness/delivery.py`)."""
        return self.home / "state.sqlite"

    @property
    def messages(self) -> Path:
        return self.home / "messages"

    @property
    def skills(self) -> Path:
        return self.home / "skills"

    @property
    def rooms(self) -> Path:
        return self.home / "rooms"

    @property
    def memory(self) -> Path:
        return self.home / "memory"

    @property
    def run(self) -> Path:
        return self.home / "run"

    @property
    def streams(self) -> Path:
        return self.home / "streams"

    @property
    def receipts(self) -> Path:
        """Last-read timestamps per conversation (chat picker unread)."""
        return self.home / "receipts.json"

    @property
    def answers(self) -> Path:
        """User responses to in-chat prompts (choice boxes), keyed by prompt id."""
        return self.home / "answers"

    @property
    def prompts(self) -> Path:
        """Open secret/choice boxes any client can pick up (app is just a UI)."""
        return self.home / "prompts"

    @property
    def blocks(self) -> Path:
        """Installed block definitions (BLOCK.md dirs), shared across bots."""
        return self.home / "blocks"

    @property
    def blocks_state(self) -> Path:
        """Durable block instances; settled ones persist so cards survive reload."""
        return self.home / "blocks-state"

    @property
    def control(self) -> Path:
        return self.home / "control"

    @property
    def routines(self) -> Path:
        return self.home / "routines"

    @property
    def sends(self) -> Path:
        """Idempotent-send acceptance ledger — see harness/sends.py."""
        return self.home / "sends"

    @property
    def obligations(self) -> Path:
        """Per-bot ack obligations — see agent/obligations.py."""
        return self.home / "obligations"

    @property
    def usage(self) -> Path:
        """Append-only per-provider usage ledger: usage/usage.jsonl."""
        return self.home / "usage"

    @property
    def audit(self) -> Path:
        """Append-only authorization trail, one JSONL per bot. Separate from
        the stream trail: that one is the live window and is swept with its
        conversation, this one is the record of what was decided."""
        return self.home / "audit"

    @property
    def machine_state(self) -> Path:
        """Canonical machine-state store: per-user trees
        cloned onto every bot machine and merged back (grokbot-style)."""
        return self.home / "machine-state"

    def canonical_home(self, user: str = "default") -> Path:
        """The canonical in-machine user home (Chrome profile, Desktop, files).

        The `user` level is a future dimension; the harness is single-user
        today so everything lives under `default`.
        """
        return self.machine_state / user / "home"

    def machine_meta(self, user: str = "default") -> Path:
        """Sync receipts / markers for the canonical store (not cloned)."""
        return self.machine_state / user / ".meta"

    # -- per-bot ----------------------------------------------------------
    def bot_memory(self, bot: str) -> Path:
        return self.memory / bot

    @property
    def deleted_bots(self) -> Path:
        """Durable 24-hour deletion jobs; never infer deletion from orphan files."""
        return self.home / "deleted-bots"

    def bot_session(self, bot: str) -> Path:
        return self.browser_sessions / bot

    def inbox(self, name: str) -> Path:
        return self.messages / name / "inbox"

    def processed(self, name: str) -> Path:
        return self.messages / name / "processed"

    def run_file(self, bot: str) -> Path:
        return self.run / f"{bot}.json"

    def log_file(self, bot: str) -> Path:
        return self.run / f"{bot}.log"

    def stream_file(self, request_id: str) -> Path:
        return self.streams / f"{request_id}.jsonl"

    def control_file(self, bot: str) -> Path:
        return self.control / f"{bot}.json"

    def control_events(self, bot: str) -> Path:
        return self.control / f"{bot}.events.jsonl"

    def bot_routines(self, bot: str) -> Path:
        return self.routines / f"{bot}.json"

    def send_ledger(self) -> Path:
        """Nonce -> acceptance record store for idempotent chat sends."""
        return self.sends / "ledger.json"

    def obligation_file(self, bot: str) -> Path:
        """One coalesced ack obligation per bot."""
        return self.obligations / f"{bot}.json"

    def computer_activity_file(self, bot: str) -> Path:
        """mtime = the last time anything drove this bot's computer (a bot
        computer_* tool, or a human input event) — the idle-browser sweep's
        clock (harness/browser_idle.py)."""
        return self.run / f"{bot}.computer-active"

    # -- machine pool ------------------------------------------------------
    def machines_file(self) -> Path:
        """Machine-pool assignment state (machines backend)."""
        return self.run / "machines.json"

    def machines_lock(self) -> Path:
        return self.run / "machines.lock"

    def machine_dirty_file(self, machine_id: int) -> Path:
        """Set while a machine holds un-synced state (crash salvage marker)."""
        return self.run / f"machine-{machine_id}.dirty"

    def ensure_layout(self, bots: list[str] | None = None) -> None:
        """Create the shared tree (idempotent). The home and its private
        subtrees are chmod 700 (see `_make_private_dir`)."""
        for path in (
            self.credentials,
            self.browser_cookies,
            self.browser_sessions,
            self.workspace,
            self.screenshots,
            self.messages,
            self.skills,
            self.rooms,
            self.memory,
            self.run,
            self.streams,
            self.answers,
            self.prompts,
            self.blocks,
            self.blocks_state,
            self.control,
            self.routines,
            self.sends,
            self.obligations,
            self.reports,
        ):
            path.mkdir(parents=True, exist_ok=True)
        # The home holds every transcript, queued message, live stream and
        # staged screenshot; everything in it belongs to the one harness uid,
        # so no other local account gets a look. mkdir honoured the umask
        # (0755 under the usual 022), which is exactly what `harness audit`
        # flags as fs.home_mode — apply that posture here, on the path every
        # serve/up/agent boot takes, instead of only on `audit --fix`.
        for path in (
            self.home,
            self.credentials,
            self.memory,
            self.streams,
            self.messages,
            self.prompts,
            self.usage,
            self.run,
            self.browser_sessions,
            self.screenshots.parent,
        ):
            _make_private_dir(path)

        for name in ["user", *(bots or [])]:
            self.inbox(name).mkdir(parents=True, exist_ok=True)
            self.processed(name).mkdir(parents=True, exist_ok=True)
        self.canonical_home().mkdir(parents=True, exist_ok=True)
        self.machine_meta().mkdir(parents=True, exist_ok=True)

        for bot in bots or []:
            self.bot_memory(bot).mkdir(parents=True, exist_ok=True)
            self.bot_session(bot).mkdir(parents=True, exist_ok=True)
        from agent.blocks import ensure_default_blocks
        from agent.skills import ensure_default_skills

        ensure_default_skills(self)
        ensure_default_blocks(self)

    def tree(self) -> str:
        """Human-readable summary of shared vs private paths."""
        lines = [
            f"home: {self.home}",
            "shared:",
            f"  credentials     {self.credentials}",
            f"  browser/cookies {self.browser_cookies}",
            f"  workspace       {self.workspace}",
            f"  messages        {self.messages}",
            f"  skills          {self.skills}",
            f"  rooms           {self.rooms}",
            f"  run             {self.run}",
            "per-bot:",
            f"  browser/sessions/<bot> {self.browser_sessions}",
            f"  memory/<bot>           {self.memory}",
        ]
        return "\n".join(lines)
