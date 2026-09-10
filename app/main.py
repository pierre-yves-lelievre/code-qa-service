"""Shared with surrogate-model-service; adapted: data_dir, catch-all 500, migrations and pool."""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import Scope

from app.api import router
from app.config import settings
from app.db import check_database, close_pool, get_pool, run_migrations
from app.embeddings import FakeEmbeddings, VoyageEmbeddings
from app.errors import (
    ServiceError,
    WebNotBuiltError,
    service_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from app.github import GitHubClient
from app.jobs import fail_interrupted
from app.llm import ClaudeLLM, FakeLLM
from app.logging_setup import configure_logging, get_logger

DATABASE_UNREACHABLE = "Database unreachable at DATABASE_URL; start it with `make db`."


def build_clients() -> tuple[GitHubClient, VoyageEmbeddings | FakeEmbeddings, ClaudeLLM | FakeLLM]:
    """The GitHub, embeddings and LLM clients for the configured providers."""
    token = settings.github_token.get_secret_value() if settings.github_token else None
    github = GitHubClient(token)
    embeddings: VoyageEmbeddings | FakeEmbeddings
    if settings.providers == "real" and settings.voyage_api_key is not None:
        embeddings = VoyageEmbeddings(
            settings.voyage_api_key.get_secret_value(),
            settings.embedding_model,
            settings.embedding_dims,
        )
    else:
        embeddings = FakeEmbeddings(settings.embedding_dims)
    llm: ClaudeLLM | FakeLLM
    if settings.providers == "real" and settings.anthropic_api_key is not None:
        llm = ClaudeLLM(settings.anthropic_api_key.get_secret_value(), settings.llm_model)
    else:
        llm = FakeLLM()
    return github, embeddings, llm


STATIC_DIR = Path(__file__).parent / "static"


class SpaStaticFiles(StaticFiles):
    """The page built by `make web`: an unknown path gets index.html, a missing build a 404."""

    async def check_config(self) -> None:
        """Skip the startup directory check; a missing build is reported per request instead."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        """The file at `path`, else index.html so the page handles its own URL."""
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        try:
            return await super().get_response("index.html", scope)
        except HTTPException as exc:
            if exc.status_code == 404:
                raise WebNotBuiltError() from None
            raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Check the database, migrate, open the pool, fail interrupted jobs, build clients."""
    configure_logging(settings.log_level)
    log = get_logger(__name__)
    app.state.started_at = datetime.now(UTC)
    if not check_database().reachable:
        raise RuntimeError(DATABASE_UNREACHABLE)
    run_migrations()
    get_pool()
    fail_interrupted()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app.state.github, app.state.embeddings, app.state.llm = build_clients()
    log.info(
        "service_started",
        version=settings.app_version,
        providers=settings.providers,
        data_dir=str(settings.data_dir),
    )
    try:
        yield
    finally:
        app.state.llm.close()
        app.state.embeddings.close()
        app.state.github.close()
        close_pool()
        log.info("service_stopped")


app = FastAPI(title="Code Q&A Service", version=settings.app_version, lifespan=lifespan)

# Set started_at at module load so it is always present even if the lifespan
# has not yet run (e.g. during import in tests before TestClient enters context).
app.state.started_at = datetime.now(UTC)

app.add_exception_handler(ServiceError, service_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(Exception, unhandled_error_handler)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Inject a unique request_id into the structlog context for every HTTP request."""
    req_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=req_id)
    request.state.request_id = req_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = req_id
    return response


app.include_router(router)

# Mounted last, so every API route and /docs match first; same origin, so no CORS.
web = SpaStaticFiles(directory=STATIC_DIR, html=True, check_dir=False)
app.mount("/", web, name="web")


def main():
    """Entry point for the `serve` project script; starts uvicorn with reload enabled."""
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
