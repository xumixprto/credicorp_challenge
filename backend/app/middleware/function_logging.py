"""Trace individual Agent Framework tool calls without logging their payloads."""

import logging
from collections.abc import Awaitable, Callable
from time import perf_counter

from agent_framework import FunctionInvocationContext, FunctionMiddleware

logger = logging.getLogger("credicorp.backend.functions")


class FunctionLoggingMiddleware(FunctionMiddleware):
    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        started = perf_counter()
        fields = {
            "function_name": context.function.name,
            "session_id": context.session.session_id if context.session else None,
        }
        logger.info("function_call_started", extra=fields)
        try:
            await call_next()
        except Exception as exc:
            # Tool arguments, results, and exception text can contain sensitive data.
            logger.warning(
                "function_call_failed",
                extra={
                    **fields,
                    "error_type": type(exc).__name__,
                    "duration_ms": round((perf_counter() - started) * 1000, 2),
                },
            )
            raise
        logger.info(
            "function_call_completed",
            extra={**fields, "duration_ms": round((perf_counter() - started) * 1000, 2)},
        )
