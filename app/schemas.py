"""Pydantic request and response models for the HTTP boundary."""

from typing import Literal

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
