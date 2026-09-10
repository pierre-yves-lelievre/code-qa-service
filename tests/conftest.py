"""Shared with surrogate-model-service; adapted: bare client plus rolled-back db fixtures."""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import run_migrations
from app.main import app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """TestClient with fake providers, no keys, and a temporary data directory."""
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path / "data")
    monkeypatch.setattr("app.config.settings.providers", "fake")
    monkeypatch.setattr("app.config.settings.voyage_api_key", None)
    monkeypatch.setattr("app.config.settings.anthropic_api_key", None)
    monkeypatch.setattr("app.config.settings.github_token", None)
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def migrated() -> None:
    """Apply the real migrations once per session, committed, before any db test."""
    run_migrations()


@pytest.fixture
def db(migrated: None) -> Iterator[psycopg.Connection]:
    """A connection inside one transaction that is rolled back when the test ends."""
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        with conn.transaction(force_rollback=True):
            yield conn
