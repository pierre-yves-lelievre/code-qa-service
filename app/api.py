"""All HTTP routes."""

from datetime import UTC, datetime

from fastapi import APIRouter, Request

from app.config import settings
from app.schemas import ErrorResponse, HealthResponse

router = APIRouter()


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health check",
    description="Liveness: returns the service status, version, and uptime.",
    responses={
        200: {
            "content": {
                "application/json": {
                    "example": {"status": "ok", "version": "0.1.0", "uptime_seconds": 42.0}
                }
            }
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
def health(request: Request) -> HealthResponse:
    """Report service liveness, version, and uptime."""
    uptime = (datetime.now(UTC) - request.app.state.started_at).total_seconds()
    return HealthResponse(status="ok", version=settings.app_version, uptime_seconds=uptime)
