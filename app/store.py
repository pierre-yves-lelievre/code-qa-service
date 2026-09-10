"""ChunkStore: repo upsert, snapshots, per-batch file and chunk inserts, activation, sweep."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Literal

import psycopg
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from app.chunking import Chunk
from app.db import connection
from app.logging_setup import get_logger

log = get_logger(__name__)

FileMode = Literal["symbols", "windows", "skipped"]
SnapshotStatus = Literal["building", "active", "retired", "failed"]
KEEP_SNAPSHOTS = 2

_INSERT_FILE = (
    "INSERT INTO files (snapshot_id, path, language, size_bytes, mode, skip_reason, parse_errors)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id"
)
_INSERT_CHUNK = (
    "INSERT INTO chunks (snapshot_id, file_id, kind, name, qualname, part, start_line, end_line,"
    " signature, doc, text, content_hash, tokens, truncated, search_a, search_b, search_c)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)


@dataclass(frozen=True)
class FileRow:
    """One walked file as stored in `files`."""

    path: str
    language: str | None
    size_bytes: int
    mode: FileMode
    skip_reason: str | None = None
    parse_errors: int = 0


@dataclass(frozen=True)
class Snapshot:
    """One snapshot of a repository at a commit."""

    id: int
    repo_id: int
    commit_sha: str | None
    branch: str
    status: SnapshotStatus


def _chunk_params(snapshot_id: int, file_id: int, c: Chunk) -> tuple[Any, ...]:
    """The `_INSERT_CHUNK` parameters for one chunk."""
    return (
        snapshot_id, file_id, c.kind, c.name, c.qualname, c.part, c.start_line, c.end_line,
        c.signature, c.doc, c.text, c.content_hash, c.tokens, c.truncated,
        c.search_a, c.search_b, c.search_c,
    )  # fmt: skip


class ChunkStore:
    """Postgres writes for indexing: repos, snapshots, files and chunks."""

    def __init__(
        self, connect: Callable[[], AbstractContextManager[psycopg.Connection]] = connection
    ) -> None:
        """Keep the connection factory; tests pass one bound to a rolled-back transaction."""
        self._connect = connect

    def upsert_repo(self, owner: str, name: str, url: str) -> int:
        """Insert a repo or refresh its URL; return its id."""
        with self._connect() as conn, conn.transaction():
            (repo_id,) = conn.execute(
                "INSERT INTO repos (owner, name, url) VALUES (%s, %s, %s)"
                " ON CONFLICT (owner, name) DO UPDATE SET url = EXCLUDED.url RETURNING id",
                (owner, name, url),
            ).fetchone()
        return repo_id

    def active_snapshot(self, repo_id: int) -> Snapshot | None:
        """The repo's active snapshot, or None before its first successful index."""
        with self._connect() as conn, conn.cursor(row_factory=class_row(Snapshot)) as cur:
            return cur.execute(
                "SELECT id, repo_id, commit_sha, branch, status FROM snapshots"
                " WHERE repo_id = %s AND status = 'active'",
                (repo_id,),
            ).fetchone()

    def create_snapshot(self, repo_id: int, branch: str, commit_sha: str) -> int:
        """Insert a `building` snapshot at a commit; return its id."""
        with self._connect() as conn, conn.transaction():
            (snapshot_id,) = conn.execute(
                "INSERT INTO snapshots (repo_id, branch, commit_sha) VALUES (%s, %s, %s)"
                " RETURNING id",
                (repo_id, branch, commit_sha),
            ).fetchone()
        log.info("snapshot_created", snapshot_id=snapshot_id, repo_id=repo_id)
        return snapshot_id

    def add_files(self, snapshot_id: int, batch: list[tuple[FileRow, list[Chunk]]]) -> int:
        """Insert a batch of files and their chunks in one transaction; return the chunk count."""
        count = 0
        with self._connect() as conn, conn.transaction():
            for file, chunks in batch:
                (file_id,) = conn.execute(
                    _INSERT_FILE,
                    (
                        snapshot_id,
                        file.path,
                        file.language,
                        file.size_bytes,
                        file.mode,
                        file.skip_reason,
                        file.parse_errors,
                    ),
                ).fetchone()
                if chunks:
                    with conn.cursor() as cur:
                        cur.executemany(
                            _INSERT_CHUNK, [_chunk_params(snapshot_id, file_id, c) for c in chunks]
                        )
                count += len(chunks)
        return count

    def activate(self, snapshot_id: int, stats: dict[str, Any]) -> None:
        """Retire the repo's active snapshot and activate this one, in one transaction."""
        with self._connect() as conn, conn.transaction():
            row = conn.execute(
                "SELECT repo_id FROM snapshots WHERE id = %s AND status = 'building'",
                (snapshot_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Snapshot {snapshot_id} is not building.")
            (repo_id,) = row
            conn.execute("SELECT id FROM repos WHERE id = %s FOR UPDATE", (repo_id,))
            conn.execute(
                "UPDATE snapshots SET status = 'retired' WHERE repo_id = %s AND status = 'active'",
                (repo_id,),
            )
            conn.execute(
                "UPDATE snapshots SET status = 'active', stats = %s, indexed_at = now()"
                " WHERE id = %s",
                (Jsonb(stats), snapshot_id),
            )
        log.info("snapshot_activated", snapshot_id=snapshot_id, repo_id=repo_id)

    def fail_snapshot(self, snapshot_id: int) -> None:
        """Mark a building snapshot failed; any other status is left alone."""
        with self._connect() as conn, conn.transaction():
            conn.execute(
                "UPDATE snapshots SET status = 'failed' WHERE id = %s AND status = 'building'",
                (snapshot_id,),
            )

    def sweep(self, repo_id: int, keep: int = KEEP_SNAPSHOTS) -> int:
        """Delete retired and failed snapshots outside the newest `keep`; return how many."""
        with self._connect() as conn, conn.transaction():
            deleted = conn.execute(
                "DELETE FROM snapshots WHERE repo_id = %s AND status IN ('retired', 'failed')"
                " AND id NOT IN (SELECT id FROM snapshots WHERE repo_id = %s"
                " AND status <> 'building' ORDER BY id DESC LIMIT %s)",
                (repo_id, repo_id, keep),
            ).rowcount
        log.info("snapshots_swept", repo_id=repo_id, deleted=deleted)
        return deleted
