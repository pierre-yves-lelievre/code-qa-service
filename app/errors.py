from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ServiceError(Exception):
    """Base class for all application errors; maps to a JSON error response."""

    code: str = "service_error"
    status_code: int = 500
    default_message: str = "An unexpected error occurred."

    def __init__(self, message: str | None = None):
        self.message = message or self.default_message
        super().__init__(self.message)


class ModelNotFoundError(ServiceError):
    """Raised when a requested model ID does not exist in the store."""

    code = "model_not_found"
    status_code = 404
    default_message = "Model not found."


class JobNotFoundError(ServiceError):
    """Raised when a requested job ID does not exist in the job store."""

    code = "job_not_found"
    status_code = 404
    default_message = "Job not found."


class InvalidDatasetError(ServiceError):
    """Raised when the supplied features/targets fail shape validation."""

    code = "invalid_dataset"
    status_code = 422
    default_message = "Invalid dataset."


class TrainingFailedError(ServiceError):
    """Raised when model training fails for an unexpected reason."""

    code = "training_failed"
    status_code = 500
    default_message = "Model training failed."


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
