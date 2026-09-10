"""Postgres access. Phase 0: a connectivity and pgvector probe; the pool and migrations follow."""

from dataclasses import dataclass

import psycopg

from app.config import settings
from app.logging_setup import get_logger

log = get_logger(__name__)


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
