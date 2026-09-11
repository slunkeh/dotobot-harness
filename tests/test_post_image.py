"""post_image: images land in uploads (the served, unswept store) as markdown.

Chat images must reference `workspace/uploads` — the one folder
`GET /api/uploads/<name>` serves and the one with no retention sweep — so the
`![alt](path)` a bot puts in its reply keeps rendering when the user scrolls
back. These tests cover the three sources (URL, staged screenshot, workspace
file), the image sniff, the size cap, and the screenshot mtime refresh that
keeps the copy from racing the retention sweeper.
"""

from __future__ import annotations

import os
import struct
import time
import zlib
from pathlib import Path

from agent import postimage
from agent.memory import Memory
from agent.tools import ToolContext, default_tools
from harness.paths import HarnessPaths
from harness.screenshots import ScreenshotStore


def _paths(tmp_path) -> HarnessPaths:
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas"])
    return p


def _ctx(paths) -> ToolContext:
    return ToolContext(paths=paths, bot="atlas", memory=Memory(paths=paths, bot="atlas"))


def _post(ctx, **args) -> str:
    return default_tools()["post_image"].handler(ctx, args)


def _png(width: int = 1, height: int = 1) -> bytes:
    """A minimal valid PNG (grey pixels), stdlib-built."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x80" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


# -- sniffing ---------------------------------------------------------------


def test_sniff_knows_the_four_formats():
    assert postimage.sniff_mime(_png()) == "image/png"
    assert postimage.sniff_mime(b"\xff\xd8\xff\xe0" + b"\x00" * 16) == "image/jpeg"
    assert postimage.sniff_mime(b"GIF89a" + b"\x00" * 16) == "image/gif"
    assert postimage.sniff_mime(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8) == "image/webp"
    assert postimage.sniff_mime(b"<html>hello</html>" + b" " * 16) is None
    assert postimage.sniff_mime(b"") is None


# -- local sources ----------------------------------------------------------


def test_workspace_file_is_copied_into_uploads_as_markdown(tmp_path):
    paths = _paths(tmp_path)
    src = paths.workspace / "plot.png"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(_png(2, 2))
    out = _post(_ctx(paths), source=str(src), alt="the plot")
    assert out.startswith("ok:")
    assert "![the plot](" in out
    stored = out.rsplit("(", 1)[1].rstrip(")")
    assert os.path.dirname(stored) == str(paths.uploads)
    assert paths.uploads / os.path.basename(stored) in list(paths.uploads.iterdir())
    # the source stays where it was — post_image copies, never moves
    assert src.is_file()


def test_relative_source_resolves_against_the_workspace(tmp_path):
    paths = _paths(tmp_path)
    (paths.workspace / "shot.png").write_bytes(_png())
    out = _post(_ctx(paths), source="shot.png")
    assert out.startswith("ok:")


def test_staged_screenshot_is_copied_and_its_retention_clock_restarts(tmp_path):
    paths = _paths(tmp_path)
    staged = ScreenshotStore(paths).stage(_png(), ext="png")
    stale = time.time() - 3600
    os.utime(staged, (stale, stale))
    out = _post(_ctx(paths), source=str(staged), alt="screen")
    assert out.startswith("ok:")
    # the read refreshed the staged file's mtime, so the copy cannot have
    # raced the sweeper
    assert staged.stat().st_mtime > stale + 1800
    assert any(paths.uploads.iterdir())


def test_paths_outside_workspace_and_screenshots_are_refused(tmp_path):
    paths = _paths(tmp_path)
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(_png())
    out = _post(_ctx(paths), source=str(outside))
    assert out.startswith("error:")
    assert not paths.uploads.is_dir() or not any(paths.uploads.iterdir())


def test_non_image_bytes_are_refused(tmp_path):
    paths = _paths(tmp_path)
    src = paths.workspace / "notes.png"
    src.write_bytes(b"just text pretending, definitely not pixels")
    out = _post(_ctx(paths), source=str(src))
    assert out.startswith("error:")
    assert "not an image" in out


def test_missing_file_and_missing_source_error(tmp_path):
    paths = _paths(tmp_path)
    assert _post(_ctx(paths), source=str(paths.workspace / "nope.png")).startswith("error:")
    assert _post(_ctx(paths)).startswith("error:")


def test_miss_message_names_both_machine_roots(tmp_path):
    """A bot following the workspace prompt that hits a missing file must not
    be told /workspace is not a valid source."""
    paths = _paths(tmp_path)
    out = _post(_ctx(paths), source="/workspace/site/missing.png")
    assert out.startswith("error:")
    assert "/home/agent" in out and "/workspace" in out


# -- remote sources ---------------------------------------------------------


def test_url_is_downloaded_into_uploads_not_hotlinked(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setattr(postimage, "fetch", lambda url, **kw: _png(3, 3))
    out = _post(_ctx(paths), source="https://example.com/img/cat.png", alt="a cat")
    assert out.startswith("ok:")
    assert "![a cat](" in out
    stored = out.rsplit("(", 1)[1].rstrip(")")
    # the markdown points at the local copy, never back at example.com
    assert str(paths.uploads) in stored
    assert "example.com" not in stored
    assert os.path.basename(stored).endswith(".png")
    assert "cat" in os.path.basename(stored)


def test_fetch_rejects_non_http_schemes():
    assert isinstance(postimage.fetch("ftp://example.com/x.png"), str)
    assert isinstance(postimage.fetch("file:///etc/passwd"), str)


def test_url_fetch_error_reaches_the_model(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setattr(postimage, "fetch", lambda url, **kw: "error: could not fetch it")
    out = _post(_ctx(paths), source="https://example.com/x.png")
    assert out == "error: could not fetch it"


def test_url_response_that_is_not_an_image_is_refused(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setattr(postimage, "fetch", lambda url, **kw: b"<html>404 lol</html>")
    out = _post(_ctx(paths), source="https://example.com/x.png")
    assert out.startswith("error:")


# -- size handling ----------------------------------------------------------


def test_oversize_image_without_imagemagick_is_refused(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    big = _png() + b"\x00" * (postimage.MAX_STORED_BYTES + 1)
    src = paths.workspace / "huge.png"
    src.write_bytes(big)
    monkeypatch.setattr(postimage.shutil, "which", lambda name: None)
    out = _post(_ctx(paths), source=str(src))
    assert out.startswith("error:")
    assert "MB" in out


def test_small_images_are_never_recoded():
    data = _png()
    out, mime = postimage.recode_for_chat(data, "image/png")
    assert out is data and mime == "image/png"


def test_gifs_are_never_recoded(monkeypatch):
    calls = []
    monkeypatch.setattr(postimage.shutil, "which", lambda name: calls.append(name))
    data = b"GIF89a" + b"\x00" * (postimage.RECODE_THRESHOLD + 10)
    out, mime = postimage.recode_for_chat(data, "image/gif")
    assert out is data and mime == "image/gif"
    assert not calls  # convert was never even looked up


def test_recode_falls_back_to_original_without_imagemagick(monkeypatch):
    monkeypatch.setattr(postimage.shutil, "which", lambda name: None)
    data = _png() + b"\x00" * (postimage.RECODE_THRESHOLD + 10)
    out, mime = postimage.recode_for_chat(data, "image/png")
    assert out is data and mime == "image/png"


# -- naming -----------------------------------------------------------------


def test_stored_names_are_upload_safe_and_collision_free(tmp_path):
    paths = _paths(tmp_path)
    a = postimage.stage_upload(paths, _png(), "image/png", label="a b/c!.png")
    b = postimage.stage_upload(paths, _png(), "image/png", label="a b/c!.png")
    assert a != b
    for stored in (a, b):
        # the Mac client fetches GET /api/uploads/<basename>; the server's
        # _SAFE_NAME filter must not mangle the name into a 404
        assert "/" not in stored.name and " " not in stored.name
        assert stored.suffix == ".png"


def test_label_from_source_handles_urls_and_paths():
    assert postimage.label_from_source("https://x.com/a/b/cat.png?w=2") == "cat"
    assert postimage.label_from_source("/tmp/shots/scr.jpeg") == "scr"


def test_promote_upload_copies_machine_downloads(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    png = _png(2, 2)
    (paths.run / "amazon-seller-manager.json").write_text(
        '{"backend":"machines","machine":"harness-machine-0"}', encoding="utf-8"
    )
    monkeypatch.setattr(
        postimage,
        "read_machine_file",
        lambda machine, path: (
            png
            if machine == "harness-machine-0"
            and path == "/home/agent/Downloads/sample-image-set-4-5x7.jpg"
            else None
        ),
    )
    stored = postimage.promote_upload(paths, "sample-image-set-4-5x7.jpg")
    assert stored is not None
    assert stored.name == "sample-image-set-4-5x7.jpg"
    assert stored.read_bytes() == png
    # second call hits the uploads copy, no docker
    monkeypatch.setattr(
        postimage, "read_machine_file", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    assert postimage.promote_upload(paths, "sample-image-set-4-5x7.jpg") == stored


def test_rewrite_chat_images_promotes_jail_markdown(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    png = _png()
    monkeypatch.setattr(postimage, "machine_names", lambda _p: ["harness-machine-0"])
    monkeypatch.setattr(
        postimage,
        "read_machine_file",
        lambda machine, path: png if path.endswith("sample-image-set-4-5x7.jpg") else None,
    )
    text = (
        "Last one you packed.\n\n"
        "![Sample image set of 4, 5x7](/home/agent/Downloads/sample-image-set-4-5x7.jpg)\n"
    )
    out = postimage.rewrite_chat_images(text, paths, machine="harness-machine-0")
    assert "/home/agent/Downloads/" not in out
    assert "![Sample image set of 4, 5x7](" in out
    stored = out.rsplit("(", 1)[1].rstrip(")\n")
    assert Path(stored).is_file()
    assert Path(stored).read_bytes() == png


def test_jail_path_rejects_escape():
    assert postimage.jail_path("/home/agent/Downloads/a.jpg") == "/home/agent/Downloads/a.jpg"
    assert postimage.jail_path("/home/agent/../etc/passwd") is None
    assert postimage.jail_path("/etc/passwd") is None


def test_jail_path_accepts_the_shared_workspace():
    """/workspace is the project directory every machine shares; a bot may
    post_image from it exactly like from its home — and no further."""
    assert postimage.jail_path("/workspace/site/chart.png") == "/workspace/site/chart.png"
    assert postimage.jail_path("/workspace") == "/workspace"
    assert postimage.jail_path("/workspace/../etc/passwd") is None
    assert postimage.jail_path("/workspaces/other.png") is None
