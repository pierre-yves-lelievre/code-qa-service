from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    models_dir: Path = Path("./models")
    log_level: str = "INFO"
    app_version: str = "0.1.0"


settings = Settings()
