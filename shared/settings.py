"""Environment configuration shared by the two processes."""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class LoggingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_dir: Path = Path("logs")
    log_max_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    log_backup_count: int = Field(default=5, ge=1)

    @property
    def resolved_log_dir(self) -> Path:
        return PROJECT_ROOT / self.log_dir
