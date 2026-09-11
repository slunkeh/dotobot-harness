import threading
import urllib.error
import urllib.request

import pytest

from harness.orchestrator import Orchestrator
from harness.screen import capture_png, compress_for_model, image_size, screen_available
from harness.server import make_server

ROSTER = '[[bots]]\nname = "atlas"\nprovider = "echo"\n'


def test_capture_via_command(monkeypatch):
    monkeypatch.setenv("HARNESS_SCREENSHOT_CMD", "printf PNGDATA")
    assert capture_png() == b"PNGDATA"
    assert screen_available() is True


def test_capture_none_when_headless(monkeypatch):
    monkeypatch.delenv("HARNESS_SCREENSHOT_CMD", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert capture_png() is None
    assert screen_available() is False


def test_image_size_reads_png_and_jpeg_headers():
    png = (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + (1280).to_bytes(4, "big")
        + (720).to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )
    assert image_size(png) == (1280, 720)
    jpeg = bytes(
        [
            0xFF,
            0xD8,
            0xFF,
            0xC0,
            0x00,
            0x11,
            0x08,
            0x03,
            0x20,  # height 800
            0x05,
            0x00,  # width 1280
            0x03,
            0x01,
            0x22,
            0x00,
            0x02,
            0x11,
            0x01,
            0x03,
            0x11,
            0x01,
            0xFF,
            0xD9,
        ]
    )
    assert image_size(jpeg) == (1280, 800)
    assert image_size(b"") is None
    assert image_size(b"not-an-image") is None


def test_compress_for_model_falls_back_to_png(monkeypatch):
    monkeypatch.setattr("harness.screen.shutil.which", lambda _name: None)
    data, mime = compress_for_model(b"\x89PNG-bytes")
    assert mime == "image/png"
    assert data == b"\x89PNG-bytes"


@pytest.fixture
def server(tmp_path):
    rp = tmp_path / "roster.toml"
    rp.write_text(ROSTER, encoding="utf-8")
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    httpd = make_server(orch, "127.0.0.1", 0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        orch.down()


def test_screen_endpoint_returns_png(server, monkeypatch):
    monkeypatch.setenv("HARNESS_SCREENSHOT_CMD", "printf PNGDATA")
    with urllib.request.urlopen(f"{server}/api/screen/atlas", timeout=5) as r:
        assert r.headers.get("Content-Type") == "image/png"
        assert r.read() == b"PNGDATA"


def test_screen_endpoint_503_when_unavailable(server, monkeypatch):
    monkeypatch.delenv("HARNESS_SCREENSHOT_CMD", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{server}/api/screen/atlas", timeout=5)
    assert exc.value.code == 503
