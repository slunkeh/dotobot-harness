"""A `StreamEvent` field is declared in one place and restated in two others.

`StreamEvent` declares the fields, `harness/server.py`'s `asdict_event` decides
which reach a client, and `StreamEvent.from_dict` decides which survive being
read back off disk. Three hand-maintained lists, and adding a field to only the
first is silent: the card renders locally, the value never reaches the app, and
nothing anywhere says so.

This is the drift check. It is deliberately **not** "all three lists match",
because two of the current asymmetries are correct and a test demanding
symmetry would have to be neutered to pass:

* `ts` is stamped when the event is written. Clients order events by the
  server's `{epoch, seq}` instead, so it does not travel.
* `offset` is computed by the *reader* (`StreamReader` sets it after measuring
  the line), so it is never in the JSON on disk for `from_dict` to find — but
  it does reach the client, which is what a resuming app needs.

So the exceptions are named, with the reason, and everything else must appear
in both. A new field is caught; the two known asymmetries stay documented where
somebody changing them will look.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent.streaming import StreamEvent

ROOT = Path(__file__).resolve().parent.parent

#: Declared, but deliberately not sent to clients.
NOT_SENT = {
    "ts": "stamped locally; clients order by the server's {epoch, seq}",
}

#: Sent, but deliberately not read back — derived by the reader rather than
#: stored, so there is nothing on disk to parse.
NOT_PARSED = {
    "offset": "computed by StreamReader after measuring the line, never stored",
}


def _declared() -> list[str]:
    source = (ROOT / "agent" / "streaming.py").read_text(encoding="utf-8")
    block = re.search(r"class StreamEvent.*?(?=\n@|\nclass |\ndef )", source, re.S)
    assert block, "could not find the StreamEvent declaration"
    return re.findall(r"^    ([a-z_]+)\s*:", block.group(0), re.M)


def _sent() -> set[str]:
    source = (ROOT / "harness" / "server.py").read_text(encoding="utf-8")
    block = re.search(r"def asdict_event\(ev\):.*?\n    \}", source, re.S)
    assert block, "could not find asdict_event"
    return set(re.findall(r'"([a-z_]+)":', block.group(0)))


def _parsed() -> set[str]:
    source = (ROOT / "agent" / "streaming.py").read_text(encoding="utf-8")
    block = re.search(r"def from_dict.*?(?=\n    def |\n@|\nclass |\ndef )", source, re.S)
    assert block, "could not find from_dict"
    return set(re.findall(r'"([a-z_]+)"', block.group(0)))


def test_every_declared_field_reaches_a_client():
    missing = [f for f in _declared() if f not in _sent() and f not in NOT_SENT]
    assert not missing, (
        f"StreamEvent declares {missing} but asdict_event does not send them, so they "
        "never reach the app. Add them there, or add them to NOT_SENT in this file "
        "with the reason they stay local."
    )


def test_every_sent_field_survives_being_read_back():
    missing = [
        f for f in _declared() if f in _sent() and f not in _parsed() and f not in NOT_PARSED
    ]
    assert not missing, (
        f"asdict_event sends {missing} but from_dict drops them, so a client that "
        "resumes a stream loses them. Add them there, or add them to NOT_PARSED "
        "in this file with the reason they are derived rather than stored."
    )


def test_the_exceptions_still_describe_real_fields():
    """A stale exception is a hole nobody knows is open."""
    declared = set(_declared())
    for name in {**NOT_SENT, **NOT_PARSED}:
        assert name in declared, f"{name} is excepted here but no longer a StreamEvent field"


def test_the_exceptions_are_still_needed():
    """If a field named here HAS been wired up, the exception should go — it is
    otherwise a licence for the next one to be forgotten."""
    for name in NOT_SENT:
        assert name not in _sent(), f"{name} is sent now; drop it from NOT_SENT"
    for name in NOT_PARSED:
        assert name not in _parsed(), f"{name} is parsed now; drop it from NOT_PARSED"


def test_a_card_round_trips_with_its_fields_intact():
    """The mechanical check above is about names. This one is about values —
    the fields a card actually rides on survive a real round trip."""
    from harness.server import asdict_event

    event = StreamEvent(
        type="card",
        card_type="table",
        payload={"rows": [[1, 2]]},
        id="c1",
        mutation="updated",
        streaming=False,
        resolution="answered",
    )
    sent = {k: v for k, v in asdict_event(event).items() if v is not None}
    back = StreamEvent.from_dict(sent)

    assert back.card_type == "table"
    assert back.payload == {"rows": [[1, 2]]}
    assert back.id == "c1"
    assert back.mutation == "updated"
    assert back.resolution == "answered"


def test_streaming_false_survives_the_relay_filter():
    """The relay drops None, not falsey. `streaming=False` is meaningful and
    has to reach the client, which is a distinction that is easy to lose."""
    from harness.server import asdict_event

    sent = {
        k: v
        for k, v in asdict_event(StreamEvent(type="card", streaming=False)).items()
        if v is not None
    }
    assert sent["streaming"] is False
    assert StreamEvent.from_dict(sent).streaming is False
