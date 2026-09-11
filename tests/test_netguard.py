"""harness/netguard.py — the shape of the socket the harness opens for a bot.

Offline: DNS is a fake resolver (or a monkeypatched socket.getaddrinfo) and
HTTP is a fake urllib handler that never touches a socket.
"""

from __future__ import annotations

import email.message
import io
import socket
import time
import urllib.request
import urllib.response

import pytest

from harness import netguard

PUBLIC = "93.184.216.34"


def _resolver(table: dict[str, list[str]]):
    """getaddrinfo lookalike answering from `table`; unknown names fail."""

    def resolve(host, port, *args, **kwargs):
        if host not in table:
            raise socket.gaierror(f"fake: no such host {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in table[host]]

    return resolve


class FakeHTTP(urllib.request.BaseHandler):
    """Answers http(s) opens from a route table and records what it saw."""

    handler_order = 100  # before the real HTTPHandler (500)

    def __init__(self, routes: dict[str, tuple[int, dict[str, str], object]]):
        self.routes = routes
        self.seen: list[tuple[str, dict[str, str]]] = []

    def _open(self, req):
        self.seen.append((req.full_url, {k.lower(): v for k, v in req.header_items()}))
        status, hdrs, body = self.routes[req.full_url]
        msg = email.message.Message()
        for k, v in hdrs.items():
            msg[k] = v
        fp = body if hasattr(body, "read") else io.BytesIO(body)
        resp = urllib.response.addinfourl(fp, msg, req.full_url, status)
        resp.msg = "OK" if status == 200 else "Found"
        return resp

    http_open = _open
    https_open = _open


class Slow:
    """A body that trickles one byte per read — urllib's per-recv timeout
    never fires on it."""

    closed = False

    def __init__(self, pause: float = 0.02):
        self.pause = pause

    def read(self, n: int = -1) -> bytes:
        if n == 0 or n is None or n < 0:
            return b""
        time.sleep(self.pause)
        return b"x"

    def close(self) -> None:
        pass


# -- destination check ------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.1.2.3:8765/api/health",
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://169.254.169.254/example-host/v1/metadata",
        "http://10.0.0.1/",
        "http://172.17.0.1:2375/containers/json",
        "http://192.168.1.1/",
        "http://100.64.0.1/",
        "http://224.0.0.1/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
        "http://[fd12::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:10.0.0.1]/",
        "http://[::]/",
        "http://localhost/",
        "http://foo.localhost/",
    ],
)
def test_private_literal_destinations_are_refused(url, monkeypatch):
    def no_dns(*a, **k):
        raise AssertionError("literal addresses must never hit DNS")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    with pytest.raises(netguard.UnsafeDestination):
        netguard.check_destination(url)


def test_hostname_resolving_to_private_space_is_refused(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _resolver({"intranet.example": ["10.1.2.3"]}))
    with pytest.raises(netguard.UnsafeDestination, match="not a public address"):
        netguard.check_destination("http://intranet.example/admin")


def test_one_private_answer_among_public_ones_is_enough_to_refuse():
    res = _resolver({"rebind.example": [PUBLIC, "127.0.0.1"]})
    with pytest.raises(netguard.UnsafeDestination):
        netguard.check_destination("https://rebind.example/", resolver=res)


def test_public_destinations_pass():
    res = _resolver({"example.com": [PUBLIC], "v6.example": ["2606:4700::6810:84e5"]})
    assert netguard.check_destination("https://example.com/x", resolver=res) == "example.com"
    assert netguard.check_destination("http://v6.example:8080/", resolver=res) == "v6.example"
    assert netguard.check_destination("http://93.184.216.34/") == "93.184.216.34"


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "gopher://x/"])
def test_non_http_schemes_are_refused(url):
    with pytest.raises(netguard.UnsafeDestination):
        netguard.check_destination(url, resolver=_resolver({"example.com": [PUBLIC]}))


def test_https_only_refuses_plain_http():
    res = _resolver({"example.com": [PUBLIC]})
    with pytest.raises(netguard.UnsafeDestination, match="not HTTPS"):
        netguard.check_destination("http://example.com/", https_only=True, resolver=res)
    netguard.check_destination("https://example.com/", https_only=True, resolver=res)


def test_unresolvable_host_is_an_os_error():
    with pytest.raises(OSError):
        netguard.check_destination("http://nope.example/", resolver=_resolver({}))


# -- guarded fetch ----------------------------------------------------------


def _fetch(url, fake, res, **kw):
    kw.setdefault("timeout", 1.0)
    kw.setdefault("max_bytes", 1 << 16)
    kw.setdefault("deadline", 5.0)
    opener = netguard.guarded_opener(resolver=res, deadline_at=time.monotonic() + kw["deadline"])
    opener.add_handler(fake)
    return netguard.fetch_bytes(url, resolver=res, opener=opener, **kw)


def test_fetch_refuses_before_opening_a_private_target():
    fake = FakeHTTP({})
    with pytest.raises(netguard.UnsafeDestination):
        _fetch("http://127.0.0.1:8765/", fake, _resolver({}))
    assert fake.seen == []


def test_redirect_to_a_private_address_is_refused():
    fake = FakeHTTP(
        {
            "http://public.example/go": (302, {"Location": "http://169.254.169.254/latest"}, b""),
            "http://169.254.169.254/latest": (200, {}, b"metadata"),
        }
    )
    res = _resolver({"public.example": [PUBLIC]})
    with pytest.raises(netguard.UnsafeDestination):
        _fetch("http://public.example/go", fake, res)
    assert [u for u, _ in fake.seen] == ["http://public.example/go"]


def test_redirect_to_a_hostname_resolving_private_is_refused():
    fake = FakeHTTP(
        {
            "http://public.example/go": (302, {"Location": "http://inner.example/"}, b""),
            "http://inner.example/": (200, {}, b"secret"),
        }
    )
    res = _resolver({"public.example": [PUBLIC], "inner.example": ["172.17.0.1"]})
    with pytest.raises(netguard.UnsafeDestination):
        _fetch("http://public.example/go", fake, res)
    assert len(fake.seen) == 1


def test_redirect_to_a_public_address_is_followed():
    fake = FakeHTTP(
        {
            "http://a.example/": (301, {"Location": "https://b.example/final"}, b""),
            "https://b.example/final": (200, {}, b"<title>ok</title>"),
        }
    )
    res = _resolver({"a.example": [PUBLIC], "b.example": [PUBLIC]})
    assert _fetch("http://a.example/", fake, res) == b"<title>ok</title>"


def test_deadline_bounds_a_trickling_body():
    fake = FakeHTTP({"http://slow.example/": (200, {}, Slow(0.02))})
    res = _resolver({"slow.example": [PUBLIC]})
    t0 = time.monotonic()
    with pytest.raises(netguard.FetchDeadline):
        _fetch("http://slow.example/", fake, res, deadline=0.3, max_bytes=1 << 20)
    assert time.monotonic() - t0 < 2.0


def test_byte_cap_truncates_the_body():
    fake = FakeHTTP({"http://big.example/": (200, {}, b"z" * 100_000)})
    res = _resolver({"big.example": [PUBLIC]})
    out = _fetch("http://big.example/", fake, res, max_bytes=1000)
    assert len(out) == 1000


def test_fetch_defaults_resolve_through_socket(monkeypatch):
    calls = []

    def resolve(host, port, *a, **k):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.9.9.9", port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with pytest.raises(netguard.UnsafeDestination):
        netguard.fetch_bytes("http://corp.example/", timeout=1, max_bytes=10, deadline=1)
    assert calls == ["corp.example"]


# -- credential-stripping redirects -----------------------------------------


def _open_with(handler_cls, fake, url, headers):
    opener = urllib.request.build_opener(handler_cls, fake)
    with opener.open(urllib.request.Request(url, headers=headers), timeout=1) as resp:
        return resp.read()


def test_authorization_and_cookie_dropped_on_cross_host_redirect():
    fake = FakeHTTP(
        {
            "https://api.example/v1/file": (302, {"Location": "https://cdn.other/blob"}, b""),
            "https://cdn.other/blob": (200, {}, b"blob"),
        }
    )
    body = _open_with(
        netguard.SafeRedirectHandler,
        fake,
        "https://api.example/v1/file",
        {"Authorization": "Bearer SECRET", "Cookie": "sid=1", "X-Trace": "keep"},
    )
    assert body == b"blob"
    first, second = fake.seen
    assert first[1]["authorization"] == "Bearer SECRET"
    assert "authorization" not in second[1]
    assert "cookie" not in second[1]
    assert second[1]["x-trace"] == "keep"


def test_authorization_kept_on_same_origin_redirect():
    fake = FakeHTTP(
        {
            "https://api.example/v1/x": (301, {"Location": "https://api.example/v1/x/"}, b""),
            "https://api.example/v1/x/": (200, {}, b"{}"),
        }
    )
    _open_with(
        netguard.SafeRedirectHandler,
        fake,
        "https://api.example/v1/x",
        {"Authorization": "Bearer SECRET"},
    )
    assert fake.seen[1][1]["authorization"] == "Bearer SECRET"


def test_authorization_dropped_on_https_to_http_downgrade():
    fake = FakeHTTP(
        {
            "https://api.example/v1/x": (302, {"Location": "http://api.example/v1/x"}, b""),
            "http://api.example/v1/x": (200, {}, b"{}"),
        }
    )
    _open_with(
        netguard.SafeRedirectHandler,
        fake,
        "https://api.example/v1/x",
        {"Authorization": "Bearer SECRET"},
    )
    assert "authorization" not in fake.seen[1][1]


def test_stock_urllib_forwards_the_bearer_cross_host():
    """The behaviour being closed: without the handler the credential goes
    wherever the 3xx points. Pins that the test above is meaningful."""
    fake = FakeHTTP(
        {
            "https://api.example/v1/file": (302, {"Location": "https://cdn.other/blob"}, b""),
            "https://cdn.other/blob": (200, {}, b"blob"),
        }
    )
    _open_with(
        urllib.request.HTTPRedirectHandler,
        fake,
        "https://api.example/v1/file",
        {"Authorization": "Bearer SECRET"},
    )
    assert fake.seen[1][1]["authorization"] == "Bearer SECRET"


def test_install_safe_redirects_is_idempotent_and_global():
    first = netguard.install_safe_redirects()
    second = netguard.install_safe_redirects()
    assert second is False
    assert first in (True, False)  # another test in the process may have won the race
    opener = urllib.request._opener
    assert opener is not None
    assert any(isinstance(h, netguard.SafeRedirectHandler) for h in opener.handlers)
    assert not any(type(h) is urllib.request.HTTPRedirectHandler for h in opener.handlers), (
        "the stock forwarding handler must not remain installed"
    )
