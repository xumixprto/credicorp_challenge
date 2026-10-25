import logging

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.app.core.errors import ApplicationError

logger = logging.getLogger("credicorp.backend.errors")


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Exclude input values and validation context, which may contain private text.
        logger.warning(
            "request_validation_failed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": 422,
                "validation_errors": [
                    {"location": error["loc"], "type": error["type"]} for error in exc.errors()
                ],
            },
        )
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(ApplicationError)
    async def application_error(request: Request, exc: ApplicationError) -> JSONResponse:
        logger.warning(
            "application_error",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": exc.status_code,
                "reason": exc.detail,
            },
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "requestId": request.state.request_id},
        )
