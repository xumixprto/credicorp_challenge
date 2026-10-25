from functools import lru_cache
from pathlib import Path

from pydantic import Field, HttpUrl, SecretStr, field_validator

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
    alpaca_mcp_enabled: bool = True
    alpaca_api_key: SecretStr = SecretStr("")
    alpaca_secret_key: SecretStr = SecretStr("")
    alpaca_paper_trade: bool = True
    alpaca_toolsets: str = "stock-data,assets,news"
    alpaca_mcp_request_timeout_seconds: int = Field(default=30, gt=0)

    @field_validator("alpaca_toolsets")
    @classmethod
    def validate_alpaca_toolsets(cls, value: str) -> str:
        supported = {
            "account",
            "trading",
            "watchlists",
            "assets",
            "stock-data",
            "crypto-data",
            "options-data",
            "corporate-actions",
            "news",
            "fixed-income-data",
            "locates",
        }
        selected = list(dict.fromkeys(part.strip() for part in value.split(",")))
        if not selected or any(part not in supported for part in selected):
            raise ValueError("ALPACA_TOOLSETS must contain known, nonempty toolset names.")
        return ",".join(selected)

    @property
    def resolved_sessions_dir(self) -> Path:
        return PROJECT_ROOT / self.sessions_dir


@lru_cache
def get_settings() -> Settings:
    return Settings()
