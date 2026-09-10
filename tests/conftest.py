"""Shared with surrogate-model-service; adapted: surrogate fixtures replaced by a bare client."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """TestClient with settings pointed at a temporary data directory."""
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path / "data")
    with TestClient(app) as c:
        yield c
