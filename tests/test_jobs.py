"""JobStore tests against the compose Postgres, one rolled-back transaction per test."""

import uuid
from datetime import UTC, datetime

import pytest

from app.errors import IndexInProgressError, TooManyJobsError

# ── Lifecycle ─────────────────────────────────────────────────────────────────


def test_job_moves_pending_to_running_to_succeeded_with_progress(jobs, make_repo):
    repo_id = make_repo()
    job = jobs.create(repo_id)
    assert uuid.UUID(job.id)
    assert (job.repo_id, job.status, job.progress, job.started_at) == (repo_id, "pending", {}, None)

    started = datetime.now(UTC)
    jobs.update(
        job.id, status="running", started_at=started, progress={"stage": "parse", "files": 50}
    )
    running = jobs.get(job.id)
    assert running.status == "running"
    assert running.started_at == started
    assert running.progress == {"stage": "parse", "files": 50}

    jobs.update(job.id, status="succeeded", completed_at=datetime.now(UTC))
    done = jobs.get(job.id)
    assert done.status == "succeeded"
    assert done.completed_at >= done.started_at
    assert jobs.count_active() == 0


def test_get_returns_none_for_unknown_or_malformed_id(jobs):
    assert jobs.get(str(uuid.uuid4())) is None
    assert jobs.get("not-a-uuid") is None


def test_update_rejects_unknown_fields_and_ignores_unknown_ids(jobs, make_repo):
    job = jobs.create(make_repo())
    with pytest.raises(ValueError, match="repo_id"):
        jobs.update(job.id, repo_id=999)
    jobs.update(str(uuid.uuid4()), status="failed")
    jobs.update("not-a-uuid", status="failed")
    assert jobs.get(job.id).status == "pending"


# ── Limits ────────────────────────────────────────────────────────────────────


def test_second_active_job_for_same_repo_raises_index_in_progress(jobs, make_repo):
    repo_id = make_repo()
    first = jobs.create(repo_id)
    with pytest.raises(IndexInProgressError):
        jobs.create(repo_id)
    jobs.update(first.id, status="running")
    with pytest.raises(IndexInProgressError):
        jobs.create(repo_id)
    assert jobs.count_active() == 1


def test_third_active_job_overall_raises_too_many_jobs(
    jobs, make_repo, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("app.config.settings.max_running_jobs", 2)
    jobs.create(make_repo())
    second = jobs.create(make_repo())
    jobs.update(second.id, status="running")
    with pytest.raises(TooManyJobsError):
        jobs.create(make_repo())
    assert jobs.count_active() == 2


def test_finished_job_frees_its_slot(jobs, make_repo, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.settings.max_running_jobs", 1)
    repo_id = make_repo()
    first = jobs.create(repo_id)
    jobs.update(first.id, status="failed", error="clone timed out", completed_at=datetime.now(UTC))

    second = jobs.create(repo_id)
    assert second.id != first.id
    assert jobs.get(first.id).error == "clone timed out"
    assert jobs.count_active() == 1
