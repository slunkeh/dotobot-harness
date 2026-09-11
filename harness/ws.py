"""Minimal RFC 6455 WebSocket codec (stdlib only).

Enough of the protocol to run a JSON message bus over a single socket: the
handshake accept key, reading (unmasking) client frames, and writing server
frames. No third-party dependency, in keeping with the dependency-free runtime.

Only text/close/ping/pong are handled; fragmentation is not expected for our
small JSON messages but continuation frames are tolerated by concatenation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from typing import BinaryIO

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Chat messages are small JSON; uploads go over HTTP. Cap frames so a hostile
# client cannot declare a 2**63-byte payload and drive an unbounded allocation.
MAX_PAYLOAD = 8 * 1024 * 1024

CLOSE_GOING_AWAY = 1001  # server restarting (e.g. an update)
CLOSE_TOO_BIG = 1009


class PayloadTooLarge(ValueError):
    """A frame (or fragmented message) declared more bytes than allowed."""

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def accept_key(sec_websocket_key: str) -> str:
    """Compute the Sec-WebSocket-Accept response value."""
    digest = hashlib.sha1((sec_websocket_key + _GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _read_exact(rfile: BinaryIO, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = rfile.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def read_frame(rfile: BinaryIO, max_payload: int = MAX_PAYLOAD) -> tuple[int, bytes] | None:
    """Read one (possibly fragmented) message. Returns (opcode, payload) or None on EOF.

    Raises PayloadTooLarge before allocating when a frame, or the sum of a
    fragmented message's frames, declares more than max_payload bytes.
    """
    first_opcode: int | None = None
    payload = bytearray()
    while True:
        header = _read_exact(rfile, 2)
        if header is None:
            return None
        b1, b2 = header[0], header[1]
        fin = b1 & 0x80
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        length = b2 & 0x7F

        if length == 126:
            ext = _read_exact(rfile, 2)
            if ext is None:
                return None
            length = struct.unpack("!H", ext)[0]
        elif length == 127:
            ext = _read_exact(rfile, 8)
            if ext is None:
                return None
            length = struct.unpack("!Q", ext)[0]

        if length > max_payload or len(payload) + length > max_payload:
            raise PayloadTooLarge(f"frame declares {length} bytes (cap {max_payload})")

        mask = b""
        if masked:
            mask = _read_exact(rfile, 4) or b""
            if len(mask) != 4:
                return None

        data = _read_exact(rfile, length) if length else b""
        if data is None:
            return None
        if masked and data:
            # One big-int XOR instead of a per-byte Python loop — take-control
            # input events ride this path at pointer-move rate.
            n = len(data)
            repeated = (mask * (n // 4 + 1))[:n]
            data = (int.from_bytes(data, "big") ^ int.from_bytes(repeated, "big")).to_bytes(
                n, "big"
            )

        # control frames are not fragmented
        if opcode in (OP_CLOSE, OP_PING, OP_PONG):
            return opcode, bytes(data)

        if first_opcode is None:
            first_opcode = opcode if opcode != OP_CONT else OP_TEXT
        payload.extend(data)
        if fin:
            return first_opcode, bytes(payload)


def send_frame(wfile: BinaryIO, payload: bytes, opcode: int = OP_TEXT) -> None:
    """Write a single unmasked server frame."""
    header = bytearray()
    header.append(0x80 | opcode)  # FIN + opcode
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", n))
    wfile.write(bytes(header) + payload)
    wfile.flush()


def send_text(wfile: BinaryIO, text: str) -> None:
    send_frame(wfile, text.encode("utf-8"), OP_TEXT)


def send_json(wfile: BinaryIO, obj) -> None:
    send_text(wfile, json.dumps(obj, ensure_ascii=False))


def send_close(wfile: BinaryIO, code: int | None = None) -> None:
    try:
        body = struct.pack("!H", code) if code is not None else b""
        send_frame(wfile, body, OP_CLOSE)
    except OSError:
        pass
