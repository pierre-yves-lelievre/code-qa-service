"""All HTTP routes."""

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import JSONResponse

from app.answering import ask
from app.config import settings
from app.db import check_database
from app.embeddings import FakeEmbeddings, VoyageEmbeddings
from app.errors import (
    GitHubRateLimitedError,
    GitHubRepoNotFoundError,
    GitHubUnavailableError,
    IndexInProgressError,
    InvalidRepoUrlError,
    JobNotFoundError,
    ProviderError,
    RepoNotFoundError,
    RepoNotIndexedError,
    RepoTooLargeError,
    ServiceError,
    TooManyJobsError,
)
from app.github import GitHubClient, RepoRef, parse_repo_url
from app.indexing import _run_index
from app.jobs import JobStore
from app.llm import ClaudeLLM, FakeLLM
from app.logging_setup import get_logger
from app.schemas import (
    AskRequest,
    AskResponse,
    DatabaseHealth,
    ErrorResponse,
    HealthResponse,
    IndexAccepted,
    IndexRequest,
    JobResponse,
    KeysHealth,
    RepoHitResponse,
    RepoResponse,
    RepoSearchResponse,
    Retrievers,
    SnapshotInfo,
    SnapshotRef,
    SourceResponse,
    Timings,
    Tokens,
)
from app.store import ChunkStore

log = get_logger(__name__)

router = APIRouter()

_HEALTHY_EXAMPLE = {
    "status": "ok",
    "version": "0.1.0",
    "uptime_seconds": 42.0,
    "providers": "fake",
    "database": {"reachable": True, "vector_available": "0.8.0", "vector_installed": "0.8.0"},
    "keys": {"voyage": False, "anthropic": False, "github": False},
}
_DEGRADED_EXAMPLE = {
    **_HEALTHY_EXAMPLE,
    "status": "degraded",
    "database": {"reachable": False, "vector_available": None, "vector_installed": None},
}
_INTERNAL_ERROR = {"detail": "Internal error.", "code": "internal_error"}
_VALIDATION_ERROR = {"detail": "Field required", "code": "validation_error"}


def _errors(*errors: type[ServiceError]) -> dict[int | str, dict[str, Any]]:
    """OpenAPI `responses` for error types grouped by status, plus validation and internal."""
    examples: dict[int, dict[str, Any]] = {422: {"validation_error": {"value": _VALIDATION_ERROR}}}
    for error in errors:
        body = {"detail": error.default_message, "code": error.code}
        examples.setdefault(error.status_code, {})[error.code] = {"value": body}
    examples[500] = {"internal_error": {"value": _INTERNAL_ERROR}}
    return {
        status: {"model": ErrorResponse, "content": {"application/json": {"examples": named}}}
        for status, named in sorted(examples.items())
    }


# ── Dependencies ──────────────────────────────────────────────────────────────


def get_github(request: Request) -> GitHubClient:
    """The app's GitHubClient, built in the lifespan; tests override it."""
    return request.app.state.github


def get_embeddings(request: Request) -> VoyageEmbeddings | FakeEmbeddings:
    """The app's embeddings client, built in the lifespan from PROVIDERS; tests override it."""
    return request.app.state.embeddings


def get_llm(request: Request) -> ClaudeLLM | FakeLLM:
    """The app's LLM client, built in the lifespan from PROVIDERS; tests override it."""
    return request.app.state.llm


def get_jobs() -> JobStore:
    """A JobStore over the pool."""
    return JobStore()


def get_store() -> ChunkStore:
    """A ChunkStore over the pool."""
    return ChunkStore()


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health check",
    description=(
        "Reports database reachability, the pgvector extension's available and installed "
        "versions, the provider mode, and which API keys are configured (presence only; no "
        "provider is called). HTTP 200 when the database is reachable and pgvector is "
        "installed, 503 otherwise."
    ),
    responses={
        200: {"content": {"application/json": {"example": _HEALTHY_EXAMPLE}}},
        503: {
            "model": HealthResponse,
            "content": {"application/json": {"example": _DEGRADED_EXAMPLE}},
        },
        500: {
            "model": ErrorResponse,
            "content": {"application/json": {"example": _INTERNAL_ERROR}},
        },
    },
)
def health(request: Request) -> JSONResponse:
    """Report service status, database and pgvector state, and configured keys."""
    db = check_database()
    healthy = db.reachable and db.vector_installed is not None
    body = HealthResponse(
        status="ok" if healthy else "degraded",
        version=settings.app_version,
        uptime_seconds=(datetime.now(UTC) - request.app.state.started_at).total_seconds(),
        providers=settings.providers,
        database=DatabaseHealth(**asdict(db)),
        keys=KeysHealth(
            voyage=settings.voyage_api_key is not None,
            anthropic=settings.anthropic_api_key is not None,
            github=settings.github_token is not None,
        ),
    )
    return JSONResponse(status_code=200 if healthy else 503, content=body.model_dump())


@router.get(
    "/repos/search",
    response_model=RepoSearchResponse,
    summary="Search GitHub repositories",
    description=(
        "Proxies GitHub repository search and returns the top five results with size, "
        "language, default branch and clone URL. Unauthenticated unless GITHUB_TOKEN is set."
    ),
    responses=_errors(GitHubRateLimitedError, GitHubUnavailableError),
)
def search_repos(
    q: Annotated[str, Query(min_length=1, max_length=256, description="GitHub search query.")],
    github: Annotated[GitHubClient, Depends(get_github)],
) -> RepoSearchResponse:
    """Top five GitHub repositories for a query."""
    return RepoSearchResponse(items=[RepoHitResponse(**asdict(hit)) for hit in github.search(q)])


@router.get(
    "/repos/{repo_id}",
    response_model=RepoResponse,
    summary="Indexed repository",
    description=(
        "A repository known to the service: its URL, the summary and suggested questions written "
        "at index time (null until a summary call succeeds), and its active snapshot with commit "
        "sha, branch, indexing time and stats (null before the first successful index)."
    ),
    responses=_errors(RepoNotFoundError),
)
def get_repo(repo_id: int, store: Annotated[ChunkStore, Depends(get_store)]) -> RepoResponse:
    """One repository with its summary and active snapshot, or 404 when the id is unknown."""
    repo = store.repo(repo_id)
    if repo is None:
        raise RepoNotFoundError()
    snapshot = store.active_snapshot(repo_id)
    return RepoResponse(
        repo_id=repo.id,
        owner=repo.owner,
        name=repo.name,
        url=repo.url,
        summary=repo.summary,
        suggested_questions=repo.suggested_questions,
        snapshot=None
        if snapshot is None
        else SnapshotInfo(
            snapshot_id=snapshot.id,
            commit_sha=snapshot.commit_sha,
            branch=snapshot.branch,
            indexed_at=snapshot.indexed_at,
            stats=snapshot.stats,
        ),
    )


@router.post(
    "/ask",
    response_model=AskResponse,
    summary="Ask a question about an indexed repository",
    description=(
        "Plans the search (one structured call), retrieves from the active snapshot over three "
        "legs, and answers with one Claude call over the 12 best sources as citable search "
        "results plus a compact index of the next ones. Citations that do not map to a briefed "
        "source are dropped and noted; links are built server-side from path, lines and commit "
        "sha. `not_found` is set when the relevance floor fires, nothing is retrieved (no call "
        "is made), or the model says so. Send the returned `conversation_id` to continue: the "
        "last four turns are replayed with their sources and cached. Every request is logged."
    ),
    responses=_errors(RepoNotFoundError, RepoNotIndexedError, ProviderError),
)
def ask_question(
    body: AskRequest,
    request: Request,
    store: Annotated[ChunkStore, Depends(get_store)],
    embeddings: Annotated[VoyageEmbeddings | FakeEmbeddings, Depends(get_embeddings)],
    llm: Annotated[ClaudeLLM | FakeLLM, Depends(get_llm)],
) -> AskResponse:
    """Answer one question; a plain def, so the sync SDK and psycopg calls run in the threadpool."""
    conversation_id = str(body.conversation_id) if body.conversation_id else None
    result = ask(
        body.repo_id,
        body.question,
        conversation_id,
        request.state.request_id,
        store,
        embeddings,
        llm,
    )
    return AskResponse(
        conversation_id=result.conversation_id,
        answer=result.answer,
        not_found=result.not_found,
        sources=[
            SourceResponse(**{k: getattr(s, k) for k in SourceResponse.model_fields})
            for s in result.sources
        ],
        retrievers=Retrievers(**result.retrievers),
        timings=Timings(**result.timings),
        tokens=Tokens(**result.tokens),
        snapshot=SnapshotRef(commit_sha=result.commit_sha, indexed_at=result.indexed_at),
        notes=result.notes,
    )


@router.post(
    "/index",
    status_code=202,
    response_model=IndexAccepted,
    summary="Index a public GitHub repository",
    description=(
        "Validates the URL, checks the repository on the GitHub API (exists, public, under "
        "the size limit), then starts a background job that shallow-clones, parses, chunks "
        "and embeds it; only content without a vector for the current model is embedded, "
        "under the per-job token cap. Returns 202 with the job id; poll GET /index/{job_id}. "
        "One active job per repository and a global cap on active jobs are enforced here."
    ),
    responses=_errors(
        InvalidRepoUrlError,
        GitHubRepoNotFoundError,
        IndexInProgressError,
        RepoTooLargeError,
        GitHubRateLimitedError,
        TooManyJobsError,
        GitHubUnavailableError,
    ),
)
def index_repo(
    body: IndexRequest,
    background: BackgroundTasks,
    github: Annotated[GitHubClient, Depends(get_github)],
    jobs: Annotated[JobStore, Depends(get_jobs)],
    store: Annotated[ChunkStore, Depends(get_store)],
    embeddings: Annotated[VoyageEmbeddings | FakeEmbeddings, Depends(get_embeddings)],
    llm: Annotated[ClaudeLLM | FakeLLM, Depends(get_llm)],
) -> IndexAccepted:
    """Pre-check a repository, create its job, and run the index in the background."""
    requested = parse_repo_url(body.url)
    info = github.repo(requested)
    ref = RepoRef(info.owner, info.name, requested.branch or info.default_branch)
    repo_id = store.upsert_repo(
        info.owner, info.name, f"https://github.com/{info.owner}/{info.name}"
    )
    job = jobs.create(repo_id)
    background.add_task(_run_index, job.id, repo_id, ref, jobs, store, github, embeddings, llm)
    log.info("index_requested", job_id=job.id, repo_id=repo_id)
    return IndexAccepted(
        job_id=job.id,
        repo_id=repo_id,
        owner=ref.owner,
        name=ref.name,
        branch=ref.branch or info.default_branch,
        status="pending",
    )


@router.get(
    "/index/{job_id}",
    response_model=JobResponse,
    summary="Index job status",
    description=(
        "Status of an index job: pending, running (with stage and file counts), succeeded, "
        "or failed with its error. A failed clone reads `Cloning the repository failed.`"
    ),
    responses=_errors(JobNotFoundError),
)
def get_index_job(job_id: str, jobs: Annotated[JobStore, Depends(get_jobs)]) -> JobResponse:
    """One index job, or 404 when the id is unknown."""
    job = jobs.get(job_id)
    if job is None:
        raise JobNotFoundError()
    fields = asdict(job)
    return JobResponse(job_id=fields.pop("id"), **fields)
