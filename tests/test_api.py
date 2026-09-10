"""API tests through TestClient."""

import json
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings, settings
from app.db import DatabaseStatus
from app.jobs import JobStore
from app.main import app
from app.store import ChunkStore

SEARCH = json.loads((Path(__file__).parent / "fixtures" / "github" / "search.json").read_text())
REPO_URL = "https://github.com/octo/py_app"


def _repo_api(status: int = 200, **fields) -> Callable[[httpx.Request], httpx.Response]:
    """A GitHub API handler answering every call with one repo body, or an error status."""
    body = {"full_name": "octo/py_app", "private": False, "default_branch": "main", "size": 10}
    return lambda request: httpx.Response(status, json=body | fields)


def _repo_count() -> int:
    """Committed rows in `repos`."""
    with psycopg.connect(settings.database_url) as conn:
        (count,) = conn.execute("SELECT count(*) FROM repos").fetchone()
    return count


def _error(response: httpx.Response) -> tuple[int, str]:
    """Status and error code of an error response."""
    return response.status_code, response.json()["code"]


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
    assert body["database"]["vector_installed"]
    assert body["keys"] == {"voyage": False, "anthropic": False, "github": False}
    assert r.headers["X-Request-ID"]


def test_health_is_503_when_database_unreachable(client, monkeypatch: pytest.MonkeyPatch):
    down = DatabaseStatus(reachable=False, vector_available=None, vector_installed=None)
    monkeypatch.setattr("app.api.check_database", lambda: down)
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"
    assert r.json()["database"]["reachable"] is False


# ── Repo search ───────────────────────────────────────────────────────────────


def test_search_proxies_github_and_returns_five_hits(client, mock_github):
    mock_github(lambda request: httpx.Response(200, json=SEARCH))
    r = client.get("/repos/search", params={"q": "fastapi"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["full_name"] for i in items] == [i["full_name"] for i in SEARCH["items"]]
    assert set(items[0]) == {
        "full_name", "owner", "name", "description", "stars", "language",
        "size_kb", "default_branch", "url", "clone_url",
    }  # fmt: skip


def test_search_without_a_query_is_a_validation_error(client):
    assert _error(client.get("/repos/search")) == (422, "validation_error")


@pytest.mark.parametrize(
    ("status", "expected"),
    [(403, (429, "github_rate_limited")), (500, (502, "github_unavailable"))],
)
def test_search_maps_github_failures_to_typed_errors(
    client, mock_github, status: int, expected: tuple[int, str]
):
    mock_github(lambda request: httpx.Response(status, json={}))
    assert _error(client.get("/repos/search", params={"q": "fastapi"})) == expected


# ── Index ─────────────────────────────────────────────────────────────────────


def test_index_rejects_a_url_outside_github(client, committed):
    r = client.post("/index", json={"url": "https://gitlab.com/octo/py_app"})
    assert _error(r) == (422, "invalid_repo_url")
    assert _repo_count() == 0


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (_repo_api(404), (404, "github_repo_not_found")),
        (_repo_api(private=True), (404, "github_repo_not_found")),
        (_repo_api(403), (429, "github_rate_limited")),
        (_repo_api(503), (502, "github_unavailable")),
        (_repo_api(size=10**9), (413, "repo_too_large")),
    ],
)
def test_index_precheck_errors_start_no_work(
    client, committed, mock_github, handler, expected: tuple[int, str]
):
    mock_github(handler)
    assert _error(client.post("/index", json={"url": REPO_URL})) == expected
    assert _repo_count() == 0


def test_index_is_accepted_and_a_failed_clone_is_recorded_on_the_job(
    client, committed, mock_github
):
    mock_github(_repo_api(), clone_base="file:///nowhere")
    r = client.post("/index", json={"url": "https://github.com/Octo/Py_App"})
    assert r.status_code == 202
    accepted = r.json()
    assert (accepted["owner"], accepted["name"], accepted["branch"], accepted["status"]) == (
        "octo",
        "py_app",
        "main",
        "pending",
    )
    job = client.get(f"/index/{accepted['job_id']}").json()
    assert (job["status"], job["error"], job["snapshot_id"]) == (
        "failed",
        "Cloning the repository failed.",
        None,
    )
    assert job["completed_at"] >= job["started_at"]


def test_second_index_while_one_is_active_is_409(client, committed, mock_github):
    mock_github(_repo_api())
    JobStore().create(ChunkStore().upsert_repo("octo", "py_app", REPO_URL))
    assert _error(client.post("/index", json={"url": REPO_URL})) == (409, "index_in_progress")


def test_index_over_the_global_job_cap_is_429(
    client, committed, mock_github, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("app.config.settings.max_running_jobs", 1)
    mock_github(_repo_api())
    JobStore().create(ChunkStore().upsert_repo("octo", "other", "https://github.com/octo/other"))
    assert _error(client.post("/index", json={"url": REPO_URL})) == (429, "too_many_jobs")


@pytest.mark.parametrize("job_id", [str(uuid.uuid4()), "not-a-uuid"])
def test_unknown_job_is_404(client, job_id: str):
    assert _error(client.get(f"/index/{job_id}")) == (404, "job_not_found")


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
