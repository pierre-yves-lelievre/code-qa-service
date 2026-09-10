"""Eval runner: hit@5 over the golden set, plus not-found, the rewrite, latency and cost.

Cases that expect a chunk pay only for retrieval: `plan()` and `retrieve()` run in-process and
the top five full hits are matched. Cases expecting `null`, and every turn before a
conversation's last, go through `ask()`: they are answered and logged to `queries`, where their
latency and cost are read back. Report only; there is no threshold in v0.
"""

import argparse
import json
import statistics
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from app.answering import ask
from app.config import settings
from app.db import check_database, close_pool, connection, run_migrations
from app.embeddings import FakeEmbeddings, VoyageEmbeddings
from app.errors import ServiceError
from app.github import GitHubClient, RepoRef, parse_repo_url
from app.indexing import _run_index
from app.jobs import JobStore
from app.llm import ClaudeLLM, FakeLLM
from app.logging_setup import configure_logging
from app.main import DATABASE_UNREACHABLE, build_clients
from app.planning import HISTORY_TURNS, Turn, plan
from app.retrieval import Hit, retrieve
from app.store import ChunkStore, Snapshot
from app.summary import README_BYTES, SUMMARY_MAX_TOKENS

GOLDEN = Path(__file__).parent / "golden.json"
TOP_K = 5
BYTES_PER_TOKEN = 3  # chunking.estimate_tokens' rule
HEX = frozenset("0123456789abcdef")
FAKE_BANNER = "PROVIDERS=fake: fake vectors and canned answers; the numbers below mean nothing."

Outcome = Literal["hit", "miss", "absent", "not_found", "answered", "error"]
LABELS: dict[Outcome, str] = {
    "hit": "hit",
    "miss": "miss",
    "absent": "absent",
    "not_found": "not found ✓",
    "answered": "answered ✗",
    "error": "error",
}
Row = tuple[dict[str, Any], dict[str, Any]]  # (timings, tokens) of one answered `queries` row

# `right()`, not LIKE, as in store.py: an `_` in an identifier would be a LIKE wildcard.
_TARGET = (
    "SELECT EXISTS (SELECT 1 FROM chunks c JOIN files f ON f.id = c.file_id,"
    " (SELECT %(q)s::text AS q) t"
    " WHERE c.snapshot_id = %(snapshot)s AND f.path = %(path)s AND (t.q IS NULL"
    " OR c.qualname = t.q OR c.name = t.q OR right(c.qualname, length(t.q) + 1) = '.' || t.q))"
)
_ROW = "SELECT timings, tokens FROM queries WHERE request_id = %s ORDER BY id DESC LIMIT 1"
_RUN_ROWS = (
    "SELECT timings, tokens FROM queries"
    " WHERE starts_with(request_id, %s) AND answer IS NOT NULL ORDER BY id"
)
_REPO_ROWS = (
    "SELECT timings, tokens FROM queries WHERE repo_id = %s AND answer IS NOT NULL ORDER BY id"
)


@dataclass(frozen=True)
class Expect:
    """The chunk that should be retrieved: a path, and the symbol when one is the answer."""

    path: str
    qualname: str | None = None


@dataclass(frozen=True)
class Case:
    """One scored question: an entry has one turn, a conversation is scored on its last."""

    id: int
    turns: tuple[str, ...]
    expect: Expect | None
    proves: str


@dataclass(frozen=True)
class Golden:
    """The pinned repository, branch and commit, and the cases."""

    repo: str
    branch: str
    commit: str
    cases: tuple[Case, ...]


@dataclass(frozen=True)
class Result:
    """One case's outcome; `ms` and `usd` are its scored turn's."""

    case: Case
    outcome: Outcome
    rank: int | None = None
    tier: str | None = None
    floor: bool | None = None
    planner: str | None = None
    ms: int = 0
    usd: float = 0.0


class EvalSetupError(Exception):
    """The eval cannot start: the index job failed or the branch has moved past the pin."""


# ── Golden file ───────────────────────────────────────────────────────────────


def load_golden(path: Path = GOLDEN) -> Golden:
    """Read and check the golden file; a malformed one is a ValueError naming the problem."""
    data = json.loads(path.read_text())
    repo, branch, commit = data.get("repo"), data.get("branch"), data.get("commit")
    if not isinstance(repo, str) or not isinstance(branch, str) or not branch:
        raise ValueError(f"{path.name}: `repo` and `branch` are required strings.")
    parse_repo_url(repo)
    if not isinstance(commit, str) or len(commit) != 40 or not set(commit) <= HEX:
        raise ValueError(f"{path.name}: `commit` must be a full 40-character sha.")
    cases = [_case(e, (e.get("question"),)) for e in data.get("entries", [])]
    cases += [_case(c, tuple(c.get("turns") or ())) for c in data.get("conversations", [])]
    ids = [case.id for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path.name}: case ids must be unique.")
    return Golden(repo, branch, commit, tuple(cases))


def _case(raw: Mapping[str, Any], turns: tuple[Any, ...]) -> Case:
    """One entry or conversation as a Case."""
    if not isinstance(raw.get("id"), int) or not turns:
        raise ValueError(f"golden case {raw.get('id')!r}: needs an integer id and a question.")
    if not all(isinstance(t, str) and t.strip() for t in turns):
        raise ValueError(f"golden case {raw['id']}: every question must be a non-empty string.")
    expect = raw.get("expect")
    if expect is not None and not (
        isinstance(expect, dict) and isinstance(expect.get("path"), str)
    ):
        raise ValueError(f"golden case {raw['id']}: `expect` is null or {{path, qualname?}}.")
    target = None if expect is None else Expect(expect["path"], expect.get("qualname"))
    return Case(raw["id"], tuple(turns), target, str(raw.get("proves", "")))


# ── Matching and cost ─────────────────────────────────────────────────────────


def matches(hit: Hit, expect: Expect) -> bool:
    """Same path and, when a symbol is expected, its qualname or name, or a dotted suffix."""
    if hit.path != expect.path:
        return False
    q = expect.qualname
    return q is None or q in (hit.qualname, hit.name) or (hit.qualname or "").endswith(f".{q}")


def rank(hits: Sequence[Hit], expect: Expect) -> tuple[int, Hit] | None:
    """The 1-based rank and the hit of the first match among the top five, or None."""
    for position, hit in enumerate(hits[:TOP_K], start=1):
        if matches(hit, expect):
            return position, hit
    return None


def usd(tokens: Mapping[str, int]) -> float:
    """Dollars for a `queries.tokens`-shaped dict: Claude tokens at the LLM prices, plus embed."""
    return (
        tokens.get("input", 0) * settings.llm_usd_per_mtok_input
        + tokens.get("output", 0) * settings.llm_usd_per_mtok_output
        + tokens.get("cache_read", 0) * settings.llm_usd_per_mtok_cache_read
        + tokens.get("cache_write", 0) * settings.llm_usd_per_mtok_cache_write
        + tokens.get("embed", 0) * settings.embed_usd_per_mtok
    ) / 1_000_000


def index_estimate(size_kb: int) -> str:
    """An upper bound on one index run's cost, from GitHub's size (which includes history)."""
    tokens = size_kb * 1024 // BYTES_PER_TOKEN
    embed = tokens * settings.embed_usd_per_mtok / 1_000_000
    summary = usd({"input": README_BYTES // BYTES_PER_TOKEN, "output": SUMMARY_MAX_TOKENS})
    return (
        f"Index estimate (upper bound from GitHub's size, {size_kb:,} KB): at most {tokens:,}"
        f" tokens to embed, ${embed:.2f}, plus one summary call, about ${summary:.2f}."
        " Content already embedded for this model costs nothing."
    )


# ── Index ─────────────────────────────────────────────────────────────────────


def is_current(snapshot: Snapshot, commit: str, model: str) -> bool:
    """Whether a snapshot is at the pinned commit with vectors from this embedding model."""
    embedded = snapshot.stats.get("embedding", {}).get("model")
    return snapshot.commit_sha == commit and embedded == model


def ensure_indexed(
    golden: Golden,
    reindex: bool,
    store: ChunkStore,
    jobs: JobStore,
    github: GitHubClient,
    embeddings: VoyageEmbeddings | FakeEmbeddings,
    llm: ClaudeLLM | FakeLLM,
) -> tuple[int, Snapshot]:
    """The repo id and its active snapshot at the pin; index first only when it is not current."""
    info = github.repo(parse_repo_url(golden.repo))
    url = f"https://github.com/{info.owner}/{info.name}"
    repo_id = store.upsert_repo(info.owner, info.name, url)
    active = store.active_snapshot(repo_id)
    if active is not None and not reindex and is_current(active, golden.commit, embeddings.model):
        print(f"Indexed at {golden.commit[:12]} with {embeddings.model}: no index cost.")
        return repo_id, active
    print(index_estimate(info.size_kb))
    # The job skips a commit that is already active; rebuild it when asked or for a new model.
    force = reindex or (active is not None and active.commit_sha == golden.commit)
    job = jobs.create(repo_id)
    ref = RepoRef(info.owner, info.name, golden.branch)
    _run_index(job.id, repo_id, ref, jobs, store, github, embeddings, llm, force=force)
    done = jobs.get(job.id)
    if done is None or done.status != "succeeded":
        raise EvalSetupError(f"Indexing failed: {done.error if done else 'the job is gone'}")
    active = store.active_snapshot(repo_id)
    sha = active.commit_sha if active else None
    if active is None or sha != golden.commit:
        raise EvalSetupError(
            f"{golden.branch} is at {sha}, golden.json is pinned at {golden.commit}:"
            f" verify the paths at {sha} and update `commit` in golden.json."
        )
    embedding = active.stats.get("embedding", {})
    print(
        f"Indexed: {embedding.get('tokens', 0):,} tokens embedded,"
        f" ${embedding.get('cost_usd', 0):.4f}."
    )
    return repo_id, active


def absent_targets(snapshot_id: int, cases: Sequence[Case]) -> set[int]:
    """Ids of cases whose expected chunk is not in the snapshot: fix golden.json, not the code."""
    absent: set[int] = set()
    with connection() as conn:
        for case in cases:
            if case.expect is None:
                continue
            params = {"snapshot": snapshot_id, "path": case.expect.path, "q": case.expect.qualname}
            (found,) = conn.execute(_TARGET, params).fetchone()
            if not found:
                absent.add(case.id)
    return absent


# ── Scoring ───────────────────────────────────────────────────────────────────


def request_id(run_id: str, case_id: int, turn: int) -> str:
    """The `queries.request_id` of one asked turn; the run prefix lets its rows be read back."""
    return f"eval-{run_id}-{case_id}-{turn}"


def evaluate(
    golden: Golden,
    repo_id: int,
    snapshot_id: int,
    run_id: str,
    store: ChunkStore,
    embeddings: VoyageEmbeddings | FakeEmbeddings,
    llm: ClaudeLLM | FakeLLM,
) -> list[Result]:
    """Score every case, in file order."""
    absent = absent_targets(snapshot_id, golden.cases)
    return [
        score(case, repo_id, snapshot_id, absent, run_id, store, embeddings, llm)
        for case in golden.cases
    ]


def score(
    case: Case,
    repo_id: int,
    snapshot_id: int,
    absent: set[int],
    run_id: str,
    store: ChunkStore,
    embeddings: VoyageEmbeddings | FakeEmbeddings,
    llm: ClaudeLLM | FakeLLM,
) -> Result:
    """Ask the turns before the last, then score the last by retrieval or through `ask()`."""
    try:
        conversation: str | None = None
        for turn, question in enumerate(case.turns[:-1], start=1):
            rid = request_id(run_id, case.id, turn)
            conversation = ask(
                repo_id, question, conversation, rid, store, embeddings, llm
            ).conversation_id
        last = case.turns[-1]
        if case.expect is None:
            rid = request_id(run_id, case.id, len(case.turns))
            answer = ask(repo_id, last, conversation, rid, store, embeddings, llm)
            timings, tokens = query_row(rid)
            return Result(
                case,
                "not_found" if answer.not_found else "answered",
                planner=answer.retrievers["planner"],
                ms=timings["total_ms"],
                usd=usd(tokens),
            )
        turns = store.recent_turns(repo_id, conversation, HISTORY_TURNS) if conversation else []
        started = time.monotonic()
        planned = plan(last, [Turn(t.question, t.answer) for t in turns], llm)
        found = retrieve(planned, snapshot_id, store, embeddings)
        ms = round((time.monotonic() - started) * 1000)
    except ServiceError as exc:
        print(f"case {case.id}: {exc.code}: {exc}", file=sys.stderr)
        return Result(case, "error")
    ranked = rank(found.full, case.expect)
    outcome: Outcome = "hit" if ranked else "absent" if case.id in absent else "miss"
    return Result(
        case,
        outcome,
        rank=ranked[0] if ranked else None,
        tier=ranked[1].tier if ranked else None,
        floor=found.no_relevant_sources,
        planner=planned.planner,
        ms=ms,
        usd=usd({**asdict(planned.usage), "embed": found.embed_tokens}),
    )


# ── Queries ───────────────────────────────────────────────────────────────────


def query_row(rid: str) -> Row:
    """The timings and tokens logged for one request id."""
    with connection() as conn:
        return conn.execute(_ROW, (rid,)).fetchone()


def run_rows(run_id: str) -> list[Row]:
    """The answered `queries` rows of one eval run."""
    with connection() as conn:
        return conn.execute(_RUN_ROWS, (f"eval-{run_id}-",)).fetchall()


def repo_rows(repo_id: int) -> list[Row]:
    """Every answered `queries` row for the repo, from evals and from the UI alike."""
    with connection() as conn:
        return conn.execute(_REPO_ROWS, (repo_id,)).fetchall()


# ── Report ────────────────────────────────────────────────────────────────────


def report(results: Sequence[Result], run: Sequence[Row], repo: Sequence[Row]) -> str:
    """The markdown table, then hit@5, not-found, latency and cost lines."""
    lines = [
        "| # | Question | Expect | Result | Rank | Tier | Floor | Planner | ms | USD |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        cells = [
            str(r.case.id),
            _cell(" → ".join(r.case.turns)),
            _cell(_expect(r.case.expect)),
            LABELS[r.outcome],
            str(r.rank or "—"),
            r.tier or "—",
            "—" if r.floor is None else "yes" if r.floor else "no",
            r.planner or "—",
            f"{r.ms:,}",
            f"{r.usd:.4f}",
        ]
        lines.append("| " + " | ".join(cells) + " |")
    scored = [r for r in results if r.case.expect is not None]
    hits = sum(r.outcome == "hit" for r in scored)
    absent = [f"#{r.case.id}" for r in scored if r.outcome == "absent"]
    hit_line = f"hit@5: {hits}/{len(scored)}"
    if absent:
        hit_line += f"; absent from the index (fix golden.json, not the code): {', '.join(absent)}"
    null = [r for r in results if r.case.expect is None]
    lines += ["", hit_line, f"not-found: {sum(r.outcome == 'not_found' for r in null)}/{len(null)}"]
    timed = [r for r in scored if r.outcome != "error"]
    if timed:
        lines.append(
            f"retrieval (plan + retrieve) per question: n={len(timed)},"
            f" median {statistics.median(r.ms for r in timed):,.0f} ms,"
            f" median ${statistics.median(r.usd for r in timed):.4f}"
        )
    lines.append(_rows_line("answers this run (queries)", run))
    lines.append(_rows_line("answers for this repo, all runs (queries)", repo))
    total = sum(r.usd for r in scored) + sum(usd(tokens) for _, tokens in run)
    lines.append(f"run total: ${total:.4f}")
    return "\n".join(lines)


def _rows_line(label: str, rows: Sequence[Row]) -> str:
    """Count, median latency, median cost and cache-read share of answered `queries` rows."""
    if not rows:
        return f"{label}: none"
    ms = statistics.median(timings["total_ms"] for timings, _ in rows)
    cost = statistics.median(usd(tokens) for _, tokens in rows)
    read = sum(tokens.get("cache_read", 0) for _, tokens in rows)
    prompt = read + sum(t.get("input", 0) + t.get("cache_write", 0) for _, t in rows)
    share = f"{read / prompt:.0%}" if prompt else "n/a"
    return f"{label}: n={len(rows)}, median {ms:,.0f} ms, median ${cost:.4f}, cache_read {share}"


def _expect(expect: Expect | None) -> str:
    """`path :: qualname`, or `not found` for a null expectation."""
    if expect is None:
        return "not found"
    return f"{expect.path} :: {expect.qualname}" if expect.qualname else expect.path


def _cell(text: str, width: int = 70) -> str:
    """Text safe for one markdown table cell, shortened to `width`."""
    text = text.replace("|", "\\|")
    return text if len(text) <= width else text[: width - 1] + "…"


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    """Run the golden set and print the report; 2 when the eval cannot start."""
    parser = argparse.ArgumentParser(prog="python -m evals.run_evals", description=__doc__)
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="index the pinned repo even when it is current (prints the estimate first)",
    )
    parser.add_argument("--golden", type=Path, default=GOLDEN, help="the golden file")
    parser.add_argument("--verbose", action="store_true", help="service logs at INFO on stderr")
    args = parser.parse_args(argv)
    configure_logging("INFO" if args.verbose else "WARNING")
    try:
        golden = load_golden(args.golden)
    except (OSError, ValueError, ServiceError) as exc:
        return _fail(exc)
    if not check_database().reachable:
        return _fail(DATABASE_UNREACHABLE)
    run_migrations()
    github, embeddings, llm = build_clients()
    try:
        if settings.providers == "fake":
            print(FAKE_BANNER)
        store = ChunkStore()
        repo_id, snapshot = ensure_indexed(
            golden, args.reindex, store, JobStore(), github, embeddings, llm
        )
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        print(f"Run {run_id}: {len(golden.cases)} cases at {golden.commit[:12]}.\n")
        results = evaluate(golden, repo_id, snapshot.id, run_id, store, embeddings, llm)
        print(report(results, run_rows(run_id), repo_rows(repo_id)))
        return 0
    except (EvalSetupError, ServiceError) as exc:
        return _fail(exc)
    finally:
        llm.close()
        embeddings.close()
        github.close()
        close_pool()


def _fail(problem: object) -> int:
    """Print why the eval cannot run; exit code 2."""
    print(f"eval: {problem}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
