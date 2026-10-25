"""Public conversation contracts shared by the API and Streamlit client."""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


def utc_now() -> datetime:
    return datetime.now(UTC)


class ConversationModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class CreateConversationRequest(ConversationModel):
    user_id: UUID
    session_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)


class ConversationMessage(ConversationModel):
    role: Literal["user", "assistant"]
    content: str
    request_id: str
    created_at: datetime = Field(default_factory=utc_now)


class ConversationSummary(ConversationModel):
    user_id: UUID
    session_id: str
    title: str = "New chat"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class OperationConfirmation(ConversationModel):
    confirmation_id: UUID = Field(default_factory=uuid4)
    tool_name: str
    arguments: dict[str, Any]
    paper_trading: bool
    status: Literal["pending", "processing", "completed", "rejected", "outcome_unknown"] = "pending"
    approved: bool | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ConversationDetail(ConversationSummary):
    messages: list[ConversationMessage] = Field(default_factory=list)
    confirmations: list[OperationConfirmation] = Field(default_factory=list)
