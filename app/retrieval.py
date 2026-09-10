"""Retrieval: identifier candidates, the three legs with per-leg isolation, fusion, the floor.

Everything here is pure except `search_legs()` and `retrieve()`, the only functions that do I/O:
they call the store and the embeddings client, and a failing leg becomes `unavailable` in the
trace.
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from app.config import settings
from app.logging_setup import get_logger

if TYPE_CHECKING:
    from app.embeddings import FakeEmbeddings, VoyageEmbeddings
    from app.planning import Intent, Plan
    from app.store import ChunkStore

log = get_logger(__name__)

Leg = Literal["symbol", "fts", "vector"]
LegStatus = Literal["ok", "or_fallback", "empty", "skipped", "unavailable"]
LEGS: tuple[Leg, ...] = ("symbol", "fts", "vector")  # also the tie-break priority
SYMBOL_LIMIT = 50
FTS_LIMIT = 50
VECTOR_LIMIT = 20
MAX_CANDIDATES = 32
RRF_K = 60
FULL_HITS = 12
INDEX_HITS = 30
ENUMERATE_INDEX_HITS = 60


@dataclass(frozen=True)
class Hit:
    """One retrieved chunk; `score` is the leg's (higher is better), then the fused score."""

    id: int
    path: str
    kind: str
    name: str | None
    qualname: str | None
    part: int
    start_line: int
    end_line: int
    signature: str | None
    text: str
    truncated: bool
    score: float
    tier: Leg | None = None


@dataclass(frozen=True)
class Legs:
    """Each leg's hits in rank order and trace, the member identifiers, the query embedding."""

    hits: dict[Leg, list[Hit]]
    trace: dict[Leg, dict[str, Any]]
    identifiers: tuple[str, ...]
    embed_tokens: int
    embed_ms: int = 0


@dataclass(frozen=True)
class Retrieval:
    """What the answerer gets: full hits, the compact index, the floor verdict, the trace."""

    full: list[Hit]
    index: list[Hit]
    no_relevant_sources: bool
    legs: dict[Leg, dict[str, Any]]
    identifiers: tuple[str, ...]
    embed_tokens: int
    embed_ms: int = 0


# ── Tokens ────────────────────────────────────────────────────────────────────


def split_identifiers(text: str) -> str:
    """Each whitespace token followed by its identifier parts, when it has more than one."""
    out: list[str] = []
    for token in text.split():
        out.append(token)
        parts = _identifier_parts(token)
        if len(parts) > 1:
            out.extend(parts)
    return " ".join(out)


def _identifier_parts(token: str) -> list[str]:
    """Split on non-alphanumerics and at camelCase, acronym and letter/digit boundaries."""
    parts: list[str] = []
    current = ""
    for i, ch in enumerate(token):
        if not ch.isalnum():
            if current:
                parts.append(current)
            current = ""
            continue
        if current:
            prev = current[-1]
            following = token[i + 1] if i + 1 < len(token) else ""
            if (
                ch.isdigit() != prev.isdigit()
                or (ch.isupper() and prev.islower())
                or (ch.isupper() and prev.isupper() and following.islower())
            ):
                parts.append(current)
                current = ""
        current += ch
    if current:
        parts.append(current)
    return parts


def candidate_tokens(query: str, identifiers: Sequence[str] = ()) -> list[str]:
    """Identifier candidates: the planner's, then each query token, each with its dotted parts."""
    out: dict[str, None] = {}
    for token in [*identifiers, *query.split()]:
        core = _trim(token)
        if not core:
            continue
        out.setdefault(core)
        parts = _punctuation_parts(core)
        if len(parts) > 1:
            for part in parts:
                out.setdefault(part)
    return list(out)[:MAX_CANDIDATES]


def or_terms(query: str) -> list[str]:
    """Distinct lower-case alphanumeric terms for the OR fallback; safe in tsquery syntax."""
    out: dict[str, None] = {}
    for token in query.split():
        if token.isalnum():
            out.setdefault(token.lower())
        for part in _identifier_parts(token):
            out.setdefault(part.lower())
    return list(out)


def _is_identifier_char(ch: str) -> bool:
    """A character that can be part of a code identifier."""
    return ch.isalnum() or ch == "_"


def _trim(token: str) -> str:
    """The token without leading and trailing punctuation: `` `a.b()`? `` becomes `a.b`."""
    start, end = 0, len(token)
    while start < end and not _is_identifier_char(token[start]):
        start += 1
    while end > start and not _is_identifier_char(token[end - 1]):
        end -= 1
    return token[start:end]


def _punctuation_parts(token: str) -> list[str]:
    """The runs of identifier characters in a token, split at any other character."""
    parts: list[str] = []
    current = ""
    for ch in token:
        if _is_identifier_char(ch):
            current += ch
        elif current:
            parts.append(current)
            current = ""
    if current:
        parts.append(current)
    return parts


# ── Legs ──────────────────────────────────────────────────────────────────────


def search_legs(
    plan: "Plan",
    snapshot_id: int,
    store: "ChunkStore",
    embeddings: "VoyageEmbeddings | FakeEmbeddings",
) -> Legs:
    """Run the symbol, full-text and vector legs; a leg that raises is marked unavailable."""
    identifiers: list[str] = []
    embed_tokens = embed_ms = 0

    def symbol() -> tuple[list[Hit], LegStatus]:
        """Resolve candidates by membership, then walk the ladder over the members."""
        nonlocal identifiers
        candidates = candidate_tokens(plan.query, plan.identifiers)
        identifiers = store.resolve_identifiers(snapshot_id, candidates)
        if not identifiers:
            return [], "skipped"
        found = store.symbol_search(snapshot_id, identifiers, SYMBOL_LIMIT)
        return found, "ok" if found else "empty"

    def fts() -> tuple[list[Hit], LegStatus]:
        """AND over the split query, or OR over its terms when AND finds nothing."""
        found, mode = store.text_search(
            snapshot_id, split_identifiers(plan.query), or_terms(plan.query), FTS_LIMIT
        )
        if not found:
            return [], "empty"
        return found, "ok" if mode == "and" else "or_fallback"

    def vector() -> tuple[list[Hit], LegStatus]:
        """Embed the query and take its nearest chunks."""
        nonlocal embed_tokens, embed_ms
        started = time.monotonic()
        embedded = embeddings.embed_query(plan.query)
        embed_ms = round((time.monotonic() - started) * 1000)
        embed_tokens = embedded.tokens
        found = store.vector_search(
            snapshot_id, embeddings.model, embedded.vectors[0], VECTOR_LIMIT
        )
        return found, "ok" if found else "empty"

    hits: dict[Leg, list[Hit]] = {}
    trace: dict[Leg, dict[str, Any]] = {}
    for leg, run in (("symbol", symbol), ("fts", fts), ("vector", vector)):
        started = time.monotonic()
        try:
            found, status = run()
        except Exception as exc:
            log.warning("retrieval_leg_failed", leg=leg, error_type=type(exc).__name__)
            found, status = [], "unavailable"
        hits[leg] = found
        trace[leg] = {
            "status": status,
            "hits": len(found),
            "ms": round((time.monotonic() - started) * 1000),
        }
    return Legs(hits, trace, tuple(identifiers), embed_tokens, embed_ms)


def retrieve(
    plan: "Plan",
    snapshot_id: int,
    store: "ChunkStore",
    embeddings: "VoyageEmbeddings | FakeEmbeddings",
) -> Retrieval:
    """Search every leg, fuse, collapse parts, split full hits from the index, apply the floor."""
    legs = search_legs(plan, snapshot_id, store, embeddings)
    full, index = split_hits(collapse_parts(rrf(legs.hits)), plan.intent)
    and_hits = legs.hits["fts"] if legs.trace["fts"]["status"] == "ok" else []
    vector = legs.hits["vector"]
    no_relevant = apply_floor(
        len(legs.hits["symbol"]),
        len(and_hits),
        vector[0].score if vector else None,
        settings.relevance_floor,
    )
    log.info(
        "retrieval_done",
        full=len(full),
        index=len(index),
        no_relevant_sources=no_relevant,
        **{leg: trace["status"] for leg, trace in legs.trace.items()},
    )
    return Retrieval(
        full, index, no_relevant, legs.trace, legs.identifiers, legs.embed_tokens, legs.embed_ms
    )


# ── Fusion ────────────────────────────────────────────────────────────────────


def rrf(lists: dict[Leg, list[Hit]], k: int = RRF_K) -> list[Hit]:
    """Reciprocal-rank fusion by chunk; the tier is the leg where the chunk ranked best."""
    scores: dict[int, float] = {}
    best: dict[int, tuple[int, int]] = {}  # chunk id -> (rank, leg priority)
    hits: dict[int, Hit] = {}
    for priority, leg in enumerate(LEGS):
        for rank, hit in enumerate(lists.get(leg, []), start=1):
            scores[hit.id] = scores.get(hit.id, 0.0) + 1.0 / (k + rank)
            hits.setdefault(hit.id, hit)
            if hit.id not in best or (rank, priority) < best[hit.id]:
                best[hit.id] = (rank, priority)
    fused = [replace(hits[i], score=scores[i], tier=LEGS[best[i][1]]) for i in scores]
    return sorted(fused, key=lambda h: (-h.score, best[h.id][1], h.path, h.start_line, h.part))


def collapse_parts(hits: list[Hit]) -> list[Hit]:
    """Keep the best-ranked hit of each symbol or named section; the input is in rank order."""
    seen: set[tuple[Any, ...]] = set()
    kept: list[Hit] = []
    for hit in hits:
        key = _group(hit)
        if key not in seen:
            seen.add(key)
            kept.append(hit)
    return kept


def _group(hit: Hit) -> tuple[Any, ...]:
    """A symbol's parts (and a getter with its setter) share a key; unnamed windows never do."""
    if hit.qualname:
        return (hit.path, hit.kind, "qualname", hit.qualname)
    if hit.name:
        return (hit.path, hit.kind, "name", hit.name)
    return ("chunk", hit.id)


def split_hits(fused: list[Hit], intent: "Intent") -> tuple[list[Hit], list[Hit]]:
    """The full hits, then the compact index: 30 entries, or 60 for an enumerate question."""
    size = ENUMERATE_INDEX_HITS if intent == "enumerate" else INDEX_HITS
    return fused[:FULL_HITS], fused[FULL_HITS : FULL_HITS + size]


def apply_floor(
    symbol_hits: int, fts_and_hits: int, best_cosine: float | None, floor: float
) -> bool:
    """True when no symbol or AND full-text hit exists and no vector clears the floor."""
    return symbol_hits == 0 and fts_and_hits == 0 and (best_cosine is None or best_cosine < floor)
