import asyncio
import logging
from datetime import UTC, datetime

from agent_framework import (
    Agent,
    AgentResponse,
    AgentSession,
    InMemoryHistoryProvider,
    MCPStdioTool,
    Message,
)
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
from backend.app.evaluation.trace import active_run
from backend.app.middleware.agent_logging import AgentLoggingMiddleware
from backend.app.middleware.function_logging import FunctionLoggingMiddleware
from backend.app.middleware.tool_approval import ToolApprovalMiddleware
from backend.app.middleware.tool_trace import ToolTraceMiddleware
from backend.app.tools import get_tools

logger = logging.getLogger("credicorp.backend.agent")


def build_agent(
    settings: Settings, client: AsyncOpenAI, alpaca: MCPStdioTool | None = None
) -> Agent:
    local_tools = get_tools(settings, alpaca=alpaca) if alpaca is not None else get_tools(settings)
    trace = active_run.get()
    if trace is not None:
        trace.configuration_json["available_tools"] = [
            {"name": tool.name, "description": tool.description, "parameters": tool.parameters()}
            for tool in [*local_tools, *(alpaca.functions if alpaca is not None else [])]
        ]
    return Agent(
        id="credicorp-agent",
        name="CredicorpAgent",
        client=OpenAIChatClient(model=settings.openai_model, async_client=client),
        instructions=SYSTEM_INSTRUCTIONS
        + f"\nCurrent UTC date: {datetime.now(UTC).date().isoformat()}."
        + (
            "\nAlpaca tools are connected; use their advertised schemas."
            if alpaca is not None
            else "\nAlpaca is not configured. The assessment can fetch Yahoo financials, but "
            "Alpaca market data is unavailable."
        ),
        tools=[*local_tools, *([alpaca] if alpaca is not None else [])],
        context_providers=[InMemoryHistoryProvider(source_id="conversation-history")],
        middleware=[
            AgentLoggingMiddleware(),
            ToolApprovalMiddleware(),
            ToolTraceMiddleware(),
            FunctionLoggingMiddleware(),
        ],
        default_options={"store": False},
    )


class AgentRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.alpaca: MCPStdioTool | None = None

    async def run(self, query: str | Message, session: AgentSession) -> AgentResponse:
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
                    result = await build_agent(self.settings, client, self.alpaca).run(
                        query, session=session
                    )
                    if isinstance(query, Message) and any(
                        content.type == "function_result" and content.exception
                        for message in result.messages
                        for content in message.contents
                    ):
                        raise ApplicationError(502, "The continuation includes a failed tool call.")
                    if not result.text.strip() and not result.user_input_requests:
                        raise ApplicationError(
                            502, "The agent returned an empty reply. Please retry."
                        )
                    return result
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
