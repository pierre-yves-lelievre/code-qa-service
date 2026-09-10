"""Shared with surrogate-model-service; adapted: surrogate fixtures replaced by a bare client."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
