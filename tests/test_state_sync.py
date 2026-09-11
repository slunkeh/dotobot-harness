"""Canonical machine-state sync engine: pure trees + tar streams, no docker."""

from __future__ import annotations

import io
import os
import sqlite3
import tarfile
from pathlib import Path

from harness.paths import HarnessPaths
from isolation.state_sync import (
    DEFAULT_EXCLUDES,
    JUNK_DIR_NAMES,
    SQLITE_MAGIC,
    TreeIndex,
    canonical_lock,
    excluded,
    extract_winners,
    index_local,
    index_tar,
    is_sqlite_base,
    pack_to_bytes,
    plan_merge,
    purge_junk,
    seed_canonical_from_jar,
    split_sqlite_groups,
    sqlite_backup,
    sqlite_row_count,
    sqlite_skip_reason,
    sweep_canonical_store,
    write_receipt,
)


def _paths(tmp_path) -> HarnessPaths:
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas"])
    return p


def _write(root, rel: str, text: str, mtime_ns: int | None = None):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


S = 1_000_000_000  # one second in ns


# -- excludes ---------------------------------------------------------------


def test_excludes_cover_top_level_and_nested_noise():
    noisy = [
        "SingletonLock",  # top level, no leading dir
        ".config/harness-chrome/SingletonSocket",
        ".config/harness-chrome/Default/Cache/f_0001",
        ".config/harness-chrome/Default/Code Cache/js/x",
        ".cache/fontconfig/x.cache",
        ".local/share/Trash/files/old.txt",
        ".Xauthority",
        ".harness-local/desktop/tint2rc",
        "tmp/app.sock",
        ".config/harness-chrome/Default/Service Worker/CacheStorage/a/b/f_0",
        ".config/harness-chrome/Default/Service Worker/ScriptCache/index",
        ".config/harness-chrome/Safe Browsing/chrome_url_hashes.store",
        ".config/harness-chrome/BrowserMetrics/BrowserMetrics-1.pma",
        ".config/harness-chrome/DeferredBrowserMetrics/x",
        ".config/harness-chrome/optimization_guide_model_store/a",
        ".config/harness-chrome/component_crx_cache/a",
        ".config/harness-chrome/WasmTtsEngine/a",
        ".config/google-chrome/Safe Browsing/x",
    ]
    for rel in noisy:
        assert excluded(rel, DEFAULT_EXCLUDES), rel
    kept = [
        ".config/harness-chrome/Default/Cookies",
        ".config/harness-chrome/Default/Login Data",
        ".config/harness-chrome/Default/Login Data For Account",
        ".config/harness-chrome/Default/Local Storage/leveldb/000003.log",
        ".config/harness-chrome/Default/Service Worker/Database/000003.log",
        ".config/harness-chrome/Local State",
        "Desktop/notes.txt",
        "Downloads/report.pdf",
    ]
    for rel in kept:
        assert not excluded(rel, DEFAULT_EXCLUDES), rel


def test_clone_down_excludes_match_merge_up_junk_dirs():
    """A polluted canonical must not re-infect machines (the 72G incident)."""
    from isolation.machines import _TAR_EXCLUDES

    for name in JUNK_DIR_NAMES:
        assert name in _TAR_EXCLUDES, name
        assert excluded(f".config/harness-chrome/{name}/x", DEFAULT_EXCLUDES), name


def test_purge_junk_drops_chrome_noise_keeps_logins(tmp_path):
    root = tmp_path / "home"
    cookies = root / ".config/harness-chrome/Default/Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"sqlite")
    notes = root / "Desktop/notes.txt"
    notes.parent.mkdir(parents=True)
    notes.write_text("keep", encoding="utf-8")
    junk = [
        root / ".config/harness-chrome/Safe Browsing/store",
        root / ".config/harness-chrome/BrowserMetrics/x.pma",
        root / ".config/google-chrome/Safe Browsing/y",
        root / ".cache/fontconfig/z",
        root / ".config/harness-chrome/Default/Cache/f_0001",
    ]
    for path in junk:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"noise" * 100)
    removed = purge_junk(root)
    assert removed > 0
    assert cookies.is_file()
    assert notes.is_file()
    for path in junk:
        assert not path.exists(), path


def test_sweep_canonical_store_walks_every_home(tmp_path):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    default = paths.canonical_home()
    other = paths.machine_state / "other" / "home"
    for home in (default, other):
        target = home / ".config/harness-chrome/Safe Browsing/store"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"x" * 50)
        login = home / ".config/harness-chrome/Default/Login Data"
        login.parent.mkdir(parents=True, exist_ok=True)
        login.write_bytes(b"keep")
    assert sweep_canonical_store(paths) > 0
    assert not (default / ".config/harness-chrome/Safe Browsing").exists()
    assert not (other / ".config/harness-chrome/Safe Browsing").exists()
    assert (default / ".config/harness-chrome/Default/Login Data").is_file()
    assert (other / ".config/harness-chrome/Default/Login Data").is_file()


# -- indexing ---------------------------------------------------------------


def test_index_local_skips_excluded_and_symlinks(tmp_path):
    root = tmp_path / "home"
    _write(root, "Desktop/a.txt", "a")
    _write(root, "SingletonLock", "x")
    (root / "link.txt").symlink_to(root / "Desktop" / "a.txt")

    index = index_local(root)
    assert "Desktop/a.txt" in index
    assert "SingletonLock" not in index
    assert "link.txt" not in index


def test_index_tar_strips_components_and_guards_traversal(tmp_path):
    root = tmp_path / "agent"
    _write(root, "Desktop/a.txt", "hello", mtime_ns=42 * S)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        tar.add(root, arcname="agent")
        evil = tarfile.TarInfo(name="../escape.txt")
        evil.size = 0
        tar.addfile(evil, io.BytesIO(b""))
    buf.seek(0)

    index = index_tar(buf, strip_components=1)
    assert index["Desktop/a.txt"][0] == 42 * S
    assert not any("escape" in rel for rel in index)


# -- incomplete walks ---------------------------------------------


def _flaky_walk(bad: str):
    """os.walk lookalike that fails to list the `bad` subdirectory (running
    as root makes a real chmod-000 dir happily listable, so inject it)."""

    def walk(top, onerror=None):
        for dirpath, dirnames, filenames in os.walk(top, onerror=onerror):
            if bad in dirnames:
                dirnames.remove(bad)
                onerror(PermissionError(13, "Permission denied", str(Path(dirpath) / bad)))
            yield dirpath, dirnames, filenames

    return walk


def test_index_local_marks_walk_incomplete_when_a_dir_fails_to_list(tmp_path):
    root = tmp_path / "home"
    _write(root, "Desktop/a.txt", "a")
    _write(root, "locked/hidden.txt", "h")

    index = index_local(root, walk=_flaky_walk("locked"))
    assert "Desktop/a.txt" in index
    assert "locked/hidden.txt" not in index
    assert index.complete is False
    assert index.failed == ["locked"]

    # the same tree on a clean walk is complete
    clean = index_local(root)
    assert clean.complete is True and clean.failed == []
    assert "locked/hidden.txt" in clean


def test_plan_merge_never_reads_absence_from_an_incomplete_walk():
    src = {"Desktop/a.txt": (20 * S, 1), "locked/db.txt": (20 * S, 1)}
    dst = TreeIndex({"Desktop/a.txt": (10 * S, 1), "only-dst.txt": (9 * S, 2)})
    dst.complete = False
    dst.failed = ["locked"]

    rels = plan_merge(src, dst)
    assert "Desktop/a.txt" in rels  # files the walk DID see still merge
    assert "locked/db.txt" not in rels  # unknown territory: absence proves nothing
    assert "only-dst.txt" not in rels  # and never, ever a delete

    # incomplete with no localized failure: every absence is untrustworthy,
    # but mtime comparisons of seen files stay valid
    blind = TreeIndex({"Desktop/a.txt": (10 * S, 1)})
    blind.complete = False
    assert plan_merge(src, blind) == ["Desktop/a.txt"]


def test_plan_merge_with_complete_walk_still_fills_gaps():
    src = {"locked/db.txt": (20 * S, 1)}
    assert plan_merge(src, TreeIndex()) == ["locked/db.txt"]


# -- merge planning ---------------------------------------------------------


def test_plan_merge_newest_wins_and_never_deletes():
    src = {"a.txt": (20 * S, 5), "b.txt": (10 * S, 3), "only-src.txt": (5 * S, 1)}
    dst = {"a.txt": (10 * S, 5), "b.txt": (30 * S, 3), "only-dst.txt": (9 * S, 2)}
    rels = plan_merge(src, dst)
    assert "a.txt" in rels  # src newer
    assert "b.txt" not in rels  # dst newer: LWW keeps it
    assert "only-src.txt" in rels  # dst missing
    assert "only-dst.txt" not in rels  # never a delete


def test_plan_merge_size_breaks_exact_mtime_tie():
    src = {"a.txt": (10 * S, 9)}
    assert plan_merge(src, {"a.txt": (10 * S, 5)}) == ["a.txt"]
    assert plan_merge(src, {"a.txt": (10 * S, 9)}) == []


def test_plan_merge_groups_sqlite_siblings_after_base():
    src = {
        "p/Cookies": (20 * S, 10),
        "p/Cookies-journal": (5 * S, 1),  # older than dst but rides along
        "aaa.txt": (20 * S, 1),
        "zzz.txt": (20 * S, 1),
    }
    dst = {"p/Cookies": (10 * S, 10), "p/Cookies-journal": (10 * S, 1)}
    rels = plan_merge(src, dst)
    assert rels.index("p/Cookies-journal") == rels.index("p/Cookies") + 1
    assert set(rels) == {"aaa.txt", "p/Cookies", "p/Cookies-journal", "zzz.txt"}


def test_plan_merge_selects_older_larger_sqlite():
    """Idle Chrome bumps mtime on a smaller Cookies file; the older richer
    copy must still enter the merge plan so the installer can rank by rows."""
    src = TreeIndex(
        {"p/Cookies": (10 * S, 80), "p/Cookies-journal": (10 * S, 0)}
    )
    src.sqlite = {"p/Cookies"}
    dst = TreeIndex({"p/Cookies": (20 * S, 40)})
    dst.sqlite = {"p/Cookies"}
    rels = plan_merge(src, dst)
    assert "p/Cookies" in rels
    assert rels.index("p/Cookies-journal") == rels.index("p/Cookies") + 1


def test_plan_merge_never_ships_a_lone_sqlite_sidecar():
    """A -journal/-wal ahead of its base is exactly the torn pair to avoid:
    sidecars only ever ride with a selected base."""
    src = {"p/Cookies": (10 * S, 5), "p/Cookies-journal": (30 * S, 1)}
    dst = {"p/Cookies": (10 * S, 5), "p/Cookies-journal": (10 * S, 1)}
    assert plan_merge(src, dst) == []


# -- sqlite groups ------------------------------------------------


def _sqlite_db(path: Path, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (v TEXT)")
    con.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    con.commit()
    con.close()


def test_indexes_sniff_sqlite_header_magic(tmp_path):
    root = tmp_path / "home"
    _sqlite_db(root / "p" / "Cookies", ["x"])  # Chrome DBs have no extension
    _write(root, "p/notes.txt", "not a database, just long enough to sniff")

    index = index_local(root)
    assert index.sqlite == {"p/Cookies"}

    tar_index = index_tar(
        io.BytesIO(pack_to_bytes(root, ["p/Cookies", "p/notes.txt"])),
    )
    assert tar_index.sqlite == {"p/Cookies"}
    assert "p/notes.txt" in tar_index


def test_split_sqlite_groups_excludes_db_groups_from_plain_merge():
    src = TreeIndex(
        {
            "p/Cookies": (20 * S, 4096),  # magic-detected (below)
            "p/Cookies-journal": (20 * S, 12),
            "notes.db": (20 * S, 512),  # extension-detected
            "Desktop/a.txt": (20 * S, 1),
        }
    )
    src.sqlite = {"p/Cookies"}
    plan = plan_merge(src, TreeIndex())
    plain, groups = split_sqlite_groups(plan, src, TreeIndex())

    assert plain == ["Desktop/a.txt"]
    assert ["p/Cookies", "p/Cookies-journal"] in groups
    assert ["notes.db"] in groups
    assert not is_sqlite_base("p/Cookies-journal", src)  # a sidecar is not a base


def test_split_sqlite_groups_detects_by_live_sibling_alone():
    """No extension, no magic recorded: a -wal next to the file is proof
    enough to keep it out of the per-file copy path."""
    src = {"History": (20 * S, 4096), "History-wal": (20 * S, 100)}
    plain, groups = split_sqlite_groups(plan_merge(src, {}), src, {})
    assert plain == []
    assert groups == [["History", "History-wal"]]


def test_sqlite_skip_reason_keeps_the_richer_copy(tmp_path):
    rich = tmp_path / "rich.db"
    poor = tmp_path / "poor.db"
    _sqlite_db(rich, ["a", "b", "c"])
    _sqlite_db(poor, ["x"])
    os.utime(rich, ns=(1 * S, 1 * S))
    os.utime(poor, ns=(9 * S, 9 * S))
    assert sqlite_row_count(rich) == 3
    assert sqlite_row_count(poor) == 1
    assert sqlite_skip_reason(poor, rich, 9 * S) == "fewer rows (1 < 3)"
    assert sqlite_skip_reason(rich, poor, 1 * S) is None  # more rows, even if older
    missing = tmp_path / "missing.db"
    assert sqlite_skip_reason(rich, missing, 1 * S) is None


def test_sqlite_backup_of_a_live_db_is_consistent(tmp_path):
    db = tmp_path / "live.db"
    _sqlite_db(db, ["a", "b", "c"])
    writer = sqlite3.connect(db)
    writer.execute("INSERT INTO t VALUES ('uncommitted')")  # open write txn

    out = tmp_path / "snap.db"
    assert sqlite_backup(db, out) is None
    writer.rollback()
    writer.close()

    snap = sqlite3.connect(out)
    assert snap.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert snap.execute("SELECT count(*) FROM t").fetchone()[0] == 3
    snap.close()


def test_sqlite_backup_rejects_a_torn_capture(tmp_path):
    torn = tmp_path / "torn.db"
    torn.write_bytes(SQLITE_MAGIC + b"\xff" * 4096)  # right magic, garbage body

    out = tmp_path / "snap.db"
    reason = sqlite_backup(torn, out)
    assert reason is not None
    assert not out.exists()  # a failed capture never leaves a file behind


# -- pack / extract round trip ----------------------------------------------


def test_pack_extract_round_trip_preserves_content_and_mtime(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _write(src, "Desktop/a.txt", "payload", mtime_ns=1234 * S + 500)
    data = pack_to_bytes(src, ["Desktop/a.txt"])

    result = extract_winners(io.BytesIO(data), ["Desktop/a.txt"], dst)
    assert result["copied"] == 1
    out = dst / "Desktop" / "a.txt"
    assert out.read_text(encoding="utf-8") == "payload"
    assert out.stat().st_mtime_ns == 1234 * S + 500  # PAX keeps ns for LWW


def test_extract_only_takes_requested_rels(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _write(src, "keep.txt", "k")
    _write(src, "skip.txt", "s")
    data = pack_to_bytes(src, ["keep.txt", "skip.txt"])

    extract_winners(io.BytesIO(data), ["keep.txt"], dst)
    assert (dst / "keep.txt").is_file()
    assert not (dst / "skip.txt").exists()


def test_extract_rejects_traversal_members(tmp_path):
    dst = tmp_path / "dst"
    dst.mkdir()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        evil = tarfile.TarInfo(name="../../outside.txt")
        evil.size = 4
        tar.addfile(evil, io.BytesIO(b"pwnd"))
    buf.seek(0)

    result = extract_winners(buf, ["../../outside.txt"], dst)
    assert result["copied"] == 0
    assert not (tmp_path / "outside.txt").exists()


def test_full_merge_cycle_converges_two_replicas(tmp_path):
    """machine A writes -> canonical -> machine B sees it (the login story)."""
    canonical = tmp_path / "canonical"
    machine_a = tmp_path / "a"
    machine_b = tmp_path / "b"
    _write(machine_a, ".config/harness-chrome/Default/Cookies", "login=1", mtime_ns=100 * S)
    _write(machine_b, ".config/harness-chrome/Default/Cookies", "stale", mtime_ns=50 * S)

    # sync-up from A
    up = plan_merge(index_local(machine_a), index_local(canonical))
    extract_winners(io.BytesIO(pack_to_bytes(machine_a, up)), up, canonical)
    # sync-down into B
    down = plan_merge(index_local(canonical), index_local(machine_b))
    extract_winners(io.BytesIO(pack_to_bytes(canonical, down)), down, machine_b)

    cookie = machine_b / ".config/harness-chrome/Default/Cookies"
    assert cookie.read_text(encoding="utf-8") == "login=1"


# -- lock, receipt, seed ----------------------------------------------------


def test_canonical_lock_creates_lock_file(tmp_path):
    paths = _paths(tmp_path)
    with canonical_lock(paths):
        assert (paths.machine_state / "default" / ".lock").exists()


def test_write_receipt(tmp_path):
    paths = _paths(tmp_path)
    receipt = write_receipt(paths, "harness-machine-0", "up", {"copied": 3, "errors": []})
    assert receipt.is_file()
    assert '"direction": "up"' in receipt.read_text(encoding="utf-8")


def test_seed_canonical_from_jar_once(tmp_path):
    paths = _paths(tmp_path)
    (paths.browser_cookies / "Cookies").write_text("jar-login", encoding="utf-8")

    assert seed_canonical_from_jar(paths) is True
    seeded = paths.canonical_home() / ".config/harness-chrome/Default/Cookies"
    assert seeded.read_text(encoding="utf-8") == "jar-login"

    # marker guards a re-run even after the jar changes
    (paths.browser_cookies / "Cookies").write_text("newer-jar", encoding="utf-8")
    assert seed_canonical_from_jar(paths) is False
    assert seeded.read_text(encoding="utf-8") == "jar-login"


def test_seed_ignores_jar_symlinks(tmp_path):
    paths = _paths(tmp_path)
    real = paths.home / "elsewhere"
    real.write_text("x", encoding="utf-8")
    (paths.browser_cookies / "Cookies").symlink_to(real)
    seed_canonical_from_jar(paths)
    assert not (paths.canonical_home() / ".config/harness-chrome/Default/Cookies").exists()
