import logging
import re
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from shared.logging_config import request_id_context

logger = logging.getLogger("credicorp.backend.requests")
_valid_request_id = re.compile(r"[A-Za-z0-9._-]{1,128}")


def register_request_logging(app: FastAPI) -> None:
    @app.middleware("http")
    async def log_request(request: Request, call_next):
        incoming_id = request.headers.get("X-Request-ID", "")
        request_id = incoming_id if _valid_request_id.fullmatch(incoming_id) else str(uuid4())
        request.state.request_id = request_id
        token = request_id_context.set(request_id)
        started = perf_counter()
        context = {"method": request.method, "path": request.url.path}
        try:
            logger.info("request_started", extra=context)
            try:
                response = await call_next(request)
            except Exception:
                logger.exception("request_failed", extra={**context, "status_code": 500})
                response = JSONResponse(
                    status_code=500,
                    content={"detail": "Internal server error", "requestId": request_id},
                )
            response.headers["X-Request-ID"] = request_id
            level = logging.INFO
            if response.status_code >= 500:
                level = logging.ERROR
            elif response.status_code >= 400:
                level = logging.WARNING
            logger.log(
                level,
                "request_completed",
                extra={
                    **context,
                    "status_code": response.status_code,
                    "duration_ms": round((perf_counter() - started) * 1000, 2),
                },
            )
            return response
        finally:
            request_id_context.reset(token)
