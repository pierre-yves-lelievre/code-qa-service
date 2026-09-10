"""Postgres access: connectivity probe, connection pool, and the migrations runner."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

from app.config import settings
from app.logging_setup import get_logger

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
POOL_MAX_SIZE = 10
# Arbitrary advisory-lock key reserved for the migrations runner.
_MIGRATIONS_LOCK_KEY = 7_300_001

_pool: ConnectionPool | None = None


# ── Probe ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DatabaseStatus:
    """Result of one database probe: reachability and pgvector versions."""

    reachable: bool
    vector_available: str | None
    vector_installed: str | None


def check_database() -> DatabaseStatus:
    """Probe Postgres over a short-lived connection and report pgvector availability."""
    try:
        with psycopg.connect(settings.database_url, connect_timeout=2) as conn:
            row = conn.execute(
                "SELECT default_version, installed_version FROM pg_available_extensions"
                " WHERE name = %s",
                ("vector",),
            ).fetchone()
    except psycopg.Error as exc:
        log.warning("database_check_failed", error_type=type(exc).__name__)
        return DatabaseStatus(reachable=False, vector_available=None, vector_installed=None)
    available, installed = row if row else (None, None)
    return DatabaseStatus(reachable=True, vector_available=available, vector_installed=installed)


# ── Pool ──────────────────────────────────────────────────────────────────────


def get_pool() -> ConnectionPool:
    """Return the process-wide pool, creating and opening it on first use."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=POOL_MAX_SIZE,
            timeout=5,
            kwargs={"connect_timeout": 2},
            name="codeqa",
            open=True,
        )
    return _pool


def close_pool() -> None:
    """Close the pool if it is open; the next get_pool() call creates a fresh one."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """Borrow a pooled connection; commit on success, roll back on exception."""
    with get_pool().connection() as conn:
        yield conn


# ── Migrations ────────────────────────────────────────────────────────────────


def _migration_files(directory: Path) -> list[tuple[int, Path]]:
    """List `NNNN_*.sql` files as (version, path) in version order; reject duplicate versions."""
    files = sorted((int(p.name[:4]), p) for p in directory.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    versions = [version for version, _ in files]
    if len(versions) != len(set(versions)):
        raise RuntimeError(f"Duplicate migration version in {directory}.")
    return files


def run_migrations(
    conn: psycopg.Connection | None = None, directory: Path = MIGRATIONS_DIR
) -> list[str]:
    """Apply unapplied numbered migrations in order under an advisory lock; return their names."""
    if conn is None:
        with psycopg.connect(settings.database_url, connect_timeout=2) as own:
            return run_migrations(own, directory)
    applied: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATIONS_LOCK_KEY,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version integer PRIMARY KEY,"
            " name text NOT NULL,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {version for (version,) in conn.execute("SELECT version FROM schema_migrations")}
        for version, path in _migration_files(directory):
            if version in done:
                continue
            conn.execute(path.read_text())
            conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                (version, path.name),
            )
            applied.append(path.name)
    log.info("migrations_applied", count=len(applied), names=applied)
    return applied
