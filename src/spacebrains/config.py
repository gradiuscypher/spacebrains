"""Process-level configuration (secrets and paths) loaded from the environment / .env.

Runtime-tweakable settings (models, intervals, budgets) live in the database instead so the
web UI can change them without a restart; see `spacebrains.settings`.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    spacetraders_api_key: str
    typesafe_api_key: str
    openrouter_api_key: str

    spacetraders_base_url: str = "https://api.spacetraders.io/v2"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    host: str = "0.0.0.0"
    port: int = 8080
    data_dir: Path = Path("data")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "spacebrains.sqlite3"


def load_config() -> Config:
    cfg = Config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
