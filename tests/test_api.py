"""API tests through TestClient."""

import json
import threading
import time
import uuid
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.answering import FLOOR_NOTE, NO_CITATIONS_NOTE, NOT_FOUND
from app.chunking import Chunk, chunk_file, estimate_tokens
from app.config import Settings, settings
from app.db import DatabaseStatus
from app.embeddings import EMBED_BATCH_TOKENS, Embedded, FakeEmbeddings, VoyageEmbeddings
from app.errors import ProviderError
from app.github import GitHubClient, RepoRef
from app.indexing import _run_index
from app.jobs import INTERRUPTED_MESSAGE, JobStore
from app.llm import Citation, Completion, FakeLLM, Usage
from app.main import app
from app.parsing import file_symbols, language_for
from app.retrieval import Retrieval
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


def _fixture_chunks(name: str) -> list[Chunk]:
    """Chunks that parsing and chunking produce for every file of a fixture repo."""
    root, chunks = FIXTURES / name, []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel, text = path.relative_to(root).as_posix(), path.read_text()
        chunks += chunk_file(rel, text, file_symbols(rel, text, language_for(rel)).rows)
    return chunks


def _expected_chunks(name: str) -> int:
    """How many chunks a fixture repo produces."""
    return len(_fixture_chunks(name))


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
    ((hashes, tokens),) = _rows(
        "SELECT count(*), sum(tokens) FROM (SELECT DISTINCT content_hash, tokens FROM chunks"
        " WHERE snapshot_id = %s) c",
        snapshot_id,
    )
    assert stats["embedding"] == {
        "model": "fake",
        "embedded": hashes,
        "reused": 0,
        "tokens_estimated": tokens,
        "tokens": tokens,  # the fake reports the estimate as its usage
        "cost_usd": 0.0,
    }
    assert _rows(
        "SELECT count(*) FROM chunks c WHERE c.snapshot_id = %s AND NOT EXISTS"
        " (SELECT 1 FROM embeddings e WHERE e.content_hash = c.content_hash AND e.model = 'fake')",
        snapshot_id,
    ) == [(0,)]
    assert not any((settings.data_dir / "clones").iterdir())  # the clone is removed


def test_index_stores_a_summary_that_the_repo_route_returns_with_its_snapshot(
    client, committed, mock_github, fixture_repo
):
    repo = fixture_repo("py_app")
    job = _index(client, mock_github, repo)
    r = client.get(f"/repos/{job['repo_id']}")
    assert r.status_code == 200
    body = r.json()
    assert (body["owner"], body["name"], body["url"]) == ("octo", "py_app", REPO_URL)
    assert body["summary"] == "A fake summary of the repository."
    assert len(body["suggested_questions"]) == 4
    snapshot = body["snapshot"]
    assert (snapshot["snapshot_id"], snapshot["commit_sha"]) == (job["snapshot_id"], repo.head())
    assert snapshot["indexed_at"] is not None
    assert snapshot["stats"]["summary"]["status"] == "ok"
    assert snapshot["stats"]["summary"]["input_tokens"] > 0


def test_a_failed_summary_still_activates_the_snapshot_and_leaves_the_summary_null(
    client, committed, mock_github, mock_llm, fixture_repo
):
    mock_llm(FakeLLM(replies=[ProviderError("Claude timed out or is unreachable.")]))
    job = _index(client, mock_github, fixture_repo("py_app"))
    assert job["status"] == "succeeded"
    body = client.get(f"/repos/{job['repo_id']}").json()
    assert (body["summary"], body["suggested_questions"]) == (None, None)
    assert body["snapshot"]["stats"]["summary"] == {"status": "failed"}


def test_a_repo_without_a_snapshot_has_none_and_an_unknown_repo_is_404(client, committed):
    repo_id = ChunkStore().upsert_repo("octo", "fresh", "https://github.com/octo/fresh")
    assert client.get(f"/repos/{repo_id}").json()["snapshot"] is None
    assert _error(client.get("/repos/999999")) == (404, "repo_not_found")


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


def test_forced_index_at_the_same_commit_builds_and_activates_a_new_snapshot(
    client, committed, mock_github, fixture_repo
):
    repo = fixture_repo("py_app")
    first = _index(client, mock_github, repo)
    github = GitHubClient(transport=httpx.MockTransport(_repo_api()), clone_base=repo.clone_base)
    jobs = JobStore()
    job = jobs.create(first["repo_id"])
    _run_index(
        job.id,
        first["repo_id"],
        RepoRef("octo", "py_app", "main"),
        jobs,
        ChunkStore(),
        github,
        FakeEmbeddings(settings.embedding_dims),
        FakeLLM(),
        force=True,
    )
    forced = jobs.get(job.id)
    assert forced.status == "succeeded"
    assert "already_indexed" not in forced.progress
    statuses = dict(_rows("SELECT id, status FROM snapshots"))
    assert statuses == {first["snapshot_id"]: "retired", forced.snapshot_id: "active"}
    shas = _rows("SELECT commit_sha FROM snapshots WHERE id = %s", forced.snapshot_id)
    assert shas == [(repo.head(),)]


class _SlowFakeEmbeddings(FakeEmbeddings):
    """Pauses before each batch, so an index job sits mid-run long enough to be watched."""

    def embed_documents(self, texts: list[str]) -> Embedded:
        """Wait, then embed like the fake."""
        time.sleep(0.5)
        return super().embed_documents(texts)


def test_index_progress_is_visible_to_another_connection_while_the_job_runs(
    client, committed, fixture_repo
):
    repo = fixture_repo("py_app")
    repo_id = ChunkStore().upsert_repo("octo", "py_app", REPO_URL)
    jobs = JobStore()
    job = jobs.create(repo_id)
    github = GitHubClient(transport=httpx.MockTransport(_repo_api()), clone_base=repo.clone_base)
    embeddings = _SlowFakeEmbeddings(settings.embedding_dims)
    ref = RepoRef("octo", "py_app", "main")
    args = (job.id, repo_id, ref, jobs, ChunkStore(), github, embeddings, FakeLLM())
    worker = threading.Thread(target=_run_index, args=args)
    stages: list[str | None] = []
    with psycopg.connect(settings.database_url, autocommit=True) as watcher:
        worker.start()
        while worker.is_alive():
            row = watcher.execute("SELECT progress FROM index_jobs WHERE id = %s", (job.id,))
            stages.append(row.fetchone()[0].get("stage"))
            time.sleep(0.02)
        worker.join()
    assert jobs.get(job.id).status == "succeeded"
    assert {"parsing", "embedding"} & set(stages)  # committed per batch, not at the end


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


# ── Embeddings ────────────────────────────────────────────────────────────────


class _RecordingFake(FakeEmbeddings):
    """FakeEmbeddings that records every batch it is asked to embed."""

    def __init__(self, batch_tokens: int = EMBED_BATCH_TOKENS, overbill: int = 1) -> None:
        """Use the real vector size; optionally a smaller batch budget or inflated usage."""
        super().__init__(settings.embedding_dims)
        self.batch_tokens = batch_tokens
        self.overbill = overbill
        self.batches: list[list[str]] = []

    def embed_documents(self, texts: list[str]) -> Embedded:
        """Record the batch, then embed it, reporting `overbill` times the estimated usage."""
        self.batches.append(list(texts))
        embedded = super().embed_documents(texts)
        return Embedded(embedded.vectors, embedded.tokens * self.overbill)

    def embedded_hashes(self) -> list[str]:
        """Content hashes of every text embedded so far, in order."""
        return [sha256(text.encode()).hexdigest() for batch in self.batches for text in batch]


def _hashes(snapshot_id: int) -> set[str]:
    """Distinct committed chunk hashes of one snapshot."""
    rows = _rows("SELECT DISTINCT content_hash FROM chunks WHERE snapshot_id = %s", snapshot_id)
    return {content_hash for (content_hash,) in rows}


def test_embedding_batches_respect_the_token_budget(
    client, committed, mock_github, mock_embeddings, fixture_repo
):
    fake = mock_embeddings(_RecordingFake(batch_tokens=200))
    job = _index(client, mock_github, fixture_repo("py_app"))
    assert job["status"] == "succeeded"
    assert len(fake.batches) > 1
    for batch in fake.batches:
        assert len(batch) == 1 or sum(map(estimate_tokens, batch)) <= 200  # oversize goes alone
    embedded = fake.embedded_hashes()
    assert sorted(embedded) == sorted(_hashes(job["snapshot_id"]))  # each hash exactly once


def test_embedding_estimate_over_the_job_cap_fails_before_any_request(
    client, committed, mock_github, mock_embeddings, fixture_repo, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("app.config.settings.max_embed_tokens_per_job", 1)
    fake = mock_embeddings(_RecordingFake())
    job = _index(client, mock_github, fixture_repo("py_app"))
    assert job["status"] == "failed"
    assert job["error"].startswith("This repository needs about ")
    assert fake.batches == []
    assert _rows("SELECT status FROM snapshots") == [("failed",)]
    assert _rows("SELECT count(*) FROM embeddings") == [(0,)]


def test_actual_usage_over_the_job_cap_fails_and_keeps_committed_batches(
    client, committed, mock_github, mock_embeddings, fixture_repo, monkeypatch: pytest.MonkeyPatch
):
    distinct = {c.content_hash: c.tokens for c in _fixture_chunks("py_app")}
    # The estimate fits the cap exactly; the provider then bills ten times the estimate.
    monkeypatch.setattr("app.config.settings.max_embed_tokens_per_job", sum(distinct.values()))
    fake = mock_embeddings(_RecordingFake(batch_tokens=200, overbill=10))
    job = _index(client, mock_github, fixture_repo("py_app"))
    assert job["status"] == "failed"
    assert job["error"].startswith("Embedding used ")
    assert 0 < len(fake.embedded_hashes()) < len(distinct)
    stored = _rows("SELECT content_hash FROM embeddings WHERE model = 'fake'")
    assert {content_hash for (content_hash,) in stored} == set(fake.embedded_hashes())


def test_rejected_embedding_key_fails_the_job_before_cloning(
    client, committed, mock_github, mock_embeddings
):
    calls: list[int] = []

    def _unauthorized(request: httpx.Request) -> httpx.Response:
        """Voyage refusing the key."""
        calls.append(1)
        return httpx.Response(401)

    mock_embeddings(
        VoyageEmbeddings(
            "test-key",
            "voyage-code-4",
            settings.embedding_dims,
            transport=httpx.MockTransport(_unauthorized),
            sleep=lambda seconds: None,
        )
    )
    mock_github(_repo_api(), clone_base="file:///nowhere")  # a clone would fail as clone_failed
    r = client.post("/index", json={"url": REPO_URL})
    job = client.get(f"/index/{r.json()['job_id']}").json()
    assert (job["status"], job["error"]) == ("failed", "Voyage rejected the API key.")
    assert len(calls) == 1
    assert _rows("SELECT count(*) FROM snapshots") == [(0,)]


def test_reindex_of_unchanged_content_embeds_nothing(
    client, committed, mock_github, mock_embeddings, fixture_repo
):
    fake = mock_embeddings(_RecordingFake())
    repo = fixture_repo("py_app")
    first = _index(client, mock_github, repo)
    embedded = len(fake.embedded_hashes())
    assert embedded == len(_hashes(first["snapshot_id"])) > 0
    fake.batches.clear()

    repo.commit("Empty commit.")  # a new sha over the same tree
    second = _index(client, mock_github, repo)
    assert second["status"] == "succeeded"
    assert second["snapshot_id"] != first["snapshot_id"]
    assert fake.batches == []
    ((stats,),) = _rows(
        "SELECT stats->'embedding' FROM snapshots WHERE id = %s", second["snapshot_id"]
    )
    assert (stats["embedded"], stats["reused"], stats["tokens"]) == (0, embedded, 0)

    third = _index(client, mock_github, repo)  # the same sha short-circuits before embedding
    assert third["progress"] == {"stage": "done", "already_indexed": True}
    assert fake.batches == []


def test_new_content_is_embedded_and_unchanged_content_reused(
    client, committed, mock_github, mock_embeddings, fixture_repo
):
    fake = mock_embeddings(_RecordingFake())
    repo = fixture_repo("py_app")
    before = _hashes(_index(client, mock_github, repo)["snapshot_id"])
    fake.batches.clear()

    (repo.path / "shop" / "extra.py").write_text("def extra():\n    return 1\n")
    repo.commit()
    after = _hashes(_index(client, mock_github, repo)["snapshot_id"])
    assert sorted(fake.embedded_hashes()) == sorted(after - before)
    assert 0 < len(after - before) < len(after)


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


# ── Ask ───────────────────────────────────────────────────────────────────────

TAX_QUESTION = "Where is tax_rate defined?"


def _ask(client, repo_id: int, question: str, conversation_id: str | None = None, **headers):
    """POST /ask for one question, optionally continuing a conversation."""
    body: dict = {"repo_id": repo_id, "question": question}
    if conversation_id:
        body["conversation_id"] = conversation_id
    return client.post("/ask", json=body, headers=headers)


def _indexed_py_app(client, mock_github, fixture_repo) -> tuple[int, str]:
    """Index the py_app fixture; its repo id and commit sha."""
    repo = fixture_repo("py_app")
    job = _index(client, mock_github, repo)
    assert job["status"] == "succeeded"
    return job["repo_id"], repo.head()


def _kinds(llm: FakeLLM) -> list[str]:
    """The kinds of the calls the fake received, in order."""
    return [r["kind"] for r in llm.requests]


def test_ask_answers_with_cited_sources_server_links_tokens_and_a_logged_query(
    client, committed, mock_github, mock_llm, fixture_repo
):
    repo_id, sha = _indexed_py_app(client, mock_github, fixture_repo)
    llm = mock_llm(FakeLLM())  # installed after indexing: the summary call is not counted
    r = client.post(
        "/ask", json={"repo_id": repo_id, "question": TAX_QUESTION}, headers={"X-Request-ID": "q-1"}
    )
    assert r.status_code == 200, r.json()
    assert r.headers["X-Request-ID"] == "q-1"
    body = r.json()
    assert body["not_found"] is False and body["notes"] == []
    (source,) = body["sources"]
    lines = f"#L{source['start_line']}-L{source['end_line']}"
    assert source["github_url"] == f"{REPO_URL}/blob/{sha}/{source['path']}{lines}"
    assert source["excerpt"] and source["tier"] in ("symbol", "fts", "vector")
    assert body["retrievers"]["symbol"]["status"] == "ok"
    assert body["retrievers"]["planner"] == "ok"
    assert body["snapshot"]["commit_sha"] == sha
    assert set(body["timings"]) == {"plan_ms", "embed_ms", "retrieve_ms", "llm_ms", "total_ms"}

    planner, answer = llm.requests
    assert _kinds(llm) == ["structured", "complete"]
    assert (answer["max_tokens"], answer["timeout"]) == (1500, settings.answer_timeout_s)
    assert answer["system"][1]["text"].startswith(f"Repository octo/py_app at commit {sha}.")
    assert "A fake summary of the repository." in answer["system"][1]["text"]
    plan_reply = {"query": TAX_QUESTION, "identifiers": [], "intent": "explain"}
    inputs = [estimate_tokens(json.dumps([q["system"], q["messages"]])) for q in llm.requests]
    outputs = [estimate_tokens(json.dumps(plan_reply)), estimate_tokens(body["answer"])]
    assert body["tokens"] == {
        "input": sum(inputs),
        "output": sum(outputs),
        "cache_read": 0,
        "cache_write": 0,
    }
    ((request_id, conversation, question, logged, not_found, tokens, sources),) = _rows(
        "SELECT request_id, conversation_id::text, question, answer, not_found, tokens, sources"
        " FROM queries"
    )
    assert (request_id, conversation, question, logged, not_found) == (
        "q-1",
        body["conversation_id"],
        TAX_QUESTION,
        body["answer"],
        False,
    )
    assert tokens["embed"] > 0 and tokens["input"] == body["tokens"]["input"]
    assert sources[0]["commit_sha"] == sha and sources[0]["blocks"]


def test_a_citation_to_a_source_that_was_not_briefed_is_dropped_and_noted(
    client, committed, mock_github, mock_llm, fixture_repo
):
    repo_id, _ = _indexed_py_app(client, mock_github, fixture_repo)
    stray = Citation(99, "nowhere.py:1-2", None, "x", 0, 1)
    mock_llm(FakeLLM(completions=[Completion("It is in tax.py.", (stray,), Usage(10, 5))]))
    body = _ask(client, repo_id, TAX_QUESTION).json()
    assert body["sources"] == []
    assert body["notes"] == [
        "1 citation was dropped: not a source that was provided.",
        NO_CITATIONS_NOTE,
    ]


def test_a_question_the_floor_rejects_is_not_found_and_the_request_says_so(
    client, committed, mock_github, mock_llm, fixture_repo
):
    repo_id, _ = _indexed_py_app(client, mock_github, fixture_repo)
    llm = mock_llm(FakeLLM())
    body = _ask(client, repo_id, "Where is the Stripe integration?").json()
    assert body["not_found"] is True
    answer = llm.requests[-1]
    assert {"type": "text", "text": FLOOR_NOTE} in answer["messages"][-1]["content"]


def test_the_briefing_honours_the_full_hits_and_index_lines_settings(
    client, committed, mock_github, mock_llm, fixture_repo, monkeypatch: pytest.MonkeyPatch
):
    repo_id, _ = _indexed_py_app(client, mock_github, fixture_repo)
    monkeypatch.setattr("app.config.settings.answer_full_hits", 1)
    monkeypatch.setattr("app.config.settings.answer_index_lines", 2)
    llm = mock_llm(FakeLLM())
    assert _ask(client, repo_id, TAX_QUESTION).status_code == 200
    _, answer = llm.requests
    content = answer["messages"][-1]["content"]
    assert sum(block["type"] == "search_result" for block in content) == 1
    index_block = content[-1]["text"].splitlines()
    assert len(index_block) == 1 + 2  # the header, then two index lines


def test_feedback_is_stored_on_the_answer_and_an_unknown_query_is_404(
    client, committed, mock_github, fixture_repo
):
    repo_id, _ = _indexed_py_app(client, mock_github, fixture_repo)
    query_id = _ask(client, repo_id, TAX_QUESTION).json()["query_id"]
    rated = client.post(f"/queries/{query_id}/feedback", json={"feedback": "down"})
    assert rated.status_code == 200
    assert rated.json() == {"query_id": query_id, "feedback": "down"}
    assert _rows("SELECT feedback FROM queries WHERE id = %s", query_id) == [("down",)]
    missing = client.post("/queries/999999/feedback", json={"feedback": "up"})
    assert (missing.status_code, missing.json()["code"]) == (404, "query_not_found")


def test_with_nothing_retrieved_no_answer_call_is_made(
    client, committed, mock_github, mock_llm, fixture_repo, monkeypatch: pytest.MonkeyPatch
):
    repo_id, _ = _indexed_py_app(client, mock_github, fixture_repo)
    empty = {"status": "empty", "hits": 0, "ms": 0}
    nothing = Retrieval([], True, {"symbol": empty, "fts": empty, "vector": empty}, (), 0)
    monkeypatch.setattr("app.answering.retrieve", lambda *args: nothing)
    llm = mock_llm(FakeLLM())
    body = _ask(client, repo_id, TAX_QUESTION).json()
    assert _kinds(llm) == ["structured"]
    assert (body["answer"], body["not_found"], body["sources"]) == (f"{NOT_FOUND}.", True, [])
    assert body["timings"]["llm_ms"] == 0
    assert _rows("SELECT answer, not_found FROM queries") == [(f"{NOT_FOUND}.", True)]


def test_a_second_turn_replays_the_first_with_its_sources_under_a_cache_breakpoint(
    client, committed, mock_github, mock_llm, fixture_repo
):
    repo_id, _ = _indexed_py_app(client, mock_github, fixture_repo)
    llm = mock_llm(FakeLLM())
    first = _ask(client, repo_id, TAX_QUESTION).json()
    second = _ask(client, repo_id, "and what calls it?", first["conversation_id"]).json()
    assert second["conversation_id"] == first["conversation_id"]
    _, answer1, planner2, answer2 = llm.requests
    assert planner2["messages"] == [
        {"role": "user", "content": TAX_QUESTION},
        {"role": "assistant", "content": first["answer"]},
        {"role": "user", "content": "and what calls it?"},
    ]
    cited = next(b for b in answer1["messages"][-1]["content"] if b["type"] == "search_result")
    replayed_user, replayed_answer = answer2["messages"][:2]
    assert replayed_user == {
        "role": "user",
        "content": [{"type": "text", "text": TAX_QUESTION}, cited],
    }
    assert replayed_answer == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": first["answer"], "cache_control": {"type": "ephemeral"}}
        ],
    }
    assert answer2["system"] == answer1["system"]
    assert second["sources"]  # cited at an index offset by the replayed source

    other = _ask(client, repo_id, "and what calls it?").json()
    assert other["conversation_id"] != first["conversation_id"]
    assert len(llm.requests[-1]["messages"]) == 1  # a new conversation has no history


def test_history_is_capped_at_four_turns(client, committed, mock_github, mock_llm, fixture_repo):
    repo_id, _ = _indexed_py_app(client, mock_github, fixture_repo)
    llm = mock_llm(FakeLLM())
    conversation = None
    for n in range(6):
        body = _ask(client, repo_id, f"{TAX_QUESTION} ({n})", conversation).json()
        conversation = body["conversation_id"]
    answer = llm.requests[-1]
    assert len(answer["messages"]) == 4 * 2 + 1
    assert answer["messages"][0]["content"][0]["text"] == f"{TAX_QUESTION} (1)"


def test_ask_errors_are_typed(client, committed):
    assert _error(_ask(client, 999999, "x")) == (404, "repo_not_found")
    repo_id = ChunkStore().upsert_repo("octo", "fresh", "https://github.com/octo/fresh")
    assert _error(_ask(client, repo_id, "x")) == (409, "repo_not_indexed")
    assert _error(_ask(client, repo_id, "   ")) == (422, "validation_error")


def test_a_failed_answer_call_is_502_and_logged_without_an_answer(
    client, committed, mock_github, mock_llm, fixture_repo
):
    repo_id, _ = _indexed_py_app(client, mock_github, fixture_repo)
    down = ProviderError("Claude timed out or is unreachable.")
    mock_llm(FakeLLM(completions=[down]))
    r = _ask(client, repo_id, TAX_QUESTION)
    assert _error(r) == (502, "provider_error")
    assert r.json()["detail"] == "Claude timed out or is unreachable."
    assert _rows("SELECT answer, not_found FROM queries") == [(None, False)]
    # a failed turn is not history: the next turn in a new conversation sees none
    assert _ask(client, repo_id, TAX_QUESTION).status_code == 200


# ── Web page ──────────────────────────────────────────────────────────────────

PAGE = "<div id=root></div>"


@pytest.fixture
def built_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for the `make web` output, served in place of app/static."""
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(PAGE)
    (static / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("outside the build")
    monkeypatch.setattr("app.main.web.all_directories", [static])
    return static


def test_the_page_is_served_at_the_root_and_for_any_unknown_path(client, built_page):
    for path in ("/", "/?repo=1", "/some/deep/link"):
        r = client.get(path)
        assert (r.status_code, r.text) == (200, PAGE)
        assert r.headers["content-type"].startswith("text/html")
    assert client.get("/assets/app.js").text == "console.log(1)"


def test_api_routes_and_the_docs_match_before_the_page(client, committed, built_page):
    assert client.get("/health").headers["content-type"] == "application/json"
    assert _error(client.get("/repos/999999")) == (404, "repo_not_found")
    assert _error(client.get(f"/index/{uuid.uuid4()}")) == (404, "job_not_found")
    assert "paths" in client.get("/openapi.json").json()


def test_a_path_escaping_the_build_gets_the_page_and_never_the_file(client, built_page):
    assert client.get("/%2E%2E/secret.txt").text == PAGE


def test_without_a_build_the_root_is_404_web_not_built(client, tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.web.all_directories", [tmp_path / "missing"])
    r = client.get("/")
    assert _error(r) == (404, "web_not_built")
    assert "make web" in r.json()["detail"]
