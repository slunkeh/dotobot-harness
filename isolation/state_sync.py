"""Canonical machine-state sync: clone-down on start, merge-up on stop.

Every bot machine (machines backend) is a sandboxed computer whose user home
is a *replica* of the canonical store `machine-state/default/home/`. A session
starts by merging the canonical tree down into the machine and ends by merging
the machine's home back up, so a Chrome login on machine A shows up on machine
B without two live Chromes ever sharing a profile directory (this retires the
shared-jar hazard in machine mode). Between those ends, live machines
also merge up mid-session — on the serve loop's interval flush and, so spawns
are deterministic rather than timer-lucky, whenever a new machine is about to
clone down (peer flush in machines.py).

Merge semantics (both directions): per-file newest-mtime-wins, size difference
breaking an exact mtime tie. Deletions never propagate — a deleted file
resurrects from the canonical store until tombstones land (documented in the
ADR). SQLite groups get one extra rule on top of that: a destination that
already holds *more rows* is kept (idle Chrome touches Cookies and bumps
mtime without adding logins; newest-mtime-wins would otherwise install the
poorer copy). Two safety rails harden the file rule:

* **Incomplete walks are never authoritative.** A directory that fails to
  list (permissions, or a race with a stopping container) marks its
  `TreeIndex` incomplete and records the failed path; `plan_merge` then
  refuses to treat absence under a failed directory as evidence of anything —
  no copy into unknown territory, and (should tombstones ever land) never a
  delete derived from an incomplete walk.
* **SQLite databases move as groups, never as torn per-file copies.** A base
  file (detected by live `-journal`/`-wal`/`-shm` siblings, a `.db`/
  `.sqlite`/`.sqlite3` extension, or the "SQLite format 3" header magic
  gathered during indexing) only ever travels together with its siblings from
  one snapshot; a sidecar never travels alone. Live machines additionally go
  through `sqlite_backup` (stage the group, replay its journal, verify) —
  see machines.py. Stale sidecars on the destination still cannot be removed
  (deletions never propagate); the backup path sidesteps them by installing
  a self-contained, checkpointed main file.

Everything here is pure trees + tar streams (stdlib only, no engine calls):
the machines backend moves bytes with `docker cp`, which speaks tar on stdio,
so this module indexes/packs/extracts tars and never needs the daemon. That
keeps it fully testable without docker.
"""

from __future__ import annotations

import contextlib
import fcntl
import fnmatch
import io
import json
import os
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path

from harness.paths import HarnessPaths


class TreeIndex(dict):
    """rel -> (mtime_ns, size), plus walk-health and content bookkeeping.

    * `complete`/`failed`: `complete` flips False when a directory could not
      be listed (PermissionError, or a race with a stopping container);
      `failed` records those rel dirs. An incomplete index is still safe to
      copy *from* (an unseen file just waits for the next sync), but absence
      in it proves nothing: nothing may ever read a file's absence from an
      incomplete walk as a deletion or as license to overwrite/prune.
    * `sqlite`: rels whose first bytes carried the SQLite header magic,
      gathered while indexing (16 bytes per file — cheap for both local
      trees and tar streams).
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.complete: bool = True
        self.failed: list[str] = []
        #: rels left out of the index because they exceed the per-file cap
        #: (see max_file_bytes) — listed in the receipt, never merged.
        self.oversized: list[str] = []
        self.sqlite: set[str] = set()

# Regenerable Chrome / cache directory *names*. Merge-up's GNU tar --exclude
# matches these as last path components; clone-down's fnmatch patterns are
# derived from the same set so a polluted canonical cannot re-infect a
# machine. Nothing in the harness reads these trees — logins live in the
# auth SQLite DBs + Local Storage / IndexedDB, which are not named here.
JUNK_DIR_NAMES: tuple[str, ...] = (
    "Cache",
    "Code Cache",
    "GPUCache",
    "ShaderCache",
    "GrShaderCache",
    "Crashpad",
    "Crash Reports",
    "CacheStorage",
    "ScriptCache",
    "Safe Browsing",
    "BrowserMetrics",
    "DeferredBrowserMetrics",
    "optimization_guide_model_store",
    "component_crx_cache",
    "WasmTtsEngine",
    "google-chrome",
)

# Machine-local noise that must never enter (or leave) the canonical store:
# browser locks/caches, crash dumps, trash, X cookies, the container-local
# throwaway harness home, sockets. Clone-down indexes through this list.
#: Per-file ceiling for machine <-> host state sync (see max_file_bytes).
DEFAULT_MAX_FILE_BYTES = 1 << 30
DEFAULT_EXCLUDES: tuple[str, ...] = (
    "**/Singleton*",
    # Service-worker *caches* stay local; the registrations themselves (the
    # rest of "Service Worker/", plus Local Storage / IndexedDB) do sync —
    # they carry web-app auth state (same split grokbot ships).
    "**/Service Worker/CacheStorage/**",
    "**/Service Worker/ScriptCache/**",
    ".cache/**",
    ".local/share/Trash/**",
    ".Xauthority",
    ".harness-local/**",
    "**/*.sock",
    *tuple(f"**/{name}/**" for name in JUNK_DIR_NAMES),
)

_SQLITE_SIBLINGS = ("-journal", "-wal", "-shm")
_SQLITE_EXTENSIONS = (".db", ".sqlite", ".sqlite3")
SQLITE_MAGIC = b"SQLite format 3\x00"


def _rmtree(path: Path) -> int:
    """Unlink a file or tree. Returns bytes removed (best-effort size)."""
    if not path.exists() and not path.is_symlink():
        return 0
    size = 0
    try:
        if path.is_dir() and not path.is_symlink():
            for dirpath, _dirnames, filenames in os.walk(path):
                for name in filenames:
                    fp = Path(dirpath) / name
                    with contextlib.suppress(OSError):
                        size += fp.stat().st_size
            shutil.rmtree(path, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                size = path.stat().st_size
            path.unlink()
    except OSError:
        return size
    return size


def purge_junk(root: Path) -> int:
    """Remove regenerable Chrome/cache trees under a machine home.

    Returns approximate bytes unlinked. Deletions never propagate through
    merge — this is the only path that actually drops junk from a volume or
    the canonical store. Safe to run on a live tree: auth DBs and Local
    Storage are not in JUNK_DIR_NAMES.
    """
    if not root.is_dir():
        return 0
    removed = 0
    for rel in (".cache", ".local/share/Trash"):
        removed += _rmtree(root / rel)
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        keep: list[str] = []
        for name in dirnames:
            if name in JUNK_DIR_NAMES:
                removed += _rmtree(Path(dirpath) / name)
            else:
                keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            path = Path(dirpath) / name
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if excluded(rel):
                removed += _rmtree(path)
    return removed


def sweep_canonical_store(paths: HarnessPaths) -> int:
    """Purge junk from every `machine-state/<id>/home` tree (serve start)."""
    root = paths.machine_state
    if not root.is_dir():
        return 0
    removed = 0
    for home in sorted(root.glob("*/home")):
        removed += purge_junk(home)
    return removed


def excluded(rel: str, excludes: Iterable[str] = DEFAULT_EXCLUDES) -> bool:
    """True when a posix relpath matches an exclude pattern.

    fnmatch has no `**`; its `*` already crosses `/`, so a `**/x` pattern is
    additionally tried without the prefix to cover top-level entries.
    """
    for pat in excludes:
        if fnmatch.fnmatch(rel, pat):
            return True
        if pat.startswith("**/") and fnmatch.fnmatch(rel, pat[3:]):
            return True
    return False


def index_local(
    root: Path, excludes: Iterable[str] = DEFAULT_EXCLUDES, *, walk=os.walk
) -> TreeIndex:
    """Index the regular files under `root` (symlinks/specials are skipped —
    the canonical store holds real files only).

    A directory that fails to list marks the index incomplete and records the
    failed rel path instead of silently vanishing its subtree — plan_merge
    then treats everything under it as unknown, not absent. `walk` is
    injectable for tests (permission failures are hard to fake as root).
    """
    index = TreeIndex()
    if not root.is_dir():
        return index

    def _listing_failed(exc: OSError) -> None:
        try:
            rel = Path(exc.filename or root).relative_to(root).as_posix()
        except ValueError:
            rel = "."
        index.complete = False
        index.failed.append(rel)

    for dirpath, _dirnames, filenames in walk(root, onerror=_listing_failed):
        for fname in filenames:
            path = Path(dirpath) / fname
            rel = path.relative_to(root).as_posix()
            if excluded(rel, excludes):
                continue
            try:
                st = path.lstat()
            except OSError:  # deleted mid-walk (Chrome does this)
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            index[rel] = (st.st_mtime_ns, st.st_size)
            if st.st_size >= len(SQLITE_MAGIC) and _sibling_base(rel) is None:
                with contextlib.suppress(OSError):
                    with open(path, "rb") as fh:
                        if fh.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC:
                            index.sqlite.add(rel)
    return index


def _strip(name: str, strip_components: int) -> str | None:
    """Normalize a tar member name to a rel path, or None to skip it."""
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if ".." in parts or name.startswith("/"):
        return None  # traversal attempt: never index or extract it
    parts = parts[strip_components:]
    if not parts:
        return None
    return "/".join(parts)


def max_file_bytes() -> int:
    """Largest file the state sync will carry between a machine and the host.

    Anything a bot can create inside its machine is tarred to the host and
    then copied into the canonical store and every sibling machine, so one
    `truncate -s 2T` (or a genuinely huge download) would fill the host disk
    on every flush. `HARNESS_MACHINE_SYNC_MAX_FILE` (bytes) overrides the
    default of 1 GiB; 0 disables the cap.
    """
    raw = os.environ.get("HARNESS_MACHINE_SYNC_MAX_FILE", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return DEFAULT_MAX_FILE_BYTES


def index_tar(
    fileobj,
    *,
    strip_components: int = 0,
    excludes: Iterable[str] = DEFAULT_EXCLUDES,
    max_bytes: int | None = None,
) -> TreeIndex:
    """Index a tar byte stream (e.g. `docker cp <c>:/home/agent -` output).

    `strip_components=1` drops the leading `agent/` that docker cp adds.
    PAX tars carry sub-second mtimes; they are normalized to ns. The first
    bytes of each member are sniffed in-stream for the SQLite header magic
    (a machine-side Chrome DB has no telltale extension). A truncated stream
    raises — a partial machine snapshot must abort the sync (dirty marker
    keeps it salvageable), never masquerade as a complete tree.
    """
    cap = max_file_bytes() if max_bytes is None else max_bytes
    index = TreeIndex()
    with tarfile.open(fileobj=fileobj, mode="r|*") as tar:
        for member in tar:
            if not member.isreg():
                continue
            rel = _strip(member.name, strip_components)
            if rel is None or excluded(rel, excludes):
                continue
            if cap and member.size > cap:
                # Skipped here so plan_merge never ranks it: a sparse file
                # reports its logical size and would be materialised in full
                # on extraction.
                index.oversized.append(rel)
                continue
            index[rel] = (int(member.mtime * 1_000_000_000), member.size)
            if member.size >= len(SQLITE_MAGIC) and _sibling_base(rel) is None:
                src = tar.extractfile(member)
                if src is not None and src.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC:
                    index.sqlite.add(rel)
    return index


def _under_failed(rel: str, failed: Iterable[str]) -> bool:
    """True when `rel` sits inside a directory that failed to list."""
    return any(f == "." or rel == f or rel.startswith(f + "/") for f in failed)


def plan_merge(src_index: TreeIndex, dst_index: TreeIndex) -> list[str]:
    """Rels to copy src -> dst: dst missing, src newer, or size differs at an
    exact mtime tie. Never plans a delete.

    SQLite bases also select when src is *larger* than dst even if older —
    Chrome logins grow Cookies/Login Data; an idle profile's newer but
    smaller file must not hide the richer one from the installer. The
    installer (machines.py) then keeps the copy with more rows.

    Incomplete-walk guard: when the dst walk failed to list a directory, a
    file's absence under it is unknown — not evidence — so nothing is copied
    there until a complete walk can see it (a plain incomplete flag with no
    recorded paths blinds the whole tree, conservatively).

    SQLite siblings only ever travel with their base file, ordered right
    after it; a sidecar is never selected on its own — a lone `-journal`/
    `-wal` copy is exactly the torn pair this module exists to avoid.
    """
    dst_failed = tuple(getattr(dst_index, "failed", ()))
    dst_blind = not getattr(dst_index, "complete", True) and not dst_failed
    selected = []
    for rel in sorted(src_index):
        if _sibling_base(rel) is not None:
            continue  # rides with its base or stays put
        mtime, size = src_index[rel]
        dst = dst_index.get(rel)
        if dst is None:
            if dst_blind or _under_failed(rel, dst_failed):
                continue  # incomplete dst walk: absence proves nothing
            selected.append(rel)
        elif mtime > dst[0] or (mtime == dst[0] and size != dst[1]):
            selected.append(rel)
        elif size > dst[1] and is_sqlite_base(rel, src_index, dst_index):
            selected.append(rel)  # older but larger SQLite: installer ranks by rows
    return _order_with_siblings(selected, src_index)


def _sibling_base(rel: str) -> str | None:
    for suffix in _SQLITE_SIBLINGS:
        if rel.endswith(suffix):
            return rel[: -len(suffix)]
    return None


def _order_with_siblings(selected: list[str], src_index: TreeIndex) -> list[str]:
    chosen = set(selected)
    for rel in selected:
        if _sibling_base(rel) is None:  # a base file drags its live siblings
            for suffix in _SQLITE_SIBLINGS:
                if rel + suffix in src_index:
                    chosen.add(rel + suffix)
    ordered: list[str] = []
    for rel in sorted(chosen):
        base = _sibling_base(rel)
        if base is not None and base in chosen:
            continue  # emitted right after its base below
        ordered.append(rel)
        if base is None:
            for suffix in _SQLITE_SIBLINGS:
                if rel + suffix in chosen:
                    ordered.append(rel + suffix)
    return ordered


def is_sqlite_base(rel: str, *indexes: TreeIndex) -> bool:
    """True when `rel` looks like a SQLite database (not a sidecar): a live
    `-journal`/`-wal`/`-shm` sibling in any index, a SQLite extension, or the
    header magic sniffed while indexing."""
    if _sibling_base(rel) is not None:
        return False
    if rel.endswith(_SQLITE_EXTENSIONS):
        return True
    for index in indexes:
        if rel in getattr(index, "sqlite", ()):
            return True
        if any(rel + suffix in index for suffix in _SQLITE_SIBLINGS):
            return True
    return False


def split_sqlite_groups(
    rels: Iterable[str], src_index: TreeIndex, dst_index: TreeIndex = ()
) -> tuple[list[str], list[list[str]]]:
    """Partition a merge plan into (plain rels, SQLite groups).

    Each group is `[base, *siblings present in src_index]` — the unit that
    must be captured atomically. Newest-mtime-wins file copies of a live
    database are unsafe even sibling-grouped, so callers exclude groups from
    the per-file merge and capture them whole (quiesced) or via
    `sqlite_backup` (live). Order within the plan is preserved.
    """
    plain: list[str] = []
    groups: list[list[str]] = []
    grouped: set[str] = set()
    for rel in rels:
        if rel in grouped:
            continue  # a sibling already claimed by its base's group
        if is_sqlite_base(rel, src_index, dst_index):
            group = [rel] + [rel + s for s in _SQLITE_SIBLINGS if rel + s in src_index]
            grouped.update(group)
            groups.append(group)
        else:
            plain.append(rel)
    return plain, groups


def sqlite_row_count(path: Path) -> int | None:
    """Total rows across user tables, or None if `path` will not open as SQLite.

    Used to rank Chrome auth DBs (Cookies / Login Data): an idle profile that
    only bumped mtime must not replace a copy that still holds more logins.
    Table names are identifier-safe before interpolation; anything else is
    skipped rather than guessed.
    """
    try:
        with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as con:
            names = [
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            total = 0
            for name in names:
                if not isinstance(name, str) or not name.replace("_", "").isalnum():
                    continue
                total += con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            return total
    except sqlite3.Error:
        return None


def sqlite_skip_reason(new_db: Path, dest: Path, new_mtime_ns: int) -> str | None:
    """Why `new_db` must not replace `dest`, or None to install.

    Richer (more rows) always wins — that is the same 'deletions never
    propagate' rule applied to Chrome logins. Equal row counts fall back to
    newest-mtime-wins. A dest that will not open as SQLite is treated as
    missing so a verified backup can repair it.
    """
    if not dest.is_file():
        return None
    new_n = sqlite_row_count(new_db)
    old_n = sqlite_row_count(dest)
    if new_n is not None and old_n is not None:
        if new_n < old_n:
            return f"fewer rows ({new_n} < {old_n})"
        if new_n > old_n:
            return None
    try:
        old_mtime = dest.stat().st_mtime_ns
    except OSError:
        return None
    if new_mtime_ns < old_mtime:
        return "same rows, older mtime"
    return None


def sqlite_backup(staged: Path, out: Path) -> str | None:
    """Produce a consistent single-file snapshot of a staged SQLite database.

    Opens the staged copy (stdlib sqlite3 replays any `-journal`/`-wal`
    captured next to it), streams it into `out` via Connection.backup, and
    integrity-checks the result. Returns None on success, else a short reason
    — a torn capture fails here instead of poisoning the destination. The
    staged copy must be private (it is opened read-write for recovery).
    """
    try:
        with contextlib.closing(sqlite3.connect(staged)) as src:
            with contextlib.closing(sqlite3.connect(out)) as dst:
                src.backup(dst)
                row = dst.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            out.unlink(missing_ok=True)
            return f"integrity_check failed: {row[0] if row else 'no result'}"
        return None
    except sqlite3.Error as exc:
        out.unlink(missing_ok=True)
        return f"{type(exc).__name__}: {exc}"


def place_file(src: Path, target: Path, mtime_ns: int) -> None:
    """Install `src` at `target` via temp sibling + os.replace (the same
    no-torn-reader discipline as extract_winners), stamping `mtime_ns`."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.sync-")
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            while chunk := inp.read(1 << 20):
                out.write(chunk)
        os.utime(tmp_name, ns=(mtime_ns, mtime_ns))
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def pack_tar(
    root: Path,
    rels: Iterable[str],
    fileobj,
    *,
    owner: tuple[int, int] | None = None,
) -> dict:
    """Write a tar of `rels` (paths relative to `root`) into `fileobj`.

    PAX format keeps ns mtimes so newest-wins stays stable across transfers.
    `owner=(uid, gid)` stamps every member (docker cp preserves tar ownership,
    so copy-ins must carry the in-machine user, not the harness host user).
    Files that vanish mid-pack are recorded, not fatal.
    """
    result = {"packed": 0, "errors": []}

    def _stamp(info: tarfile.TarInfo) -> tarfile.TarInfo:
        if owner is not None:
            info.uid, info.gid = owner
            info.uname = info.gname = ""
        return info

    with tarfile.open(fileobj=fileobj, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel in rels:
            try:
                tar.add(root / rel, arcname=rel, recursive=False, filter=_stamp)
                result["packed"] += 1
            except (FileNotFoundError, OSError) as exc:
                result["errors"].append(f"{rel}: {exc}")
    return result


def extract_winners(
    fileobj,
    rels: Iterable[str],
    dst: Path,
    *,
    strip_components: int = 0,
) -> dict:
    """Extract exactly `rels` from a tar stream into `dst`, preserving mtimes.

    Member names are re-validated (no absolute paths, no `..`) so a hostile
    tar cannot escape `dst`. Each file lands via a temp sibling + os.replace
    so readers never observe a half-written file.
    """
    wanted = set(rels)
    result = {"copied": 0, "errors": []}
    with tarfile.open(fileobj=fileobj, mode="r|*") as tar:
        for member in tar:
            if not member.isreg():
                continue
            rel = _strip(member.name, strip_components)
            if rel is None or rel not in wanted:
                continue
            target = dst / rel
            try:
                src = tar.extractfile(member)
                if src is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(
                    dir=target.parent, prefix=f".{target.name}.sync-"
                )
                try:
                    with os.fdopen(fd, "wb") as out:
                        while chunk := src.read(1 << 20):
                            out.write(chunk)
                    mtime_ns = int(member.mtime * 1_000_000_000)
                    os.utime(tmp_name, ns=(mtime_ns, mtime_ns))
                    os.replace(tmp_name, target)
                except BaseException:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                    raise
                result["copied"] += 1
            except (OSError, tarfile.TarError) as exc:
                result["errors"].append(f"{rel}: {exc}")
    return result


class canonical_lock:
    """flock over the canonical store — one clone/merge at a time per user.

    Single-host bind mounts only, where flock is reliable (the machines
    backend runs on the engine host by construction).
    """

    def __init__(self, paths: HarnessPaths, user: str = "default") -> None:
        self._path = paths.machine_state / user / ".lock"
        self._fh = None

    def __enter__(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "w", encoding="utf-8")  # noqa: SIM115
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


def write_receipt(paths: HarnessPaths, machine: str, direction: str, result: dict) -> Path:
    """Append-friendly per-sync receipt under machine-state/default/.meta."""
    meta = paths.machine_meta()
    meta.mkdir(parents=True, exist_ok=True)
    receipt = meta / f"{machine}.last-sync.json"
    payload = {"machine": machine, "direction": direction, "at": time.time(), **result}
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    return receipt


# -- one-time migration from the process-backend cookie jar -----------------

_SEED_FILES = [
    "Cookies",
    "Cookies-journal",
    "Login Data",
    "Login Data-journal",
    "Login Data For Account",
    "Login Data For Account-journal",
    "Web Data",
    "Web Data-journal",
]
_SEED_MARKER = ".seeded-from-jar"


def seed_canonical_from_jar(paths: HarnessPaths) -> bool:
    """Copy existing shared-jar logins into the canonical Chrome profile once.

    Existing `browser/cookies/` logins (process backend) carry over to machine
    mode; afterwards the two worlds are parallel — the jar keeps serving the
    process backend, machines only see the canonical store.
    """
    from agent.browser import MACHINE_CHROME_DIR

    marker = paths.machine_state / "default" / _SEED_MARKER
    if marker.exists():
        return False
    profile = paths.canonical_home() / MACHINE_CHROME_DIR / "Default"
    seeded = 0
    for name in _SEED_FILES:
        src = paths.browser_cookies / name
        dst = profile / name
        if not src.is_file() or src.is_symlink() or dst.exists():
            continue
        profile.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        dst.write_bytes(data)
        st = src.stat()
        os.utime(dst, ns=(st.st_mtime_ns, st.st_mtime_ns))
        seeded += 1
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"at": time.time(), "files": seeded}), encoding="utf-8")
    return seeded > 0


def pack_to_bytes(root: Path, rels: Iterable[str]) -> bytes:
    """Convenience for small transfers/tests: pack_tar into memory."""
    buf = io.BytesIO()
    pack_tar(root, rels, buf)
    return buf.getvalue()
