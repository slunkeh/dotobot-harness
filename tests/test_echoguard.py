"""inbound echo suppression at message admission.

A delayed copy of a bot's own outbound message (bus write or room append)
must be dropped at admission — before session recording or dispatch — so
it can never start an A→B→A reply loop. One process-shared guard
(`agent.echoguard`), reservation before the send completes, bounded map.
"""

from __future__ import annotations

import shutil

from agent import echoguard, messaging
from agent.echoguard import EchoGuard
from agent.history import user_thread
from agent.memory import Memory
from agent.runtime import Agent
from harness.control import Control
from harness.paths import HarnessPaths
from harness.rooms import append_message, create_room
from harness.roster import Bot
from providers.echo import EchoProvider


def _paths(tmp_path, names=("atlas", "nova")):
    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(list(names))
    return paths


def _agent(paths, name):
    return Agent(
        paths=paths,
        bot=Bot(name=name, role="assistant", provider="echo"),
        provider=EchoProvider(persona=name),
        memory=Memory(paths=paths, bot=name),
        control=Control(paths),
        stream_delay=0.0,
        reply_timeout=2.0,
    )


def _fresh_guard(monkeypatch, **kwargs):
    """Swap the process singleton so tests never see each other's entries."""
    g = EchoGuard(**kwargs)
    monkeypatch.setattr(echoguard, "_GUARD", g)
    return g


# -- the guard itself --------------------------------------------------------


def test_reserve_matches_the_exact_identity_only():
    g = EchoGuard()
    g.reserve("atlas", "nova", "m1")
    assert g.is_echo("atlas", "nova", "m1")
    assert not g.is_echo("nova", "nova", "m1")
    assert not g.is_echo("atlas", "room-1", "m1")
    assert not g.is_echo("atlas", "nova", "m2")


def test_blank_sender_or_id_is_never_recorded():
    g = EchoGuard()
    g.record("", "nova", "m1")
    g.record("atlas", "nova", "")
    assert len(g) == 0
    assert not g.is_echo("", "nova", "m1")


def test_ttl_expiry_allows_a_genuine_later_identical_message():
    t = [0.0]
    g = EchoGuard(ttl=30.0, clock=lambda: t[0])
    g.record("atlas", "nova", "m1")
    assert g.is_echo("atlas", "nova", "m1")
    t[0] = 30.1
    assert not g.is_echo("atlas", "nova", "m1")
    # the expired entry was dropped, not revived by the miss
    assert not g.is_echo("atlas", "nova", "m1")


def test_hit_refreshes_ttl_so_redelivered_echoes_stay_suppressed():
    t = [0.0]
    g = EchoGuard(ttl=30.0, clock=lambda: t[0])
    g.record("atlas", "nova", "m1")
    t[0] = 20.0
    assert g.is_echo("atlas", "nova", "m1")
    t[0] = 40.0  # inside the refreshed window, outside the original one
    assert g.is_echo("atlas", "nova", "m1")


def test_map_stays_bounded_under_flood():
    g = EchoGuard(cap=100)
    for i in range(500):
        g.record("atlas", "nova", f"m{i}")
    assert len(g) == 100
    assert not g.is_echo("atlas", "nova", "m0")  # oldest evicted
    assert g.is_echo("atlas", "nova", "m499")  # newest kept


def test_lru_refresh_on_hit_protects_hot_entries_from_eviction():
    g = EchoGuard(cap=2)
    g.record("atlas", "nova", "m1")
    g.record("atlas", "nova", "m2")
    assert g.is_echo("atlas", "nova", "m1")  # refresh: m2 is now the oldest
    g.record("atlas", "nova", "m3")
    assert g.is_echo("atlas", "nova", "m1")
    assert not g.is_echo("atlas", "nova", "m2")


def test_expired_entries_are_pruned_lazily_on_insert():
    t = [0.0]
    g = EchoGuard(ttl=30.0, clock=lambda: t[0])
    for i in range(5):
        g.record("atlas", "nova", f"m{i}")
    t[0] = 31.0
    g.record("atlas", "nova", "fresh")
    assert len(g) == 1


def test_one_shared_guard_per_process():
    assert echoguard.guard() is echoguard.guard()


# -- recording at the outbound chokepoints -----------------------------------


def test_send_reserves_identity_before_the_inbox_file_is_visible(tmp_path, monkeypatch):
    g = _fresh_guard(monkeypatch)
    paths = _paths(tmp_path)
    msg = messaging.Msg(to="nova", frm="atlas", text="ping")
    seen = {}
    real_replace = messaging.os.replace

    def replace(src, dst):
        seen["reserved"] = g.is_echo("atlas", "nova", msg.id)
        return real_replace(src, dst)

    monkeypatch.setattr(messaging.os, "replace", replace)
    messaging.send(paths, msg)
    assert seen["reserved"] is True


def test_room_append_records_identity_with_a_message_id(tmp_path, monkeypatch):
    g = _fresh_guard(monkeypatch)
    paths = _paths(tmp_path)
    room = create_room(paths, "war room", ["atlas", "nova"])
    record = append_message(paths, room.id, frm="atlas", text="shipping it")
    assert record["id"]
    assert g.is_echo("atlas", room.id, record["id"])


# -- admission ---------------------------------------------------------------


def test_echoed_inbox_file_dropped_at_admission(tmp_path, monkeypatch):
    _fresh_guard(monkeypatch)
    paths = _paths(tmp_path)
    atlas = _agent(paths, "atlas")
    sent = messaging.send(paths, messaging.Msg(to="nova", frm="atlas", text="do the thing"))
    # a delayed copy of our own outbound message lands back in our inbox
    shutil.copy(sent, paths.inbox("atlas") / sent.name)
    assert atlas.process_inbox_once() is False  # nothing was admitted
    assert not list(paths.inbox("atlas").glob("*.json"))  # archived, not left queued
    assert user_thread(atlas.memory, peer="user") == []  # no session recording
    # the genuine delivery to nova is untouched
    assert len(list(paths.inbox("nova").glob("*.json"))) == 1


def test_room_echo_of_own_transcript_append_is_dropped(tmp_path, monkeypatch):
    _fresh_guard(monkeypatch)
    paths = _paths(tmp_path)
    atlas = _agent(paths, "atlas")
    room = create_room(paths, "war room", ["atlas", "nova"])
    record = append_message(paths, room.id, frm="atlas", text="shipping it")
    echo = messaging.Msg(to="atlas", frm="atlas", text="shipping it", id=record["id"], room=room.id)
    # written straight into the inbox: only the append's record can match
    inbox = paths.inbox("atlas")
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{echo.ts:.6f}-{echo.id}.json").write_text(echo.to_json(), encoding="utf-8")
    assert atlas.process_inbox_once() is False
    assert not list(inbox.glob("*.json"))
    assert user_thread(atlas.memory, peer="user") == []


def test_genuine_user_message_admitted_when_sender_shares_the_process(tmp_path, monkeypatch):
    _fresh_guard(monkeypatch)
    paths = _paths(tmp_path)
    atlas = _agent(paths, "atlas")
    # send() records (user -> atlas) in the same process-wide guard the bot
    # checks — the delivery must still go through (the guard only suppresses
    # a bot's OWN outbound identities).
    messaging.send(paths, messaging.Msg(to="atlas", frm="user", text="hello"))
    assert atlas.process_inbox_once() is True
    rows = user_thread(atlas.memory, peer="user")
    assert [r["frm"] for r in rows] == ["user", "atlas"]


def test_peer_bot_message_is_not_an_echo(tmp_path, monkeypatch):
    _fresh_guard(monkeypatch)
    paths = _paths(tmp_path)
    atlas = _agent(paths, "atlas")
    messaging.send(paths, messaging.Msg(to="atlas", frm="nova", text="over to you"))
    assert atlas.process_inbox_once() is True
    assert not list(paths.inbox("atlas").glob("*.json"))


def test_expired_identity_admits_a_genuine_later_identical_message(tmp_path, monkeypatch):
    t = [1000.0]
    _fresh_guard(monkeypatch, ttl=30.0, clock=lambda: t[0])
    paths = _paths(tmp_path)
    atlas = _agent(paths, "atlas")
    sent = messaging.send(paths, messaging.Msg(to="nova", frm="atlas", text="status?"))
    t[0] += 31.0
    shutil.copy(sent, paths.inbox("atlas") / sent.name)
    # past the TTL this is no longer "our echo" — it is fresh input
    assert atlas.process_inbox_once() is True
