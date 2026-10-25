from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr

from shared.settings import PROJECT_ROOT, LoggingSettings


class Settings(LoggingSettings):
    api_title: str = "Credicorp Agent API"
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-4.1-mini"
    agent_timeout_seconds: float = Field(default=60, gt=0)
    sessions_dir: Path = Path("shared/sessions")
    docs_config_path: Path = Path("config.yaml")

    @property
    def resolved_sessions_dir(self) -> Path:
        return PROJECT_ROOT / self.sessions_dir


@lru_cache
def get_settings() -> Settings:
    return Settings()
