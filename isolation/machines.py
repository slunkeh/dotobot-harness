"""Machines backend: a pool of persistent, sandboxed bot computers.

This is OUR isolation model (Hermes-style per-bot jails), not xAI Grok Bot's —
Grok Bot is one computer per account with a screen per bot, see
`docs/research-grok-bot.md`. Successor to the ephemeral `container` backend,
which is kept unchanged:

* Each machine is a long-lived container (`<prefix>-<n>`, default prefix
  `harness-machine`) running its own Xvfb desktop — Chrome, a file manager,
  and a terminal. Stopping a bot stops the container but never removes it;
  the machine's named volume (`<prefix>-<n>-home` at /home/agent) survives
  between sessions. `$HARNESS_MACHINE_PREFIX` is how two harness homes on
  one docker daemon keep distinct container names.
  The one exception: a *stopped* container whose image has since been rebuilt
  is recreated on the next spawn (same name, same volume), so machines pick
  up new desktop code instead of booting the old baked-in /app forever.
* **The machine is a jail.** No harness state is mounted inside — no
  credentials, serve.json, message bus, or other bots' memory. The in-machine
  terminal can only see the container. Hardening: --cap-drop ALL,
  --security-opt no-new-privileges, --pids-limit. Chromium also needs
  `--security-opt seccomp=unconfined` (default seccomp SIGTRAPs it even
  with --no-sandbox); caps stay dropped.
* **One shared project directory, `/workspace`.** Every machine on a harness
  home also mounts the ONE named volume `<prefix>-workspace` at
  MACHINE_WORKSPACE, read-write. It is the handoff surface: a bot that wants
  a peer to have a file puts it under `/workspace/<project>/` and tells the
  peer the path (message_agent) — no notification, no locks, last writer
  wins, and deleting a bot never touches it. Homes, displays, processes and
  credentials stay private; nothing under /workspace is ever a secret, and
  it is never part of the home state sync. `HARNESS_MACHINE_WORKSPACE=0`
  leaves the mount out.
* The agent itself runs as a *host* process (exactly the process backend's
  mechanics) with HARNESS_MACHINE_NAME set, so its computer driver targets
  the machine over `docker exec` — the same channel human takeover uses.
* User state is shared via clone-down / merge-up against the canonical store
  (`machine-state/default/home/`), moved as `docker cp` tar streams
  (see state_sync). A dirty marker per machine makes crashed sessions
  salvageable on the next start. Three merge-ups keep the store fresh: the
  quiesced end-of-session sync (authoritative), the serve-loop interval
  flush (`harness.server.start_machine_flush`), and a spawn-time flush of
  every live peer machine right before the new machine's clone-down — so a
  freshly spawned bot opens Chrome already holding the logins the running
  fleet has, not state frozen at the last stop.

Pool assignment lives in run/machines.json (flock-guarded). The pool grows on
demand — bot-created bots spawn mid-session through the same path — up to
$HARNESS_MACHINE_POOL when set.

Lifecycle hardening:

* **Content-addressed identity.** Every container the backend creates is
  labeled at `docker run` time: `com.agent-harness.machine=1` (owner),
  a schema-version label, and a sha256 label over the runtime the harness
  drives inside machines (the in-machine supervisor + desktop bringup code —
  see `runtime_fingerprint`). On acquire/spawn the labels are inspected: a
  same-named container *without* the owner label is refused loudly and never
  removed (it isn't ours); schema or runtime-hash drift recreates the
  container (stop + rm) while KEEPING the named home volume — the volume is
  the machine's disk. Only delayed bot deletion removes that disk after 24 hours.
* **Secrets are files, never `-e` values.** Each bot has an initially empty
  host directory (run/bot-secrets/<bot>/, mode 0700), mounted read-only at
  /run/harness. The governed use_secret_file tool grants individual stored
  credentials as 0600 files through harness.machine_secrets; rotations and
  deletion update existing grants. This directory is never home-synced.
  Legacy $HARNESS_MACHINE_SECRETS entries still grant on spawn, with
  `HARNESS_SECRET_<NAME>_FILE` carrying only the file PATH. A bot label forces
  recreation when a pooled machine changes owners, retaining its home volume.
* **Generation tokens.** Each spawned agent (host "brain") process gets a
  random token passed both as env and argv (see isolation.process_identity);
  the child refuses to boot unless both agree, and the backend never signals
  a recorded pid without verifying the live process's /proc cmdline still
  carries the exact token — recycled pids never receive our SIGTERM. On a
  harness restart a healthy, identity-verified agent is adopted as-is instead
  of being stopped and respawned.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from harness.fsutil import write_atomic
from harness.paths import HarnessPaths
from harness.version import __version__

from . import engine, state_sync
from .base import BotHandle, IsolationBackend, IsolationUnavailable, Status
from .process import _pid_alive, stop_grace
from .process_identity import (
    GENERATION_TOKEN_ENV,
    mint_generation_token,
    token_argument,
    verify_process_token,
)

_DEFAULT_IMAGE = "agent-harness-machine"
_DEFAULT_PREFIX = "harness-machine"
#: docker name: [a-zA-Z0-9][a-zA-Z0-9_.-]* — keep it short so -<n>-home fits.
_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
#: owner label: containers carrying this were created (and may be recreated)
#: by this backend. A same-named container without it is never touched.
OWNER_LABEL = "com.agent-harness.machine"
SCHEMA_LABEL = "com.agent-harness.machine.schema-version"
RUNTIME_LABEL = "com.agent-harness.machine.runtime-sha256"
#: whether the container was created WITH the shared /workspace volume ("1")
#: or opted out ("0"). Part of the identity: toggling HARNESS_MACHINE_WORKSPACE
#: recreates the container (home volume kept) so the mount, the supervisor's
#: boot log and the bot's prompt can never disagree about what /workspace is.
WORKSPACE_LABEL = "com.agent-harness.machine.workspace"
#: bump when the container contract changes (mounts, env, entrypoint shape);
#: existing machines are recreated (volume kept) on the next spawn.
#: 2: the shared /workspace project volume joined the mounts.
#: 3: each bot always has its own read-only script-credential mount.
MACHINE_SCHEMA_VERSION = "3"
#: A pooled machine must never retain another bot's granted credential mount.
SECRET_BOT_LABEL = "com.agent-harness.machine.secret-bot"
#: where the per-machine secrets dir is mounted (read-only) inside a machine.
MACHINE_SECRETS_MOUNT = "/run/harness"
#: the code the harness runs/drives inside machines — the supervisor that is
#: the container's entrypoint and the desktop bringup it calls. Stable across
#: a build, changes exactly when in-machine behavior changes.
_RUNTIME_SOURCES = (
    Path(__file__).resolve().parent / "machine_supervisor.py",
    Path(__file__).resolve().parent.parent / "harness" / "computer_env.py",
)
#: the sandboxed user's home inside every machine (the synced tree).
MACHINE_HOME = "/home/agent"
#: the project directory every machine on this home shares (one named volume,
#: `workspace_volume()`), mounted read-write; never synced, never a secret.
MACHINE_WORKSPACE = "/workspace"
#: Chrome auth DBs that must be ranked even when an idle profile's newer
#: mtime would hide them from newest-wins (same SQLite page size is common).
_CHROME_AUTH_DBS = frozenset({"Cookies", "Login Data", "Login Data For Account"})
#: GNU tar --exclude patterns for `tar -C /home -cf - agent`. Chrome Cache
#: is tens of GB of files that state_sync already drops; packing them made
#: every merge-up a multi-minute 5G tar index. Leaf dir names come from
#: state_sync.JUNK_DIR_NAMES so clone-down and merge-up exclude the same set.
_TAR_EXCLUDES = (
    "agent/.cache",
    "agent/.local/share/Trash",
    "agent/.Xauthority",
    "agent/.harness-local",
    # GNU tar: a pattern without `/` matches the last path component anywhere
    # in the tree. `*/Cache` only matches one level deep and missed Chrome's
    # nested Cache / 3GB Safe Browsing dir.
    *state_sync.JUNK_DIR_NAMES,
    "Singleton*",
    "*.sock",
)
#: uid/gid of the `agent` user baked into deploy/Dockerfile.machine.
MACHINE_OWNER = (1000, 1000)


def tar_output_cap() -> int:
    """Most bytes one machine-home snapshot may put on the host disk.

    `HARNESS_MACHINE_SYNC_MAX_TAR` (bytes) overrides the default of 8 GiB;
    0 disables the cap. The docker client streaming the tar runs under
    RLIMIT_FSIZE so it dies (SIGXFSZ) at the cap and the temp tar is
    unlinked — a machine cannot fill the host by growing its home.
    """
    raw = os.environ.get("HARNESS_MACHINE_SYNC_MAX_TAR", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return 8 << 30


def _tar_output_limit():
    """preexec_fn applying tar_output_cap() to the docker client child."""
    cap = tar_output_cap()
    if not cap:
        return None

    def _limit() -> None:  # pragma: no cover - runs in the forked child
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_FSIZE, (cap, cap))
        except (ImportError, ValueError, OSError):
            pass

    return _limit


#: a busy machine whose container is down is only reclaimed after this grace,
#: so a concurrent spawn between acquire and `docker run` is not stolen.
_RECLAIM_GRACE = 60.0


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def machine_prefix() -> str:
    """Docker name prefix for new machines on this harness home.

    Unset or blank → `harness-machine` (today's names). A custom prefix is
    how two `serve` processes on one daemon keep container/volume names
    distinct. Existing `run/machines.json` entries keep the names they
    already have.
    """
    raw = (os.environ.get("HARNESS_MACHINE_PREFIX") or "").strip()
    if not raw:
        return _DEFAULT_PREFIX
    if not _PREFIX_RE.fullmatch(raw):
        raise IsolationUnavailable(
            f"HARNESS_MACHINE_PREFIX={raw!r} is not a docker name prefix "
            "(start with alphanumeric, then [A-Za-z0-9_.-], at most 63 chars)"
        )
    return raw


def workspace_volume() -> str:
    """Docker named volume holding the shared `/workspace` project directory.

    One per harness home (it follows `machine_prefix()`, so two homes on one
    daemon keep separate workspaces), mounted into every machine.
    """
    return f"{machine_prefix()}-workspace"


def shared_workspace_enabled() -> bool:
    """`HARNESS_MACHINE_WORKSPACE=0` (any registered no-value) leaves the
    shared project volume out of new machines. Default on."""
    from harness.settings import BOOL_FALSE

    raw = (os.environ.get("HARNESS_MACHINE_WORKSPACE") or "").strip().lower()
    return raw not in BOOL_FALSE


def runtime_fingerprint(sources: tuple[Path, ...] = _RUNTIME_SOURCES) -> str:
    """sha256 over the injected/driven in-machine runtime (content-addressed).

    Hashes the files that define what the harness runs inside a machine, so
    the container label changes exactly when that runtime changes.
    """
    digest = hashlib.sha256()
    for source in sources:
        digest.update(source.name.encode("utf-8"))
        digest.update(b"\x00")
        try:
            digest.update(source.read_bytes())
        except OSError:
            digest.update(b"<missing>")
        digest.update(b"\x00")
    return digest.hexdigest()


def identity_decision(labels: dict | None, runtime_hash: str, workspace: bool | None = None) -> str:
    """Classify an *existing* container's labels against our identity.

    Returns 'unowned' (missing owner label: refuse, never rm), 'recreate'
    (schema, runtime-hash, or shared-workspace drift: rm the container, keep
    the volume), or 'ok' (the container is ours and current). `workspace` is
    whether the shared volume should be mounted now (default: the live
    HARNESS_MACHINE_WORKSPACE switch); a container without the label was
    created with the mount, the schema-2 default.
    """
    labels = labels or {}
    if labels.get(OWNER_LABEL) != "1":
        return "unowned"
    if labels.get(SCHEMA_LABEL) != MACHINE_SCHEMA_VERSION:
        return "recreate"
    if labels.get(RUNTIME_LABEL) != runtime_hash:
        return "recreate"
    want = shared_workspace_enabled() if workspace is None else workspace
    if labels.get(WORKSPACE_LABEL, "1") != ("1" if want else "0"):
        return "recreate"
    return "ok"


def stage_secret_file(directory: Path, name: str, value: str) -> Path:
    """Write one secret to `<directory>/<name>` as a 0600 file (dir 0700).

    Atomic replace; the plaintext never appears in argv or the environment —
    only the file path does. Callers bind-mount `directory` read-only into
    the machine at MACHINE_SECRETS_MOUNT.
    """
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    target = directory / name
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    target.chmod(0o600)
    return target


def _secret_env_name(name: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in name).upper()
    return f"HARNESS_SECRET_{safe}_FILE"


@dataclass
class Machine:
    id: int
    name: str
    state: str = "idle"  # idle | busy | retired (awaiting delayed deletion)
    bot: str | None = None
    since: float = 0.0
    last_bot: str | None = None

    @property
    def volume(self) -> str:
        return f"{self.name}-home"


class MachinePool:
    """Flock-guarded assignment state for the persistent machine pool."""

    def __init__(self, paths: HarnessPaths, cap: int | None = None) -> None:
        self.paths = paths
        self.cap = cap if cap is not None else (_int_env("HARNESS_MACHINE_POOL", 0) or None)

    def machines(self, *, strict: bool = False) -> list[Machine]:
        path = self.paths.machines_file()
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            if strict:
                raise IsolationUnavailable("Cannot read machine ownership for cleanup") from None
            # corrupt/partial file: start empty; entries rebuild as bots spawn
            return []
        # Filter to known fields so state written by a newer harness version
        # (extra keys) still loads instead of raising TypeError.
        known = {f.name for f in dataclasses.fields(Machine)}
        return [
            Machine(**{k: v for k, v in m.items() if k in known}) for m in data.get("machines", [])
        ]

    def _write(self, machines: list[Machine]) -> None:
        path = self.paths.machines_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".machines-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"machines": [asdict(m) for m in machines]}, fh)
        os.replace(tmp, path)

    @contextlib.contextmanager
    def _lock(self):
        lock_path = self.paths.machines_lock()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        import fcntl

        with open(lock_path, "w", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def acquire(self, bot: str, container_running) -> Machine:
        from harness.bot_cleanup import job_path

        with self._lock():
            # Legacy repair is safe only when no retained deletion disks
            # could be mistaken for empty/new slots.
            machines = self.machines(strict=any(self.paths.deleted_bots.glob("*.json")))
            for m in machines:
                if m.bot == bot:
                    if job_path(self.paths, bot).exists():
                        raise IsolationUnavailable("Bot is pending deletion")
                    if m.state == "retired":
                        m.state, m.since = "busy", time.time()
                        self._write(machines)
                    return m  # idempotent respawn keeps the same machine
            now = time.time()
            for m in machines:
                # The DELETE receipt precedes background disk reservation.
                if m.state == "idle" and m.last_bot and job_path(self.paths, m.last_bot).exists():
                    m.state, m.bot = "retired", m.last_bot
                stale = m.state == "busy" and now - m.since > _RECLAIM_GRACE
                deleting = m.bot and job_path(self.paths, m.bot).exists()
                if stale and not deleting and not container_running(m.name):
                    m.state, m.bot = "idle", None
            free = next((m for m in machines if m.state == "idle"), None)
            if free is None:
                if self.cap is not None and len(machines) >= self.cap:
                    raise IsolationUnavailable(
                        f"machine pool exhausted ({len(machines)}/{self.cap} busy); "
                        "raise HARNESS_MACHINE_POOL or stop a bot"
                    )
                free = Machine(id=len(machines), name=f"{machine_prefix()}-{len(machines)}")
                machines.append(free)
            free.state, free.bot, free.since = "busy", bot, now
            free.last_bot = bot
            self._write(machines)
            return free

    def release(self, bot: str) -> Machine | None:
        from harness.bot_cleanup import job_path

        with self._lock():
            machines = self.machines(strict=True)
            for m in machines:
                if m.bot == bot:
                    m.last_bot = bot
                    deleting = job_path(self.paths, bot).exists()
                    m.state, m.bot, m.since = (
                        "retired" if deleting else "idle",
                        bot if deleting else None,
                        time.time(),
                    )
                    self._write(machines)
                    return m
        return None


class MachineBackend(IsolationBackend):
    id = "machines"

    def __init__(self, paths: HarnessPaths, **options) -> None:
        self.paths = paths
        self.image = (
            os.environ.get("HARNESS_MACHINE_IMAGE")
            or str(options.get("image") or "")
            or _DEFAULT_IMAGE
        )
        self.pool = MachinePool(paths)
        # Do not sweep canonical here. `Orchestrator.backend` now caches the
        # instance, but construction must stay cheap and side-effect-free:
        # a walk of machine-state on each /api/bots once hung the splash
        # screen. Instances hold only immutable config; assignment state
        # lives in the flock-guarded machines file, so one shared instance
        # is safe across server request threads.

    # -- container plumbing ------------------------------------------------
    def reserve_deleted(self, bot: str) -> None:
        """Reserve stopped disks too: normal Stop may already have released them."""
        with self.pool._lock():
            machines = self.pool.machines(strict=True)
            for machine in machines:
                if machine.state == "idle" and machine.last_bot == bot:
                    machine.state, machine.bot = "retired", bot
            self.pool._write(machines)

    def purge_deleted(self, bot: str) -> None:
        """Remove a retired computer and its home disk, never a shared volume.

        Keep the pool lock through inspection/removal so allocation cannot
        race cleanup. List failures are errors, never evidence of absence.
        A missing container/volume makes retries after partial deletion safe.
        """
        from harness.bot_cleanup import job_path
        from harness.machine_secrets import MOUNT, directory_for_bot

        with self.pool._lock():
            if not job_path(self.paths, bot).exists():
                raise IsolationUnavailable("Missing deletion job")
            machines = self.pool.machines(strict=True)
            for machine in [m for m in machines if m.bot == bot]:
                if machine.state not in ("retired", "busy"):
                    raise IsolationUnavailable("Computer is not reserved for deletion")
                if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", machine.name):
                    raise IsolationUnavailable("Invalid machine name")
                result = engine.run("ps", "-a", "--format", "{{.Names}}")
                if result.returncode:
                    raise IsolationUnavailable("Cannot list computers for cleanup")
                if machine.name in result.stdout.splitlines():
                    inspected = engine.run("inspect", machine.name)
                    if inspected.returncode:
                        raise IsolationUnavailable("Cannot inspect deleted computer")
                    info = json.loads(inspected.stdout)[0]
                    labels = info.get("Config", {}).get("Labels") or {}
                    mounts = info.get("Mounts", [])
                    expected = str(directory_for_bot(self.paths, bot))
                    owned_mount = any(
                        m.get("Type") == "bind"
                        and m.get("Source") == expected
                        and m.get("Destination") == MOUNT
                        for m in mounts
                    )
                    owned_disk = any(
                        m.get("Type") == "volume"
                        and m.get("Name") == machine.volume
                        and m.get("Destination") == "/home/agent"
                        for m in mounts
                    )
                    if (
                        labels.get(OWNER_LABEL) != "1"
                        or labels.get(SECRET_BOT_LABEL) != bot
                        or not owned_mount
                        or not owned_disk
                        or info.get("State", {}).get("Running") is not False
                    ):
                        raise IsolationUnavailable("Deleted computer ownership/state changed")
                    if engine.run("rm", machine.name).returncode:
                        raise IsolationUnavailable("Could not remove deleted computer")
                volumes = engine.run("volume", "ls", "--format", "{{.Name}}")
                if volumes.returncode:
                    raise IsolationUnavailable("Cannot list disks for cleanup")
                if machine.volume in volumes.stdout.splitlines():
                    if engine.run("volume", "rm", machine.volume).returncode:
                        raise IsolationUnavailable("Could not remove deleted computer disk")
                self.paths.machine_dirty_file(machine.id).unlink(missing_ok=True)
                # Reuse the empty slot; removing rows would make len-based IDs collide.
                machine.state, machine.bot, machine.since = "idle", None, time.time()
                machine.last_bot = None
                self.pool._write(machines)

    def _container_state(self, name: str) -> str | None:
        """inspect state ('running', 'exited', ...) or None when absent."""
        proc = engine.run("inspect", "-f", "{{.State.Status}}", name)
        if proc.returncode != 0:
            return None
        return proc.stdout.strip().lower()

    def _container_health(self, name: str) -> str:
        """The engine's own health verdict: starting | healthy | unhealthy, or
        "" for an image that declares no HEALTHCHECK.

        Kept separate from `_container_state` because they answer different
        questions — "is the process alive" and "is the desktop up" — and
        conflating them is what let a machine with a dead Xvfb read as fine.
        """
        try:
            proc = engine.run(
                "inspect", "-f", "{{if .State.Health}}{{.State.Health.Status}}{{end}}", name
            )
        except IsolationUnavailable:
            return ""
        if proc.returncode != 0:
            return ""
        return proc.stdout.strip().lower()

    def _container_running(self, name: str) -> bool:
        try:
            if self._container_state(name) != "running":
                return False
        except IsolationUnavailable:
            return False
        # An image with no HEALTHCHECK reports "", and an image whose check has
        # not settled yet reports "starting". Neither is evidence of a problem,
        # so only an explicit "unhealthy" counts against a running container —
        # a machine must not be declared dead because it is still booting.
        return self._container_health(name) != "unhealthy"

    def _wait_display_ready(self, machine: Machine, *, timeout: float | None = None) -> bool:
        """Block until Docker reports the machine desktop healthy.

        Returns True when the display is up. Unknown health (no HEALTHCHECK,
        inspect failed) is treated as ready so spawn tests and hosts without
        a probe are not stalled. `starting` polls until `healthy` or timeout;
        `unhealthy` returns False and spawn still continues — computer tools
        then fail with display unavailable instead of looping.
        """
        if timeout is None:
            timeout = float(os.environ.get("HARNESS_MACHINE_DISPLAY_WAIT", "20"))
        health = self._container_health(machine.name)
        if health not in ("starting", "healthy", "unhealthy"):
            return True
        if health == "healthy":
            return True
        if health == "unhealthy":
            return False
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            time.sleep(0.4)
            health = self._container_health(machine.name)
            if health == "healthy":
                return True
            if health != "starting":
                return health == ""  # probe vanished: don't block forever
        return False

    def _image_id(self, ref: str, *, container: bool = False) -> str | None:
        args = (
            ("inspect", "-f", "{{.Image}}", ref)
            if container
            else ("image", "inspect", "-f", "{{.Id}}", ref)
        )
        proc = engine.run(*args)
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def _container_stale(self, name: str) -> bool:
        """True when the container was created from an image that has since
        been rebuilt. Unknown (engine hiccup, missing image) counts as fresh."""
        current = self._image_id(self.image)
        existing = self._image_id(name, container=True)
        return bool(current and existing and current != existing)

    def _container_labels(self, name: str) -> dict | None:
        """The container's labels, {} when unlabeled, None when absent."""
        proc = engine.run("inspect", "-f", "{{json .Config.Labels}}", name)
        if proc.returncode != 0:
            return None
        try:
            data = json.loads(proc.stdout.strip() or "null")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _secret_run_args(self, machine: Machine) -> list[str]:
        """Stage machine secrets as files; return the mount + path-env args.

        Every bot gets an initially empty private read-only directory so
        use_secret_file can supply a selected credential without a restart.
        Legacy $HARNESS_MACHINE_SECRETS names are also staged there. The env var each
        secret contributes carries only the in-machine file path — a secret
        VALUE never rides `-e` where `docker inspect` would show it.
        """
        wanted = [
            n.strip()
            for n in (os.environ.get("HARNESS_MACHINE_SECRETS") or "").split(",")
            if n.strip()
        ]
        from harness.machine_secrets import MOUNT, prepare_directory, stage
        from harness.secrets import get_secret

        owner = machine.bot or machine.name
        host_dir = prepare_directory(self.paths, owner)
        args: list[str] = []
        for name in wanted:
            if "/" in name or name.startswith("."):
                continue  # a secret name is a bare filename, never a path
            value = get_secret(name, self.paths)
            if value is None:
                continue
            stage(self.paths, owner, name, value)
            args += ["-e", f"{_secret_env_name(name)}={MACHINE_SECRETS_MOUNT}/{name}"]
        args += ["-v", f"{host_dir}:{MOUNT}:ro"]
        return args

    def _ensure_container(self, machine: Machine) -> None:
        state = self._container_state(machine.name)
        runtime_hash = runtime_fingerprint()
        if state is not None:
            labels = self._container_labels(machine.name)
            decision = identity_decision(labels, runtime_hash)
            if decision == "unowned":
                # Loud refusal, never rm: a same-named container without our
                # owner label belongs to someone else (or predates labeling).
                raise IsolationUnavailable(
                    f"container {machine.name!r} exists but does not carry the "
                    f"harness owner label {OWNER_LABEL}=1; refusing to touch it. "
                    "If it is a leftover you own, remove or rename it yourself "
                    f"(`docker rm {machine.name}` keeps the {machine.volume} volume) "
                    "and retry."
                )
            drifted = (
                decision == "recreate"
                or labels.get(SECRET_BOT_LABEL) != (machine.bot or machine.name)
                or (state != "running" and self._container_stale(machine.name))
            )
            if drifted:
                # Schema/runtime-hash drift, or the image was rebuilt under a
                # stopped container (which would keep booting the OLD baked-in
                # desktop forever). Recreate on current content — the machine's
                # identity is its /home/agent volume, which `rm` does not touch.
                if state == "running":
                    stop_t = _int_env("HARNESS_MACHINE_STOP_TIMEOUT", 30)
                    engine.run("stop", "-t", str(stop_t), machine.name, timeout=stop_t + 30)
                if engine.run("rm", machine.name).returncode == 0:
                    state = None
                else:
                    raise IsolationUnavailable("Could not replace a stale bot machine safely")
        if state is not None:
            if state != "running":
                proc = engine.run("start", machine.name)
                if proc.returncode != 0:
                    raise IsolationUnavailable(
                        f"could not start machine {machine.name}: "
                        f"{proc.stderr.strip() or proc.stdout.strip()}"
                    )
            return
        run_args = [
            "run",
            "-d",
            "--name",
            machine.name,
            # content-addressed identity: owner + schema + runtime hash. The
            # next spawn inspects these and recreates on drift (volume kept).
            "--label",
            f"{OWNER_LABEL}=1",
            "--label",
            f"{SCHEMA_LABEL}={MACHINE_SCHEMA_VERSION}",
            "--label",
            f"{RUNTIME_LABEL}={runtime_hash}",
            "--label",
            f"{WORKSPACE_LABEL}={'1' if shared_workspace_enabled() else '0'}",
            "--label",
            f"{SECRET_BOT_LABEL}={machine.bot or machine.name}",
            # the machine's own persistent disk; the harness home (credentials,
            # serve.json, bus) must stay unreachable
            "-v",
            f"{machine.volume}:{MACHINE_HOME}",
        ]
        if shared_workspace_enabled():
            # the one project volume every machine on this home shares — the
            # handoff surface (module docstring). A named volume, not a host
            # path, so the jail's uid owns it and no $HARNESS_HOME dir leaks in.
            run_args += ["-v", f"{workspace_volume()}:{MACHINE_WORKSPACE}"]
        else:
            # the image still ships a writable /workspace dir; tell the
            # supervisor it is private so its boot log says so
            run_args += ["-e", "HARNESS_MACHINE_WORKSPACE=0"]
        run_args += [
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            # default docker seccomp kills Chromium (SIGTRAP) even with
            # --no-sandbox; the jail is still cap-drop ALL + no-new-privs
            "--security-opt",
            "seccomp=unconfined",
            "--pids-limit",
            str(_int_env("HARNESS_MACHINE_PIDS", 512)),
        ]
        if os.environ.get("HARNESS_MACHINE_MEMORY"):
            run_args += ["--memory", os.environ["HARNESS_MACHINE_MEMORY"]]
        if os.environ.get("HARNESS_MACHINE_CPUS"):
            run_args += ["--cpus", os.environ["HARNESS_MACHINE_CPUS"]]
        if os.environ.get("HARNESS_MACHINE_GEOMETRY"):
            run_args += ["-e", f"HARNESS_MACHINE_GEOMETRY={os.environ['HARNESS_MACHINE_GEOMETRY']}"]
        # dock launch-chrome reads this at exec time; always pass 0|1 so a
        # host default-on is explicit inside the jail too
        from harness import cdp

        run_args += ["-e", f"HARNESS_CHROME_CDP={'1' if cdp.enabled() else '0'}"]
        # secrets ride as read-only files, never as -e values (module docstring)
        run_args += self._secret_run_args(machine)
        run_args += [self.image]
        proc = engine.run(*run_args)
        if proc.returncode != 0:
            raise IsolationUnavailable(
                f"machine spawn for {machine.name!r} failed (is the daemon running and "
                f"image {self.image!r} built? build it with `docker build -t "
                f"{_DEFAULT_IMAGE} -f deploy/Dockerfile.machine .`, or use "
                f"`--backend process`): {proc.stderr.strip() or proc.stdout.strip()}"
            )

    # -- state sync (host-mediated docker cp tar streams) ------------------
    def _machine_tar(self, machine_name: str) -> Path:
        """Snapshot the machine home as a local tar (cache-excluded).

        `docker cp /home/agent` packs Chrome Cache (multi-GB, hundreds of
        thousands of files). state_sync then walks every member to exclude
        them. `tar --exclude` inside the machine never puts that noise on
        the host; members still start with `agent/` so strip_components=1
        matches the docker-cp layout clone-down still uses.
        """
        self.paths.run.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.paths.run, prefix=".machine-home-", suffix=".tar")
        # --sparse: a hole-only file inside the machine costs nothing there
        # and must not be expanded into gigabytes of zeros on the host.
        argv = ["exec", machine_name, "tar", "-C", "/home", "-cf", "-", "--sparse"]
        for pat in _TAR_EXCLUDES:
            argv += ["--exclude", pat]
        argv.append("agent")
        try:
            with os.fdopen(fd, "wb") as out:
                proc = engine.run_raw(argv, stdout=out, preexec_fn=_tar_output_limit())
            if proc.returncode != 0:
                raise IsolationUnavailable(
                    f"tar out of {machine_name} failed: "
                    f"{(proc.stderr or b'').decode(errors='replace').strip()}"
                )
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return Path(tmp)

    def _sync_session_start(self, machine: Machine) -> None:
        """Salvage a crashed session's writes (dirty marker), then clone-down."""
        canonical = self.paths.canonical_home()
        canonical.mkdir(parents=True, exist_ok=True)
        dirty = self.paths.machine_dirty_file(machine.id)
        with state_sync.canonical_lock(self.paths):
            tar_path = self._machine_tar(machine.name)
            try:
                with open(tar_path, "rb") as fh:
                    machine_index = state_sync.index_tar(fh, strip_components=1)
                if dirty.is_file():
                    canonical_index = state_sync.index_local(canonical)
                    up = state_sync.plan_merge(machine_index, canonical_index)
                    with open(tar_path, "rb") as fh:
                        result = state_sync.extract_winners(fh, up, canonical, strip_components=1)
                    result["walk_complete"] = canonical_index.complete
                    result["walk_failed"] = len(canonical_index.failed)
                    state_sync.write_receipt(self.paths, machine.name, "salvage", result)
            finally:
                tar_path.unlink(missing_ok=True)
            down = state_sync.plan_merge(state_sync.index_local(canonical), machine_index)
            if down:
                with tempfile.TemporaryFile() as buf:
                    state_sync.pack_tar(canonical, down, buf, owner=MACHINE_OWNER)
                    buf.seek(0)
                    proc = engine.run_raw(["cp", "-", f"{machine.name}:{MACHINE_HOME}"], stdin=buf)
                if proc.returncode != 0:
                    raise IsolationUnavailable(
                        f"cp into {machine.name} failed: "
                        f"{(proc.stderr or b'').decode(errors='replace').strip()}"
                    )
        self._chown_home(machine)
        dirty.write_text("1", encoding="utf-8")

    def _chown_home(self, machine: Machine) -> None:
        """docker cp extracts as root; the jail drops CHOWN, so fix from a helper.

        Chrome SIGTRAPs if ~/.config/harness-chrome is not owned by uid 1000.
        """
        uid, gid = MACHINE_OWNER
        engine.run(
            "run",
            "--rm",
            "--user",
            "0",
            "-v",
            f"{machine.volume}:/data",
            "--entrypoint",
            "chown",
            self.image,
            "-R",
            f"{uid}:{gid}",
            "/data",
        )

    def _sync_up(self, machine_name: str, *, receipt: str = "up", live: bool = False) -> dict:
        """Merge the machine home up into the canonical store.

        SQLite groups (base + `-journal`/`-wal`/`-shm`) are excluded from the
        per-file merge. Quiesced (`live=False`: stop/salvage, Chrome down)
        they copy as whole groups out of the single tar snapshot; live
        (`live=True`: interval flush) each group is staged from the snapshot
        and only a sqlite3-backup-verified copy lands in the canonical store
        — a torn capture is skipped and recorded, never installed.
        """
        canonical = self.paths.canonical_home()
        canonical.mkdir(parents=True, exist_ok=True)
        with state_sync.canonical_lock(self.paths):
            tar_path = self._machine_tar(machine_name)
            try:
                with open(tar_path, "rb") as fh:
                    machine_index = state_sync.index_tar(fh, strip_components=1)
                canonical_index = state_sync.index_local(canonical)
                rels = state_sync.plan_merge(machine_index, canonical_index)
                plain, groups = self._sqlite_groups(rels, machine_index, canonical_index)
                with open(tar_path, "rb") as fh:
                    result = state_sync.extract_winners(fh, plain, canonical, strip_components=1)
                if live:
                    result["sqlite"] = self._capture_live_groups(
                        tar_path, groups, machine_index, canonical
                    )
                else:
                    result["sqlite"] = self._install_quiesced_groups(
                        tar_path, groups, machine_index, canonical
                    )
            finally:
                tar_path.unlink(missing_ok=True)
        result["walk_complete"] = canonical_index.complete
        result["walk_failed"] = len(canonical_index.failed)
        # Files above the per-file cap never entered the plan; say so in the
        # receipt rather than letting a missing download look like a bug.
        result["oversized"] = list(machine_index.oversized)
        state_sync.write_receipt(self.paths, machine_name, receipt, result)
        return result

    def _capture_live_groups(
        self, tar_path: Path, groups: list[list[str]], machine_index, canonical: Path
    ) -> dict:
        """Consistent capture of live SQLite groups via stdlib sqlite3 backup.

        Each group is staged out of the tar snapshot into a private dir,
        recovered/verified by state_sync.sqlite_backup, and the clean main
        file installed with the machine base's mtime (newest-wins stays
        stable). Groups that will not capture consistently are skipped with
        the reason in the receipt; sidecars are never installed live, and
        nothing is ever deleted.
        """
        out: dict = {"groups": len(groups), "backed_up": 0, "skipped": []}
        if not groups:
            return out
        with tempfile.TemporaryDirectory(dir=self.paths.run, prefix=".sqlite-stage-") as staged_dir:
            self._extract_groups(tar_path, groups, Path(staged_dir))
            for group in groups:
                base = group[0]
                staged_base = Path(staged_dir) / base
                if not staged_base.is_file():
                    out["skipped"].append(f"{base}: missing from snapshot")
                    continue
                clean = staged_base.with_name(staged_base.name + ".clean")
                reason = state_sync.sqlite_backup(staged_base, clean)
                if reason is not None:
                    out["skipped"].append(f"{base}: {reason}")
                    continue
                skip = state_sync.sqlite_skip_reason(
                    clean, canonical / base, machine_index[base][0]
                )
                if skip is not None:
                    out["skipped"].append(f"{base}: {skip}")
                    continue
                state_sync.place_file(clean, canonical / base, machine_index[base][0])
                out["backed_up"] += 1
        return out

    def _install_quiesced_groups(
        self, tar_path: Path, groups: list[list[str]], machine_index, canonical: Path
    ) -> dict:
        """Copy quiesced SQLite groups as units, keeping the richer dest.

        Chrome-down snapshots are coherent, so the group (base + journal/WAL)
        copies as files. Row-count rank still applies: an idle machine that
        stopped after a signed-in peer must not install fewer logins.
        """
        out: dict = {"groups": len(groups), "skipped": []}
        if not groups:
            return out
        with tempfile.TemporaryDirectory(dir=self.paths.run, prefix=".sqlite-stage-") as staged_dir:
            staged = Path(staged_dir)
            self._extract_groups(tar_path, groups, staged)
            for group in groups:
                base = group[0]
                dest = canonical / base
                staged_base = staged / base
                if not staged_base.is_file():
                    out["skipped"].append(f"{base}: missing from snapshot")
                    continue
                # Count via a duplicate: sqlite3 replay deletes the live
                # -journal next to the original, and we still need that
                # sibling on the quiesced copy.
                count_base = staged_base.with_name(staged_base.name + ".count")
                shutil.copy2(staged_base, count_base)
                for suffix in ("-journal", "-wal", "-shm"):
                    sib = Path(str(staged_base) + suffix)
                    if sib.is_file():
                        shutil.copy2(sib, Path(str(count_base) + suffix))
                compare = count_base
                clean = count_base.with_name(count_base.name + ".clean")
                if state_sync.sqlite_backup(count_base, clean) is None:
                    compare = clean
                skip = state_sync.sqlite_skip_reason(compare, dest, machine_index[base][0])
                if skip is not None:
                    out["skipped"].append(f"{base}: {skip}")
                    continue
                for rel in group:
                    src = staged / rel
                    if src.is_file():
                        state_sync.place_file(src, canonical / rel, machine_index[rel][0])
        return out

    def _sqlite_groups(self, planned_rels, machine_index, canonical_index):
        """Merge-plan SQLite groups plus Chrome auth DBs that mtime hid."""
        plain, groups = state_sync.split_sqlite_groups(planned_rels, machine_index, canonical_index)
        have = {group[0] for group in groups}
        extra = [
            rel
            for rel in machine_index
            if Path(rel).name in _CHROME_AUTH_DBS
            and rel not in have
            and state_sync.is_sqlite_base(rel, machine_index, canonical_index)
        ]
        if extra:
            _, more = state_sync.split_sqlite_groups(extra, machine_index, canonical_index)
            groups.extend(more)
        return plain, groups

    def _extract_groups(self, tar_path: Path, groups: list[list[str]], dest: Path) -> None:
        rels = [rel for group in groups for rel in group]
        if not rels:
            return
        with open(tar_path, "rb") as fh:
            state_sync.extract_winners(fh, rels, dest, strip_components=1)

    def _flush_running_peers(self, bot: str) -> None:
        """Live merge-up from every OTHER busy machine before a clone-down.

        The interval flush (harness.server.start_machine_flush) keeps the
        canonical store *roughly* fresh; this makes a spawn deterministic: a
        freshly started bot clones down the logins the live fleet holds right
        now (grokbot's "new bot opens Chrome already signed in"), not state
        frozen at the last stop or timer tick. Best-effort per peer — Chrome
        is live over there, so SQLite groups go through the verified backup
        path, and a peer that will not snapshot never blocks this spawn.
        """
        for m in self.pool.machines():
            if m.state != "busy" or m.bot in (None, bot):
                continue
            if not self.paths.machine_dirty_file(m.id).is_file():
                continue  # nothing un-synced on that machine
            if not self._container_running(m.name):
                continue  # down peer: its dirty state salvages on next start
            try:
                self._sync_up(m.name, receipt="peer-flush", live=True)
            except (IsolationUnavailable, subprocess.SubprocessError):
                continue

    def _quiesce_chrome(self, machine_name: str) -> None:
        """Stop Chrome inside the machine so the final sync sees a settled profile."""
        engine.run("exec", machine_name, "pkill", "-f", "chrom")
        deadline = time.time() + _int_env("HARNESS_MACHINE_QUIESCE_TIMEOUT", 15)
        while time.time() < deadline:
            proc = engine.run("exec", machine_name, "pgrep", "-f", "chrom")
            if proc.returncode != 0:
                return
            time.sleep(0.5)

    def _purge_machine_junk(self, machine_name: str) -> None:
        """Drop regenerable Chrome/cache trees from the machine volume.

        Called after `_sync_up` (Chrome already pkilled). Excluded dirs are
        not durable state; leaving them makes every volume 4–5 GB of dead
        weight. Best-effort: a wedged exec must not block stop/release.
        """
        names = list(state_sync.JUNK_DIR_NAMES)
        script = (
            "import os, shutil\n"
            "from pathlib import Path\n"
            f"NAMES={names!r}\n"
            "root=Path('/home/agent')\n"
            "if not root.is_dir(): raise SystemExit(0)\n"
            "for rel in ('.cache', '.local/share/Trash'):\n"
            "    p=root/rel\n"
            "    shutil.rmtree(p, ignore_errors=True)\n"
            "for dirpath, dirnames, _files in os.walk(root, topdown=True):\n"
            "    keep=[]\n"
            "    for name in dirnames:\n"
            "        if name in NAMES:\n"
            "            shutil.rmtree(os.path.join(dirpath, name), ignore_errors=True)\n"
            "        else:\n"
            "            keep.append(name)\n"
            "    dirnames[:]=keep\n"
        )
        try:
            engine.run("exec", "-u", "0", machine_name, "python3", "-c", script)
        except (IsolationUnavailable, subprocess.SubprocessError):
            pass

    # -- agent host process (process-backend mechanics + machine env) ------
    def _spawn_agent(self, bot: str, argv: list[str], machine: Machine) -> tuple[int, str]:
        """Spawn the host-side brain; returns (pid, generation token).

        The token travels BOTH as env and argv so the child can refuse a
        mangled spawn, and so this backend can later verify a recorded pid
        still is that child before signaling it (see process_identity).
        """
        self.paths.run.mkdir(parents=True, exist_ok=True)
        log_fh = open(self.paths.log_file(bot), "a", encoding="utf-8")  # noqa: SIM115
        token = mint_generation_token()
        env = os.environ.copy()
        env["HARNESS_MACHINE_NAME"] = machine.name
        # own display per machine: a takeover of one bot must not pause others
        env["HARNESS_SHARED_DISPLAY"] = "0"
        # the same switch that decided the mount: an opted-out machine's bot
        # must not be told /workspace is shared (agent.runtime reads this)
        env["HARNESS_MACHINE_WORKSPACE"] = "1" if shared_workspace_enabled() else "0"
        env[GENERATION_TOKEN_ENV] = token
        proc = subprocess.Popen(
            [sys.executable, *argv, token_argument(token)],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
        return proc.pid, token

    def _agent_identity_ok(self, pid: int | None, token: str | None) -> bool:
        """Is the recorded pid still OUR spawned agent?

        Records without a token (written by an older harness) verify by
        liveness alone; with a token the live /proc cmdline must carry it.
        """
        if not pid:
            return False
        if not token:
            return _pid_alive(pid)
        return verify_process_token(pid, token, alive=_pid_alive)

    def _stop_agent(self, pid: int) -> None:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        deadline = time.time() + stop_grace()
        while time.time() < deadline:
            if not _pid_alive(pid):
                return
            time.sleep(0.1)
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    # -- run-file persistence ---------------------------------------------
    def _record(self, handle: BotHandle) -> None:
        self.paths.run.mkdir(parents=True, exist_ok=True)
        data = {
            "bot": handle.bot,
            "backend": handle.backend,
            "pid": handle.pid,
            "machine": handle.meta.get("machine"),
            "machine_id": handle.meta.get("machine_id"),
            # Generation token minted at spawn (see process_identity): pid
            # verification before any signal, adoption across restarts.
            "token": handle.meta.get("token"),
            # Code version the agent was spawned with (see process.py._record).
            "version": handle.meta.get("version", __version__),
            "started": handle.meta.get("started", time.time()),
        }
        write_atomic(self.paths.run_file(handle.bot), json.dumps(data))

    def load(self, bot: str) -> BotHandle | None:
        rf = self.paths.run_file(bot)
        if not rf.is_file():
            return None
        try:
            data = json.loads(rf.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        handle = BotHandle(
            bot=bot,
            backend=self.id,
            pid=data.get("pid"),
            meta={
                "machine": data.get("machine"),
                "machine_id": data.get("machine_id"),
                "token": data.get("token"),
                "version": data.get("version"),
                "started": data.get("started"),
            },
        )
        handle.status = self.status(handle)
        return handle

    # -- lifecycle ---------------------------------------------------------
    def spawn(self, bot: str, argv: list[str]) -> BotHandle:
        # Adopt-if-busy: on a harness restart a bot whose agent process is
        # alive AND identity-verified (status folds in the generation-token
        # cmdline check) keeps running untouched — its machine may hold
        # in-flight work, and stop/respawn would tear that down for nothing.
        existing = self.load(bot)
        if existing and existing.status == Status.RUNNING:
            return existing

        state_sync.seed_canonical_from_jar(self.paths)
        machine = self.pool.acquire(bot, self._container_running)
        self._ensure_container(machine)
        # Freshness before the clone-down: pull the live fleet's latest
        # Chrome logins into the canonical store so this bot starts current.
        self._flush_running_peers(bot)
        self._sync_session_start(machine)
        pid, token = self._spawn_agent(bot, argv, machine)
        handle = BotHandle(
            bot=bot,
            backend=self.id,
            pid=pid,
            status=Status.RUNNING,
            meta={"machine": machine.name, "machine_id": machine.id, "token": token},
        )
        self._record(handle)
        # Display boot is async. Record and return as soon as the agent
        # process is up so Restart/API are not blocked for ~20s (clients
        # otherwise time out and keep showing Needs restart). Computer
        # tools still fail closed until X is up.
        threading.Thread(
            target=self._wait_display_ready,
            args=(machine,),
            name=f"display-ready-{machine.name}",
            daemon=True,
        ).start()
        return handle

    def status(self, handle: BotHandle) -> Status:
        if not handle.pid:
            return Status.STOPPED
        if not _pid_alive(handle.pid):
            return Status.DEAD
        token = handle.meta.get("token")
        if token and not self._agent_identity_ok(handle.pid, token):
            # something alive wears our recorded pid but not our token: the
            # pid was recycled — our agent is gone.
            return Status.DEAD
        machine = handle.meta.get("machine")
        if machine:
            try:
                if self._container_state(machine) != "running":
                    return Status.DEAD  # agent alive but its computer is gone
            except IsolationUnavailable:
                return Status.UNKNOWN
        return Status.RUNNING

    def stop(self, handle: BotHandle) -> None:
        if handle.pid:
            token = handle.meta.get("token")
            if token and _pid_alive(handle.pid) and not self._agent_identity_ok(handle.pid, token):
                # Recorded pid is alive but its cmdline no longer carries our
                # generation token: the pid was recycled by an innocent
                # process. NEVER signal it; just retire the stale record.
                print(
                    f"machines: refusing to signal pid {handle.pid} for bot "
                    f"{handle.bot!r}: process identity does not match the "
                    "recorded generation token (pid recycled?)",
                    file=sys.stderr,
                )
            else:
                self._stop_agent(handle.pid)
        machine_name = handle.meta.get("machine")
        if machine_name:
            machine_id = int(handle.meta.get("machine_id") or 0)
            try:
                self._quiesce_chrome(machine_name)
                self._sync_up(machine_name)
                self._purge_machine_junk(machine_name)
                self.paths.machine_dirty_file(machine_id).unlink(missing_ok=True)
            except (IsolationUnavailable, subprocess.SubprocessError):
                # engine gone or wedged (timeout): dirty marker stays; next
                # start salvages. Never skip the release/unlink below — a
                # stale run file keeps routing the user's input to a machine
                # that is no longer this bot's.
                pass
            stop_t = _int_env("HARNESS_MACHINE_STOP_TIMEOUT", 30)
            try:
                # stop, never rm: the machine and its disk persist
                engine.run("stop", "-t", str(stop_t), machine_name, timeout=stop_t + 30)
            except (IsolationUnavailable, subprocess.TimeoutExpired):
                pass
        self.pool.release(handle.bot)
        rf = self.paths.run_file(handle.bot)
        if rf.is_file():
            rf.unlink()

    def close_browser(self, bot: str) -> bool:
        """Quiesce Chrome inside a live machine whose bot has gone idle
        (harness/browser_idle.py). Merges the machine up first so the logins
        Chrome wrote reach the canonical store before the process dies; the
        bot's next computer_open relaunches on the same profile. True when a
        browser was running and is now gone; False when there was nothing to
        close or the machine is not running."""
        handle = self.load(bot)
        if not handle or handle.status != Status.RUNNING:
            return False
        machine_name = handle.meta.get("machine")
        if not machine_name:
            return False
        probe = engine.run("exec", machine_name, "pgrep", "-f", "chrom")
        if probe.returncode != 0:
            return False
        try:
            self._sync_up(machine_name, receipt="idle-close", live=True)
        except (IsolationUnavailable, subprocess.SubprocessError):
            pass  # the dirty marker stays; the next flush/stop syncs
        self._quiesce_chrome(machine_name)
        return True

    def flush(self, bot: str) -> dict | None:
        """Periodic merge-up for a live machine (no quiesce; end-of-session
        sync remains the authoritative one).

        Skips machines that have no dirty marker — they either never started
        a session or already synced on stop. Chrome is live here, so SQLite
        groups go through the sqlite3-backup capture path (`live=True`), not
        plain file copies.
        """
        handle = self.load(bot)
        if not handle or handle.status != Status.RUNNING:
            return None
        machine_name = handle.meta.get("machine")
        if not machine_name:
            return None
        machine_id = handle.meta.get("machine_id")
        if machine_id is not None and not self.paths.machine_dirty_file(int(machine_id)).is_file():
            return None
        return self._sync_up(machine_name, receipt="flush", live=True)
