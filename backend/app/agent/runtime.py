import asyncio
import logging

from agent_framework import Agent, AgentSession, InMemoryHistoryProvider
from agent_framework.openai import OpenAIChatClient
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)

from backend.app.agent.prompts import SYSTEM_INSTRUCTIONS
from backend.app.core.config import Settings
from backend.app.core.errors import ApplicationError
from backend.app.middleware.agent_logging import AgentLoggingMiddleware
from backend.app.middleware.function_logging import FunctionLoggingMiddleware
from backend.app.tools import get_tools

logger = logging.getLogger("credicorp.backend.agent")


def build_agent(settings: Settings, client: AsyncOpenAI) -> Agent:
    return Agent(
        id="credicorp-agent",
        name="CredicorpAgent",
        client=OpenAIChatClient(model=settings.openai_model, async_client=client),
        instructions=SYSTEM_INSTRUCTIONS,
        tools=get_tools(),
        context_providers=[InMemoryHistoryProvider(source_id="conversation-history")],
        middleware=[AgentLoggingMiddleware(), FunctionLoggingMiddleware()],
        default_options={"store": False},
    )


class AgentRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run(self, query: str, session: AgentSession) -> str:
        key = self.settings.openai_api_key.get_secret_value().strip()
        if not key:
            raise ApplicationError(
                503, "Set OPENAI_API_KEY in the server .env, then restart FastAPI."
            )
        try:
            async with asyncio.timeout(self.settings.agent_timeout_seconds):
                async with AsyncOpenAI(
                    api_key=key, timeout=self.settings.agent_timeout_seconds, max_retries=0
                ) as client:
                    result = await build_agent(self.settings, client).run(query, session=session)
                    if not result.text.strip():
                        raise ApplicationError(
                            502, "The agent returned an empty reply. Please retry."
                        )
                    return result.text
        except ApplicationError:
            raise
        except Exception as exc:
            # Framework providers wrap SDK errors. Inspect causes without exposing their text.
            causes: list[BaseException] = []
            current: BaseException | None = exc
            while current is not None and current not in causes:
                causes.append(current)
                current = current.__cause__ or current.__context__
            logger.warning("model_request_failed", extra={"error_type": type(exc).__name__})
            if any(isinstance(error, (TimeoutError, APITimeoutError)) for error in causes):
                raise ApplicationError(504, "The agent timed out. Please try again.") from None
            if any(isinstance(error, AuthenticationError) for error in causes):
                raise ApplicationError(
                    502, "OpenAI rejected the server API key. Check .env."
                ) from None
            if any(isinstance(error, RateLimitError) for error in causes):
                raise ApplicationError(
                    429, "OpenAI quota or rate limit reached. Please retry later."
                ) from None
            if any(isinstance(error, APIConnectionError) for error in causes):
                raise ApplicationError(502, "Unable to reach OpenAI. Please try again.") from None
            raise ApplicationError(
                502, "The agent could not produce a reply. Please try again."
            ) from None
