"""Wrap untrusted external content in random boundary markers.

Port of OpenClaw v2's external-content wrapper (src/security/external-content.ts).
Every network-backed tool result — connector tools (linear_* and other
namespaced service tools, MCP connector tools, the generic `{type}_get` /
`{type}_request` runtime) and `preview_link` unfurls — is bounded,
normalized, and wrapped before the model sees it:

    <<<EXTERNAL_UNTRUSTED_CONTENT id="a1b2c3d4e5f6a7b8">>>
    ...content...
    <<<END_EXTERNAL_UNTRUSTED_CONTENT id="a1b2c3d4e5f6a7b8">>>

The marker id is fresh random bytes per wrap, so fetched content cannot
forge its own closing boundary and smuggle text outside the untrusted
envelope: a forged marker carries the wrong id and stays inside.

A small suspicious-pattern list (ignore-previous-instructions phrasing,
fake system headers, embedded shell such as `rm -rf`) is used for the bot
log only — flagged content is still delivered to the model, wrapped.
Blocking on a pattern match is deliberately out of scope: the false-positive
rate is too high, and the wrapper itself is the defense.

This is presentation of results, not governance. Nothing here decides
whether a tool may run — that is `agent/govern.py`'s job, and no policy
logic belongs in this module.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from collections.abc import Callable
from typing import Any

from .history import TOOL_RESULT_CHAR_LIMIT

MARKER = "EXTERNAL_UNTRUSTED_CONTENT"

#: chars of external content delivered per wrap; the rest is dropped with a
#: note inside the envelope. Override with $HARNESS_EXTERNAL_MAX_CHARS. The
#: effective bound is always clamped so the whole envelope fits under the
#: runtime's per-result cap (see _content_cap).
DEFAULT_MAX_CHARS = 64_000
_MAX_CHARS_ENV = "HARNESS_EXTERNAL_MAX_CHARS"

#: reserved for the two marker lines, the truncation note, and an `error: `
#: prefix a caller may keep outside the envelope. The runtime hard-caps every
#: tool result at TOOL_RESULT_CHAR_LIMIT (`cap_tool_result`); an envelope
#: bigger than that would lose its closing marker to the cap, and a forged
#: closer inside the body would become the only end the model sees.
_ENVELOPE_OVERHEAD = 256

# ANSI escape sequences (CSI plus lone ESC-letter forms) and the remaining
# C0/C7F control characters, newline and tab excepted. External text gets
# no terminal control: an escape sequence in a fetched page is at best noise
# and at worst a way to redraw over the envelope in a raw log view.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: (name, pattern) pairs flagged in the bot log. Logging only — matching
#: content is still delivered, wrapped. Names are stable identifiers an
#: operator can grep the bot log for.
SUSPICIOUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore-instructions",
        re.compile(
            r"(?i)\b(ignore|disregard|forget|override)\b[^\n.]{0,40}"
            r"\b(previous|prior|above|earlier|all|any)\s+"
            r"(instructions?|prompts?|rules?|directives?|context)"
        ),
    ),
    (
        "new-instructions",
        re.compile(
            r"(?i)\b(new|real|actual|true|updated)\s+(instructions?|task|mission|objective)\s*:"
        ),
    ),
    (
        "fake-system-header",
        re.compile(
            r"(?im)^[ \t>*#-]*\[?\b(system|assistant|developer)\b\]?\s*(message|prompt|note)?\s*:"
        ),
    ),
    (
        "prompt-markup",
        re.compile(r"(?i)<\|im_(start|end)\|>|\[/?(INST|SYS)\]|<\|(system|assistant|user)\|>"),
    ),
    ("boundary-forgery", re.compile(r"<<<\s*(END_)?EXTERNAL_UNTRUSTED_CONTENT")),
    (
        "embedded-shell",
        re.compile(
            r"(?i)\brm\s+-[a-z]*[rf][a-z]*\b"
            r"|\b(curl|wget)\b[^\n;|&]{0,120}\|\s*(ba|z|da)?sh\b"
            r"|\bmkfs\.|\bdd\s+if="
        ),
    ),
    (
        "credential-exfil",
        re.compile(
            r"(?i)\b(send|post|reveal|share|forward|exfiltrate|paste)\b[^\n.]{0,60}"
            r"\b(api[ _-]?key|secret|token|password|credential)s?\b"
        ),
    ),
)


def scan_suspicious(text: str) -> list[str]:
    """Names of the suspicious patterns matching `text` (logging only)."""
    return [name for name, pattern in SUSPICIOUS_PATTERNS if pattern.search(text)]


def _max_chars() -> int:
    raw = os.environ.get(_MAX_CHARS_ENV, "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_CHARS
    return value if value > 0 else DEFAULT_MAX_CHARS


def _content_cap(requested: int) -> int:
    """Clamp so envelope + overhead fits under the runtime's tool-result cap."""
    return min(requested, TOOL_RESULT_CHAR_LIMIT - _ENVELOPE_OVERHEAD)


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ANSI.sub("", text)
    return _CTRL.sub("", text)


def _log_flags(log: Callable[[str], None] | None, source: str, hits: list[str]) -> None:
    line = (
        f"suspicious external content ({source or 'unknown source'}): "
        f"{', '.join(hits)} — delivered wrapped, not blocked"
    )
    if log is not None:
        log(line)
        return
    # Bot processes log to stdout ($HARNESS_HOME/run/<bot>.log), same shape
    # as Agent._log; recording must never fail the wrap.
    try:
        print(f"[{time.strftime('%H:%M:%S')}] {line}", flush=True)
    except Exception:
        pass


def wrap_external(
    content: Any,
    source: str = "",
    *,
    max_chars: int | None = None,
    log: Callable[[str], None] | None = None,
) -> str:
    """Bound, normalize, and wrap one external result for the model.

    `source` names where the content came from (a tool name, a URL) for the
    bot-log flag line. `max_chars` overrides the size bound (default
    $HARNESS_EXTERNAL_MAX_CHARS or DEFAULT_MAX_CHARS). `log` replaces the
    stdout flag writer — tests, or a caller with its own logger.
    """
    text = _normalize(str(content if content is not None else ""))
    limit = _content_cap(max_chars if max_chars is not None else _max_chars())
    total = len(text)
    if total > limit:
        text = (
            text[:limit].rstrip()
            + f"\n[external content truncated: {total - limit} of {total} characters dropped]"
        )
    hits = scan_suspicious(text)
    if hits:
        _log_flags(log, source, hits)
    boundary = secrets.token_hex(8)
    return f'<<<{MARKER} id="{boundary}">>>\n{text}\n<<<END_{MARKER} id="{boundary}">>>'
