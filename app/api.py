"""All HTTP routes."""

from dataclasses import asdict
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import check_database
from app.schemas import DatabaseHealth, ErrorResponse, HealthResponse, KeysHealth

router = APIRouter()

_HEALTHY_EXAMPLE = {
    "status": "ok",
    "version": "0.1.0",
    "uptime_seconds": 42.0,
    "providers": "fake",
    "database": {"reachable": True, "vector_available": "0.8.0", "vector_installed": None},
    "keys": {"voyage": False, "anthropic": False, "github": False},
}
_DEGRADED_EXAMPLE = {
    **_HEALTHY_EXAMPLE,
    "status": "degraded",
    "database": {"reachable": False, "vector_available": None, "vector_installed": None},
}


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health check",
    description=(
        "Reports database reachability, the pgvector extension's available and installed "
        "versions, the provider mode, and which API keys are configured (presence only; no "
        "provider is called). HTTP 200 when the database is reachable and pgvector is "
        "available, 503 otherwise."
    ),
    responses={
        200: {"content": {"application/json": {"example": _HEALTHY_EXAMPLE}}},
        503: {
            "model": HealthResponse,
            "content": {"application/json": {"example": _DEGRADED_EXAMPLE}},
        },
        500: {
            "model": ErrorResponse,
            "content": {
                "application/json": {
                    "example": {"detail": "Internal error.", "code": "internal_error"}
                }
            },
        },
    },
)
def health(request: Request) -> JSONResponse:
    """Report service status, database and pgvector state, and configured keys."""
    db = check_database()
    healthy = db.reachable and db.vector_available is not None
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
