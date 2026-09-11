"""Optional semantic recall: embeddings over the keyword floor.

Keyword search is the floor, embeddings are an enhancement — recall keeps
keyword results available when an optional embedding provider cannot start.
Everything here is best-effort by contract: a missing or failing embedding
route leaves records unembedded and recall keyword-only; it never fails a
turn (the same swallow contract as the usage ledger).

Vectors are packed as base64-encoded little-endian float32 on the record —
the JSONL analog of the nullable `embedding` BLOB column. When the
sqlite state store lands, the packed bytes move into that column unchanged
(nullable bare column, no schema version bump).

Brute-force cosine in Python is fine to roughly 100k rows per bot. Past
that, the step up is the sqlite-vec extension (still one file, still no
server) — Postgres/pgvector is explicitly rejected: a server dependency
breaks the stdlib-only, unpack-and-restart deployment model.
"""

from __future__ import annotations

import base64
import binascii
import math
import os
import struct
from collections.abc import Callable, Sequence

from harness.paths import HarnessPaths
from harness.secrets import get_secret, resolve_env_name
from providers import Auth, build_provider

#: A semantic-only candidate (no keyword hit) must clear this cosine before
#: it participates in recall, so brute-forcing every embedded row does not
#: drag unrelated text into a sparse result set.
SEMANTIC_FLOOR = 0.25

_DEFAULT_HISTORY_TOKENS = 8_000


def pack_embedding(vector: Sequence[float]) -> str:
    """Pack a float vector as base64 little-endian float32 bytes."""
    blob = struct.pack(f"<{len(vector)}f", *[float(v) for v in vector])
    return base64.b64encode(blob).decode("ascii")


def unpack_embedding(packed: object) -> list[float] | None:
    """Inverse of pack_embedding; tolerant — any malformed value is None."""
    if not isinstance(packed, str) or not packed:
        return None
    try:
        blob = base64.b64decode(packed.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError):
        return None
    if not blob or len(blob) % 4:
        return None
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity; 0.0 for mismatched lengths or zero-norm vectors."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    if norm == 0.0:
        return 0.0
    return dot / norm


def grade_relevance(keyword_hits: int, semantic: float, *, floor: float = SEMANTIC_FLOOR) -> float:
    """Merge the keyword floor and the semantic enhancement into one grade.

    Keyword hits map to [0, 1) via k/(k+1) — monotone in the hit count, so
    with no embeddings the ranking is exactly today's keyword ordering. A
    row with keyword hits adds any positive cosine on top; a row with no
    keyword hit participates only above SEMANTIC_FLOOR. A row scoring on
    both channels outranks one scoring on either alone.
    """
    kw_part = keyword_hits / (keyword_hits + 1.0) if keyword_hits > 0 else 0.0
    if keyword_hits > 0:
        sem_part = max(0.0, semantic)
    else:
        sem_part = semantic if semantic >= floor else 0.0
    return kw_part + sem_part


def recall_token_budget() -> int:
    """Token cap for a budgeted recall fill: a quarter of the history knob.

    The recall block rides the same request `$HARNESS_HISTORY_TOKENS`
    governs, so its ceiling scales with that knob instead of adding one.
    """
    try:
        cap = int(os.environ.get("HARNESS_HISTORY_TOKENS", _DEFAULT_HISTORY_TOKENS))
    except ValueError:
        cap = _DEFAULT_HISTORY_TOKENS
    return max(1, cap // 4)


def parse_route(spec: str) -> tuple[str, str | None] | None:
    """Parse a per-bot embedding route `provider[:model]`; empty spec is None."""
    raw = (spec or "").strip()
    if not raw:
        return None
    provider, _, model = raw.partition(":")
    provider = provider.strip()
    if not provider:
        return None
    return provider, model.strip() or None


def resolve_embedder(bot, paths: HarnessPaths) -> Callable[[str], list[float]] | None:
    """Build `text -> vector` for the bot's embedding route, or None.

    Resolves through the existing provider machinery (`build_provider` +
    the secrets store). An empty route — the default, and every echo bot —
    is None, so behavior is identical to keyword-only today. A route that
    cannot start also resolves to None: keyword recall stays available.
    Missing credentials still raise from the provider's own `embed()`
    before any network I/O; Memory swallows that per call.
    """
    route = parse_route(getattr(bot, "embeddings", "") or "")
    if route is None:
        return None
    provider_id, model = route
    # A custom auth_ref names where this bot's key lives; honor it when the
    # route uses the bot's own chat vendor (alias-aware: codex and openai
    # resolve to the same key), falling back to the conventional provider
    # name so a shared key still works.
    ref = getattr(bot, "auth_ref", None)
    chat_provider = str(getattr(bot, "provider", "") or "")
    use_ref = bool(ref) and resolve_env_name(chat_provider) == resolve_env_name(provider_id)

    def _route_key() -> str:
        # Re-read per call (the Auth.refresh seam) so a key saved after the
        # agent spawned — or after serve built its cached Memory — is picked
        # up without a restart, instead of freezing at resolve time.
        value = get_secret(ref, paths) if use_ref else None
        return value or get_secret(provider_id, paths) or ""

    try:
        provider = build_provider(provider_id, auth=Auth(refresh=_route_key))
    except Exception:
        return None

    def embed_one(text: str) -> list[float]:
        return provider.embed([text], model=model)[0]

    return embed_one
