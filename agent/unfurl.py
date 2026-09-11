"""Fetch link metadata (title/description) for the chat's link preview card.

Stdlib only, deliberately conservative: http(s) URLs, a hard timeout, a byte
cap on the response, and text-only output — no HTML ever reaches a renderer.
"""

from __future__ import annotations

import re
import urllib.error
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from harness import netguard

_TIMEOUT = 5.0
#: Total wall-clock budget for one unfurl, redirects included. `_TIMEOUT`
#: is per socket operation, so a tarpit dripping a byte every few seconds
#: used to hold the turn until the silence watchdog abandoned it.
_DEADLINE = 10.0
_MAX_BYTES = 131072
_TITLE_LIMIT = 120
_DESC_LIMIT = 300
_UA = "Mozilla/5.0 (compatible; dotobot link preview)"


_META_KEYS = (
    "og:title",
    "og:description",
    "og:image",
    "og:site_name",
    "description",
    "twitter:title",
    "twitter:description",
    "twitter:image",
    "twitter:card",
)
_LINEAR_ISSUE = re.compile(r"/issue/([A-Za-z][A-Za-z0-9]*-\d+)")


class _MetaParser(HTMLParser):
    """Collect Open Graph / Twitter card tags, <title>, and favicon links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.og: dict[str, str] = {}
        self.title = ""
        self.icons: list[tuple[str, str]] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        d = dict(attrs)
        if tag == "title":
            self._in_title = True
            return
        if tag == "link":
            rel = (d.get("rel") or "").lower()
            href = (d.get("href") or "").strip()
            if href and "icon" in rel:
                self.icons.append((rel, href))
            return
        if tag != "meta":
            return
        prop = (d.get("property") or d.get("name") or "").lower()
        content = (d.get("content") or "").strip()
        if content and prop in _META_KEYS:
            self.og.setdefault(prop, content)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and len(self.title) < _TITLE_LIMIT * 2:
            self.title += data


def _fetch(url: str, timeout: float, max_bytes: int) -> bytes:
    # The destination check lives here, not in unfurl(): this runs on the
    # harness host, so a model-supplied loopback / metadata / RFC1918 URL
    # (or a public one that 302s there) was a GET from inside the operator's
    # network. Every redirect hop is re-checked and the whole fetch has a
    # wall-clock deadline.
    return netguard.fetch_bytes(
        url,
        timeout=timeout,
        max_bytes=max_bytes,
        deadline=_DEADLINE,
        headers={"User-Agent": _UA},
    )


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def unfurl(url: str, timeout: float = _TIMEOUT, max_bytes: int = _MAX_BYTES) -> dict | str:
    """Return a link-card payload dict, or an error string.

    Runs inside the tool loop — the timeout is what keeps a dead link from
    stalling a whole turn.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"error: preview_link needs an http(s) URL, got {url!r}"
    try:
        raw = _fetch(url, timeout, max_bytes)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"error: could not fetch {url}: {exc}"
    parser = _MetaParser()
    try:
        parser.feed(raw.decode("utf-8", errors="replace"))
    except Exception:  # malformed markup: keep whatever was collected
        pass
    title = parser.og.get("og:title") or parser.og.get("twitter:title") or parser.title.strip()
    desc = (
        parser.og.get("og:description")
        or parser.og.get("twitter:description")
        or parser.og.get("description")
        or ""
    )
    image = parser.og.get("og:image") or parser.og.get("twitter:image") or ""
    payload: dict = {
        "url": url,
        "domain": parts.hostname,
        "favicon": _favicon(url, parser.icons),
    }
    if title:
        payload["title"] = _clip(title, _TITLE_LIMIT)
    if desc:
        payload["description"] = _clip(desc, _DESC_LIMIT)
    if image:
        abs_img = urljoin(url, image)
        if abs_img.startswith("http"):
            payload["image"] = abs_img
    site = parser.og.get("og:site_name")
    if site:
        payload["site_name"] = _clip(site, 80)
    card = parser.og.get("twitter:card")
    if card:
        payload["twitter_card"] = card
    return payload


def _favicon(page_url: str, icons: list[tuple[str, str]]) -> str:
    """Best icon href, resolved, or a Google favicon fallback."""
    order = ("apple-touch-icon", "icon", "shortcut icon")
    href = None
    for wanted in order:
        for rel, h in icons:
            if wanted in rel:
                href = h
                break
        if href:
            break
    if not href and icons:
        href = icons[0][1]
    if href:
        abs_icon = urljoin(page_url, href)
        if abs_icon.startswith("http"):
            return abs_icon
    host = urlsplit(page_url).hostname or ""
    return f"https://www.google.com/s2/favicons?domain={host}&sz=64"


def linear_issue_ref(url: str) -> dict | None:
    """Pull TEST-123 from a linear.app issue URL, or None."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host != "linear.app" and not host.endswith(".linear.app"):
        return None
    match = _LINEAR_ISSUE.search(parts.path or "")
    if not match:
        return None
    return {"identifier": match.group(1).upper(), "url": url}


def is_linear_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "linear.app" or host.endswith(".linear.app")
