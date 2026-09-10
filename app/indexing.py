"""The index job: clone, walk, parse or window per file, batched inserts, activate, sweep."""

import shutil
import time
from datetime import UTC, datetime
from typing import Any

from app.chunking import Chunk, chunk_file, window_file
from app.config import settings
from app.errors import IndexTimeoutError, ServiceError
from app.github import GitHubClient, RepoRef, WalkEntry, walk_files
from app.jobs import JobStore
from app.logging_setup import get_logger
from app.parsing import file_symbols, language_for
from app.store import ChunkStore, FileRow

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
