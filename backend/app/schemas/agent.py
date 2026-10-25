from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    query: str = Field(max_length=20000)
    session_id: str = Field(alias="sessionId", min_length=1, max_length=128)
    message_type: Literal["TEXT", "HUMAN_FLOW_REPLY"] = Field(default="TEXT", alias="messageType")

    @model_validator(mode="after")
    def require_text(self) -> Self:
        if self.message_type == "TEXT" and not self.query:
            raise ValueError("query must not be empty for TEXT messages")
        return self


class AgentResponse(BaseModel):
    message: str
