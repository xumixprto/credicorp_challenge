from pydantic import TypeAdapter

from frontend.services.api_client import APIClient, APIError
from shared.conversations import ConversationDetail, ConversationSummary

AgentAPIError = APIError


class AgentClient(APIClient):
    target_name = "agent"
    timeout_message = "The reply took too long. Please try again."

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
            json={
                "userId": user_id,
                "query": query,
                "sessionId": session_id,
                "messageType": "TEXT",
            },
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
