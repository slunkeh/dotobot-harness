"""An idle desktop should not cost `fps` frames a second on the wire.

ffmpeg's x11grab emits frames whether or not anything moved, and every one of
them crossed the WebSocket — including to the iOS client over someone's LAN,
with the screen sitting still.
"""

from __future__ import annotations

from harness.screen import dedupe_frames


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _frames(*payloads: bytes):
    for payload in payloads:
        yield payload, "image/jpeg"


def test_an_unchanged_frame_is_not_sent_twice():
    clock = _Clock()
    out = [f for f, _ in dedupe_frames(_frames(b"A", b"A", b"A"), keepalive=99, clock=clock)]
    assert out == [b"A"]


def test_a_change_is_always_sent():
    clock = _Clock()
    out = [f for f, _ in dedupe_frames(_frames(b"A", b"B", b"A"), keepalive=99, clock=clock)]
    assert out == [b"A", b"B", b"A"]


def test_an_idle_stream_still_beats_periodically():
    """A viewer connecting mid-stall must not wait for the user to jiggle the
    mouse before it sees anything."""
    clock = _Clock()

    def ticking():
        for _ in range(5):
            clock.t += 1.0
            yield b"SAME", "image/jpeg"

    out = [f for f, _ in dedupe_frames(ticking(), keepalive=2.0, clock=clock)]
    # t=1 (first), then t=3 and t=5 clear the 2s keepalive
    assert len(out) == 3


def test_the_mime_type_rides_along():
    clock = _Clock()
    out = list(dedupe_frames(iter([(b"A", "image/png")]), keepalive=99, clock=clock))
    assert out == [(b"A", "image/png")]


def test_an_empty_stream_is_empty():
    assert list(dedupe_frames(iter([]), clock=_Clock())) == []


def test_identical_frames_far_apart_are_both_sent():
    clock = _Clock()

    def spaced():
        yield b"A", "image/jpeg"
        clock.t += 10.0
        yield b"A", "image/jpeg"

    assert len([f for f, _ in dedupe_frames(spaced(), keepalive=2.0, clock=clock)]) == 2


def test_dedupe_is_by_content_not_identity():
    clock = _Clock()
    # built at runtime so the compiler cannot fold them into one object
    a1 = bytes(bytearray(b"same-bytes"))
    a2 = bytes(bytearray(b"same-bytes"))
    assert a1 is not a2
    out = [f for f, _ in dedupe_frames(_frames(a1, a2), keepalive=99, clock=clock)]
    assert out == [a1]
