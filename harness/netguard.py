"""Outbound fetch safety for URLs the harness did not choose itself.

Three pieces, all stdlib:

1. `check_destination(url)` — the public-destination check. Resolves the host
   and refuses anything that lands on loopback, link-local (169.254.0.0/16,
   the cloud metadata address included), RFC1918, CGNAT, unique-local or
   link-local IPv6, unspecified, multicast or reserved space, IPv4-mapped
   forms included. http(s) only. This closes the host-vantage SSRF a jailed
   bot otherwise gets through `preview_link` / `post_image`: those handlers
   run in the agent process on the harness host, not in the bot's machine,
   so a model-supplied `http://169.254.169.254/...` or `http://127.0.0.1:
   <port>/...` was a GET from inside the operator's network boundary.

2. `fetch_bytes(url, ...)` — the fetch helper. Validates the destination,
   follows redirects through an opener that re-validates every hop (a public
   URL that 302s to a private address is refused), reads in bounded chunks
   up to a byte cap, and gives up at a total wall-clock deadline. urllib's
   `timeout` is per socket operation, so a server dripping one byte every
   few seconds never tripped it; the deadline is what bounds the turn.

3. `SafeRedirectHandler` + `install_safe_redirects()` — a process-wide
   opener whose redirects drop `Authorization` / `Cookie` /
   `Proxy-Authorization` when a 3xx leaves the original scheme+host (or
   downgrades https→http). The stdlib handler copies every header onto the
   follow-up request, so a connector's host-pinned call could hand its
   bearer to whatever host the pinned API redirected to. Idempotent; the bot
   process and the CLI call it at startup.

Nothing here is a policy decision — approval and policy live in
`agent/govern.py`. This is the shape of the socket the harness is willing
to open on a bot's behalf.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urlsplit

#: Default read chunk; small enough that the deadline is checked often.
CHUNK_BYTES = 16384

_AUTH_HEADERS = ("Authorization", "Proxy-Authorization", "Cookie")
_SCHEMES = ("http", "https")
_DEFAULT_PORTS = {"http": 80, "https": 443}

Resolver = Callable[..., list]


class UnsafeDestination(ValueError):
    """The URL points at (or redirects to) a non-public address or scheme."""


class FetchDeadline(TimeoutError):
    """The fetch's total wall-clock deadline passed before the body ended."""


def _addresses(host: str, port: int, resolver: Resolver) -> list[ipaddress._BaseAddress]:
    """Every address `host` resolves to. IP literals never hit the resolver."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    infos = resolver(host, port, type=socket.SOCK_STREAM)
    out: list[ipaddress._BaseAddress] = []
    for info in infos:
        sockaddr = info[4]
        try:
            out.append(ipaddress.ip_address(sockaddr[0]))
        except (ValueError, IndexError, TypeError):
            continue
    return out


#: Set to 1 on a self-hosted LAN deployment whose bots legitimately preview
#: intranet links: RFC1918 / CGNAT / IPv6 unique-local destinations are then
#: allowed. Loopback, link-local (the cloud metadata address) and the other
#: non-routable classes stay refused — the harness's own services and the
#: instance metadata endpoint are never a bot's to fetch from the host.
PRIVATE_FETCH_ENV = "HARNESS_FETCH_ALLOW_PRIVATE"


def _private_fetch_allowed() -> bool:
    return os.environ.get(PRIVATE_FETCH_ENV, "").strip().lower() in {"1", "true", "yes"}


def is_public_address(addr: ipaddress._BaseAddress | str) -> bool:
    """True when `addr` is a globally routable unicast address (or a private
    LAN address the operator opted in to, see PRIVATE_FETCH_ENV)."""
    ip = ipaddress.ip_address(addr) if isinstance(addr, str) else addr
    # ::ffff:127.0.0.1 is loopback wearing an IPv6 coat; judge the inner v4.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return False
    if ip.is_reserved:
        return False
    if ip.is_private:
        # RFC1918, CGNAT, ULA, plus the documentation/benchmark ranges that
        # is_private also covers; the opt-in is for a LAN, so still require
        # a real address class (ipaddress reports those as non-global too).
        return _private_fetch_allowed() and not ip.is_reserved
    # is_global also refuses CGNAT (100.64/10) and other non-routable space.
    return bool(ip.is_global)


def check_destination(
    url: str, *, https_only: bool = False, resolver: Resolver | None = None
) -> str:
    """Raise UnsafeDestination unless `url` is http(s) to a public host.

    Returns the hostname on success. Resolution errors propagate as
    `socket.gaierror` (an OSError), which callers already treat as a failed
    fetch.
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in _SCHEMES:
        raise UnsafeDestination(f"refused: {url!r} is not an http(s) URL")
    if https_only and scheme != "https":
        raise UnsafeDestination(f"refused: {url!r} is not HTTPS")
    host = parts.hostname
    if not host:
        raise UnsafeDestination(f"refused: {url!r} has no host")
    try:
        port = parts.port or _DEFAULT_PORTS[scheme]
    except ValueError as exc:
        raise UnsafeDestination(f"refused: {url!r} has an invalid port") from exc
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        raise UnsafeDestination(f"refused: {host} is not a public address")
    addrs = _addresses(host, port, resolver or socket.getaddrinfo)
    if not addrs:
        raise UnsafeDestination(f"refused: {host} did not resolve to any address")
    for addr in addrs:
        if not is_public_address(addr):
            raise UnsafeDestination(f"refused: {host} is not a public address")
    return host


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    try:
        port = parts.port or _DEFAULT_PORTS.get(scheme)
    except ValueError:
        port = None
    return scheme, (parts.hostname or "").lower(), port


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib's redirect handler, minus credential forwarding off-origin.

    A 3xx to another scheme+host (or an https→http downgrade on the same
    host) gets a follow-up request without Authorization / Cookie /
    Proxy-Authorization. Same-origin redirects keep them, so an API that
    bounces `/v1/x` to `/v1/x/` still works.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        old_scheme, old_host, old_port = _origin(req.full_url)
        new_scheme, new_host, new_port = _origin(new.full_url)
        same_origin = (old_scheme, old_host, old_port) == (new_scheme, new_host, new_port)
        if not same_origin:
            for name in _AUTH_HEADERS:
                new.remove_header(name)
        return new


class GuardedRedirectHandler(SafeRedirectHandler):
    """SafeRedirectHandler that also re-runs the public-destination check
    on every hop and honours the fetch's wall-clock deadline."""

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        https_only: bool = False,
        deadline_at: float | None = None,
    ) -> None:
        super().__init__()
        self._resolver = resolver
        self._https_only = https_only
        self._deadline_at = deadline_at

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self._deadline_at is not None and time.monotonic() > self._deadline_at:
            raise FetchDeadline("fetch deadline passed while following a redirect")
        # Validate before the parent builds the follow-up: a private target
        # must never even become a Request object.
        check_destination(newurl, https_only=self._https_only, resolver=self._resolver)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def guarded_opener(
    *,
    resolver: Resolver | None = None,
    https_only: bool = False,
    deadline_at: float | None = None,
) -> urllib.request.OpenerDirector:
    """An opener whose every redirect hop is destination-checked."""
    return urllib.request.build_opener(
        GuardedRedirectHandler(resolver=resolver, https_only=https_only, deadline_at=deadline_at)
    )


def fetch_bytes(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    deadline: float,
    headers: dict[str, str] | None = None,
    https_only: bool = False,
    resolver: Resolver | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> bytes:
    """GET `url` and return up to `max_bytes` of its body.

    `timeout` is the per-socket-operation timeout urllib already had;
    `deadline` is the total wall-clock budget for the whole fetch, redirects
    included. Raises UnsafeDestination, FetchDeadline, or whatever urllib
    raises — the callers' existing `except (URLError, OSError, ValueError)`
    catches all three.
    """
    check_destination(url, https_only=https_only, resolver=resolver)
    deadline_at = time.monotonic() + deadline
    if opener is None:
        opener = guarded_opener(resolver=resolver, https_only=https_only, deadline_at=deadline_at)
    req = urllib.request.Request(url, headers=dict(headers or {}))
    chunks: list[bytes] = []
    got = 0
    with opener.open(req, timeout=timeout) as resp:
        while got < max_bytes:
            if time.monotonic() > deadline_at:
                raise FetchDeadline(f"fetch exceeded its {deadline:g}s deadline")
            chunk = resp.read(min(CHUNK_BYTES, max_bytes - got))
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
    return b"".join(chunks)


# -- process-wide opener -----------------------------------------------------

_install_lock = threading.Lock()
_installed = False


def install_safe_redirects() -> bool:
    """Install the credential-stripping opener for every urlopen in this
    process. Idempotent; returns True the first time it installs."""
    global _installed
    with _install_lock:
        if _installed:
            return False
        urllib.request.install_opener(urllib.request.build_opener(SafeRedirectHandler))
        _installed = True
        return True
