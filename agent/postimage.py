"""Copy an image into chat's durable store (`workspace/uploads`) for `post_image`.

Stdlib only, same posture as `unfurl.py`: http(s) only, a hard timeout, byte
caps, and content sniffing — a response that is not actually an image never
lands in uploads. Remote images are always copied here rather than hotlinked:
`workspace/uploads` is the one folder `GET /api/uploads/<name>` serves and the
one with no retention sweep, so a chat reference into it keeps rendering for
as long as the history does. Large images are re-encoded down for chat with
ImageMagick when available (the `compress_for_model` pattern in
`harness/screen.py`); without it they pass through untouched up to the hard
cap.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import subprocess
import urllib.error
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from harness import netguard
from harness.paths import HarnessPaths

#: Chrome/curl downloads inside a bot machine land here (jail home).
MACHINE_HOME = "/home/agent"
MACHINE_DOWNLOADS = f"{MACHINE_HOME}/Downloads"
#: the project directory every machine shares (isolation.machines.MACHINE_WORKSPACE)
MACHINE_WORKSPACE = "/workspace"
#: in-machine roots a bot may post_image from
_JAIL_ROOTS = (MACHINE_HOME, MACHINE_WORKSPACE)
_JAIL_CAT_TIMEOUT = 15.0
_MD_IMG = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_SAFE_BASE = re.compile(r"[^A-Za-z0-9._-]+")

#: fetch cap: past this the download is abandoned, not truncated.
MAX_DOWNLOAD_BYTES = 15_000_000
#: hard cap on what lands in uploads (matches the vision-frame cap).
MAX_STORED_BYTES = 6_000_000
#: above this we re-encode for chat when ImageMagick is available.
RECODE_THRESHOLD = 2_000_000
_RECODE_MAX_W = 1600
_RECODE_MAX_H = 1600
_RECODE_QUALITY = 82
_FETCH_TIMEOUT = 15.0
#: Total wall-clock budget for one download (the timeout above is per
#: socket operation and never fires on a server that trickles bytes).
_FETCH_DEADLINE = 45.0
_CONVERT_TIMEOUT = 20.0
#: ImageMagick resource ceilings for the host-side recode. The bytes are
#: attacker-shaped (a page the bot visited, a file in its jail); a PNG whose
#: header declares 40000x40000 would otherwise ask convert for gigabytes of
#: pixel cache on the harness host before the timeout killed it.
_CONVERT_LIMITS = (
    ("memory", "256MiB"),
    ("map", "512MiB"),
    ("disk", "512MiB"),
    ("area", "64MP"),
    ("width", "16000"),
    ("height", "16000"),
    ("time", str(int(_CONVERT_TIMEOUT))),
)
#: Input coder pinned from the sniffed mime so convert never re-sniffs.
_CODER_BY_MIME = {"image/png": "png", "image/jpeg": "jpeg", "image/webp": "webp"}
_UA = "Mozilla/5.0 (compatible; dotobot image fetch)"

_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}


def sniff_mime(data: bytes) -> str | None:
    """Image mime from magic bytes (png/jpeg/gif/webp), or None."""
    if len(data) < 12:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def fetch(url: str, *, timeout: float = _FETCH_TIMEOUT) -> bytes | str:
    """Download an image URL; bytes on success, an error string otherwise."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"error: post_image needs an http(s) URL or a local file path, got {url!r}"
    # Host-side fetch of a model-supplied URL: public destinations only,
    # every redirect hop re-checked, and a wall-clock deadline. This is also
    # the seam `rewrite_chat_images` reaches for `![x](http://...)` markdown
    # in a reply, which never passes through a tool call.
    try:
        data = netguard.fetch_bytes(
            url,
            timeout=timeout,
            max_bytes=MAX_DOWNLOAD_BYTES + 1,
            deadline=_FETCH_DEADLINE,
            headers={"User-Agent": _UA},
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"error: could not fetch {url}: {exc}"
    if len(data) > MAX_DOWNLOAD_BYTES:
        return f"error: {url} is larger than {MAX_DOWNLOAD_BYTES // 1_000_000} MB"
    return data


def recode_for_chat(data: bytes, mime: str) -> tuple[bytes, str]:
    """(possibly re-encoded bytes, mime) sized for chat.

    Only images past RECODE_THRESHOLD are touched, and GIFs never are (a
    re-encode would keep one frame of an animation). Needs ImageMagick's
    `convert`; without it the original passes through and the caller's hard
    cap decides.
    """
    if len(data) <= RECODE_THRESHOLD or mime == "image/gif":
        return data, mime
    convert = shutil.which("convert")
    if not convert:
        return data, mime
    coder = _CODER_BY_MIME.get(mime)
    if coder is None:
        return data, mime
    limits = [flag for name, value in _CONVERT_LIMITS for flag in ("-limit", name, value)]
    try:
        proc = subprocess.run(
            [
                convert,
                *limits,
                f"{coder}:-",
                "-resize",
                f"{_RECODE_MAX_W}x{_RECODE_MAX_H}>",
                "-quality",
                str(_RECODE_QUALITY),
                "jpeg:-",
            ],
            input=data,
            capture_output=True,
            timeout=_CONVERT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return data, mime
    if proc.returncode == 0 and proc.stdout[:2] == b"\xff\xd8" and len(proc.stdout) < len(data):
        return proc.stdout, "image/jpeg"
    return data, mime


def stage_upload(paths: HarnessPaths, data: bytes, mime: str, label: str = "") -> Path:
    """Write image bytes into uploads and return the stored path.

    Same naming shape as the server's POST /api/upload (`<uuid8>-<name>`), so
    the folder reads uniformly and basenames never collide.
    """
    ext = _EXT_BY_MIME.get(mime, "png")
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", label).strip("-")[:40] or "image"
    paths.uploads.mkdir(parents=True, exist_ok=True)
    path = paths.uploads / f"{uuid.uuid4().hex[:8]}-{slug}.{ext}"
    path.write_bytes(data)
    return path


def label_from_source(source: str) -> str:
    """A filename-ish label from a URL or path, for the stored name."""
    if source.startswith(("http://", "https://")):
        stem = Path(urlsplit(source).path).stem
    else:
        stem = Path(source).stem
    return stem


def safe_basename(name: str) -> str:
    """The GET /api/uploads/<name> filter: alphanumerics, dot, underscore, hyphen."""
    return _SAFE_BASE.sub("", os.path.basename(name)).strip(".")


def jail_path(path: str) -> str | None:
    """Absolute in-machine path under /home/agent or /workspace, or None."""
    raw = (path or "").strip()
    root = next((r for r in _JAIL_ROOTS if raw.startswith(r)), None)
    if root is None:
        return None
    norm = posixpath.normpath(raw)
    if norm != root and not norm.startswith(root + "/"):
        return None
    if ".." in Path(norm).parts:
        return None
    return norm


def machine_names(paths: HarnessPaths) -> list[str]:
    """Container names recorded for live machine-backend bots."""
    run = paths.run
    if not run.is_dir():
        return []
    out: list[str] = []
    for rf in run.glob("*.json"):
        try:
            data = json.loads(rf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("backend") == "machines" and data.get("machine"):
            out.append(str(data["machine"]))
    return out


def read_machine_file(machine: str, path: str) -> bytes | None:
    """Bytes of an in-jail file, or None. `path` must already be a jail_path()."""
    from harness.machine_view import exec_prefix

    # Bound the capture inside the exec: `capture_output` buffers everything
    # the child emits in the host process, so a `cat` of a multi-GB (or
    # sparse) jail file was held in host memory before the cap below ever
    # looked at it. `head -c cap+1` keeps the "> cap means too large" answer
    # while never shipping more than cap+1 bytes out of the machine.
    try:
        proc = subprocess.run(
            [*exec_prefix(machine), "head", "-c", str(MAX_DOWNLOAD_BYTES + 1), "--", path],
            capture_output=True,
            timeout=_JAIL_CAT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    if len(proc.stdout) > MAX_DOWNLOAD_BYTES:
        return None
    return proc.stdout


def promote_upload(paths: HarnessPaths, basename: str) -> Path | None:
    """Copy a missing uploads basename in from workspace or a machine Downloads.

    Chat tiles fetch GET /api/uploads/<basename>. Bots that curl into the jail
    (`/home/agent/Downloads/foo.jpg`) and paste that path in markdown 404
    unless we copy the file here under the same basename.
    """
    safe = safe_basename(basename)
    if not safe:
        return None
    paths.uploads.mkdir(parents=True, exist_ok=True)
    dest = paths.uploads / safe
    if dest.is_file():
        return dest
    workspace_hit = paths.workspace / safe
    if workspace_hit.is_file() and workspace_hit.resolve() != dest.resolve():
        dest.write_bytes(workspace_hit.read_bytes())
        return dest
    jail = f"{MACHINE_DOWNLOADS}/{safe}"
    for machine in machine_names(paths):
        data = read_machine_file(machine, jail)
        if not data:
            continue
        dest.write_bytes(data)
        return dest
    return None


def materialize(paths: HarnessPaths, source: str, *, machine: str | None = None) -> Path | str:
    """Copy `source` into uploads. Path on success, error string otherwise."""
    source = (source or "").strip()
    if not source:
        return "error: post_image needs 'source' (an http(s) URL or a local file path)"
    if not source.startswith(("http://", "https://")):
        # Already in uploads (including a jail Downloads file we promoted
        # under the same basename) — do not copy again.
        base = safe_basename(source)
        if base:
            hit = paths.uploads / base
            if hit.is_file():
                return hit
    if source.startswith(("http://", "https://")):
        fetched = fetch(source)
        if isinstance(fetched, str):
            return fetched
        data = fetched
    else:
        data = None
        host = Path(source).expanduser()
        if not host.is_absolute():
            host = paths.workspace / host
        try:
            resolved = host.resolve()
        except OSError:
            resolved = None
        allowed = (paths.workspace.resolve(), paths.screenshots.resolve())
        if resolved is not None:
            root = next((r for r in allowed if resolved == r or r in resolved.parents), None)
            if root is not None and resolved.is_file():
                if root == paths.screenshots.resolve():
                    from harness.screenshots import ScreenshotStore

                    data = ScreenshotStore(paths).read(resolved)
                else:
                    data = resolved.read_bytes()
        if data is None:
            jail = jail_path(source)
            if jail:
                names = [machine] if machine else machine_names(paths)
                for name in names:
                    if not name:
                        continue
                    data = read_machine_file(name, jail)
                    if data:
                        break
        if data is None:
            # Last chance: GET /api/uploads/<basename> shape already on disk,
            # or sitting in a machine Downloads folder under that name.
            promoted = promote_upload(paths, source)
            if promoted is not None:
                return promoted
            return (
                f"error: post_image only reads files under the workspace "
                f"({paths.workspace}), staged screenshots ({paths.screenshots}), "
                f"or a bot machine path under {MACHINE_HOME} or the shared "
                f"{MACHINE_WORKSPACE}; got {source!r}"
            )
    mime = sniff_mime(data)
    if mime is None:
        return (
            "error: that is not an image (png/jpeg/gif/webp). For other files "
            "save into uploads and call show_file."
        )
    data, mime = recode_for_chat(data, mime)
    if len(data) > MAX_STORED_BYTES:
        return (
            f"error: image is {len(data)} bytes; the chat limit is "
            f"{MAX_STORED_BYTES // 1_000_000} MB and it could not be "
            "re-encoded smaller"
        )
    return stage_upload(paths, data, mime, label=label_from_source(source))


def rewrite_chat_images(text: str, paths: HarnessPaths, *, machine: str | None = None) -> str:
    """Replace `![alt](non-upload path)` with an uploads copy so chat tiles load.

    Idempotent for paths already in uploads. Failures leave the original mark
    so a missing file still renders as a broken tile instead of dropping it.
    """
    if not text or "![" not in text:
        return text

    def repl(match: re.Match[str]) -> str:
        alt, src = match.group(1), match.group(2).strip()
        stored = materialize(paths, src, machine=machine)
        if isinstance(stored, str):
            return match.group(0)
        return f"![{alt}]({stored})"

    return _MD_IMG.sub(repl, text)
