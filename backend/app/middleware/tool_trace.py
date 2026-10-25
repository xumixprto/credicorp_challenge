import json
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from time import perf_counter
from uuid import uuid4

from agent_framework import Content, FunctionInvocationContext, FunctionMiddleware
from pydantic import BaseModel

from backend.app.evaluation.trace import active_run


def _json_default(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    # Never stringify unknown internal objects or credentials.
    return "[unserializable]"


def serialize_result(value: object) -> object:
    if isinstance(value, Content):
        return value.to_dict()
    if isinstance(value, list):
        return [serialize_result(item) for item in value]
    # No repr/exception strings: avoid accidental capture of internal objects or credentials.
    return json.loads(json.dumps(value, default=_json_default))


class ToolTraceMiddleware(FunctionMiddleware):
    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        trace = active_run.get()
        if trace is None:
            await call_next()
            return
        entry = {
            "tool_call_id": context.metadata.get("function_call_occurrence_id") or str(uuid4()),
            "name": context.function.name,
            "arguments": serialize_result(context.arguments),
            "status": "running",
        }
        trace.tools_json.append(entry)
        started = perf_counter()
        try:
            await call_next()
            entry.update(status="completed", result=serialize_result(context.result))
        except BaseException as exc:
            entry.update(status="error", error_type=type(exc).__name__)
            raise
        finally:
            entry["duration_ms"] = round((perf_counter() - started) * 1000, 2)
