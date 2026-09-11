"""Secret redaction registry + sentinel tokens.

Port of OpenClaw v2's secret-handling design. Two halves:

**Exact-value redaction registry.** Secret values are registered when they
enter the process (`set_secret`, `get_secret`) and `scrub()` replaces every
registered form in outbound text — per-bot log lines, stream events, tool
results, API error payloads, session records. Each value is registered in
three forms so a secret that was URL-encoded into a query string or
JSON-escaped into a serialized frame is still caught. The registry is
bounded (`MAX_VALUES`, oldest evicted first) and the raw value is always
registered LAST, so eviction can never keep only a transform while dropping
the raw credential. Values shorter than `MIN_LENGTH` are never registered —
scrubbing 1-byte "secrets" would shred ordinary text.

**Sentinel tokens.** The replacement text is not a `****` mask but a stable
sentinel (`hs-v1.<b64url>.end`) that *seals* the value: the payload is the
value encrypted with a process-local random key (HMAC-SHA256-derived
keystream + MAC — stdlib only), with a nonce derived as
`HMAC(process key, label || value)` so the same secret yields the same
sentinel without any reverse plaintext map held in memory. The model and
the transcript may see and repeat the sentinel freely; `unseal()` — used
only at the final network boundary (connector HTTP request construction,
provider auth headers) — is the single way back to plaintext. A
sentinel-shaped value that cannot be unsealed there fails closed:
`resolve_outbound()` raises `UnresolvedSentinelError` before any I/O, so a
request is refused rather than forwarded containing the sentinel.

The key is minted per process lifetime and never persisted: a sentinel is
worthless in a log shipped off-box, and a restarted harness simply mints
fresh sentinels the next time each secret is registered.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import urllib.parse
from collections import OrderedDict
from collections.abc import Callable

#: Registry bounds: at most this many registered forms; values shorter than
#: MIN_LENGTH are never registered.
MAX_VALUES = 512
MIN_LENGTH = 6

_PREFIX = "hs-v1."
_SUFFIX = ".end"
_NONCE_LEN = 12
_TAG_LEN = 16

#: nonce + tag alone are 28 bytes -> 38 unpadded base64url chars, so any
#: real token body is at least that long.
SENTINEL_RE = re.compile(r"hs-v1\.[A-Za-z0-9_-]{30,}\.end")


class SentinelError(ValueError):
    """A token is not a valid sentinel sealed by this process."""


class UnresolvedSentinelError(SentinelError):
    """A sentinel-shaped value reached a network boundary and could not be
    unsealed — the request must be refused, never forwarded."""


# -- process key -------------------------------------------------------------

_key_lock = threading.Lock()
_process_key: bytes | None = None


def _key() -> bytes:
    """Random sealing key, minted once per process lifetime, never persisted."""
    global _process_key
    with _key_lock:
        if _process_key is None:
            _process_key = os.urandom(32)
        return _process_key


# -- sealing -----------------------------------------------------------------


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        counter += 1
    return out[:length]


def seal(value: str, label: str = "") -> str:
    """Mint the stable sentinel token standing in for `value`.

    Deterministic per (process key, label, value): the nonce is
    HMAC(key, label || value), so re-registering the same secret yields the
    same token with no plaintext map kept anywhere.
    """
    key = _key()
    data = value.encode("utf-8")
    nonce = hmac.new(
        key, b"hs-v1 nonce\x00" + label.encode("utf-8") + b"\x00" + data, hashlib.sha256
    ).digest()[:_NONCE_LEN]
    ct = bytes(a ^ b for a, b in zip(data, _keystream(key, nonce, len(data)), strict=True))
    tag = hmac.new(key, b"hs-v1 tag\x00" + nonce + ct, hashlib.sha256).digest()[:_TAG_LEN]
    body = base64.urlsafe_b64encode(nonce + ct + tag).decode("ascii").rstrip("=")
    return f"{_PREFIX}{body}{_SUFFIX}"


def is_sentinel(text: str) -> bool:
    """True when `text` is exactly one sentinel token (any process's)."""
    return isinstance(text, str) and SENTINEL_RE.fullmatch(text) is not None


def unseal(token: str) -> str:
    """Decrypt a sentinel minted by THIS process back to its plaintext.

    The only way from sentinel to value. Raises SentinelError for anything
    that is not a well-formed token sealed under this process's key.
    """
    if not is_sentinel(token):
        raise SentinelError("not a sentinel token")
    body = token[len(_PREFIX) : -len(_SUFFIX)]
    try:
        blob = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (ValueError, TypeError) as exc:
        raise SentinelError("sentinel body is not base64url") from exc
    if len(blob) < _NONCE_LEN + _TAG_LEN:
        raise SentinelError("sentinel body too short")
    nonce, ct, tag = blob[:_NONCE_LEN], blob[_NONCE_LEN:-_TAG_LEN], blob[-_TAG_LEN:]
    key = _key()
    want = hmac.new(key, b"hs-v1 tag\x00" + nonce + ct, hashlib.sha256).digest()[:_TAG_LEN]
    if not hmac.compare_digest(want, tag):
        raise SentinelError("sentinel was not sealed by this process")
    data = bytes(a ^ b for a, b in zip(ct, _keystream(key, nonce, len(ct)), strict=True))
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - tag makes this unreachable
        raise SentinelError("sentinel plaintext is not utf-8") from exc


def resolve_outbound(
    text: str,
    *,
    where: str = "an outbound request",
    json_escaped: bool = False,
    url_escaped: bool = False,
    allow: Callable[[str], bool] | None = None,
) -> str:
    """Substitute plaintext for sentinels at the final network boundary.

    Every sentinel-shaped run in `text` is replaced with its unsealed value;
    one this process cannot unseal fails CLOSED with UnresolvedSentinelError
    (raised before any I/O — the caller must not have opened a connection
    yet). The plaintext is escaped for the context it is spliced into:
    `json_escaped=True` for serialized JSON, `url_escaped=True` for an
    already-encoded URL (percent-encoding, so a secret containing `&`, `=`,
    `+`, or a space cannot split the request or leak into a neighboring
    query parameter).

    `allow` scopes WHICH secrets may be spliced in: the registry is
    process-wide (every provider key, every connector's credential), and
    sentinels are visible to the model by design, so a caller that unseals
    model-supplied text must say which plaintext it is entitled to send —
    a connector its own key, nothing else. A sentinel whose plaintext the
    predicate rejects fails closed the same way as an unsealable one.
    """
    if not text or _PREFIX not in text:
        return text

    def replace(match: re.Match[str]) -> str:
        try:
            plain = unseal(match.group(0))
        except SentinelError as exc:
            raise UnresolvedSentinelError(
                f"refusing to send {where}: it contains a secret sentinel this "
                "process cannot unseal (the secret was never stored here, or the "
                "harness restarted since it was sealed)"
            ) from exc
        if allow is not None and not allow(plain):
            raise UnresolvedSentinelError(
                f"refusing to send {where}: it contains the sentinel of a secret "
                "this request is not entitled to carry"
            )
        if json_escaped:
            return json.dumps(plain, ensure_ascii=False)[1:-1]
        if url_escaped:
            return urllib.parse.quote(plain, safe="")
        return plain

    return SENTINEL_RE.sub(replace, text)


# -- registry ----------------------------------------------------------------


class SecretRedactionRegistry:
    """Bounded map of exact secret forms -> the sentinel that replaces them."""

    def __init__(self, max_values: int = MAX_VALUES, min_length: int = MIN_LENGTH) -> None:
        self.max_values = max_values
        self.min_length = min_length
        self._entries: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()
        self._ordered: list[tuple[str, str]] | None = None

    def register(self, value: str, label: str = "") -> str | None:
        """Register a secret's forms; returns its sentinel (None if too short).

        Order matters: the URL-encoded and JSON-escaped transforms go in
        first and the raw value LAST, so FIFO eviction always drops a
        transform before the raw credential it derives from.
        """
        if not isinstance(value, str):
            return None
        if len(value) < self.min_length:
            return None
        sentinel = seal(value, label)
        # Both URL encodings: quote() (%20 for a space — path segments) and
        # quote_plus() (+ for a space — urlencode'd query strings), so a
        # secret that rode either encoding into a log line is still caught.
        forms: list[str] = []
        for form in (
            urllib.parse.quote(value, safe=""),
            urllib.parse.quote_plus(value),
            json.dumps(value, ensure_ascii=False)[1:-1],
        ):
            if form != value and form not in forms:
                forms.append(form)
        forms.append(value)  # raw last: evicted last
        with self._lock:
            for form in forms:
                if form in self._entries:
                    self._entries.move_to_end(form)
                self._entries[form] = sentinel
            while len(self._entries) > self.max_values:
                self._entries.popitem(last=False)
            self._ordered = None
        return sentinel

    def scrub(self, text: str) -> str:
        """Replace every registered secret form in `text` with its sentinel."""
        if not text or not isinstance(text, str):
            return text
        with self._lock:
            if not self._entries:
                return text
            if self._ordered is None:
                # longest form first, so a secret that contains another
                # registered secret is replaced whole
                self._ordered = sorted(
                    self._entries.items(), key=lambda kv: len(kv[0]), reverse=True
                )
            ordered = self._ordered
        for form, sentinel in ordered:
            if form in text:
                text = text.replace(form, sentinel)
        return text

    def speech(self, text: str, *, final: bool = False) -> str:
        """Safe cumulative speech: hold secret prefixes across streaming chunks.

        Normal text transports retain sealed placeholders; a synthesizer must
        never pronounce those. Holding a possible trailing credential prefix
        also stops the first half escaping before the remaining bytes arrive.
        """
        text = SENTINEL_RE.sub("[private]", self.scrub(text))
        with self._lock:
            forms = list(self._entries)
        hold = 0
        # At the authoritative end there can be no later bytes completing a
        # secret. Release withheld ordinary letters (e.g. the h in "yeah").
        for form in [] if final else forms + [_PREFIX]:
            for size in range(min(len(form) - 1, len(text)), hold, -1):
                if text.endswith(form[:size]):
                    hold = size
                    break
        if hold:
            text = text[:-hold]
        # A sealed placeholder can itself arrive in pieces from the model.
        partial = text.rfind(_PREFIX)
        if partial >= 0:
            text = text[:partial]
        return text

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._ordered = None

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


#: The process-wide registry every scrub point uses.
_REGISTRY = SecretRedactionRegistry()


def registry() -> SecretRedactionRegistry:
    return _REGISTRY


def register_secret(value: str, label: str = "") -> str | None:
    """Register a secret value process-wide; returns its sentinel token."""
    return _REGISTRY.register(value, label)


def scrub(text: str) -> str:
    """Scrub every registered secret form out of `text` (sentinel stands in)."""
    return _REGISTRY.scrub(text)
