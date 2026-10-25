from functools import lru_cache
from pathlib import Path

from pydantic import Field, HttpUrl, SecretStr

from shared.settings import PROJECT_ROOT, LoggingSettings


class Settings(LoggingSettings):
    api_title: str = "Credicorp Agent API"
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-4.1-mini"
    agent_timeout_seconds: float = Field(default=60, gt=0)
    sessions_dir: Path = Path("shared/sessions")
    docs_config_path: Path = Path("config.yaml")
    retrieval_api_base_url: HttpUrl = HttpUrl("http://127.0.0.1:8000")
    retrieval_tool_timeout_seconds: float = Field(default=30, gt=0)
    retrieval_tool_max_tokens: int = Field(default=8000, ge=256, le=32000)

    @property
    def resolved_sessions_dir(self) -> Path:
        return PROJECT_ROOT / self.sessions_dir


@lru_cache
def get_settings() -> Settings:
    return Settings()
