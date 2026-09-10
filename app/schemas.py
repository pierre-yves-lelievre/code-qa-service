"""Pydantic request and response models for the HTTP boundary."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints


class ErrorResponse(BaseModel):
    """Every error response: a human-readable detail and a stable machine code."""

    detail: str = Field(..., json_schema_extra={"example": "Job not found."})
    code: str = Field(..., json_schema_extra={"example": "job_not_found"})


class DatabaseHealth(BaseModel):
    """Database reachability and the pgvector extension's available/installed versions."""

    reachable: bool = Field(..., json_schema_extra={"example": True})
    vector_available: str | None = Field(None, json_schema_extra={"example": "0.8.0"})
    vector_installed: str | None = Field(None, json_schema_extra={"example": None})


class KeysHealth(BaseModel):
    """Whether each credential is configured; presence only, never checked remotely."""

    voyage: bool = Field(..., json_schema_extra={"example": False})
    anthropic: bool = Field(..., json_schema_extra={"example": False})
    github: bool = Field(..., json_schema_extra={"example": False})


class HealthResponse(BaseModel):
    """Service health: status, version, uptime, providers, database, and keys."""

    status: Literal["ok", "degraded"] = Field(..., json_schema_extra={"example": "ok"})
    version: str = Field(..., json_schema_extra={"example": "0.1.0"})
    uptime_seconds: float = Field(..., json_schema_extra={"example": 42.0})
    providers: Literal["fake", "real"] = Field(..., json_schema_extra={"example": "fake"})
    database: DatabaseHealth
    keys: KeysHealth


class RepoHitResponse(BaseModel):
    """One repository search result."""

    full_name: str = Field(..., json_schema_extra={"example": "fastapi/fastapi"})
    owner: str = Field(..., json_schema_extra={"example": "fastapi"})
    name: str = Field(..., json_schema_extra={"example": "fastapi"})
    description: str | None = Field(None, json_schema_extra={"example": "FastAPI framework"})
    stars: int = Field(..., json_schema_extra={"example": 90000})
    language: str | None = Field(None, json_schema_extra={"example": "Python"})
    size_kb: int = Field(..., json_schema_extra={"example": 28000})
    default_branch: str = Field(..., json_schema_extra={"example": "master"})
    url: str = Field(..., json_schema_extra={"example": "https://github.com/fastapi/fastapi"})
    clone_url: str = Field(
        ..., json_schema_extra={"example": "https://github.com/fastapi/fastapi.git"}
    )


class RepoSearchResponse(BaseModel):
    """At most five repositories matching a search query."""

    items: list[RepoHitResponse]


class SnapshotInfo(BaseModel):
    """A repository's active snapshot: commit, branch, when it was indexed, and its stats."""

    snapshot_id: int = Field(..., json_schema_extra={"example": 3})
    commit_sha: str | None = Field(
        None, json_schema_extra={"example": "3f1c2a9e8d7b6c5a4f3e2d1c0b9a8f7e6d5c4b3a"}
    )
    branch: str = Field(..., json_schema_extra={"example": "master"})
    indexed_at: datetime | None = None
    stats: dict[str, Any] = Field(
        ..., json_schema_extra={"example": {"files": 212, "chunks": 1480, "seconds": 41.2}}
    )


class RepoResponse(BaseModel):
    """An indexed repository: summary, suggested questions, and its active snapshot."""

    repo_id: int = Field(..., json_schema_extra={"example": 1})
    owner: str = Field(..., json_schema_extra={"example": "fastapi"})
    name: str = Field(..., json_schema_extra={"example": "full-stack-fastapi-template"})
    url: str = Field(
        ..., json_schema_extra={"example": "https://github.com/fastapi/full-stack-fastapi-template"}
    )
    summary: str | None = Field(
        None, json_schema_extra={"example": "A full-stack template: FastAPI backend, React UI."}
    )
    suggested_questions: list[str] | None = Field(
        None, json_schema_extra={"example": ["Where is the login endpoint defined?"]}
    )
    snapshot: SnapshotInfo | None = None


class AskRequest(BaseModel):
    """A question about an indexed repository, optionally continuing a conversation."""

    repo_id: int = Field(..., json_schema_extra={"example": 1})
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ] = Field(..., json_schema_extra={"example": "Where is the login endpoint defined?"})
    conversation_id: UUID | None = Field(
        None,
        description="Omit to start a new conversation; send the returned id to continue it.",
        json_schema_extra={"example": None},
    )


class SourceResponse(BaseModel):
    """A cited source; the link is built by the server from path, lines and commit sha."""

    path: str = Field(..., json_schema_extra={"example": "backend/app/api/routes/login.py"})
    start_line: int = Field(..., json_schema_extra={"example": 21})
    end_line: int = Field(..., json_schema_extra={"example": 38})
    kind: str = Field(..., json_schema_extra={"example": "function"})
    qualname: str | None = Field(
        None, json_schema_extra={"example": "app.api.routes.login.login_access_token"}
    )
    tier: Literal["symbol", "fts", "vector"] | None = Field(
        None, json_schema_extra={"example": "symbol"}
    )
    excerpt: str = Field(..., json_schema_extra={"example": "def login_access_token(...):"})
    github_url: str = Field(
        ...,
        json_schema_extra={
            "example": "https://github.com/fastapi/full-stack-fastapi-template/blob/"
            "3f1c2a9e8d7b6c5a4f3e2d1c0b9a8f7e6d5c4b3a/backend/app/api/routes/login.py#L21-L38"
        },
    )


class LegTrace(BaseModel):
    """One retrieval leg: its status, hit count and time."""

    status: Literal["ok", "or_fallback", "empty", "skipped", "unavailable"] = Field(
        ..., json_schema_extra={"example": "ok"}
    )
    hits: int = Field(..., json_schema_extra={"example": 12})
    ms: int = Field(..., json_schema_extra={"example": 4})


class Retrievers(BaseModel):
    """What each retrieval leg and the planner did."""

    symbol: LegTrace
    fts: LegTrace
    vector: LegTrace
    planner: Literal["ok", "fallback"] = Field(..., json_schema_extra={"example": "ok"})


class Timings(BaseModel):
    """Milliseconds per stage; embed is inside retrieve."""

    plan_ms: int = Field(..., json_schema_extra={"example": 900})
    embed_ms: int = Field(..., json_schema_extra={"example": 120})
    retrieve_ms: int = Field(..., json_schema_extra={"example": 180})
    llm_ms: int = Field(..., json_schema_extra={"example": 6200})
    total_ms: int = Field(..., json_schema_extra={"example": 7350})


class Tokens(BaseModel):
    """Claude tokens for the planner and the answer together."""

    input: int = Field(..., json_schema_extra={"example": 2400})
    output: int = Field(..., json_schema_extra={"example": 310})
    cache_read: int = Field(..., json_schema_extra={"example": 9800})
    cache_write: int = Field(..., json_schema_extra={"example": 0})


class SnapshotRef(BaseModel):
    """The snapshot the answer was retrieved from."""

    commit_sha: str = Field(
        ..., json_schema_extra={"example": "3f1c2a9e8d7b6c5a4f3e2d1c0b9a8f7e6d5c4b3a"}
    )
    indexed_at: datetime | None = None


class AskResponse(BaseModel):
    """An answer with its cited sources, retrieval trace, timings, tokens and snapshot."""

    conversation_id: UUID
    answer: str = Field(
        ..., json_schema_extra={"example": "`login_access_token` in `login.py` issues the token."}
    )
    not_found: bool = Field(..., json_schema_extra={"example": False})
    sources: list[SourceResponse]
    retrievers: Retrievers
    timings: Timings
    tokens: Tokens
    snapshot: SnapshotRef
    notes: list[str] = Field(..., json_schema_extra={"example": []})


class IndexRequest(BaseModel):
    """A public GitHub repository URL, optionally ending in /tree/<branch>."""

    url: str = Field(
        ...,
        min_length=1,
        max_length=512,
        json_schema_extra={"example": "https://github.com/fastapi/full-stack-fastapi-template"},
    )


class IndexAccepted(BaseModel):
    """An accepted index request: poll GET /index/{job_id} for progress."""

    job_id: str = Field(..., json_schema_extra={"example": "4f9c2a1e-8d7b-4c3a-9e2f-1a2b3c4d5e6f"})
    repo_id: int = Field(..., json_schema_extra={"example": 1})
    owner: str = Field(..., json_schema_extra={"example": "fastapi"})
    name: str = Field(..., json_schema_extra={"example": "full-stack-fastapi-template"})
    branch: str = Field(..., json_schema_extra={"example": "master"})
    status: Literal["pending"] = Field(..., json_schema_extra={"example": "pending"})


class JobResponse(BaseModel):
    """An index job: status, stage progress, and the error when it failed."""

    job_id: str = Field(..., json_schema_extra={"example": "4f9c2a1e-8d7b-4c3a-9e2f-1a2b3c4d5e6f"})
    repo_id: int = Field(..., json_schema_extra={"example": 1})
    status: Literal["pending", "running", "succeeded", "failed"] = Field(
        ..., json_schema_extra={"example": "running"}
    )
    progress: dict[str, Any] = Field(
        ...,
        json_schema_extra={
            "example": {"stage": "parsing", "files_done": 50, "files_total": 212, "chunks": 640}
        },
    )
    error: str | None = Field(None, json_schema_extra={"example": None})
    snapshot_id: int | None = Field(None, json_schema_extra={"example": 3})
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
