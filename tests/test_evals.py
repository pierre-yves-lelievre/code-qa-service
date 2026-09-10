"""Eval runner: matching, cost, the golden file, the report, and a zero-spend run over py_app."""

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import psycopg
import pytest

from app.config import settings
from app.db import close_pool
from app.embeddings import FakeEmbeddings
from app.github import GitHubClient
from app.jobs import JobStore
from app.llm import FakeLLM
from app.retrieval import Hit
from app.store import ChunkStore
from evals.run_evals import (
    GOLDEN,
    Case,
    EvalSetupError,
    Expect,
    Golden,
    Result,
    ensure_indexed,
    evaluate,
    load_golden,
    matches,
    rank,
    report,
    run_rows,
    usd,
)


def _hit(path: str, qualname: str | None = None, name: str | None = None) -> Hit:
    """A retrieved chunk with only the fields matching looks at set."""
    return Hit(1, path, "function", name, qualname, 0, 1, 5, None, "text", False, 1.0, "symbol")


# ── Matching, cost, golden file, report ───────────────────────────────────────


def test_a_path_only_expectation_matches_any_chunk_of_that_exact_path():
    expect = Expect("README.md")
    assert matches(_hit("README.md"), expect)
    assert matches(_hit("README.md", "intro"), expect)
    assert not matches(_hit("docs/README.md"), expect)


def test_a_qualname_matches_exactly_or_as_a_dotted_suffix_and_never_as_a_prefix():
    user = Expect("app/models.py", "User")
    assert matches(_hit("app/models.py", "User"), user)
    assert not matches(_hit("app/models.py", "UserBase"), user)
    assert not matches(_hit("app/other.py", "User"), user)
    auth = Expect("app/crud.py", "authenticate")
    assert matches(_hit("app/crud.py", "Crud.authenticate"), auth)
    assert not matches(_hit("app/crud.py", "reauthenticate"), auth)


def test_the_name_counts_when_a_chunk_has_no_qualname():
    assert matches(_hit("src/useAuth.ts", None, "useAuth"), Expect("src/useAuth.ts", "useAuth"))


def test_rank_is_the_first_match_within_the_top_five_only():
    hits = [_hit(f"f{i}.py") for i in range(6)]
    assert rank(hits, Expect("f2.py")) == (3, hits[2])
    assert rank(hits, Expect("f5.py")) is None


def test_usd_prices_each_token_kind_per_million(monkeypatch: pytest.MonkeyPatch):
    for key, price in (("input", 1), ("output", 2), ("cache_read", 3), ("cache_write", 4)):
        monkeypatch.setattr(f"app.config.settings.llm_usd_per_mtok_{key}", float(price))
    monkeypatch.setattr("app.config.settings.embed_usd_per_mtok", 5.0)
    kinds = ("input", "output", "cache_read", "cache_write", "embed")
    assert usd(dict.fromkeys(kinds, 1_000_000)) == 15.0
    assert usd({}) == 0.0


def test_the_golden_file_is_pinned_and_keeps_the_plan_cases():
    golden = load_golden(GOLDEN)
    assert golden.repo == "https://github.com/fastapi/full-stack-fastapi-template"
    assert len(golden.commit) == 40
    by_id = {case.id: case for case in golden.cases}
    assert len(by_id) == len(golden.cases)
    assert by_id[13].expect is None
    assert by_id[7].expect == Expect("backend/app/crud.py", "authenticate")
    conversation = by_id[14]
    assert conversation.turns[0] == by_id[7].turns[0]
    assert conversation.expect == Expect("backend/tests/crud/test_user.py")
    assert sum(case.expect is not None for case in golden.cases) == 13  # hit@5 is out of 13


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["entries"][1].update(id=1), "unique"),
        (lambda d: d.update(commit="cb740b6"), "40-character sha"),
        (lambda d: d["entries"][0].update(expect={"qualname": "x"}), "expect"),
        (lambda d: d["conversations"][0].update(turns=[]), "question"),
    ],
)
def test_a_malformed_golden_file_is_rejected_with_the_problem(tmp_path: Path, change, message):
    data = json.loads(GOLDEN.read_text())
    change(data)
    path = tmp_path / "golden.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=message):
        load_golden(path)


def test_the_report_has_a_row_per_case_then_hit_at_5_not_found_and_cost_lines():
    target, absent = (
        Case(1, ("Q one?",), Expect("a.py", "f"), ""),
        Case(2, ("Q two?",), Expect("b.py"), ""),
    )
    null = Case(3, ("Stripe?",), None, "")
    results = [
        Result(target, "hit", 1, "symbol", False, "ok", 10, 0.001),
        Result(absent, "absent", floor=False, planner="ok", ms=30, usd=0.003),
        Result(null, "not_found", planner="ok", ms=900, usd=0.03),
    ]
    run = [({"total_ms": 900}, {"input": 1000, "output": 100, "cache_read": 0, "embed": 5})]
    text = report(results, run, run)
    rows = [line for line in text.splitlines() if line.startswith("| ") and "---" not in line]
    assert len(rows) == 1 + len(results)  # the header, then one row per case
    assert "| 1 | Q one? | a.py :: f | hit | 1 | symbol | no | ok | 10 | 0.0010 |" in rows
    assert "hit@5: 1/2; absent from the index (fix golden.json, not the code): #2" in text
    assert "not-found: 1/1" in text
    assert "retrieval (plan + retrieve) per question: n=2, median 20 ms" in text
    assert "answers this run (queries): n=1, median 900 ms" in text
    assert "cache_read 0%" in text


# ── Index and run over py_app, with fakes ─────────────────────────────────────


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: None) -> Iterator[None]:
    """Fake providers and a temporary data dir; the process pool is closed afterwards."""
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path / "data")
    monkeypatch.setattr("app.config.settings.providers", "fake")
    yield
    close_pool()


def _golden(repo, cases: tuple[Case, ...] = (), commit: str | None = None) -> Golden:
    """A golden set pinned to a fixture repo, at its head unless told otherwise."""
    return Golden(f"https://github.com/octo/{repo.name}", "main", commit or repo.head(), cases)


def _github(repo) -> GitHubClient:
    """A GitHubClient whose API calls describe the fixture repo and whose clones are file://."""
    body = {"full_name": f"octo/{repo.name}", "private": False, "default_branch": "main"}
    handler = httpx.MockTransport(lambda request: httpx.Response(200, json=body | {"size": 10}))
    return GitHubClient(transport=handler, clone_base=repo.clone_base)


def _rows(query: str) -> list[tuple]:
    """Committed rows for a query."""
    with psycopg.connect(settings.database_url) as conn:
        return conn.execute(query).fetchall()


def test_the_pinned_repo_is_indexed_once_then_skipped_without_a_new_job(env, fixture_repo, capsys):
    repo = fixture_repo("py_app")
    embeddings = FakeEmbeddings(settings.embedding_dims)
    args = (ChunkStore(), JobStore(), _github(repo), embeddings, FakeLLM())
    _, first = ensure_indexed(_golden(repo), False, *args)
    assert first.commit_sha == repo.head()
    assert "Index estimate (upper bound" in capsys.readouterr().out
    _, again = ensure_indexed(_golden(repo), False, *args)
    assert again.id == first.id
    assert "no index cost" in capsys.readouterr().out
    assert _rows("SELECT count(*) FROM index_jobs") == [(1,)]


def test_a_snapshot_at_the_pin_is_rebuilt_for_another_model_or_on_reindex(env, fixture_repo):
    repo = fixture_repo("py_app")
    store, jobs, github, llm = ChunkStore(), JobStore(), _github(repo), FakeLLM()
    _, first = ensure_indexed(
        _golden(repo), False, store, jobs, github, FakeEmbeddings(settings.embedding_dims), llm
    )
    other = FakeEmbeddings(settings.embedding_dims)
    other.model = "fake-other"
    _, rebuilt = ensure_indexed(_golden(repo), False, store, jobs, github, other, llm)
    assert rebuilt.id != first.id
    assert (rebuilt.commit_sha, rebuilt.stats["embedding"]["model"]) == (repo.head(), "fake-other")
    _, forced = ensure_indexed(_golden(repo), True, store, jobs, github, other, llm)
    assert forced.id not in (first.id, rebuilt.id)


def test_a_branch_that_moved_past_the_pin_stops_with_both_commits(env, fixture_repo):
    repo = fixture_repo("py_app")
    embeddings = FakeEmbeddings(settings.embedding_dims)
    with pytest.raises(EvalSetupError, match=repo.head()) as raised:
        ensure_indexed(
            _golden(repo, commit="0" * 40),
            False,
            ChunkStore(),
            JobStore(),
            _github(repo),
            embeddings,
            FakeLLM(),
        )
    assert "update `commit` in golden.json" in str(raised.value)


def test_evaluate_answers_only_the_null_case_and_earlier_turns_and_replays_history(
    env, fixture_repo
):
    repo = fixture_repo("py_app")
    cases = (
        Case(1, ("What does tax_rate do?",), Expect("shop/util.py", "tax_rate"), "symbol"),
        Case(2, ("Where is the Stripe integration?",), None, "not found"),
        Case(
            3,
            ("What does slugify do?", "Where is Product defined?"),
            Expect("shop/models.py", "Product"),
            "rewrite",
        ),
        Case(4, ("Where is the nowhere module?",), Expect("shop/nowhere.py"), "absent"),
    )
    golden = _golden(repo, cases)
    store, embeddings, llm = ChunkStore(), FakeEmbeddings(settings.embedding_dims), FakeLLM()
    repo_id, snapshot = ensure_indexed(
        golden, False, store, JobStore(), _github(repo), embeddings, llm
    )
    llm.requests.clear()  # drop the index run's summary call

    results = evaluate(golden, repo_id, snapshot.id, "run1", store, embeddings, llm)

    assert [(r.case.id, r.outcome) for r in results if r.case.expect] == [
        (1, "hit"),
        (3, "hit"),
        (4, "absent"),
    ]
    assert results[1].outcome in ("not_found", "answered")
    assert results[0].planner == "ok" and results[0].usd > 0
    answers = [r for r in llm.requests if r["kind"] == "complete"]
    assert len(answers) == 2  # the null case and the conversation's first turn; no target entry
    last_plan = [r for r in llm.requests if r["kind"] == "structured"][-2]
    assert [m["role"] for m in last_plan["messages"]] == ["user", "assistant", "user"]
    assert last_plan["messages"][0]["content"] == "What does slugify do?"
    assert _rows("SELECT request_id FROM queries ORDER BY id") == [
        ("eval-run1-2-1",),
        ("eval-run1-3-1",),
    ]
    assert len(run_rows("run1")) == 2
    assert run_rows("run2") == []
