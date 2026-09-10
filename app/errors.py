"""Shared with surrogate-model-service; adapted: code-qa error types and a catch-all 500."""

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.logging_setup import get_logger

log = get_logger(__name__)


# ── Errors ────────────────────────────────────────────────────────────────────


class ServiceError(Exception):
    """Base class for all application errors; maps to a JSON error response."""

    code: str = "service_error"
    status_code: int = 500
    default_message: str = "An unexpected error occurred."

    def __init__(self, message: str | None = None):
        self.message = message or self.default_message
        super().__init__(self.message)


class RepoNotFoundError(ServiceError):
    """Raised when a requested repository has not been indexed."""

    code = "repo_not_found"
    status_code = 404
    default_message = "Repository not found."


class JobNotFoundError(ServiceError):
    """Raised when a requested job ID does not exist in the job store."""

    code = "job_not_found"
    status_code = 404
    default_message = "Job not found."


class RepoTooLargeError(ServiceError):
    """Raised when a repository exceeds the configured size or file-count limits."""

    code = "repo_too_large"
    status_code = 413
    default_message = "Repository exceeds the size limit."


class CloneFailedError(ServiceError):
    """Raised when cloning the repository from GitHub fails."""

    code = "clone_failed"
    status_code = 502
    default_message = "Cloning the repository failed."


class InvalidRepoUrlError(ServiceError):
    """Raised when a repository URL or branch fails validation."""

    code = "invalid_repo_url"
    status_code = 422
    default_message = "Invalid GitHub repository URL."


class TooManyJobsError(ServiceError):
    """Raised when the global limit on active index jobs is reached."""

    code = "too_many_jobs"
    status_code = 429
    default_message = "Too many indexing jobs are running; try again later."


class IndexInProgressError(ServiceError):
    """Raised when the repository already has a pending or running index job."""

    code = "index_in_progress"
    status_code = 409
    default_message = "An index job for this repository is already pending or running."


class ProviderError(ServiceError):
    """Raised when an embedding or LLM provider call fails."""

    code = "provider_error"
    status_code = 502
    default_message = "An upstream model provider failed."


# ── Handlers ──────────────────────────────────────────────────────────────────


async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    """Translate any ServiceError subclass into a {"detail", "code"} JSON response."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.message, "code": exc.code},
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Translate Pydantic validation errors into the standard {"detail", "code"} shape."""
    errors = exc.errors()
    detail = errors[0]["msg"] if errors else "Validation error."
    return JSONResponse(
        status_code=422,
        content={"detail": detail, "code": "validation_error"},
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log an unexpected exception by type only and return a generic 500."""
    log.error("request_failed", error_type=type(exc).__name__, path=request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal error.", "code": "internal_error"},
    )
