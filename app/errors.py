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


class RepoNotIndexedError(ServiceError):
    """Raised when a repository has no active snapshot to answer from yet."""

    code = "repo_not_indexed"
    status_code = 409
    default_message = "Repository has no indexed snapshot yet."


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
    """Raised when the git clone of a repository fails or times out."""

    code = "clone_failed"
    status_code = 502
    default_message = "Cloning the repository failed."


class GitHubRepoNotFoundError(ServiceError):
    """Raised when GitHub reports the repository missing, or it is private."""

    code = "github_repo_not_found"
    status_code = 404
    default_message = "Repository not found on GitHub, or it is private."


class GitHubRateLimitedError(ServiceError):
    """Raised when the GitHub API refuses a call with 403 or 429 (rate limit)."""

    code = "github_rate_limited"
    status_code = 429
    default_message = "GitHub API rate limit reached; set GITHUB_TOKEN or try again later."


class GitHubUnavailableError(ServiceError):
    """Raised when the GitHub API times out, is unreachable, or answers with a server error."""

    code = "github_unavailable"
    status_code = 502
    default_message = "GitHub is unavailable; try again later."


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


class IndexTimeoutError(ServiceError):
    """Raised inside an index job that runs past `job_timeout_s`; recorded on the job."""

    code = "index_timeout"
    status_code = 504
    default_message = "The index job timed out."


class EmbedBudgetExceededError(ServiceError):
    """Raised inside an index job whose embedding tokens exceed the per-job cap; on the job."""

    code = "embed_budget_exceeded"
    status_code = 413
    default_message = "The repository needs more embedding tokens than one job may spend."


class ProviderError(ServiceError):
    """Raised when an embedding or LLM provider call fails."""

    code = "provider_error"
    status_code = 502
    default_message = "An upstream model provider failed."


class WebNotBuiltError(ServiceError):
    """Raised when the page is requested but app/static holds no build."""

    code = "web_not_built"
    status_code = 404
    default_message = "The web UI is not built; run `make web`."


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
