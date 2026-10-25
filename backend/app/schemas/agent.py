from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from shared.conversations import OperationConfirmation


class AgentRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [
                {
                    "userId": "d5bbf9d1-5945-44d8-bb10-0065164a8356",
                    "query": "thanks!",
                    "sessionId": "test-session-1001",
                    "messageType": "TEXT",
                }
            ]
        },
    )

    user_id: UUID = Field(alias="userId")
    query: str = Field(default="", max_length=20000)
    session_id: str = Field(alias="sessionId", min_length=1, max_length=128)
    message_type: Literal["TEXT", "HUMAN_FLOW_REPLY"] = Field(default="TEXT", alias="messageType")
    confirmation_id: UUID | None = Field(default=None, alias="confirmationId")
    approved: StrictBool | None = None

    @model_validator(mode="after")
    def require_text(self) -> Self:
        if self.message_type == "TEXT" and not self.query:
            raise ValueError("query must not be empty for TEXT messages")
        if self.message_type == "TEXT" and (
            self.confirmation_id is not None or self.approved is not None
        ):
            raise ValueError("confirmationId and approved are only valid for HUMAN_FLOW_REPLY")
        if self.message_type == "HUMAN_FLOW_REPLY":
            if self.confirmation_id is None or self.approved is None:
                raise ValueError("HUMAN_FLOW_REPLY requires confirmationId and approved")
            if self.query:
                raise ValueError("query must be empty for HUMAN_FLOW_REPLY")
        return self


class AgentResponse(BaseModel):
    message: str
    status: Literal["confirmation_required"] | None = None
    confirmations: list[OperationConfirmation] | None = None
