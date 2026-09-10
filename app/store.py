"""ChunkStore: repo upsert, snapshots, per-batch files, chunks and embeddings, activation, sweep."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Literal

import psycopg
from pgvector import Vector
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from app.chunking import Chunk
from app.db import connection
from app.logging_setup import get_logger
from app.retrieval import Hit

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

# ── Retrieval SQL: every query yields `Hit` columns, scoped to one snapshot ─────
_HIT = (
    "SELECT c.id, f.path, c.kind, c.name, c.qualname, c.part, c.start_line, c.end_line,"
    " c.signature, c.text, c.truncated"
)
_FROM = " FROM chunks c JOIN files f ON f.id = c.file_id"
# `right()`, not LIKE: an `_` in an identifier would be a LIKE wildcard.
_MATCHES = "(c.qualname = u.x OR c.name = u.x OR right(c.qualname, length(u.x) + 1) = '.' || u.x)"
_MEMBERS = (
    "SELECT u.x FROM unnest(%s::text[]) WITH ORDINALITY AS u(x, ord)"
    " WHERE EXISTS (SELECT 1 FROM chunks c WHERE c.snapshot_id = %s AND " + _MATCHES + ")"
    " ORDER BY u.ord"
)
_SYMBOLS = (
    "WITH m AS (SELECT c.id, u.ord,"
    " CASE WHEN c.qualname = u.x THEN 1 WHEN c.name = u.x THEN 2 ELSE 3 END AS rung"
    " FROM unnest(%s::text[]) WITH ORDINALITY AS u(x, ord)"
    " JOIN chunks c ON c.snapshot_id = %s AND " + _MATCHES + "),"
    " kept AS (SELECT id, ord, rung FROM"
    " (SELECT *, min(rung) OVER (PARTITION BY ord) AS top FROM m) r WHERE rung = top),"
    " best AS (SELECT DISTINCT ON (id) id, ord, rung FROM kept ORDER BY id, ord) "
    + _HIT + ", 1.0::float8 / b.rung AS score" + _FROM + " JOIN best b ON b.id = c.id"
    " ORDER BY b.ord, b.rung, f.path, c.start_line, c.part LIMIT %s"
)  # fmt: skip
_FTS_AND = (
    _HIT + ", ts_rank_cd(c.tsv, q) AS score" + _FROM + ", plainto_tsquery('simple', %s) AS q"
    " WHERE c.snapshot_id = %s AND c.tsv @@ q ORDER BY score DESC, c.id LIMIT %s"
)
_FTS_OR = (
    _HIT + ", ts_rank_cd(c.tsv, q) AS score" + _FROM + ", to_tsquery('simple', %s) AS q"
    " WHERE c.snapshot_id = %s AND c.tsv @@ q ORDER BY score DESC, c.id LIMIT %s"
)
_VECTOR = (
    _HIT + ", 1 - (e.embedding <=> %s) AS score" + _FROM
    + " JOIN embeddings e ON e.content_hash = c.content_hash AND e.model = %s"
    " WHERE c.snapshot_id = %s ORDER BY e.embedding <=> %s LIMIT %s"
)  # fmt: skip


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

    def pending_embeddings(self, snapshot_id: int, model: str) -> tuple[list[tuple[str, int]], int]:
        """Distinct hashes lacking a `model` vector, with tokens; and how many already have one."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT ON (c.content_hash) c.content_hash, c.tokens,"
                " EXISTS (SELECT 1 FROM embeddings e"
                "  WHERE e.content_hash = c.content_hash AND e.model = %s)"
                " FROM chunks c WHERE c.snapshot_id = %s ORDER BY c.content_hash",
                (model, snapshot_id),
            ).fetchall()
        pending = [(content_hash, tokens) for content_hash, tokens, done in rows if not done]
        return pending, len(rows) - len(pending)

    def chunk_texts(self, snapshot_id: int, hashes: list[str]) -> dict[str, str]:
        """The text for each content hash in a snapshot, fetched one embed batch at a time."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT ON (content_hash) content_hash, text FROM chunks"
                " WHERE snapshot_id = %s AND content_hash = ANY(%s)",
                (snapshot_id, hashes),
            ).fetchall()
        return dict(rows)

    def add_embeddings(self, model: str, rows: list[tuple[str, list[float]]]) -> int:
        """Store one batch of vectors in one transaction; an existing (hash, model) is kept."""
        with self._connect() as conn, conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO embeddings (content_hash, model, embedding) VALUES (%s, %s, %s)"
                " ON CONFLICT (content_hash, model) DO NOTHING",
                [(content_hash, model, Vector(vector)) for content_hash, vector in rows],
            )
        return len(rows)

    # ── Retrieval ─────────────────────────────────────────────────────────────

    def resolve_identifiers(self, snapshot_id: int, candidates: list[str]) -> list[str]:
        """The candidates that are a name, a qualname, or a dotted qualname suffix, in order."""
        if not candidates:
            return []
        with self._connect() as conn, conn.transaction():
            rows = conn.execute(_MEMBERS, (candidates, snapshot_id)).fetchall()
        return [x for (x,) in rows]

    def symbol_search(self, snapshot_id: int, identifiers: list[str], limit: int) -> list[Hit]:
        """The ladder per identifier (qualname, name, dotted suffix), each at its first rung."""
        with self._connect() as conn, conn.transaction():
            with conn.cursor(row_factory=class_row(Hit)) as cur:
                return cur.execute(_SYMBOLS, (identifiers, snapshot_id, limit)).fetchall()

    def text_search(
        self, snapshot_id: int, text: str, terms: list[str], limit: int
    ) -> tuple[list[Hit], Literal["and", "or"]]:
        """Full text: every word of `text` (AND); when that finds nothing, any of `terms` (OR)."""
        with self._connect() as conn, conn.transaction():
            with conn.cursor(row_factory=class_row(Hit)) as cur:
                if text.strip():
                    hits = cur.execute(_FTS_AND, (text, snapshot_id, limit)).fetchall()
                    if hits:
                        return hits, "and"
                if not terms:
                    return [], "and"
                return cur.execute(
                    _FTS_OR, (" | ".join(terms), snapshot_id, limit)
                ).fetchall(), "or"

    def vector_search(
        self, snapshot_id: int, model: str, vector: list[float], limit: int
    ) -> list[Hit]:
        """The chunks whose `model` vectors are nearest the query vector, by cosine."""
        query = Vector(vector)
        with self._connect() as conn, conn.transaction():
            # The HNSW index spans every snapshot; iterate it until `limit` rows pass the filter.
            conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
            with conn.cursor(row_factory=class_row(Hit)) as cur:
                return cur.execute(_VECTOR, (query, model, snapshot_id, query, limit)).fetchall()

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
