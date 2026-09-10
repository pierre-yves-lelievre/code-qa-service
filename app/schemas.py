"""Pydantic request and response models for the HTTP boundary."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


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
