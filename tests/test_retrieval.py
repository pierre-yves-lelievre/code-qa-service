"""Retrieval helpers: identifier candidates and full-text terms. Pure; no database."""

import pytest

from app.retrieval import (
    ENUMERATE_INDEX_HITS,
    FULL_HITS,
    INDEX_HITS,
    MAX_CANDIDATES,
    Hit,
    apply_floor,
    candidate_tokens,
    collapse_parts,
    or_terms,
    rrf,
    split_hits,
)

# ── Candidates ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "Where is `crud.authenticate()` defined?",
            ["Where", "is", "crud.authenticate", "crud", "authenticate", "defined"],
        ),
        ("shop/models.py", ["shop/models.py", "shop", "models", "py"]),
        ("label, label and Product.label", ["label", "and", "Product.label", "Product"]),
        ("what does __init__ do ?", ["what", "does", "__init__", "do"]),
    ],
)
def test_candidates_are_trimmed_tokens_and_their_punctuation_parts(query: str, expected: list):
    assert candidate_tokens(query) == expected


def test_planner_identifiers_come_first_and_are_trimmed_too():
    assert candidate_tokens("slugify it", ["tax_rate()", "slugify"]) == [
        "tax_rate",
        "slugify",
        "it",
    ]


def test_candidates_are_capped():
    words = " ".join(f"w{i}" for i in range(MAX_CANDIDATES + 10))
    assert len(candidate_tokens(words)) == MAX_CANDIDATES


# ── Full-text terms ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("How is the tax rate computed?", ["how", "is", "the", "tax", "rate", "computed"]),
        ("getPasswordHash", ["getpasswordhash", "get", "password", "hash"]),
        ("crud.authenticate tax_rate", ["crud", "authenticate", "tax", "rate"]),
        ("?? -- !!", []),
    ],
)
def test_or_terms_are_distinct_lower_case_alphanumerics(query: str, expected: list):
    assert or_terms(query) == expected


# ── Fusion ────────────────────────────────────────────────────────────────────


def _hit(
    id: int,
    path: str = "a.py",
    kind: str = "function",
    qualname: str | None = None,
    name: str | None = None,
    part: int = 0,
    line: int = 1,
) -> Hit:
    """A synthetic hit; only identity and grouping fields matter here."""
    return Hit(id, path, kind, name, qualname, part, line, line + 5, None, "", False, 0.0)


A, B, C = _hit(1, qualname="m.a"), _hit(2, qualname="m.b"), _hit(3, qualname="m.c")


def test_rrf_sums_reciprocal_ranks_and_tiers_by_best_rank():
    fused = rrf({"symbol": [A, B], "fts": [B, C], "vector": [C, B]}, k=60)
    assert [h.id for h in fused] == [2, 3, 1]
    assert [h.score for h in fused] == pytest.approx(
        [1 / 62 + 1 / 61 + 1 / 62, 1 / 62 + 1 / 61, 1 / 61]
    )
    assert [h.tier for h in fused] == ["fts", "vector", "symbol"]


def test_rrf_ties_go_to_the_higher_priority_leg():
    (both,) = rrf({"symbol": [A], "vector": [A]})
    assert both.tier == "symbol"  # rank 1 in each; symbol outranks vector
    fused = rrf({"vector": [B], "symbol": [A]})
    assert [h.id for h in fused] == [1, 2]  # equal scores: symbol first


def test_parts_of_one_symbol_collapse_into_the_best_ranked():
    parts = [_hit(i, qualname="m.long", part=i, line=i * 50) for i in (2, 1, 3)]
    other_file = _hit(9, path="b.py", qualname="m.long")
    assert [h.id for h in collapse_parts([*parts, other_file])] == [2, 9]


def test_getter_and_setter_collapse_but_unnamed_windows_stay_apart():
    getter = _hit(1, kind="method", qualname="m.P.label", line=10)
    setter = _hit(2, kind="method", qualname="m.P.label", line=15)
    windows = [_hit(i, path="notes.txt", kind="window", line=i * 65) for i in (3, 4)]
    sections = [_hit(i, path="README.md", kind="window", name="Usage", part=i) for i in (5, 6)]
    kept = collapse_parts([getter, setter, *windows, *sections])
    assert [h.id for h in kept] == [1, 3, 4, 5]


@pytest.mark.parametrize(
    ("count", "intent", "sizes"),
    [
        (100, "explain", (FULL_HITS, INDEX_HITS)),
        (100, "enumerate", (FULL_HITS, ENUMERATE_INDEX_HITS)),
        (50, "enumerate", (FULL_HITS, 50 - FULL_HITS)),
        (5, "lookup", (5, 0)),
    ],
)
def test_full_hits_then_a_compact_index_widened_for_enumerate(count, intent, sizes):
    fused = [_hit(i) for i in range(count)]
    full, index = split_hits(fused, intent)
    assert (len(full), len(index)) == sizes
    assert full + index == fused[: sum(sizes)]


# ── Floor ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("symbol", "fts_and", "cosine", "fires"),
    [
        (0, 0, 0.10, True),
        (0, 0, None, True),  # vector leg unavailable or empty
        (0, 0, 0.35, False),  # at the floor is enough
        (1, 0, 0.10, False),
        (0, 1, 0.10, False),
    ],
)
def test_floor_fires_only_without_symbol_and_hits_and_a_close_vector(
    symbol, fts_and, cosine, fires
):
    assert apply_floor(symbol, fts_and, cosine, floor=0.35) is fires
