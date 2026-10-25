"""Read-only data acquisition, recorded independently of the conversational agent."""

import asyncio
import logging
from collections.abc import Awaitable
from time import perf_counter
from uuid import uuid4

from backend.app.evaluation.trace import active_run
from backend.app.middleware.tool_trace import serialize_result

logger = logging.getLogger("credicorp.backend.investment.data")


async def fetch_recorded(name: str, arguments: dict, operation: Awaitable, timeout: float):
    """Record provider evidence for debugging/judging; traces are never a data source."""
    entry = {
        "tool_call_id": str(uuid4()),
        "name": name,
        "arguments": arguments,
        "status": "running",
    }
    trace = active_run.get()
    if trace is not None:
        trace.tools_json.append(entry)
    started = perf_counter()
    try:
        result = await asyncio.wait_for(operation, timeout)
        entry.update(status="completed", result=serialize_result(result))
        return result
    except BaseException as exc:
        entry.update(status="error", error_type=type(exc).__name__)
        raise
    finally:
        entry["duration_ms"] = round((perf_counter() - started) * 1000, 2)
        logger.info(
            "investment_data_fetch_completed",
            extra={
                "function_name": name,
                "ticker": arguments.get("ticker") or arguments.get("symbols"),
                "status": entry["status"],
                "error_type": entry.get("error_type"),
                "duration_ms": entry["duration_ms"],
            },
        )
