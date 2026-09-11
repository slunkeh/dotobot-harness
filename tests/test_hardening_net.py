"""Outbound-fetch hardening at the call sites: preview_link / post_image
(agent/unfurl.py, agent/postimage.py), MCP OAuth discovery
(harness/mcp_oauth.py), the jail file read, the ImageMagick recode, and the
process entrypoints installing the credential-stripping opener.

Everything is offline: socket.getaddrinfo is monkeypatched, HTTP is a fake
urllib handler, and the jail `docker exec` prefix is replaced by nothing so
the bounded `head` runs against a tmp file.
"""

from __future__ import annotations

import email.message
import io
import socket
import subprocess
import time
import urllib.request
import urllib.response

import pytest

from agent import postimage, unfurl
from harness import mcp_oauth, netguard
from harness.paths import HarnessPaths

PUBLIC = "93.184.216.34"


def _dns(monkeypatch, table: dict[str, list[str]]):
    def resolve(host, port, *a, **k):
        if host not in table:
            raise socket.gaierror(f"fake: no such host {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in table[host]]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


class FakeHTTP(urllib.request.BaseHandler):
    handler_order = 100

    def __init__(self, routes):
        self.routes = routes
        self.seen: list[str] = []

    def _open(self, req):
        self.seen.append(req.full_url)
        status, hdrs, body = self.routes[req.full_url]
        msg = email.message.Message()
        for k, v in hdrs.items():
            msg[k] = v
        fp = body if hasattr(body, "read") else io.BytesIO(body)
        resp = urllib.response.addinfourl(fp, msg, req.full_url, status)
        resp.msg = "OK"
        return resp

    http_open = _open
    https_open = _open


class Slow:
    closed = False

    def read(self, n: int = -1) -> bytes:
        if n is None or n <= 0:
            return b""
        time.sleep(0.02)
        return b"x"

    def close(self) -> None:
        pass


def _wire(monkeypatch, routes):
    """Route the guarded opener's HTTP through `routes`; return the fake."""
    fake = FakeHTTP(routes)
    real = netguard.guarded_opener

    def patched(**kw):
        opener = real(**kw)
        opener.add_handler(fake)
        return opener

    monkeypatch.setattr(netguard, "guarded_opener", patched)
    return fake


def _no_sockets(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("no HTTP connection may be opened for a refused destination")

    monkeypatch.setattr(netguard, "guarded_opener", boom)
    monkeypatch.setattr(urllib.request, "urlopen", boom)


# -- preview_link (agent/unfurl.py) -----------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8765/api/health",
        "http://[::1]/",
        "http://169.254.169.254/example-host/v1/metadata",
        "http://172.17.0.1:2375/containers/json",
        "http://10.0.0.1/",
        "http://localhost:8765/",
    ],
)
def test_unfurl_refuses_private_targets_without_connecting(url, monkeypatch):
    _no_sockets(monkeypatch)
    _dns(monkeypatch, {})
    out = unfurl.unfurl(url)
    assert isinstance(out, str) and out.startswith("error:")
    assert "not a public address" in out


def test_unfurl_refuses_a_hostname_that_resolves_private(monkeypatch):
    _no_sockets(monkeypatch)
    _dns(monkeypatch, {"intranet.example": ["192.168.4.4"]})
    out = unfurl.unfurl("http://intranet.example/admin")
    assert out.startswith("error:")


def test_unfurl_refuses_a_public_url_that_redirects_private(monkeypatch):
    _dns(monkeypatch, {"public.example": [PUBLIC]})
    fake = _wire(
        monkeypatch,
        {
            "http://public.example/go": (302, {"Location": "http://127.0.0.1:8765/"}, b""),
            "http://127.0.0.1:8765/": (200, {}, b"<title>harness</title>"),
        },
    )
    out = unfurl.unfurl("http://public.example/go")
    assert isinstance(out, str) and out.startswith("error:")
    assert fake.seen == ["http://public.example/go"]


def test_unfurl_still_unfurls_a_public_page(monkeypatch):
    _dns(monkeypatch, {"news.example": [PUBLIC]})
    _wire(
        monkeypatch,
        {
            "https://news.example/a": (
                200,
                {},
                b"<html><head><title>Story</title></head></html>",
            )
        },
    )
    out = unfurl.unfurl("https://news.example/a")
    assert isinstance(out, dict)
    assert out["title"] == "Story"
    assert out["domain"] == "news.example"


def test_unfurl_gives_up_at_its_deadline_on_a_tarpit(monkeypatch):
    _dns(monkeypatch, {"slow.example": [PUBLIC]})
    _wire(monkeypatch, {"https://slow.example/": (200, {}, Slow())})
    monkeypatch.setattr(unfurl, "_DEADLINE", 0.3)
    t0 = time.monotonic()
    out = unfurl.unfurl("https://slow.example/")
    assert isinstance(out, str) and out.startswith("error:")
    assert "deadline" in out
    assert time.monotonic() - t0 < 2.0


# -- post_image (agent/postimage.py) ----------------------------------------


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:1/x.png", "http://169.254.169.254/x.png", "http://[fd00::1]/x.png"],
)
def test_post_image_fetch_refuses_private_targets(url, monkeypatch):
    _no_sockets(monkeypatch)
    _dns(monkeypatch, {})
    out = postimage.fetch(url)
    assert isinstance(out, str) and out.startswith("error:")
    assert "not a public address" in out


def test_post_image_fetch_refuses_redirect_to_private(monkeypatch):
    _dns(monkeypatch, {"img.example": [PUBLIC]})
    fake = _wire(
        monkeypatch,
        {
            "https://img.example/cat.png": (302, {"Location": "http://10.0.0.5/cat.png"}, b""),
            "http://10.0.0.5/cat.png": (200, {}, b"\x89PNG\r\n\x1a\n" + b"\x00" * 32),
        },
    )
    out = postimage.fetch("https://img.example/cat.png")
    assert isinstance(out, str) and out.startswith("error:")
    assert fake.seen == ["https://img.example/cat.png"]


def test_post_image_fetch_returns_bytes_for_a_public_image(monkeypatch):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    _dns(monkeypatch, {"img.example": [PUBLIC]})
    _wire(monkeypatch, {"https://img.example/cat.png": (200, {}, png)})
    assert postimage.fetch("https://img.example/cat.png") == png


def test_post_image_fetch_has_a_deadline(monkeypatch):
    _dns(monkeypatch, {"slow.example": [PUBLIC]})
    _wire(monkeypatch, {"https://slow.example/x.png": (200, {}, Slow())})
    monkeypatch.setattr(postimage, "_FETCH_DEADLINE", 0.3)
    t0 = time.monotonic()
    out = postimage.fetch("https://slow.example/x.png")
    assert isinstance(out, str) and out.startswith("error:")
    assert time.monotonic() - t0 < 2.0


def test_post_image_fetch_cap_still_abandons_not_truncates(monkeypatch):
    _dns(monkeypatch, {"big.example": [PUBLIC]})
    monkeypatch.setattr(postimage, "MAX_DOWNLOAD_BYTES", 100)
    _wire(monkeypatch, {"https://big.example/x.png": (200, {}, b"z" * 500)})
    out = postimage.fetch("https://big.example/x.png")
    assert isinstance(out, str) and "larger than" in out


def test_reply_markdown_cannot_trigger_a_private_fetch(tmp_path, monkeypatch):
    """rewrite_chat_images runs on every final reply with no tool call, so
    the guard must sit in fetch() itself."""
    _no_sockets(monkeypatch)
    _dns(monkeypatch, {})
    paths = HarnessPaths(home=tmp_path)
    paths.ensure_layout([])
    text = "see ![x](http://127.0.0.1:1/x.png) now"
    assert postimage.rewrite_chat_images(text, paths) == text


# -- jail file read is bounded inside the exec ------------------------------


def test_read_machine_file_bounds_the_capture_in_the_exec(tmp_path, monkeypatch):
    import harness.machine_view as mv

    monkeypatch.setattr(mv, "exec_prefix", lambda machine, **kw: [])  # run head on the host
    argvs = []
    real_run = subprocess.run

    def spy(argv, **kw):
        argvs.append(list(argv))
        return real_run(argv, **kw)

    monkeypatch.setattr(postimage.subprocess, "run", spy)

    huge = tmp_path / "shot.png"
    with huge.open("wb") as fh:
        fh.truncate(postimage.MAX_DOWNLOAD_BYTES + 2)  # sparse: cat would stream it all
    assert postimage.read_machine_file("m0", str(huge)) is None
    argv = argvs[-1]
    assert "cat" not in argv
    assert argv[:3] == ["head", "-c", str(postimage.MAX_DOWNLOAD_BYTES + 1)]
    assert argv[-1] == str(huge)

    small = tmp_path / "ok.png"
    small.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x01" * 40)
    assert postimage.read_machine_file("m0", str(small)) == small.read_bytes()


# -- ImageMagick recode carries resource limits ------------------------------


def _fake_convert(argvs):
    def run(argv, **kw):
        argvs.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=b"\xff\xd8" + b"\x00" * 16, stderr=b"")

    return run


def test_recode_passes_resource_limits_and_a_pinned_coder(monkeypatch):
    argvs = []
    monkeypatch.setattr(postimage.shutil, "which", lambda name: "/usr/bin/convert")
    monkeypatch.setattr(postimage.subprocess, "run", _fake_convert(argvs))
    data = b"\x89PNG\r\n\x1a\n" + b"\x00" * (postimage.RECODE_THRESHOLD + 10)
    out, mime = postimage.recode_for_chat(data, "image/png")
    assert mime == "image/jpeg" and out[:2] == b"\xff\xd8"
    argv = argvs[0]
    limits = {argv[i + 1] for i, a in enumerate(argv) if a == "-limit"}
    assert {"memory", "map", "disk", "area", "width", "height", "time"} <= limits
    assert "png:-" in argv
    assert "-" not in argv  # the input coder is never left to magic sniffing
    assert argv.index("-limit") < argv.index("png:-")


def test_recode_pins_the_coder_from_the_sniffed_mime(monkeypatch):
    argvs = []
    monkeypatch.setattr(postimage.shutil, "which", lambda name: "/usr/bin/convert")
    monkeypatch.setattr(postimage.subprocess, "run", _fake_convert(argvs))
    data = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * (postimage.RECODE_THRESHOLD + 10)
    postimage.recode_for_chat(data, "image/webp")
    assert "webp:-" in argvs[0]


# -- MCP OAuth discovery refuses a non-public issuer -------------------------


class _PRM(mcp_oauth.Transport):
    def __init__(self, issuer):
        self.issuer = issuer
        self.urls: list[str] = []

    def get_json(self, url):
        self.urls.append(url)
        if url.endswith("/.well-known/oauth-protected-resource/mcp"):
            return {"authorization_servers": [self.issuer]}
        if "well-known" in url:
            return {
                "authorization_endpoint": "https://auth.example.com/authorize",
                "token_endpoint": "https://auth.example.com/token",
            }
        raise mcp_oauth.OAuthError(f"404: {url}")


@pytest.mark.parametrize(
    "issuer, why",
    [
        ("http://127.0.0.1:8765", "not HTTPS"),
        ("http://evil.example.com", "not HTTPS"),
        ("https://127.0.0.1:8765", "not a public host"),
        ("https://[::1]", "not a public host"),
        ("https://169.254.169.254", "not a public host"),
        ("https://10.0.0.1", "not a public host"),
        ("https://localhost", "not a public host"),
    ],
)
def test_discover_refuses_a_private_or_plain_http_issuer(issuer, why):
    t = _PRM(issuer)
    with pytest.raises(mcp_oauth.OAuthError, match=why):
        mcp_oauth.discover("https://mcp.example.com/mcp", t)
    assert not any(issuer in u for u in t.urls), "no metadata GET may be built from it"


def test_discover_drops_a_query_string_from_the_issuer():
    class OnlyOpenId(_PRM):
        """Answers only `<origin><path>/.well-known/openid-configuration`."""

        def get_json(self, url):
            if "well-known" in url and "protected-resource" not in url:
                self.urls.append(url)
                if url == "https://auth.example.com/tenant/.well-known/openid-configuration":
                    return {
                        "authorization_endpoint": "https://auth.example.com/authorize",
                        "token_endpoint": "https://auth.example.com/token",
                    }
                raise mcp_oauth.OAuthError(f"404: {url}")
            return super().get_json(url)

    t = OnlyOpenId("https://auth.example.com/tenant?next=x")
    mcp_oauth.discover("https://mcp.example.com/mcp", t)
    assert all("?" not in u for u in t.urls)
    assert "https://auth.example.com/tenant/.well-known/openid-configuration" in t.urls


def test_real_transport_refuses_a_discovery_host_resolving_private(monkeypatch):
    _dns(monkeypatch, {"auth.internal": ["10.0.0.9"]})

    def boom(*a, **k):
        raise AssertionError("urlopen must not run for a refused destination")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(mcp_oauth.OAuthError, match="not a public address"):
        mcp_oauth.Transport().get_json("https://auth.internal/.well-known/openid-configuration")


# -- entrypoints install the process-wide opener ----------------------------


def test_cli_main_installs_safe_redirects(monkeypatch, tmp_path):
    from harness import cli

    calls = []
    monkeypatch.setattr(cli, "install_safe_redirects", lambda: calls.append(1))
    assert cli.main(["--home", str(tmp_path), "paths"]) == 0
    assert calls == [1]


def test_agent_main_installs_safe_redirects_before_booting(monkeypatch):
    import agent.__main__ as agent_main

    calls = []
    monkeypatch.setattr(agent_main, "install_safe_redirects", lambda: calls.append(1))
    # A bad generation token stops the boot right after the install, so the
    # test never spawns a runtime.
    monkeypatch.setenv("HARNESS_GENERATION_TOKEN", "expected")
    rc = agent_main.main(["--bot", "x", "--generation-token", "wrong"])
    assert rc == 2
    assert calls == [1]
