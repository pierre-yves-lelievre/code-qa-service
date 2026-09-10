"""Shared with surrogate-model-service; adapted: Postgres over `index_jobs`, DB-generated ids."""

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import psycopg
from psycopg import sql
from psycopg.errors import UniqueViolation
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from app.config import settings
from app.db import connection
from app.errors import IndexInProgressError, TooManyJobsError
from app.logging_setup import get_logger

log = get_logger(__name__)

JobStatus = Literal["pending", "running", "succeeded", "failed"]
ACTIVE_STATUSES = ["pending", "running"]

_UPDATABLE = frozenset({"status", "progress", "error", "snapshot_id", "started_at", "completed_at"})
_COLUMNS = sql.SQL(
    "id::text AS id, repo_id, status, progress, error, snapshot_id,"
    " created_at, started_at, completed_at"
)
# Arbitrary advisory-lock key serialising job creation, so insert-then-count cannot race.
_CREATE_LOCK_KEY = 7_300_002


@dataclass(frozen=True)
class Job:
    """One index job as stored in `index_jobs`."""

    id: str
    repo_id: int
    status: JobStatus
    progress: dict[str, Any]
    error: str | None
    snapshot_id: int | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


INTERRUPTED_MESSAGE = "Interrupted by a service restart."


def fail_interrupted(
    connect: Callable[[], AbstractContextManager[psycopg.Connection]] = connection,
) -> int:
    """Fail jobs and building snapshots left by a previous process; return the job count.

    Jobs run in-process, so at startup none can still be running. Assumes a single process.
    """
    with connect() as conn, conn.transaction():
        conn.execute("UPDATE snapshots SET status = 'failed' WHERE status = 'building'")
        count = conn.execute(
            "UPDATE index_jobs SET status = 'failed', error = %s, completed_at = %s"
            " WHERE status = ANY(%s)",
            (INTERRUPTED_MESSAGE, datetime.now(UTC), ACTIVE_STATUSES),
        ).rowcount
    if count:
        log.warning("jobs_interrupted", count=count)
    return count


def _is_uuid(job_id: str) -> bool:
    """Whether a raw id string is a well-formed UUID."""
    try:
        uuid.UUID(job_id)
    except ValueError:
        return False
    return True


def _count_active(conn: psycopg.Connection) -> int:
    """Count pending and running jobs on the given connection."""
    (count,) = conn.execute(
        "SELECT count(*) FROM index_jobs WHERE status = ANY(%s)", (ACTIVE_STATUSES,)
    ).fetchone()
    return count


class JobStore:
    """Postgres-backed index jobs behind the create/update/get/count_active interface."""

    def __init__(
        self, connect: Callable[[], AbstractContextManager[psycopg.Connection]] = connection
    ) -> None:
        """Keep the connection factory; tests pass one bound to a rolled-back transaction."""
        self._connect = connect

    def create(self, repo_id: int) -> Job:
        """Insert a pending job; raise if the repo already has an active job or the cap is hit."""
        with self._connect() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_CREATE_LOCK_KEY,))
            try:
                with conn.cursor(row_factory=class_row(Job)) as cur:
                    job = cur.execute(
                        sql.SQL("INSERT INTO index_jobs (repo_id) VALUES (%s) RETURNING {}").format(
                            _COLUMNS
                        ),
                        (repo_id,),
                    ).fetchone()
            except UniqueViolation:
                raise IndexInProgressError() from None
            if _count_active(conn) > settings.max_running_jobs:
                raise TooManyJobsError()
        log.info("job_created", job_id=job.id, repo_id=repo_id)
        return job

    def update(self, job_id: str, **fields: Any) -> None:
        """Set whitelisted columns on a job; an unknown id is a no-op, an unknown field an error."""
        unknown = fields.keys() - _UPDATABLE
        if unknown:
            raise ValueError(f"Not updatable on a job: {', '.join(sorted(unknown))}")
        if not fields or not _is_uuid(job_id):
            return
        values = {k: Jsonb(v) if k == "progress" else v for k, v in fields.items()}
        query = sql.SQL("UPDATE index_jobs SET {} WHERE id = %(job_id)s").format(
            sql.SQL(", ").join(
                sql.SQL("{} = {}").format(sql.Identifier(k), sql.Placeholder(k)) for k in values
            )
        )
        with self._connect() as conn, conn.transaction():
            conn.execute(query, {**values, "job_id": job_id})
        log.info("job_updated", job_id=job_id, fields=sorted(fields), status=fields.get("status"))

    def get(self, job_id: str) -> Job | None:
        """Return the job, or None when the id is unknown or not a UUID."""
        if not _is_uuid(job_id):
            return None
        with self._connect() as conn, conn.cursor(row_factory=class_row(Job)) as cur:
            return cur.execute(
                sql.SQL("SELECT {} FROM index_jobs WHERE id = %s").format(_COLUMNS), (job_id,)
            ).fetchone()

    def count_active(self) -> int:
        """Return the number of pending and running jobs across all repos."""
        with self._connect() as conn:
            return _count_active(conn)
