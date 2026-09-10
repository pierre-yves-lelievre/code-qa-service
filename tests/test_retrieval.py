"""Retrieval helpers: identifier candidates and full-text terms. Pure; no database."""

import pytest

from app.retrieval import MAX_CANDIDATES, candidate_tokens, or_terms

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
