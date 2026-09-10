"""Pydantic request and response models for the HTTP boundary."""

from typing import Literal

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """Every error response: a human-readable detail and a stable machine code."""

    detail: str = Field(..., json_schema_extra={"example": "Job not found."})
    code: str = Field(..., json_schema_extra={"example": "job_not_found"})


class HealthResponse(BaseModel):
    """Service health: status, version, and uptime."""

    status: Literal["ok", "degraded"] = Field(..., json_schema_extra={"example": "ok"})
    version: str = Field(..., json_schema_extra={"example": "0.1.0"})
    uptime_seconds: float = Field(..., json_schema_extra={"example": 42.0})
