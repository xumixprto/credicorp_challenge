import logging
from collections.abc import Awaitable, Callable
from time import perf_counter

from agent_framework import AgentContext, AgentMiddleware

logger = logging.getLogger("credicorp.backend.agent")


class AgentLoggingMiddleware(AgentMiddleware):
    async def process(
        self, context: AgentContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        started = perf_counter()
        fields = {"session_id": context.session.session_id if context.session else None}
        logger.info("agent_run_started", extra=fields)
        try:
            await call_next()
        except Exception as exc:
            # Provider exceptions may embed prompts or credentials. Log their type only.
            logger.warning(
                "agent_run_failed",
                extra={
                    **fields,
                    "error_type": type(exc).__name__,
                    "duration_ms": round((perf_counter() - started) * 1000, 2),
                },
            )
            raise
        logger.info(
            "agent_run_completed",
            extra={**fields, "duration_ms": round((perf_counter() - started) * 1000, 2)},
        )
