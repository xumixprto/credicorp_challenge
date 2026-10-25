from functools import lru_cache
from uuid import UUID

from pydantic import HttpUrl, PositiveFloat

from shared.settings import LoggingSettings


class Settings(LoggingSettings):
    api_base_url: HttpUrl = HttpUrl("http://127.0.0.1:8000")
    api_timeout_seconds: PositiveFloat = 90
    document_api_timeout_seconds: PositiveFloat = 600
    judge_api_timeout_seconds: PositiveFloat = 600
    chat_user_id: UUID = UUID("d5bbf9d1-5945-44d8-bb10-0065164a8356")


@lru_cache
def get_settings() -> Settings:
    return Settings()
