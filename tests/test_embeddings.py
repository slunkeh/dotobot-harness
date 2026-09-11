"""Optional semantic recall: embeddings over the keyword floor.

Contracts under test:
* null-embedding recall parity — no embedder configured means behavior
  identical to keyword-only recall, byte for byte;
* a fake deterministic embedder exercises cosine ranking and keyword merge;
* a failing embedding provider leaves the turn successful and the record
  unembedded (usage-ledger swallow contract);
* budget fill picks the highest-ranked rows, not the first found;
* echo (and every provider without a route) raises ProviderError from
  `embed()`, and missing credentials raise before any network I/O.
"""

from __future__ import annotations

import io
import json
import urllib.request

import pytest

from agent import embeddings
from agent.embeddings import (
    SEMANTIC_FLOOR,
    cosine,
    grade_relevance,
    pack_embedding,
    parse_route,
    recall_token_budget,
    resolve_embedder,
    unpack_embedding,
)
from agent.memory import Memory
from agent.runtime import build_agent
from harness.paths import HarnessPaths
from harness.roster import Bot, Roster, load_roster, save_roster
from providers.base import Auth, Provider, ProviderError
from providers.echo import EchoProvider
from providers.openai import OpenAIProvider


def _paths(tmp_path):
    p = HarnessPaths.resolve(tmp_path / "home")
    p.ensure_layout(["atlas"])
    return p


class FakeEmbedder:
    """Deterministic text -> vector map; unknown text embeds to a far axis."""

    def __init__(self, table: dict[str, list[float]] | None = None):
        self.table = table or {}
        self.calls: list[str] = []

    def __call__(self, text: str) -> list[float]:
        self.calls.append(text)
        return self.table.get(text, [0.0, 0.0, 0.0, 1.0])


def _raising_embedder(text: str) -> list[float]:
    raise ProviderError("embedding backend down")


# -- packing / math ----------------------------------------------------------


def test_pack_unpack_round_trip():
    vec = [0.25, -1.5, 3.0, 0.0]
    assert unpack_embedding(pack_embedding(vec)) == pytest.approx(vec)


def test_unpack_tolerates_malformed_values():
    assert unpack_embedding(None) is None
    assert unpack_embedding("") is None
    assert unpack_embedding("not base64!!") is None
    assert unpack_embedding("AAA=") is None  # 2 bytes: not a float32 array
    assert unpack_embedding(123) is None


def test_cosine_basics():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)
    assert cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0  # length mismatch
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0  # zero norm


def test_grade_relevance_merges_channels():
    # Keyword ordering is monotone in the hit count (parity with today).
    assert grade_relevance(3, 0.0) > grade_relevance(1, 0.0) > grade_relevance(0, 0.0) == 0.0
    # A semantic-only row participates only above the floor.
    assert grade_relevance(0, SEMANTIC_FLOOR - 0.01) == 0.0
    assert grade_relevance(0, 0.9) > 0.0
    # Both channels beat either alone; a strong semantic match beats a weak
    # keyword hit.
    assert grade_relevance(1, 0.9) > grade_relevance(1, 0.0)
    assert grade_relevance(1, 0.9) > grade_relevance(0, 0.9)
    assert grade_relevance(0, 0.9) > grade_relevance(1, 0.0)


# -- null-column parity ------------------------------------------------------


def test_recall_without_embedder_matches_keyword_behavior(tmp_path):
    mem = Memory(paths=_paths(tmp_path), bot="atlas")
    mem.remember("the deploy pipeline is green")
    mem.remember("deploy deploy deploy checklist")
    mem.log_turn("s1", "in:user", "nothing about that topic here")
    hits = mem.recall("deploy")
    assert [h["text"] for h in hits] == [
        "deploy deploy deploy checklist",
        "the deploy pipeline is green",
    ]
    assert all("embedding" not in h for h in hits)


def test_embedded_rows_with_no_embedder_still_keyword_only(tmp_path):
    """Rows written with vectors rank purely by keyword once the route is gone."""
    paths = _paths(tmp_path)
    writer = Memory(paths=paths, bot="atlas", embedder=FakeEmbedder())
    writer.remember("deploy notes")
    reader = Memory(paths=paths, bot="atlas")
    hits = reader.recall("deploy")
    assert [h["text"] for h in hits] == ["deploy notes"]


# -- record-time embedding ---------------------------------------------------


def test_records_carry_embeddings_when_route_configured(tmp_path):
    fake = FakeEmbedder({"the sky is blue": [1.0, 0.0, 0.0, 0.0]})
    mem = Memory(paths=_paths(tmp_path), bot="atlas", embedder=fake)
    mem.remember("the sky is blue")
    mem.log_turn("s1", "in:user", "the sky is blue")
    fact = json.loads(mem.facts_file.read_text(encoding="utf-8").splitlines()[0])
    turn = mem.store.transcript_records("atlas")[0]  # turns land in the state store
    for record in (fact, turn):
        assert unpack_embedding(record["embedding"]) == pytest.approx([1.0, 0.0, 0.0, 0.0])


def test_empty_text_records_are_not_embedded(tmp_path):
    fake = FakeEmbedder()
    mem = Memory(paths=_paths(tmp_path), bot="atlas", embedder=fake)
    mem.log_card("s1", card_id="c1", card_type="progress", payload={}, frm="atlas")
    record = mem.store.transcript_records("atlas")[0]  # turns land in the state store
    assert "embedding" not in record
    assert fake.calls == []


def test_provider_failure_leaves_record_null_and_turn_successful(tmp_path):
    mem = Memory(paths=_paths(tmp_path), bot="atlas", embedder=_raising_embedder)
    mem.remember("survives a broken embedding backend")
    mem.log_turn("s1", "in:user", "still logged fine")
    fact = json.loads(mem.facts_file.read_text(encoding="utf-8").splitlines()[0])
    assert "embedding" not in fact
    # Recall keeps keyword results available when the provider cannot start.
    hits = mem.recall("broken backend")
    assert hits and "broken" in hits[0]["text"]


def test_embed_failure_opens_cooldown(tmp_path):
    """One hung/failed embed call must not become one per write: after a
    failure, writes and recall skip embedding until the cooldown lapses
    (Bugbot, PR #133)."""

    class _Flaky:
        def __init__(self):
            self.calls = 0

        def __call__(self, text):
            self.calls += 1
            raise ProviderError("endpoint hangs")

    flaky = _Flaky()
    mem = Memory(paths=_paths(tmp_path), bot="atlas", embedder=flaky)
    mem.remember("first write eats the failure")
    mem.remember("second write skips the embedder")
    mem.recall("skips too")
    assert flaky.calls == 1
    # Cooldown over: the embedder is tried again.
    mem._embed_down_until = 0.0
    mem.remember("third write retries")
    assert flaky.calls == 2


# -- semantic recall ---------------------------------------------------------


def _semantic_memory(tmp_path):
    """Rows spread over two axes so cosine grades are deterministic."""
    table = {
        "shipping the release automation": [1.0, 0.0, 0.0, 0.0],
        "the deploy pipeline is green": [0.9, 0.1, 0.0, 0.0],
        "rollout tooling almost matches": [0.6, 0.8, 0.0, 0.0],
        "lunch menu says tacos": [0.0, 0.0, 1.0, 0.0],
    }
    mem = Memory(paths=_paths(tmp_path), bot="atlas", embedder=FakeEmbedder(table))
    for text in table:
        mem.remember(text)
    return mem


def test_semantic_recall_finds_rows_without_keyword_overlap(tmp_path):
    mem = _semantic_memory(tmp_path)
    hits = mem.recall("shipping the release automation")
    texts = [h["text"] for h in hits]
    # The exact-vector row wins; near rows rank by cosine; the orthogonal
    # lunch row (cosine 0 < floor, no keyword hit) is excluded.
    assert texts[0] == "shipping the release automation"
    assert texts.index("the deploy pipeline is green") < texts.index(
        "rollout tooling almost matches"
    )
    assert "lunch menu says tacos" not in texts


def test_keyword_plus_semantic_outranks_either_alone(tmp_path):
    table = {
        "deploy went out this morning": [1.0, 0.0, 0.0, 0.0],  # keyword + semantic
        "release shipped to production": [1.0, 0.0, 0.0, 0.0],  # semantic only
        "deploy the lunch trolley": [0.0, 0.0, 1.0, 0.0],  # keyword only
        "deploy": [1.0, 0.0, 0.0, 0.0],
    }
    mem = Memory(paths=_paths(tmp_path), bot="atlas", embedder=FakeEmbedder(table))
    for text in list(table)[:3]:
        mem.remember(text)
    texts = [h["text"] for h in mem.recall("deploy")]
    assert texts[0] == "deploy went out this morning"
    assert set(texts) == set(list(table)[:3])
    # The strong semantic match outranks the weak keyword-only hit.
    assert texts.index("release shipped to production") < texts.index("deploy the lunch trolley")


def test_rows_without_embeddings_participate_via_keyword(tmp_path):
    paths = _paths(tmp_path)
    plain = Memory(paths=paths, bot="atlas")
    plain.remember("deploy checklist from before embeddings existed")
    table = {"deploy": [1.0, 0.0, 0.0, 0.0]}
    mem = Memory(paths=paths, bot="atlas", embedder=FakeEmbedder(table))
    hits = mem.recall("deploy")
    assert any("checklist" in h["text"] for h in hits)


def test_query_embedding_failure_falls_back_to_keyword(tmp_path):
    paths = _paths(tmp_path)
    Memory(paths=paths, bot="atlas", embedder=FakeEmbedder()).remember("deploy checklist")
    mem = Memory(paths=paths, bot="atlas", embedder=_raising_embedder)
    hits = mem.recall("deploy")
    assert [h["text"] for h in hits] == ["deploy checklist"]


# -- budget fill -------------------------------------------------------------


def test_budget_fill_picks_highest_ranked_rows(tmp_path):
    mem = Memory(paths=_paths(tmp_path), bot="atlas")
    big_top = "deploy " * 40  # ~70 tokens, highest keyword rank
    middle = "deploy deploy " + "x" * 50  # ~16 tokens, second rank
    small = "deploy ok"  # 2 tokens, third rank
    for text in (small, big_top, middle):  # insertion order != rank order
        mem.remember(text)
    picked = mem.recall("deploy", token_budget=25)
    # The over-budget top row is skipped; the next-best rows still fill.
    assert [h["text"] for h in picked] == [middle, small]
    # A roomy budget keeps the full ranking, best first.
    assert [h["text"] for h in mem.recall("deploy", token_budget=1000)][0] == big_top
    # No budget = plain top-limit, unchanged.
    assert len(mem.recall("deploy", limit=2)) == 2


def test_recall_token_budget_follows_history_knob(monkeypatch):
    monkeypatch.setenv("HARNESS_HISTORY_TOKENS", "4000")
    assert recall_token_budget() == 1000
    monkeypatch.setenv("HARNESS_HISTORY_TOKENS", "nonsense")
    assert recall_token_budget() == 2000
    monkeypatch.delenv("HARNESS_HISTORY_TOKENS")
    assert recall_token_budget() == 2000


# -- provider routes ---------------------------------------------------------


def test_echo_and_base_providers_have_no_embedding_route():
    with pytest.raises(ProviderError):
        EchoProvider().embed(["hello"])
    with pytest.raises(ProviderError):
        Provider(model="m").embed(["hello"])


def test_openai_embed_missing_credentials_raise_before_network(monkeypatch):
    def _no_network(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("network I/O attempted without credentials")

    monkeypatch.setattr(urllib.request, "urlopen", _no_network)
    with pytest.raises(ProviderError, match="OPENAI_API_KEY"):
        OpenAIProvider(auth=Auth()).embed(["hello"])


def test_openai_embed_parses_vectors_in_index_order(monkeypatch):
    seen: dict = {}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode("utf-8"))
        payload = {
            "data": [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ]
        }
        return _Resp(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    provider = OpenAIProvider(auth=Auth(api_key="sk-test"))
    vectors = provider.embed(["first", "second"])
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]  # index order, not arrival order
    assert seen["url"] == "https://api.openai.com/v1/embeddings"
    assert seen["body"] == {"model": "text-embedding-3-small", "input": ["first", "second"]}


def test_openai_embed_timeout_is_short_and_tunable(monkeypatch):
    """Embeds run synchronously inside turns: default 10s, not the chat
    120s, and $HARNESS_EMBED_TIMEOUT overrides (Bugbot, PR #133)."""
    seen: dict = {}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(req, timeout=0):
        seen["timeout"] = timeout
        payload = {"data": [{"index": 0, "embedding": [1.0]}]}
        return _Resp(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    provider = OpenAIProvider(auth=Auth(api_key="sk-test"))
    monkeypatch.delenv("HARNESS_EMBED_TIMEOUT", raising=False)
    provider.embed(["one"])
    assert seen["timeout"] == 10.0
    monkeypatch.setenv("HARNESS_EMBED_TIMEOUT", "3")
    provider.embed(["one"])
    assert seen["timeout"] == 3.0


def test_openai_embed_rejects_vector_count_mismatch(monkeypatch):
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=0: _Resp(json.dumps({"data": []}).encode("utf-8")),
    )
    with pytest.raises(ProviderError, match="0 vectors"):
        OpenAIProvider(auth=Auth(api_key="sk-test")).embed(["one"])


# -- route resolution --------------------------------------------------------


def test_parse_route():
    assert parse_route("") is None
    assert parse_route("   ") is None
    assert parse_route("openai") == ("openai", None)
    assert parse_route("openai:text-embedding-3-large") == ("openai", "text-embedding-3-large")
    assert parse_route(":model-only") is None


def test_resolve_embedder_defaults_to_none(tmp_path):
    paths = _paths(tmp_path)
    assert resolve_embedder(Bot(name="atlas"), paths) is None
    # A route that cannot start keeps keyword recall available.
    assert resolve_embedder(Bot(name="atlas", embeddings="no-such-provider"), paths) is None


def test_resolve_embedder_routes_through_provider_machinery(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    built: dict = {}

    class _StubProvider:
        def embed(self, texts, *, model=None):
            built["model"] = model
            return [[0.5, 0.5] for _ in texts]

    def _fake_build(name, model=None, *, auth=None, **options):
        built["name"] = name
        built["auth"] = auth
        return _StubProvider()

    monkeypatch.setattr(embeddings, "build_provider", _fake_build)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-route")
    embed = resolve_embedder(Bot(name="atlas", embeddings="openai:small"), paths)
    assert embed("hello") == [0.5, 0.5]
    assert built["name"] == "openai"
    assert built["model"] == "small"
    assert built["auth"].bearer() == "sk-route"


def test_resolve_embedder_honors_custom_auth_ref(tmp_path, monkeypatch):
    """A bot whose key lives under auth_ref must embed with that key, not
    silently fall back to keyword-only (Bugbot, PR #133)."""
    paths = _paths(tmp_path)
    captured: dict = {}

    class _StubProvider:
        def embed(self, texts, *, model=None):
            return [[1.0] for _ in texts]

    def _fake_build(name, model=None, *, auth=None, **options):
        captured["auth"] = auth
        return _StubProvider()

    monkeypatch.setattr(embeddings, "build_provider", _fake_build)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("MY_EMBED_KEY", "sk-custom")
    bot = Bot(name="atlas", provider="codex", auth_ref="MY_EMBED_KEY", embeddings="openai")
    embed = resolve_embedder(bot, paths)
    assert embed("hello") == [1.0]
    # codex and openai resolve to the same vendor key, so the ref applies.
    assert captured["auth"].bearer() == "sk-custom"
    # A different chat vendor's ref must not leak onto the embedding route.
    captured.clear()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-conventional")
    other = Bot(name="atlas", provider="claude", auth_ref="MY_EMBED_KEY", embeddings="openai")
    resolve_embedder(other, paths)("hello")
    assert captured["auth"].bearer() == "sk-conventional"


def test_resolve_embedder_reads_credentials_per_call(tmp_path, monkeypatch):
    """A key saved after the route resolved must be picked up without a
    restart — the resolve-time snapshot froze a missing key forever
    (Bugbot, PR #133)."""
    paths = _paths(tmp_path)
    captured: dict = {}

    def _fake_build(name, model=None, *, auth=None, **options):
        captured["auth"] = auth

        class _P:
            def embed(self, texts, *, model=None):
                return [[1.0] for _ in texts]

        return _P()

    monkeypatch.setattr(embeddings, "build_provider", _fake_build)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    resolve_embedder(Bot(name="atlas", embeddings="openai"), paths)
    assert captured["auth"].bearer() is None  # nothing configured yet
    monkeypatch.setenv("OPENAI_API_KEY", "sk-saved-later")
    assert captured["auth"].bearer() == "sk-saved-later"


def test_echo_route_never_fails_the_turn(tmp_path):
    """An echo embedding route has no embed(); records stay null, recall works."""
    paths = _paths(tmp_path)
    embed = resolve_embedder(Bot(name="atlas", embeddings="echo"), paths)
    assert embed is not None
    mem = Memory(paths=paths, bot="atlas", embedder=embed)
    mem.remember("keyword floor holds")
    fact = json.loads(mem.facts_file.read_text(encoding="utf-8").splitlines()[0])
    assert "embedding" not in fact
    assert mem.recall("keyword floor")[0]["text"] == "keyword floor holds"


# -- roster + agent wiring ---------------------------------------------------


def test_roster_embeddings_field_round_trips(tmp_path):
    toml = tmp_path / "roster.toml"
    toml.write_text(
        '[[bots]]\nname = "atlas"\nprovider = "echo"\nembeddings = "openai:small"\n'
        '[[bots]]\nname = "nova"\nprovider = "echo"\n',
        encoding="utf-8",
    )
    roster = load_roster(toml)
    assert roster.get("atlas").embeddings == "openai:small"
    assert roster.get("nova").embeddings == ""
    saved = tmp_path / "roster.json"
    save_roster(saved, Roster(bots=roster.bots))
    assert load_roster(saved).get("atlas").embeddings == "openai:small"


def test_orchestrator_memory_for_wires_embedder(tmp_path, monkeypatch):
    """Server-side writes (POST /api/bots/<name>/memory, logged commands,
    recipes, voice) must embed like the agent's own — a bare Memory left
    those rows out of cosine ranking forever (Bugbot, PR #133)."""
    from harness.orchestrator import Orchestrator

    rp = tmp_path / "roster.toml"
    rp.write_text(
        '[[bots]]\nname = "atlas"\nprovider = "echo"\nembeddings = "openai"\n'
        '[[bots]]\nname = "nova"\nprovider = "echo"\n',
        encoding="utf-8",
    )
    orch = Orchestrator.create(home=tmp_path / "home", roster_path=rp, backend="process")
    orch.init()
    assert orch.memory_for("atlas").embedder is not None
    assert orch.memory_for("nova").embedder is None
    assert orch.memory_for("no-such-bot").embedder is None
    # Clearing the route refreshes the cached Memory, not just the roster.
    monkeypatch.setattr(orch, "restart", lambda name: None)
    orch.update_bot("atlas", embeddings="")
    assert orch.memory_for("atlas").embedder is None


def test_build_agent_wires_embedder_from_bot_config(tmp_path):
    paths = _paths(tmp_path)
    agent = build_agent(paths, Bot(name="atlas", provider="echo"), stream_delay=0.0)
    assert agent.memory.embedder is None
    agent = build_agent(
        paths, Bot(name="atlas", provider="echo", embeddings="openai"), stream_delay=0.0
    )
    assert agent.memory.embedder is not None
