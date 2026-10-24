import logging
from collections.abc import Callable
from time import perf_counter
from typing import TypeVar
from uuid import uuid4

import httpx
from pydantic import TypeAdapter

from shared.conversations import ConversationDetail, ConversationSummary

logger = logging.getLogger("credicorp.frontend.agent_client")
Result = TypeVar("Result")


class AgentAPIError(Exception):
    def __init__(self, message: str, request_id: str) -> None:
        super().__init__(message)
        self.request_id = request_id


class AgentClient:
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
                message = "The reply took too long. Please try again."
            elif isinstance(exc, httpx.HTTPStatusError):
                message = "The request could not be completed. Please try again."
                if status_code in {404, 409, 429, 502, 503, 504}:
                    try:
                        detail = exc.response.json().get("detail")
                        if isinstance(detail, str):
                            message = detail
                    except (ValueError, AttributeError):
                        pass
            elif isinstance(exc, httpx.RequestError):
                message = "Unable to reach the agent. Please try again in a moment."
            else:
                message = "The agent returned an unexpected response. Please try again."
            logger.warning(
                "api_request_failed",
                extra={
                    **context,
                    "status_code": status_code,
                    "duration_ms": round((perf_counter() - started) * 1000, 2),
                    "error_type": type(exc).__name__,
                },
            )
            raise AgentAPIError(message, request_id) from exc

        logger.info(
            "api_request_completed",
            extra={
                **context,
                "status_code": status_code,
                "duration_ms": round((perf_counter() - started) * 1000, 2),
            },
        )
        return result

    def send_message(
        self, *, user_id: str, query: str, session_id: str, request_id: str | None = None
    ) -> str:
        def validate(payload: object) -> str:
            if not isinstance(payload, dict) or not isinstance(payload.get("message"), str):
                raise ValueError("Invalid agent response")
            return payload["message"]

        return self._request(
            "POST",
            "/agent",
            validate,
            request_id=request_id,
            session_id=session_id,
            json={"userId": user_id, "query": query, "sessionId": session_id},
        )

    def list_conversations(self, user_id: str) -> list[ConversationSummary]:
        return self._request(
            "GET",
            "/conversations",
            TypeAdapter(list[ConversationSummary]).validate_python,
            params={"userId": user_id},
        )

    def get_conversation(self, user_id: str, session_id: str) -> ConversationDetail:
        return self._request(
            "GET",
            "/conversations/detail",
            ConversationDetail.model_validate,
            session_id=session_id,
            params={"userId": user_id, "sessionId": session_id},
        )

    def create_conversation(self, user_id: str, session_id: str) -> ConversationDetail:
        return self._request(
            "POST",
            "/conversations",
            ConversationDetail.model_validate,
            session_id=session_id,
            json={"userId": user_id, "sessionId": session_id},
        )
