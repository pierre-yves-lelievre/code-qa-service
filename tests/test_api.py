"""API tests through TestClient."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings, settings
from app.db import DatabaseStatus
from app.main import app

# ── Health ────────────────────────────────────────────────────────────────────


def test_health_reports_db_and_keys(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == settings.app_version
    assert body["uptime_seconds"] >= 0
    assert body["providers"] == "fake"
    assert body["database"]["reachable"] is True
    assert body["database"]["vector_available"]
    assert body["keys"] == {"voyage": False, "anthropic": False, "github": False}
    assert r.headers["X-Request-ID"]


def test_health_is_503_when_database_unreachable(client, monkeypatch: pytest.MonkeyPatch):
    down = DatabaseStatus(reachable=False, vector_available=None, vector_installed=None)
    monkeypatch.setattr("app.api.check_database", lambda: down)
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"
    assert r.json()["database"]["reachable"] is False


# ── Error contract ────────────────────────────────────────────────────────────


def _explode(*args, **kwargs):
    """Stand-in for a route internal that fails unexpectedly."""
    raise RuntimeError("boom: /secret/path")


def test_unexpected_error_is_generic_500(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path / "data")
    monkeypatch.setattr("app.api.check_database", _explode)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/health")
    assert r.status_code == 500
    assert r.json() == {"detail": "Internal error.", "code": "internal_error"}
    assert "boom" not in r.text
    assert "Traceback" not in r.text


def test_real_providers_without_keys_fails_at_startup():
    with pytest.raises(ValidationError, match="requires VOYAGE_API_KEY, ANTHROPIC_API_KEY"):
        Settings(
            _env_file=None,
            database_url="postgresql://unused",
            providers="real",
            voyage_api_key="",
            anthropic_api_key=None,
        )
