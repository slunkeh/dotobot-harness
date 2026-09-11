"""Best-effort PDF text extraction, stdlib only.

The runtime is dependency-free, so there is no pypdf to lean on. This pulls
the text-showing operators (Tj / ' / " / TJ) out of every content stream it
can decode (FlateDecode via zlib, or raw), which covers digitally-produced
PDFs — invoices, reports, tickets. Scanned/image PDFs and exotic font
encodings come back empty; callers should say so and point at the saved
file instead of failing the turn.
"""

from __future__ import annotations

import re
import zlib

_STREAM_RE = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)
_NUMBER_RE = re.compile(rb"[-+]?\d*\.?\d+")
_WORD_RE = re.compile(rb"[A-Za-z'\"*]+")
#: TJ kerning more negative than this reads as a word gap.
_TJ_SPACE = -180
#: cap on one decompressed content stream, so a FlateDecode bomb in a small
#: attachment cannot exhaust memory.
_MAX_STREAM = 10 * 1024 * 1024


def _decode_string(raw: bytes) -> str:
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", "replace")
    return raw.decode("latin-1", "replace")


_ESCAPES = {
    b"n": b"\n",
    b"r": b"\r",
    b"t": b"\t",
    b"b": b"\b",
    b"f": b"\f",
    b"(": b"(",
    b")": b")",
    b"\\": b"\\",
}


def _literal(data: bytes, i: int) -> tuple[str, int]:
    """Parse a `(...)` literal string starting at data[i] == '('. Returns
    (text, index past the closing paren)."""
    out = bytearray()
    depth = 0
    i += 1
    n = len(data)
    while i < n:
        b = data[i : i + 1]
        if b == b"\\":
            nxt = data[i + 1 : i + 2]
            if nxt in _ESCAPES:
                out += _ESCAPES[nxt]
                i += 2
            elif nxt.isdigit():
                j = i + 1
                while j < min(i + 4, n) and data[j : j + 1] in b"01234567":
                    j += 1
                if j > i + 1:
                    out.append(int(data[i + 1 : j], 8) & 0xFF)
                    i = j
                else:
                    i += 1
            elif nxt in (b"\n", b"\r"):
                i += 2  # line continuation
            else:
                i += 1
        elif b == b"(":
            depth += 1
            out += b
            i += 1
        elif b == b")":
            if depth == 0:
                return _decode_string(bytes(out)), i + 1
            depth -= 1
            out += b
            i += 1
        else:
            out += b
            i += 1
    return _decode_string(bytes(out)), n


def _hex_string(data: bytes, i: int) -> tuple[str, int]:
    """Parse a `<...>` hex string starting at data[i] == '<'."""
    end = data.find(b">", i + 1)
    if end < 0:
        end = len(data)
    digits = re.sub(rb"[^0-9A-Fa-f]", b"", data[i + 1 : end])
    if len(digits) % 2:
        digits += b"0"
    try:
        return _decode_string(bytes.fromhex(digits.decode("ascii"))), end + 1
    except ValueError:
        return "", end + 1


def _extract_stream(stream: bytes) -> str:
    """Text-showing operators out of one content stream."""
    out: list[str] = []
    pending: list[str] = []
    i, n = 0, len(stream)
    while i < n:
        b = stream[i : i + 1]
        if b == b"(":
            text, i = _literal(stream, i)
            pending.append(text)
        elif b == b"<" and stream[i : i + 2] != b"<<":
            text, i = _hex_string(stream, i)
            pending.append(text)
        elif b == b"%":
            nl = stream.find(b"\n", i)
            i = n if nl < 0 else nl + 1
        elif m := _NUMBER_RE.match(stream, i):
            # TJ kerning: a large negative adjustment between strings is a
            # word gap the PDF chose to draw instead of a space character.
            try:
                if pending and float(m.group()) <= _TJ_SPACE:
                    pending.append(" ")
            except ValueError:
                pass
            i = m.end()
        elif m := _WORD_RE.match(stream, i):
            op = m.group()
            if op in (b"Tj", b"TJ", b"'", b'"'):
                if op in (b"'", b'"') and out and not out[-1].endswith("\n"):
                    out.append("\n")
                out.append("".join(pending))
            elif op in (b"Td", b"TD", b"T*", b"ET") and out and not out[-1].endswith("\n"):
                out.append("\n")
            pending = []
            i = m.end()
        else:
            i += 1
    return "".join(out)


def _content_streams(data: bytes):
    for match in _STREAM_RE.finditer(data):
        # Keep the bytes exactly as captured: a deflate stream may itself end
        # in 0x0A/0x0D, so stripping EOLs here corrupts valid FlateDecode.
        # decompressobj tolerates the trailing EOL before `endstream` (it
        # lands in unused_data) and caps the output, so a decompression bomb
        # inside a small attachment cannot balloon past _MAX_STREAM.
        raw = match.group(1)
        try:
            raw = zlib.decompressobj().decompress(raw, _MAX_STREAM)
        except zlib.error:
            pass  # uncompressed, or a filter we cannot undo — try as-is
        if b"Tj" in raw or b"TJ" in raw or b"BT" in raw:
            yield raw


def extract_text(data: bytes, limit: int = 8000) -> str:
    """Plain text from a PDF, best effort; empty when nothing is extractable
    (scanned pages, encryption, unsupported filters)."""
    if not data.startswith(b"%PDF"):
        return ""
    pieces = [text for stream in _content_streams(data) if (text := _extract_stream(stream))]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(pieces)).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n[truncated]"
    return text
