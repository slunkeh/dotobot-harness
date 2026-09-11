"""Stdlib PDF text extraction: the operators it must read, best-effort edges."""

from __future__ import annotations

import zlib

from connectors import pdftext


def _pdf(content: bytes) -> bytes:
    return (
        b"%PDF-1.4\n1 0 obj\n<< /Length "
        + str(len(content)).encode("ascii")
        + b" >>\nstream\n"
        + content
        + b"\nendstream\nendobj\ntrailer\n%%EOF\n"
    )


def test_uncompressed_tj():
    out = pdftext.extract_text(_pdf(b"BT /F1 12 Tf 72 720 Td (Hello PDF) Tj ET"))
    assert out == "Hello PDF"


def test_flate_compressed_stream():
    content = zlib.compress(b"BT (compressed words) Tj ET")
    out = pdftext.extract_text(_pdf(content))
    assert "compressed words" in out


def test_flate_stream_whose_deflate_bytes_end_in_a_newline():
    # zlib.compress of this exact content ends in 0x0A. Stripping EOLs off
    # the captured stream bytes truncated the Adler-32 trailer, failed the
    # decompression, and silently lost the text (Bugbot on PR #141).
    content = zlib.compress(b"BT (line 19999 ends here) Tj ET")
    assert content.endswith(b"\n")
    out = pdftext.extract_text(_pdf(content))
    assert "line 19999 ends here" in out


def test_decompression_bomb_is_capped():
    # A few hundred KB of FlateDecode expanding to 64MB must not balloon
    # memory: output is capped at _MAX_STREAM and the leading ops still parse.
    bomb = zlib.compress(b"BT (small ops first) Tj ET" + b"\x00" * (64 * 1024 * 1024))
    assert len(bomb) < 1024 * 1024
    out = pdftext.extract_text(_pdf(bomb))
    assert "small ops first" in out


def test_tj_array_kerning_becomes_word_gap():
    out = pdftext.extract_text(_pdf(b"BT [(Hel) -50 (lo) -250 (world)] TJ ET"))
    assert "Hello world" in out


def test_hex_string_and_utf16():
    out = pdftext.extract_text(_pdf(b"BT <48656C6C6F> Tj (: ) Tj <FEFF00480069> Tj ET"))
    assert "Hello" in out
    assert "Hi" in out


def test_literal_escapes_and_octal():
    out = pdftext.extract_text(_pdf(rb"BT (a\(b\)c \101) Tj ET"))
    assert "a(b)c A" in out


def test_td_moves_break_lines():
    out = pdftext.extract_text(_pdf(b"BT (line one) Tj 0 -14 Td (line two) Tj ET"))
    assert "line one\n" in out
    assert "line two" in out


def test_non_pdf_bytes_come_back_empty():
    assert pdftext.extract_text(b"GIF89a not a pdf") == ""


def test_scanned_pdf_with_no_text_operators_comes_back_empty():
    # An image XObject stream, no BT/Tj anywhere.
    assert pdftext.extract_text(_pdf(b"\x00\x01\x02binary image bytes")) == ""


def test_limit_truncates_with_marker():
    out = pdftext.extract_text(_pdf(b"BT (" + b"a" * 500 + b") Tj ET"), limit=100)
    assert out.endswith("[truncated]")
    assert len(out) < 200
