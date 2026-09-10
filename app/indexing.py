"""The index job: clone, walk, parse or window, batched inserts, embed, summary, activate, sweep."""

import shutil
import time
from datetime import UTC, datetime
from typing import Any

from app.chunking import Chunk, chunk_file, window_file
from app.config import settings
from app.embeddings import FakeEmbeddings, VoyageEmbeddings, token_batches
from app.errors import EmbedBudgetExceededError, IndexTimeoutError, ServiceError
from app.github import GitHubClient, RepoRef, WalkEntry, walk_files
from app.jobs import JobStore
from app.llm import ClaudeLLM, FakeLLM
from app.logging_setup import get_logger
from app.parsing import file_symbols, language_for
from app.store import ChunkStore, FileRow
from app.summary import inputs as summary_inputs
from app.summary import summarize

log = get_logger(__name__)

BATCH_FILES = 50
WINDOWED_LANGUAGE = "text"  # the stats key for files that are windowed, not parsed


# ── Job ───────────────────────────────────────────────────────────────────────


def _run_index(
    job_id: str,
    repo_id: int,
    ref: RepoRef,
    jobs: JobStore,
    store: ChunkStore,
    github: GitHubClient,
    embeddings: VoyageEmbeddings | FakeEmbeddings,
    llm: ClaudeLLM | FakeLLM,
) -> None:
    """Index a repo at its branch head; any failure marks the job and snapshot failed."""
    started = time.monotonic()
    deadline = started + settings.job_timeout_s
    dest = settings.data_dir / "clones" / job_id
    snapshot_id: int | None = None
    try:
        jobs.update(
            job_id, status="running", started_at=datetime.now(UTC), progress={"stage": "cloning"}
        )
        log.info("index_started", job_id=job_id, repo_id=repo_id)
        embeddings.check_key()
        _check_deadline(deadline)
        dest.parent.mkdir(parents=True, exist_ok=True)
        clone_timeout = min(settings.clone_timeout_s, deadline - time.monotonic())
        sha = github.shallow_clone(ref, dest, timeout=clone_timeout)

        active = store.active_snapshot(repo_id)
        if active is not None and active.commit_sha == sha:
            jobs.update(
                job_id,
                status="succeeded",
                snapshot_id=active.id,
                completed_at=datetime.now(UTC),
                progress={"stage": "done", "already_indexed": True},
            )
            log.info("index_skipped", job_id=job_id, repo_id=repo_id, snapshot_id=active.id)
            return

        snapshot_id = store.create_snapshot(repo_id, ref.branch or "", sha)
        jobs.update(job_id, snapshot_id=snapshot_id, progress={"stage": "walking"})
        entries = walk_files(dest)
        stats = _empty_stats()
        batch: list[tuple[FileRow, list[Chunk]]] = []
        for done, entry in enumerate(entries, start=1):
            _check_deadline(deadline)
            row, chunks, symbols = _index_file(entry)
            _tally(stats, row, chunks, symbols)
            batch.append((row, chunks))
            if len(batch) == BATCH_FILES or done == len(entries):
                store.add_files(snapshot_id, batch)
                batch = []
                jobs.update(
                    job_id,
                    progress={
                        "stage": "parsing",
                        "files_done": done,
                        "files_total": len(entries),
                        "chunks": stats["chunks"],
                    },
                )

        stats["embedding"] = _embed(job_id, snapshot_id, embeddings, jobs, store, deadline)
        _check_deadline(deadline)
        jobs.update(job_id, progress={"stage": "summarizing"})
        stats["summary"] = _summarize(repo_id, entries, llm, store)
        _check_deadline(deadline)
        stats["seconds"] = round(time.monotonic() - started, 2)
        store.activate(snapshot_id, stats)
        swept = store.sweep(repo_id)
        jobs.update(
            job_id,
            status="succeeded",
            completed_at=datetime.now(UTC),
            progress={"stage": "done", "files": stats["files"], "chunks": stats["chunks"]},
        )
        log.info(
            "index_succeeded",
            job_id=job_id,
            repo_id=repo_id,
            snapshot_id=snapshot_id,
            files=stats["files"],
            chunks=stats["chunks"],
            embedded=stats["embedding"]["embedded"],
            tokens=stats["embedding"]["tokens"],
            cost_usd=stats["embedding"]["cost_usd"],
            seconds=stats["seconds"],
            swept=swept,
        )
    except Exception as exc:
        _fail(job_id, snapshot_id, exc, jobs, store)
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def _check_deadline(deadline: float) -> None:
    """Raise IndexTimeoutError once the job's deadline has passed."""
    if time.monotonic() >= deadline:
        raise IndexTimeoutError(f"Index job timed out after {settings.job_timeout_s:.0f} s.")


def _fail(
    job_id: str, snapshot_id: int | None, exc: Exception, jobs: JobStore, store: ChunkStore
) -> None:
    """Mark the snapshot and the job failed, logging by type only; never raise."""
    message = exc.message if isinstance(exc, ServiceError) else "Indexing failed unexpectedly."
    log.error("index_failed", job_id=job_id, snapshot_id=snapshot_id, error_type=type(exc).__name__)
    try:
        if snapshot_id is not None:
            store.fail_snapshot(snapshot_id)
        jobs.update(job_id, status="failed", error=message, completed_at=datetime.now(UTC))
    except Exception as inner:
        log.error("index_failure_not_recorded", job_id=job_id, error_type=type(inner).__name__)


# ── Embeddings ────────────────────────────────────────────────────────────────


def _embed(
    job_id: str,
    snapshot_id: int,
    embeddings: VoyageEmbeddings | FakeEmbeddings,
    jobs: JobStore,
    store: ChunkStore,
    deadline: float,
) -> dict[str, Any]:
    """Embed the snapshot's hashes with no vector for this model, committing per batch.

    The estimate is checked against the per-job cap before any request, and the actual usage
    after each batch; batches already committed are kept either way.
    """
    budget = settings.max_embed_tokens_per_job
    pending, reused = store.pending_embeddings(snapshot_id, embeddings.model)
    estimated = sum(tokens for _, tokens in pending)
    if estimated > budget:
        raise EmbedBudgetExceededError(
            f"This repository needs about {estimated:,} tokens to embed, "
            f"over the per-job limit of {budget:,}."
        )
    used = done = 0
    ranges = token_batches(
        [tokens for _, tokens in pending], embeddings.batch_tokens, embeddings.batch_items
    )
    for batch in ranges:
        _check_deadline(deadline)
        hashes = [pending[i][0] for i in batch]
        texts = store.chunk_texts(snapshot_id, hashes)
        embedded = embeddings.embed_documents([texts[h] for h in hashes])
        store.add_embeddings(embeddings.model, list(zip(hashes, embedded.vectors, strict=True)))
        used += embedded.tokens
        done += len(hashes)
        jobs.update(
            job_id,
            progress={
                "stage": "embedding",
                "chunks_done": done,
                "chunks_total": len(pending),
                "tokens": used,
            },
        )
        if used > budget:
            raise EmbedBudgetExceededError(
                f"Embedding used {used:,} tokens, over the per-job limit of {budget:,}."
            )
    return {
        "model": embeddings.model,
        "embedded": done,
        "reused": reused,
        "tokens_estimated": estimated,
        "tokens": used,
        "cost_usd": round(used * embeddings.usd_per_mtok / 1_000_000, 6),
    }


# ── Summary ───────────────────────────────────────────────────────────────────


def _summarize(
    repo_id: int, entries: list[WalkEntry], llm: ClaudeLLM | FakeLLM, store: ChunkStore
) -> dict[str, Any]:
    """Best effort: store the summary and questions; a failed call writes nothing."""
    readme, tree = summary_inputs(entries)
    result = summarize(readme, tree, llm)
    if result is None:
        return {"status": "failed"}
    store.set_summary(repo_id, result.text, list(result.questions))
    return {
        "status": "ok",
        "input_tokens": result.usage.input,
        "output_tokens": result.usage.output,
    }


# ── Files ─────────────────────────────────────────────────────────────────────


def _index_file(entry: WalkEntry) -> tuple[FileRow, list[Chunk], int]:
    """Parse or window one walked file: its row, its chunks, and its definition count."""
    language = language_for(entry.path)
    if entry.skip_reason is not None:
        return FileRow(entry.path, language, entry.size_bytes, "skipped", entry.skip_reason), [], 0
    try:
        text = entry.abs_path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return FileRow(entry.path, language, entry.size_bytes, "skipped", "not_utf8"), [], 0
    if language is None:
        row = FileRow(entry.path, None, entry.size_bytes, "windows")
        return row, window_file(entry.path, text), 0
    result = file_symbols(entry.path, text, language)
    row = FileRow(entry.path, language, entry.size_bytes, "symbols", None, result.error_nodes)
    return row, chunk_file(entry.path, text, result.rows), len(result.rows) - 1


def _empty_stats() -> dict[str, Any]:
    """The snapshot stats before any file is counted."""
    return {
        "files": 0,
        "chunks": 0,
        "by_mode": {},
        "by_language": {},
        "skipped": {},
        "files_with_parse_errors": 0,
    }


def _tally(stats: dict[str, Any], row: FileRow, chunks: list[Chunk], symbols: int) -> None:
    """Count one file into the snapshot stats."""
    stats["files"] += 1
    stats["chunks"] += len(chunks)
    stats["by_mode"][row.mode] = stats["by_mode"].get(row.mode, 0) + 1
    if row.mode == "skipped":
        stats["skipped"][row.skip_reason] = stats["skipped"].get(row.skip_reason, 0) + 1
        return
    language = stats["by_language"].setdefault(
        row.language or WINDOWED_LANGUAGE, {"files": 0, "symbols": 0, "chunks": 0}
    )
    language["files"] += 1
    language["symbols"] += symbols
    language["chunks"] += len(chunks)
    if row.parse_errors:
        stats["files_with_parse_errors"] += 1
