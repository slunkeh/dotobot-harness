"""Secret redaction registry + sentinel tokens.

Registry: bounded, min-length, raw-value-last eviction. Scrubber: raw,
URL-encoded, and JSON-escaped forms. Sentinels: stable round-trip, tamper
rejection, plaintext substitution only at the network boundary, and
fail-closed (no I/O) on an unsealable sentinel. End-to-end: after a
`request_secret` flow with the keyless echo provider, the value is nowhere
on disk — session JSONLs and stream files carry the sentinel instead.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request

import pytest

from agent.runtime import build_agent
from agent.streaming import StreamReader, StreamWriter, list_prompts
from connectors import generic
from connectors.base import ConnectorContext
from harness import redaction
from harness.connectors import Connectors
from harness.paths import HarnessPaths
from harness.redaction import (
    SecretRedactionRegistry,
    SentinelError,
    UnresolvedSentinelError,
    is_sentinel,
    register_secret,
    resolve_outbound,
    scrub,
    seal,
    unseal,
)
from harness.roster import Bot
from harness.secrets import get_secret, set_secret
from providers.anthropic import AnthropicProvider
from providers.base import Auth, Message


@pytest.fixture(autouse=True)
def _clean_registry():
    redaction.registry().clear()
    yield
    redaction.registry().clear()


def _paths(tmp_path) -> HarnessPaths:
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas"])
    return p


def _forged_sentinel() -> str:
    """Sentinel-shaped, but never sealed by this process's key."""
    token = "hs-v1." + "A" * 44 + ".end"
    assert is_sentinel(token)
    return token


# -- registry ---------------------------------------------------------------


def test_short_values_are_never_registered():
    assert register_secret("tiny") is None
    assert scrub("tiny problem") == "tiny problem"


def test_scrubber_catches_raw_url_encoded_and_json_escaped_forms():
    value = 'pa"ss wo\\rd&+'
    sentinel = register_secret(value, "DEMO")
    assert sentinel and is_sentinel(sentinel)
    line = (
        f"login failed for token={value} "
        f"url=https://api.example/?key={urllib.parse.quote(value, safe='')} "
        f"payload={json.dumps({'key': value}, ensure_ascii=False)}"
    )
    out = scrub(line)
    assert value not in out
    assert urllib.parse.quote(value, safe="") not in out
    assert json.dumps(value, ensure_ascii=False)[1:-1] not in out
    assert out.count(sentinel) == 3
    # urlencode/quote_plus writes spaces as `+`, not %20 — caught too
    plus_line = f"GET /?key={urllib.parse.quote_plus(value)} failed"
    assert urllib.parse.quote_plus(value) not in scrub(plus_line)
    assert sentinel in scrub(plus_line)


def test_same_secret_yields_same_sentinel_and_no_reverse_map():
    value = "sw0rdfish-token"
    assert register_secret(value, "A") == register_secret(value, "A")
    # the registry holds forms -> sentinel, never sentinel -> plaintext:
    # the only way back is unseal()
    assert unseal(register_secret(value, "A")) == value


def test_bounded_eviction_drops_transforms_before_the_raw_credential():
    reg = SecretRedactionRegistry(max_values=4, min_length=6)
    value = 'sw"ord fish'  # transforms differ from raw: 3 entries
    reg.register(value)
    url_form = urllib.parse.quote(value, safe="")
    json_form = json.dumps(value, ensure_ascii=False)[1:-1]
    assert reg.scrub(url_form) != url_form
    assert reg.scrub(json_form) != json_form

    reg.register("filler-000001")  # 4 entries: full
    reg.register("filler-000002")  # overflow: the URL transform goes first
    assert reg.scrub(url_form) == url_form  # transform evicted
    assert reg.scrub(value) != value  # raw credential still scrubbed

    reg.register("filler-000003")  # next out is the JSON transform
    assert reg.scrub(json_form) == json_form
    assert reg.scrub(value) != value  # raw is always the last of its trio


def test_registry_respects_max_values_bound():
    reg = SecretRedactionRegistry(max_values=8, min_length=6)
    for i in range(50):
        reg.register(f"secret-value-{i:04d}")
    assert len(reg) == 8


# -- sentinel sealing -------------------------------------------------------


def test_sentinel_round_trip_and_stability():
    token = seal("hunter2-secret", "API_KEY")
    assert is_sentinel(token)
    assert token.startswith("hs-v1.") and token.endswith(".end")
    assert unseal(token) == "hunter2-secret"
    assert seal("hunter2-secret", "API_KEY") == token  # stable per process
    assert seal("hunter2-secret", "OTHER") != token  # label feeds the nonce
    assert unseal(seal("hunter2-secret", "OTHER")) == "hunter2-secret"


def test_unseal_rejects_forged_and_tampered_tokens():
    with pytest.raises(SentinelError):
        unseal(_forged_sentinel())
    real = seal("hunter2-secret")
    body = real[len("hs-v1.") : -len(".end")]
    flipped = "hs-v1." + ("B" + body[1:] if body[0] != "B" else "C" + body[1:]) + ".end"
    with pytest.raises(SentinelError):
        unseal(flipped)
    with pytest.raises(SentinelError):
        unseal("not-a-token")
    assert not is_sentinel("hs-v1.short.end")


def test_resolve_outbound_substitutes_plaintext():
    token = seal("tok-abc123XYZ")
    assert resolve_outbound(f"Bearer {token}") == "Bearer tok-abc123XYZ"
    awkward = seal('va"l')
    quoted = resolve_outbound(f'{{"key": "{awkward}"}}', json_escaped=True)
    assert json.loads(quoted)["key"] == 'va"l'
    # text without sentinels passes through untouched
    assert resolve_outbound("plain text") == "plain text"


def test_resolve_outbound_fails_closed_on_unsealable_sentinel():
    with pytest.raises(UnresolvedSentinelError):
        resolve_outbound(f"Bearer {_forged_sentinel()}")


# -- network boundaries -----------------------------------------------------


def _no_network(monkeypatch):
    calls: list = []

    def boom(*a, **k):  # any urlopen call is a leaked request
        calls.append(a)
        raise AssertionError("network I/O attempted with an unresolved sentinel")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(generic, "_open", boom)
    return calls


def test_unresolved_sentinel_at_connector_boundary_raises_before_io(tmp_path, monkeypatch):
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("mailchimp", "mailchimp", secret=_forged_sentinel() + "-us6")
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    calls = _no_network(monkeypatch)
    with pytest.raises(UnresolvedSentinelError):
        generic._get(ctx, {"path": "/lists"})
    assert calls == []  # refused before any request left


def test_sentinel_in_tool_args_is_unsealed_at_the_connector_boundary(tmp_path, monkeypatch):
    """The connector's OWN key may ride in a path/query/body as a sentinel
    (APIs that want it as a parameter); it is unsealed at the boundary."""
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("mailchimp", "mailchimp", secret="tok-abc123XYZ-us6")
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    token = seal("tok-abc123XYZ-us6")
    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["body"] = (req.data or b"").decode("utf-8")

        class Resp:
            status = 200

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return Resp()

    monkeypatch.setattr(generic, "_open", fake_urlopen)
    out = generic._request(ctx, {"method": "POST", "path": f"/lists/{token}", "body": {"k": token}})
    assert "HTTP 200" in out
    assert token not in seen["url"] and "tok-abc123XYZ-us6" in seen["url"]
    assert token not in seen["body"] and json.loads(seen["body"])["k"] == "tok-abc123XYZ-us6"

    # a secret full of URL metacharacters is percent-encoded into the query,
    # so it cannot split the request or leak into a neighboring parameter
    awkward = "tok&next=evil bar+baz?x-us6"
    record2 = Connectors(paths).add("mailchimp", "mailchimp", secret=awkward)
    ctx2 = ConnectorContext(paths=paths, bot="atlas", record=record2)
    out = generic._get(ctx2, {"path": "/lists", "query": {"k": seal(awkward), "keep": "1"}})
    assert "HTTP 200" in out
    query = urllib.parse.parse_qs(urllib.parse.urlparse(seen["url"]).query, strict_parsing=True)
    assert query == {"k": [awkward], "keep": ["1"]}


def test_a_foreign_secrets_sentinel_is_refused_at_the_connector_boundary(tmp_path, monkeypatch):
    """Sentinels are visible to the model by design and the registry is
    process-wide, so a page could tell the bot to put the provider key's
    sentinel in a Telegram message. Only the connector's own credential may
    be spliced into a request; anything else is refused before any I/O."""
    paths = HarnessPaths(home=tmp_path)
    record = Connectors(paths).add("mailchimp", "mailchimp", secret="abc-us6")
    ctx = ConnectorContext(paths=paths, bot="atlas", record=record)
    register_secret("sk-ant-provider-key-0123456789", "ANTHROPIC_API_KEY")
    foreign = seal("sk-ant-provider-key-0123456789")
    calls = _no_network(monkeypatch)
    with pytest.raises(UnresolvedSentinelError):
        generic._get(ctx, {"path": "/lists", "query": {"text": foreign}})
    with pytest.raises(UnresolvedSentinelError):
        generic._request(ctx, {"method": "POST", "path": f"/x/{foreign}", "body": {}})
    with pytest.raises(UnresolvedSentinelError):
        generic._request(ctx, {"method": "POST", "path": "/x", "body": {"k": foreign}})
    assert calls == []


def test_unresolved_sentinel_in_provider_auth_raises_before_io(monkeypatch):
    calls = _no_network(monkeypatch)
    with pytest.raises(UnresolvedSentinelError):
        Auth(api_key=_forged_sentinel()).header_key()
    with pytest.raises(UnresolvedSentinelError):
        Auth(oauth_token=_forged_sentinel()).bearer()
    provider = AnthropicProvider(model="claude-x", auth=Auth(api_key=_forged_sentinel()))
    with pytest.raises(UnresolvedSentinelError):
        provider.complete([Message(role="user", content="hi")])
    assert calls == []


def test_provider_auth_unseals_a_sealed_api_key():
    set_key = seal("sk-ant-hunter2-secret", "ANTHROPIC_API_KEY")
    assert Auth(api_key=set_key).header_key() == "sk-ant-hunter2-secret"


# -- scrub points -----------------------------------------------------------


def test_stream_writer_scrubs_registered_secrets(tmp_path):
    paths = _paths(tmp_path)
    sentinel = register_secret("hunter2-sup3r-secret", "DEMO_TOKEN")
    w = StreamWriter(paths, "r-scrub")
    w.delta("the token is hunter2-sup3r-secret ok")
    w.final("the token is hunter2-sup3r-secret ok", "atlas")
    raw = paths.stream_file("r-scrub").read_text(encoding="utf-8")
    assert "hunter2-sup3r-secret" not in raw
    assert sentinel in raw
    # scrubbed lines are still valid JSONL frames
    events = list(StreamReader(paths, "r-scrub").events(timeout=2.0))
    assert events[-1].type == "final"
    assert sentinel in events[-1].text


def test_request_secret_flow_leaves_no_plaintext_on_disk(tmp_path):
    """End-to-end with the echo provider: request_secret, store, then a turn
    that echoes the value back — no session JSONL or stream file may carry it."""
    paths = _paths(tmp_path)
    agent = build_agent(paths, Bot(name="atlas", role="terse", provider="echo"), stream_delay=0.0)
    value = "hunter2-sup3r-secret"

    def provide():
        # Store after a beat whether or not the box is up yet: request_secret
        # short-circuits on an already-stored value, so this can never hang.
        deadline = time.time() + 5
        while time.time() < deadline and not list_prompts(paths):
            time.sleep(0.05)
        set_secret("DEMO_TOKEN", value, paths)

    t = threading.Thread(target=provide)
    t.start()
    agent._produce("user", "I need secret DEMO_TOKEN to deploy", writer=StreamWriter(paths, "r-s1"))
    t.join()
    assert get_secret("DEMO_TOKEN", paths) == value

    # a later turn where the model would echo the raw value into its reply
    agent._produce("user", f"the token is {value}, right?", writer=StreamWriter(paths, "r-s2"))

    leaks = []
    for path in sorted(paths.home.rglob("*.jsonl")):
        if value in path.read_text(encoding="utf-8"):
            leaks.append(str(path.relative_to(paths.home)))
    assert leaks == []
    # the credentials store itself is the one place the value lives
    assert (paths.credentials / "DEMO_TOKEN").read_text(encoding="utf-8") == value
    # and what the transcript carries instead is the sentinel
    stream = paths.stream_file("r-s2").read_text(encoding="utf-8")
    assert seal(value, "DEMO_TOKEN") in stream


def test_room_transcripts_are_scrubbed(tmp_path):
    """Group chats too: append_message scrubs, so the room JSONL and the
    transcript block replayed into prompts carry the sentinel, never the value."""
    from harness.rooms import append_message, create_room, transcript_block, transcript_file

    paths = _paths(tmp_path)
    sentinel = register_secret("hunter2-sup3r-secret", "DEMO_TOKEN")
    room = create_room(paths, "Standup", ["atlas", "nova"])
    append_message(paths, room.id, frm="atlas", text="the token is hunter2-sup3r-secret ok")
    raw = transcript_file(paths, room.id).read_text(encoding="utf-8")
    assert "hunter2-sup3r-secret" not in raw
    assert sentinel in raw
    block = transcript_block(paths, room.id)
    assert "hunter2-sup3r-secret" not in block
    assert sentinel in block


def test_api_error_payloads_are_scrubbed(tmp_path):
    from harness.server import scrub_secrets

    sentinel = register_secret("hunter2-sup3r-secret", "X")
    payload = json.dumps({"error": "boom: hunter2-sup3r-secret rejected"})
    out = scrub_secrets(payload)
    assert "hunter2-sup3r-secret" not in out
    assert sentinel in out


def test_remember_scrubs_before_storing_and_embedding(tmp_path):
    """A registered secret in a fact is scrubbed before the record is written
    AND before the text reaches the embedder — the embedding provider can be
    a different vendor than the bot's chat route."""
    from agent.memory import Memory
    from harness.paths import HarnessPaths
    from harness.redaction import register_secret, registry

    registry().clear()
    secret = "fact-secret-value-123456"
    register_secret(secret, "API_KEY")
    embedded: list[str] = []

    def spy_embed(text):
        embedded.append(text)
        return [0.1, 0.2]

    paths = HarnessPaths.resolve(tmp_path / "home")
    paths.ensure_layout(["atlas"])
    try:
        memory = Memory(paths, "atlas", embedder=spy_embed)
        memory.remember(f"the deploy key is {secret}")
    finally:
        registry().clear()
    assert embedded and all(secret not in t for t in embedded)
    stored = paths.bot_memory("atlas").joinpath("facts.jsonl").read_text(encoding="utf-8")
    assert secret not in stored
