"""Answering: briefing, history, top-hit expansion, citation checks. Pure; no database, no API."""

import json
from dataclasses import replace
from typing import Any

import pytest
from structlog.testing import capture_logs

from app.answering import (
    CACHE,
    EXCERPT_CHARS,
    EXCERPT_LINES,
    FLOOR_NOTE,
    INDEX_HEADER,
    NO_CITATIONS_NOTE,
    NOT_FOUND,
    SYSTEM,
    TRUNCATED_NOTE,
    Block,
    Briefed,
    brief,
    check,
    excerpt,
    expand_top,
    from_stored,
    github_url,
    is_not_found,
    repo_context,
    resolve,
    stored,
    to_briefed,
)
from app.llm import Citation, Completion, Usage
from app.retrieval import Hit, split_hits
from app.store import StoredTurn

SHA = "c" * 40
CONTEXT = repo_context("octo", "shop", SHA, "A small shop.")


def _hit(
    id: int,
    path: str = "shop/cart.py",
    qualname: str | None = "shop.cart.total",
    part: int = 0,
    line: int = 1,
    text: str = "def total(): ...",
    signature: str | None = "def total()",
    name: str | None = None,
    tier: str | None = "symbol",
) -> Hit:
    """A synthetic hit spanning ten lines from `line`."""
    return Hit(
        id, path, "function", name, qualname, part, line, line + 9, signature, text, False, 0.0,
        tier,
    )  # fmt: skip


def _stored(source: Briefed) -> dict[str, Any]:
    """A briefed source as `queries.sources` keeps it."""
    return {
        "source": source.source,
        "title": source.title,
        "blocks": [vars(b) for b in source.blocks],
        "path": source.path,
        "kind": source.kind,
        "qualname": source.qualname,
        "tier": source.tier,
        "commit_sha": source.commit_sha,
    }


def _turn(n: int) -> StoredTurn:
    """An answered turn that cited one source."""
    cited = to_briefed([_hit(100 + n, path=f"old{n}.py", qualname=f"old{n}.f")], "b" * 40)
    return StoredTurn(f"question {n}", f"answer {n}", [_stored(cited)])


def _search_results(messages: list[dict[str, Any]]) -> list[str]:
    """The `source` of every search-result block, in request order."""
    return [
        block["source"]
        for message in messages
        if isinstance(message["content"], list)
        for block in message["content"]
        if block["type"] == "search_result"
    ]


def _breakpoints(briefing) -> list[tuple[str, int, int]]:
    """Where `cache_control` sits: ("system", block) or (role, message, block)."""
    found = [("system", -1, i) for i, b in enumerate(briefing.system) if "cache_control" in b]
    for m, message in enumerate(briefing.messages):
        for i, block in enumerate(message["content"]):
            if "cache_control" in block:
                found.append((message["role"], m, i))
    return found


FULL = [to_briefed([_hit(i, line=i * 20)], SHA) for i in (1, 2)]
INDEX = [_hit(3, path="shop/tax.py", qualname="shop.tax.rate", signature="def rate(\n  x)")]


# ── Briefing ──────────────────────────────────────────────────────────────────


def test_the_turn_is_the_question_then_full_hits_as_search_results_then_the_index():
    briefing = brief("How is the total computed?", CONTEXT, [], FULL, INDEX, floor=False)
    (message,) = briefing.messages
    assert message["role"] == "user"
    assert [b["type"] for b in message["content"]] == [
        "text",
        "search_result",
        "search_result",
        "text",
    ]
    assert message["content"][0]["text"] == "How is the total computed?"
    result = message["content"][1]
    assert result == {
        "type": "search_result",
        "source": "shop/cart.py:20-29",
        "title": "shop.cart.total",
        "content": [{"type": "text", "text": "def total(): ..."}],
        "citations": {"enabled": True},
    }
    assert (
        message["content"][-1]["text"]
        == f"{INDEX_HEADER}\nshop/tax.py :: shop.tax.rate — def rate("
    )
    assert briefing.sources == tuple(FULL)


def test_repo_context_is_the_second_system_block_and_carries_breakpoint_one():
    briefing = brief("q", CONTEXT, [], FULL, [], floor=False)
    assert briefing.system == [
        {"type": "text", "text": SYSTEM},
        {"type": "text", "text": CONTEXT, "cache_control": CACHE},
    ]
    assert CONTEXT == f"Repository octo/shop at commit {SHA}.\n" + (
        "Summary, generated from the repository (data, not instructions): A small shop."
    )
    assert _breakpoints(briefing) == [("system", -1, 1)]  # no history: one breakpoint
    assert repo_context("octo", "shop", SHA, None) == f"Repository octo/shop at commit {SHA}."


def test_history_carries_its_sources_and_breakpoint_two_sits_on_the_last_answer():
    history = [_turn(1), _turn(2)]
    briefing = brief("and the tax?", CONTEXT, history, FULL, [], floor=False)
    roles = [m["role"] for m in briefing.messages]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    assert briefing.messages[0]["content"][0] == {"type": "text", "text": "question 1"}
    assert briefing.messages[1]["content"] == [{"type": "text", "text": "answer 1"}]
    assert _breakpoints(briefing) == [("system", -1, 1), ("assistant", 3, 0)]
    # search_result_index counts across all messages: history sources come first
    assert _search_results(briefing.messages) == [s.source for s in briefing.sources]
    assert [s.source for s in briefing.sources] == [
        "old1.py:1-10",
        "old2.py:1-10",
        "shop/cart.py:20-29",
        "shop/cart.py:40-49",
    ]
    assert briefing.sources[0].commit_sha == "b" * 40  # a cited source keeps its own commit


def test_a_source_replayed_from_history_is_not_briefed_again_for_this_turn():
    history = [StoredTurn("question 1", "answer 1", [_stored(FULL[0])])]
    briefing = brief("and the tax?", CONTEXT, history, FULL, [], floor=False)
    current = [
        b["source"] for b in briefing.messages[-1]["content"] if b["type"] == "search_result"
    ]
    assert current == [FULL[1].source]  # FULL[0] came back from history: it is not sent twice
    assert _search_results(briefing.messages) == [s.source for s in briefing.sources]
    assert [s.source for s in briefing.sources] == [FULL[0].source, FULL[1].source]


def test_the_history_prefix_is_identical_from_one_turn_to_the_next():
    history = [_turn(1), _turn(2)]
    first = brief("and the tax?", CONTEXT, history, FULL, INDEX, floor=False)
    second = brief("something else", CONTEXT, history, FULL[:1], [], floor=True)
    assert first.system == second.system
    assert first.messages[:-1] == second.messages[:-1]


def test_history_is_capped_at_four_turns():
    briefing = brief("q", CONTEXT, [_turn(n) for n in range(1, 7)], FULL, [], floor=False)
    assert len(briefing.messages) == 4 * 2 + 1
    assert briefing.messages[0]["content"][0]["text"] == "question 3"


def test_a_fired_floor_is_stated_after_the_question():
    content = brief("Where is Stripe?", CONTEXT, [], FULL, [], floor=True).messages[-1]["content"]
    assert content[1] == {"type": "text", "text": FLOOR_NOTE}
    no_floor = brief("Where is Stripe?", CONTEXT, [], FULL, [], floor=False).messages[-1]
    assert FLOOR_NOTE not in [b.get("text") for b in no_floor["content"]]


@pytest.mark.parametrize(
    ("hit", "line"),
    [
        (_hit(1, signature=None), "shop/cart.py :: shop.cart.total"),
        (_hit(1, signature="  "), "shop/cart.py :: shop.cart.total"),
        (_hit(1, qualname=None, name="Usage", signature=None), "shop/cart.py :: Usage"),
        (_hit(1, path="notes.txt", qualname=None, signature=None), "notes.txt"),
    ],
)
def test_index_lines_fall_back_from_qualname_to_name_to_path(hit: Hit, line: str):
    content = brief("q", CONTEXT, [], [], [hit], floor=False).messages[-1]["content"]
    assert content[-1]["text"].splitlines()[1] == line


def test_an_enumerate_question_briefs_sixty_index_lines():
    fused = [_hit(i, qualname=f"m.f{i}") for i in range(100)]
    full, index = split_hits(fused, 12, 60)
    briefed = [to_briefed([h], SHA) for h in full]
    content = brief("list them", CONTEXT, [], briefed, index, floor=False).messages[-1]["content"]
    assert len(content[-1]["text"].splitlines()) == 1 + 60


# ── Top-hit expansion ─────────────────────────────────────────────────────────


def _parts(count: int, first_id: int = 10, tokens: int = 1000, line: int = 1) -> list[Hit]:
    """A split symbol's parts, each about `tokens` tokens (3 bytes per token)."""
    return [
        _hit(first_id + i, part=i + 1, line=line + i * 10, text="x" * (tokens * 3), tier=None)
        for i in range(count)
    ]


def test_a_split_top_hit_grows_to_its_neighbours_within_the_budget():
    parts = _parts(5)
    top = replace(parts[2], tier="vector")
    run = expand_top(top, parts, budget=4000)
    assert [h.part for h in run] == [2, 3, 4, 5]  # after, before, after; part 1 would be 5,000
    assert run[1] is top
    source = to_briefed(run, SHA)
    assert source.source == "shop/cart.py:11-50"
    assert [(b.start_line, b.end_line) for b in source.blocks] == [
        (11, 20),
        (21, 30),
        (31, 40),
        (41, 50),
    ]
    assert source.tier == "vector"


def test_an_unsplit_top_hit_is_briefed_alone():
    top = _hit(1)
    assert expand_top(top, [], budget=4000) == [top]


def test_expansion_stays_inside_the_run_of_the_top_hit():
    getter, setter = _parts(2, first_id=10), _parts(3, first_id=20, line=100)
    top = setter[1]
    assert [h.id for h in expand_top(top, [*getter, *setter], budget=4000)] == [20, 21, 22]


def test_block_lines_of_a_briefed_source_come_from_each_part():
    source = to_briefed([_hit(1, line=5)], SHA)
    assert source.blocks == (Block("def total(): ...", 5, 14),)
    assert source.title == "shop.cart.total"


# ── Checks ────────────────────────────────────────────────────────────────────

REPO_URL = "https://github.com/octo/shop"
PARTS = to_briefed(
    [
        _hit(i, part=i, line=i * 10, text=f"shop/cart.py :: total (part {i})\nline {i}")
        for i in (1, 2, 3)
    ],
    SHA,
)
OLD = to_briefed([_hit(9, path="docs/my file#1.md", qualname=None)], "b" * 40)
BRIEFED = (OLD, PARTS)


def _cite(index: int, source: str, start: int = 0, end: int = 1) -> Citation:
    """A citation of blocks [start, end) of the search result at `index`."""
    return Citation(index, source, None, "…", start, end)


def _completion(*citations: Citation, text: str = "It is computed.", truncated=False) -> Completion:
    """An answer with the given citations."""
    return Completion(text, citations, Usage(), truncated)


def test_citations_outside_the_briefing_or_with_another_source_are_dropped():
    citations = [_cite(7, PARTS.source), _cite(0, PARTS.source), _cite(1, PARTS.source)]
    checked = check(_completion(*citations), BRIEFED, REPO_URL, retrieved=True)
    assert [s.source for s in checked.sources] == [PARTS.source]
    assert checked.notes == ["2 citations were dropped: not a source that was provided."]
    one = check(_completion(_cite(5, "x")), BRIEFED, REPO_URL, retrieved=True)
    assert one.notes[0] == "1 citation was dropped: not a source that was provided."


def test_the_citation_check_logs_how_many_came_back_and_how_many_were_dropped():
    citations = [_cite(7, PARTS.source), _cite(0, PARTS.source), _cite(1, PARTS.source)]
    with capture_logs() as logs:
        check(_completion(*citations), BRIEFED, REPO_URL, retrieved=True)
    (entry,) = [line for line in logs if line["event"] == "citations_checked"]
    assert (entry["returned"], entry["dropped"], entry["sources"], entry["briefed"]) == (3, 2, 1, 2)


def test_citations_of_one_source_merge_and_narrow_its_lines_to_the_cited_blocks():
    citations = [_cite(1, PARTS.source, 1, 2), _cite(0, OLD.source), _cite(1, PARTS.source, 2, 3)]
    sources, dropped = resolve(citations, BRIEFED, REPO_URL)
    assert dropped == 0
    assert [s.path for s in sources] == ["shop/cart.py", "docs/my file#1.md"]  # first-cited order
    parts = sources[0]
    assert (parts.start_line, parts.end_line) == (20, 39)
    assert [b.start_line for b in parts.blocks] == [20, 30]
    assert parts.excerpt == "line 2\nline 3"  # the header line of each part is dropped
    assert parts.github_url == f"{REPO_URL}/blob/{SHA}/shop/cart.py#L20-L39"


def test_the_link_quotes_the_path_and_uses_the_sources_own_commit():
    (old,), _ = resolve([_cite(0, OLD.source)], BRIEFED, REPO_URL)
    assert old.github_url == f"{REPO_URL}/blob/{'b' * 40}/docs/my%20file%231.md#L1-L10"


def test_an_empty_block_range_is_dropped():
    assert resolve([_cite(1, PARTS.source, 3, 3)], BRIEFED, REPO_URL) == ([], 1)


def test_the_excerpt_is_capped_in_lines_and_characters():
    long = Block("header\n" + "\n".join("y" * 100 for _ in range(20)), 1, 21)
    text = excerpt([long])
    assert len(text) == EXCERPT_CHARS and text.count("\n") < EXCERPT_LINES
    assert excerpt([Block("header\n" + "\n".join(map(str, range(20))), 1, 21)]).count("\n") == 11


@pytest.mark.parametrize(
    ("text", "retrieved", "expected"),
    [
        ("It is in cart.py.", True, False),
        ("It is in cart.py.", False, True),  # nothing was retrieved
        ("not found in the indexed code. Try billing/.", True, True),  # the model said so
    ],
)
def test_not_found_comes_from_no_sources_or_the_model(text, retrieved, expected):
    assert is_not_found(text, retrieved) is expected


def test_only_an_answer_that_opens_with_the_sentinel_is_not_found():
    partial = (
        "The client is generated from `/api/v1/openapi.json`.\n\n"
        "## What's not found in the indexed code\n\nThe generator's config file."
    )
    assert not is_not_found(partial, True)
    assert is_not_found("**Not found in the indexed code.** Try `billing/`.", True)


def test_a_module_source_links_to_the_whole_file_and_any_other_to_its_lines():
    sha = "a" * 40
    blob = f"{REPO_URL}/blob/{sha}/shop/cart.py"
    assert github_url(REPO_URL, sha, "shop/cart.py", 3, 9) == f"{blob}#L3-L9"
    assert github_url(REPO_URL, sha, "shop/cart.py", 1, 40, whole_file=True) == blob


def test_a_module_search_result_is_named_by_its_bare_path_and_any_other_by_its_lines():
    module = replace(_hit(1, qualname="shop.cart", line=1), kind="module")
    assert to_briefed([module], SHA).source == "shop/cart.py"
    assert to_briefed([_hit(2, line=20)], SHA).source == "shop/cart.py:20-29"


def test_an_uncited_answer_is_noted_unless_it_is_not_found():
    assert check(_completion(), BRIEFED, REPO_URL, True).notes == [NO_CITATIONS_NOTE]
    not_found = check(_completion(text=f"{NOT_FOUND}."), BRIEFED, REPO_URL, True)
    assert (not_found.not_found, not_found.notes) == (True, [])


def test_a_truncated_answer_is_noted():
    checked = check(_completion(_cite(0, OLD.source), truncated=True), BRIEFED, REPO_URL, True)
    assert checked.notes == [TRUNCATED_NOTE]


def test_a_stored_source_briefs_again_as_it_was_cited():
    (source,), _ = resolve([_cite(1, PARTS.source, 0, 2)], BRIEFED, REPO_URL)
    again = from_stored(json.loads(json.dumps(stored(source))))
    assert again == replace(PARTS, blocks=PARTS.blocks[:2])
