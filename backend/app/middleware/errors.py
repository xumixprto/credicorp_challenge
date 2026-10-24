from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from backend.app.core.errors import ApplicationError


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApplicationError)
    async def application_error(request: Request, exc: ApplicationError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "requestId": request.state.request_id},
        )
