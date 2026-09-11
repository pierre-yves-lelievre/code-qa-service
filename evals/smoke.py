"""Smoke run: index one small public repository and ask it one question, end to end.

With PROVIDERS=real this is the first call that spends money: the estimate is printed first.
A second run skips the index (same commit) and costs only the question.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from app.answering import Answer, ask
from app.config import settings
from app.db import check_database, close_pool, run_migrations
from app.embeddings import FakeEmbeddings, VoyageEmbeddings
from app.errors import ServiceError
from app.github import GitHubClient, RepoRef, parse_repo_url
from app.indexing import _run_index
from app.jobs import JobStore
from app.llm import ClaudeLLM, FakeLLM
from app.logging_setup import configure_logging
from app.main import DATABASE_UNREACHABLE, build_clients
from app.store import ChunkStore
from evals.run_evals import FAKE_BANNER, index_estimate, usd

REPO = "https://github.com/pallets/markupsafe"
QUESTION = "What does escape() do with an object that has an __html__ method?"
QUESTION_USD = 0.05  # one planner call and one answer over a ~15k-token briefing


class SmokeError(Exception):
    """The smoke failed: the index job, or an answer that cites nothing."""


def index(
    url: str,
    store: ChunkStore,
    jobs: JobStore,
    github: GitHubClient,
    embeddings: VoyageEmbeddings | FakeEmbeddings,
    llm: ClaudeLLM | FakeLLM,
) -> int:
    """Index the repo at its default branch head and print what it cost; the repo id."""
    info = github.repo(parse_repo_url(url))
    print(index_estimate(info.size_kb))
    print(f"Question estimate: about ${QUESTION_USD:.2f}.")
    repo_id = store.upsert_repo(
        info.owner, info.name, f"https://github.com/{info.owner}/{info.name}"
    )
    job = jobs.create(repo_id)
    ref = RepoRef(info.owner, info.name, info.default_branch)
    _run_index(job.id, repo_id, ref, jobs, store, github, embeddings, llm)
    done = jobs.get(job.id)
    snapshot = store.active_snapshot(repo_id)
    if done is None or done.status != "succeeded" or snapshot is None:
        raise SmokeError(f"indexing failed: {done.error if done else 'the job is gone'}")
    if done.progress.get("already_indexed"):
        print(f"Already indexed at {snapshot.commit_sha[:12]}: no index cost.")
        return repo_id
    stats = snapshot.stats
    embedding = stats.get("embedding", {})
    summary = stats.get("summary", {})
    summary_usd = usd(
        {"input": summary.get("input_tokens", 0), "output": summary.get("output_tokens", 0)}
    )
    print(
        f"Indexed {info.owner}/{info.name} at {snapshot.commit_sha[:12]} in {stats['seconds']}s:"
        f" {stats['files']} files, {stats['chunks']} chunks,"
        f" {embedding.get('tokens', 0):,} tokens embedded (${embedding.get('cost_usd', 0):.4f}),"
        f" summary {summary.get('status', '?')} (${summary_usd:.4f})."
    )
    return repo_id


def answer(
    repo_id: int,
    question: str,
    store: ChunkStore,
    embeddings: VoyageEmbeddings | FakeEmbeddings,
    llm: ClaudeLLM | FakeLLM,
) -> Answer:
    """Ask one question and print its sources, tokens, latency and cost."""
    rid = f"smoke-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    result = ask(repo_id, question, None, rid, store, embeddings, llm)
    print(f"\nQ: {question}\n\n{result.answer}\n")
    for n, source in enumerate(result.sources, start=1):
        print(f"[{n}] {source.path}:{source.start_line}-{source.end_line} ({source.tier})")
    tokens = ", ".join(f"{key} {value:,}" for key, value in result.tokens.items())
    print(
        f"\nTokens: {tokens}. {result.timings.get('total_ms', 0):,} ms, ${usd(result.tokens):.4f}."
    )
    if result.not_found or not result.sources:
        raise SmokeError("the answer cites no source")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Index, ask, report; 1 when the smoke fails, 2 when it cannot start."""
    parser = argparse.ArgumentParser(prog="python -m evals.smoke", description=__doc__)
    parser.add_argument("--repo", default=REPO, help="a public GitHub repository URL")
    parser.add_argument("--question", default=QUESTION, help="the one question to ask")
    parser.add_argument("--verbose", action="store_true", help="service logs at INFO on stderr")
    args = parser.parse_args(argv)
    configure_logging("INFO" if args.verbose else "WARNING")
    if not check_database().reachable:
        print(f"smoke: {DATABASE_UNREACHABLE}", file=sys.stderr)
        return 2
    run_migrations()
    github, embeddings, llm = build_clients()
    try:
        if settings.providers == "fake":
            print(FAKE_BANNER)
        store = ChunkStore()
        repo_id = index(args.repo, store, JobStore(), github, embeddings, llm)
        answer(repo_id, args.question, store, embeddings, llm)
        print("\nsmoke: ok")
        return 0
    except (SmokeError, ServiceError) as exc:
        print(f"smoke: {exc}", file=sys.stderr)
        return 1
    finally:
        llm.close()
        embeddings.close()
        github.close()
        close_pool()


if __name__ == "__main__":
    sys.exit(main())
