"""Run with: python -m uvicorn backend.app.main:app --reload."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.app.agent.runtime import AgentRuntime
from backend.app.api.router import api_router
from backend.app.core.config import Settings, get_settings
from backend.app.documents.service import DocumentService
from backend.app.middleware.errors import register_error_handlers
from backend.app.middleware.requests import register_request_logging
from backend.app.repositories.conversations import ConversationRepository
from backend.app.services.agent import AgentService
from backend.app.tools.alpaca import alpaca_connection
from shared.logging_config import configure_logging


def create_app(
    settings: Settings | None = None,
    runtime: AgentRuntime | None = None,
    document_service: DocumentService | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    logger = configure_logging("backend", settings)
    agent_runtime = runtime or AgentRuntime(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Keep one persistent Chroma client for the full server lifetime.
        app.state.document_service = document_service or await asyncio.to_thread(
            DocumentService, settings.docs_config_path, settings.openai_api_key.get_secret_value()
        )
        try:
            async with alpaca_connection(settings) as alpaca:
                agent_runtime.alpaca = alpaca
                logger.info("application_started")
                yield
        finally:
            agent_runtime.alpaca = None
            await asyncio.to_thread(app.state.document_service.close)
            logger.info("application_stopped")

    application = FastAPI(title=settings.api_title, version="0.3.0", lifespan=lifespan)
    repository = ConversationRepository(settings.resolved_sessions_dir)
    application.state.conversations = repository
    application.state.agent_service = AgentService(repository, agent_runtime)
    register_request_logging(application)
    register_error_handlers(application)
    application.include_router(api_router)
    return application


app = create_app()
