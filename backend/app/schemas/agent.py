from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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
                }
            ]
        },
    )

    user_id: UUID = Field(alias="userId")
    query: str = Field(min_length=1, max_length=20000)
    session_id: str = Field(alias="sessionId", min_length=1, max_length=128)


class AgentResponse(BaseModel):
    message: str
