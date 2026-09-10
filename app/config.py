"""Shared with surrogate-model-service; adapted: code-qa settings and the providers switch."""

from pathlib import Path
from typing import Literal, Self

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service configuration, read from the environment and `.env`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Service ───────────────────────────────────────────────────────────────
    app_version: str = "0.1.0"
    log_level: str = "INFO"
    data_dir: Path = Path("./data")

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str

    # ── Providers ─────────────────────────────────────────────────────────────
    providers: Literal["fake", "real"] = "fake"
    voyage_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    github_token: SecretStr | None = None
    embedding_model: str = "voyage-code-4"
    embedding_dims: int = 1024
    embed_usd_per_mtok: float = 0.12  # voyage-code-4 list price on 2026-09-10
    llm_model: str = "claude-sonnet-5"
    # 2026-09-10, Sonnet 5 list price; verify on the pricing page before quoting in the README.
    llm_usd_per_mtok_input: float = 2.00
    llm_usd_per_mtok_output: float = 10.00
    llm_usd_per_mtok_cache_read: float = 0.20
    llm_usd_per_mtok_cache_write: float = 2.50

    # ── Limits ────────────────────────────────────────────────────────────────
    max_repo_mb: int = 200
    max_file_kb: int = 512
    max_files: int = 20_000
    max_running_jobs: int = 2
    max_embed_tokens_per_job: int = 5_000_000

    # ── Timeouts (seconds) ────────────────────────────────────────────────────
    github_timeout_s: float = 10.0
    clone_timeout_s: float = 120.0
    job_timeout_s: float = 1800.0
    embed_timeout_s: float = 30.0
    planner_timeout_s: float = 3.0
    summary_timeout_s: float = 30.0
    answer_timeout_s: float = 60.0

    # ── Retrieval ─────────────────────────────────────────────────────────────
    relevance_floor: float = 0.35

    @field_validator("voyage_api_key", "anthropic_api_key", "github_token", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """Treat an empty `KEY=` line in `.env` as unset."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _real_providers_need_keys(self) -> Self:
        """Refuse `providers=real` unless both API keys are set."""
        if self.providers == "real":
            keys = {
                "VOYAGE_API_KEY": self.voyage_api_key,
                "ANTHROPIC_API_KEY": self.anthropic_api_key,
            }
            missing = [name for name, value in keys.items() if value is None]
            if missing:
                raise ValueError(
                    f"PROVIDERS=real requires {', '.join(missing)}; "
                    "set them in .env or use PROVIDERS=fake."
                )
        return self


settings = Settings()
