"""HTTP transport with safe errors, request IDs, and structured timing logs."""

import logging
from collections.abc import Callable
from time import perf_counter
from typing import TypeVar
from uuid import uuid4

import httpx

logger = logging.getLogger("credicorp.frontend.api_client")
Result = TypeVar("Result")


class APIError(Exception):
    def __init__(self, message: str, request_id: str) -> None:
        super().__init__(message)
        self.request_id = request_id


class APIClient:
    target_name = "API"
    timeout_message = "The request took too long. Please try again."

    def __init__(
        self,
        base_url: str,
        timeout: float = 90,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport

    def _request(
        self,
        method: str,
        path: str,
        validate: Callable[[object], Result],
        *,
        request_id: str | None = None,
        session_id: str | None = None,
        timeout_message: str | None = None,
        **kwargs,
    ) -> Result:
        request_id = request_id or str(uuid4())
        started = perf_counter()
        context = {"request_id": request_id, "session_id": session_id, "path": path}
        status_code = None
        logger.debug("api_request_started", extra=context)
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                response = client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers={"X-Request-ID": request_id},
                    **kwargs,
                )
            status_code = response.status_code
            response.raise_for_status()
            result = validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, httpx.TimeoutException):
                message = timeout_message or self.timeout_message
            elif isinstance(exc, httpx.HTTPStatusError):
                message = "The request could not be completed. Please try again."
                if status_code in {400, 404, 409, 413, 422, 429, 502, 503, 504}:
                    try:
                        detail = exc.response.json().get("detail")
                        if isinstance(detail, str):
                            message = detail
                    except (ValueError, AttributeError):
                        pass
            elif isinstance(exc, httpx.RequestError):
                message = f"Unable to reach the {self.target_name}. Please try again in a moment."
            else:
                message = (
                    f"The {self.target_name} returned an unexpected response. Please try again."
                )
            logger.warning(
                "api_request_failed",
                extra={
                    **context,
                    "status_code": status_code,
                    "duration_ms": round((perf_counter() - started) * 1000, 2),
                    "error_type": type(exc).__name__,
                },
            )
            raise APIError(message, request_id) from exc

        logger.info(
            "api_request_completed",
            extra={
                **context,
                "status_code": status_code,
                "duration_ms": round((perf_counter() - started) * 1000, 2),
            },
        )
        return result
