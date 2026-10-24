"""Run with: python -m uvicorn backend.app.main:app --reload."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.app.agent.runtime import AgentRuntime
from backend.app.api.router import api_router
from backend.app.core.config import Settings, get_settings
from backend.app.middleware.errors import register_error_handlers
from backend.app.middleware.requests import register_request_logging
from backend.app.repositories.conversations import ConversationRepository
from backend.app.services.agent import AgentService
from shared.logging_config import configure_logging


def create_app(settings: Settings | None = None, runtime: AgentRuntime | None = None) -> FastAPI:
    settings = settings or get_settings()
    logger = configure_logging("backend", settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("application_started")
        yield
        logger.info("application_stopped")

    application = FastAPI(title=settings.api_title, version="0.2.0", lifespan=lifespan)
    repository = ConversationRepository(settings.resolved_sessions_dir)
    application.state.conversations = repository
    application.state.agent_service = AgentService(repository, runtime or AgentRuntime(settings))
    register_request_logging(application)
    register_error_handlers(application)
    application.include_router(api_router)
    return application


app = create_app()
