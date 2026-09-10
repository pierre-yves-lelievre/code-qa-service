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

from app.chunking import chunk_file
from app.config import Settings, settings
from app.db import DatabaseStatus
from app.jobs import INTERRUPTED_MESSAGE, JobStore
from app.main import app
from app.parsing import file_symbols, language_for
from app.store import ChunkStore

FIXTURES = Path(__file__).parent / "fixtures"
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


def _rows(query: str, *params) -> list[tuple]:
    """Committed rows for a query."""
    with psycopg.connect(settings.database_url) as conn:
        return conn.execute(query, params).fetchall()


def _index(client, mock_github, repo) -> dict:
    """POST /index for a fixture repo via the mock API and file:// clones; the finished job."""
    mock_github(_repo_api(full_name=f"octo/{repo.name}"), clone_base=repo.clone_base)
    r = client.post("/index", json={"url": f"https://github.com/octo/{repo.name}"})
    assert r.status_code == 202, r.json()
    return client.get(f"/index/{r.json()['job_id']}").json()


def _expected_chunks(name: str) -> int:
    """Chunks that parsing and chunking produce for every file of a fixture repo."""
    root, total = FIXTURES / name, 0
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel, text = path.relative_to(root).as_posix(), path.read_text()
        total += len(chunk_file(rel, text, file_symbols(rel, text, language_for(rel)).rows))
    return total


def _expected_symbols(name: str) -> int:
    """Definition rows (module rows excluded) that parsing finds in a fixture repo."""
    root, total = FIXTURES / name, 0
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        total += len(file_symbols(rel, path.read_text(), language_for(rel)).rows) - 1
    return total


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


# ── Index lifecycle ───────────────────────────────────────────────────────────


def test_index_fixture_repo_end_to_end_activates_a_snapshot(
    client, committed, mock_github, fixture_repo
):
    repo = fixture_repo("py_app")
    job = _index(client, mock_github, repo)
    expected = _expected_chunks("py_app")
    assert (job["status"], job["error"]) == ("succeeded", None)
    assert job["progress"] == {"stage": "done", "files": 4, "chunks": expected}
    ((snapshot_id, status, sha, branch, stats),) = _rows(
        "SELECT id, status, commit_sha, branch, stats FROM snapshots"
    )
    assert (snapshot_id, status, sha, branch) == (job["snapshot_id"], "active", repo.head(), "main")
    assert stats["by_mode"] == {"symbols": 4}
    symbols = _expected_symbols("py_app")
    assert stats["by_language"] == {"python": {"files": 4, "symbols": symbols, "chunks": expected}}
    assert stats["files_with_parse_errors"] == 1  # shop/broken.py
    assert _rows("SELECT count(*) FROM files") == [(4,)]
    assert _rows("SELECT count(*) FROM chunks WHERE snapshot_id = %s", snapshot_id) == [(expected,)]
    assert not any((settings.data_dir / "clones").iterdir())  # the clone is removed


def test_failed_reindex_leaves_the_previous_snapshot_active(
    client, committed, mock_github, fixture_repo, monkeypatch: pytest.MonkeyPatch
):
    repo = fixture_repo("py_app")
    first = _index(client, mock_github, repo)
    (repo.path / "shop" / "extra.py").write_text("def extra():\n    return 1\n")
    repo.commit()

    def _boom(*args, **kwargs):
        """Stand-in for a chunker that fails mid-job."""
        raise RuntimeError("boom")

    monkeypatch.setattr("app.indexing.chunk_file", _boom)
    second = _index(client, mock_github, repo)
    assert (second["status"], second["error"]) == ("failed", "Indexing failed unexpectedly.")
    statuses = dict(_rows("SELECT id, status FROM snapshots"))
    assert statuses == {first["snapshot_id"]: "active", second["snapshot_id"]: "failed"}
    chunks = _rows("SELECT count(*) FROM chunks WHERE snapshot_id = %s", first["snapshot_id"])
    assert chunks == [(_expected_chunks("py_app"),)]


def test_reindex_at_the_same_commit_short_circuits(client, committed, mock_github, fixture_repo):
    repo = fixture_repo("py_app")
    first = _index(client, mock_github, repo)
    again = _index(client, mock_github, repo)
    assert again["status"] == "succeeded"
    assert again["progress"] == {"stage": "done", "already_indexed": True}
    assert again["snapshot_id"] == first["snapshot_id"]
    assert _rows("SELECT count(*) FROM snapshots") == [(1,)]


def test_job_past_its_timeout_is_failed(
    client, committed, mock_github, fixture_repo, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("app.config.settings.job_timeout_s", 0)
    job = _index(client, mock_github, fixture_repo("py_app"))
    assert (job["status"], job["error"]) == ("failed", "Index job timed out after 0 s.")
    assert _rows("SELECT count(*) FROM snapshots") == [(0,)]


def test_sweep_keeps_two_snapshots_after_three_indexes(
    client, committed, mock_github, fixture_repo
):
    repo = fixture_repo("py_app")
    for i in range(3):
        (repo.path / "notes.txt").write_text(f"revision {i}\n")
        repo.commit()
        assert _index(client, mock_github, repo)["status"] == "succeeded"
    statuses = [status for (status,) in _rows("SELECT status FROM snapshots ORDER BY id")]
    assert statuses == ["retired", "active"]


def test_symlinks_pointing_outside_the_clone_are_skipped_and_never_indexed(
    client, committed, mock_github, fixture_repo, tmp_path: Path
):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("SECRET_TOKEN = 'leak'\n")
    repo = fixture_repo("py_app")
    (repo.path / "leak.py").symlink_to(outside / "secret.py")
    (repo.path / "leakdir").symlink_to(outside, target_is_directory=True)
    repo.commit()
    assert _index(client, mock_github, repo)["status"] == "succeeded"
    skipped = _rows("SELECT path, mode, skip_reason FROM files WHERE path LIKE 'leak%%'")
    assert sorted(skipped) == [("leak.py", "skipped", "symlink"), ("leakdir", "skipped", "symlink")]
    assert _rows("SELECT count(*) FROM chunks WHERE text LIKE '%%SECRET_TOKEN%%'") == [(0,)]


def test_interrupted_job_is_failed_at_startup_and_the_repo_can_be_indexed_again(
    committed, mock_github, fixture_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path / "data")
    store = ChunkStore()
    repo_id = store.upsert_repo("octo", "py_app", REPO_URL)
    stale = JobStore().create(repo_id)
    building = store.create_snapshot(repo_id, "main", "a" * 40)
    with TestClient(app) as client:  # the lifespan fails what a previous process left running
        job = client.get(f"/index/{stale.id}").json()
        assert (job["status"], job["error"]) == ("failed", INTERRUPTED_MESSAGE)
        assert _rows("SELECT status FROM snapshots WHERE id = %s", building) == [("failed",)]
        assert _index(client, mock_github, fixture_repo("py_app"))["status"] == "succeeded"


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
