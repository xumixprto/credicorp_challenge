import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from backend.app.core.errors import ApplicationError
from shared.conversations import (
    ConversationDetail,
    ConversationSummary,
    CreateConversationRequest,
)

router = APIRouter(prefix="/conversations", tags=["Conversations"])


@router.get("", response_model=list[ConversationSummary])
async def list_conversations(
    request: Request, user_id: Annotated[UUID, Query(alias="userId")]
) -> list[ConversationSummary]:
    return await asyncio.to_thread(request.app.state.conversations.list_for_user, user_id)


@router.get("/detail", response_model=ConversationDetail)
async def get_conversation(
    request: Request,
    user_id: Annotated[UUID, Query(alias="userId")],
    session_id: Annotated[str, Query(alias="sessionId", min_length=1, max_length=128)],
) -> ConversationDetail:
    record = await asyncio.to_thread(request.app.state.conversations.load, user_id, session_id)
    if record is None:
        raise ApplicationError(404, "Conversation not found.")
    return record.public_detail()


@router.post("", response_model=ConversationDetail, status_code=201)
async def create_conversation(payload: CreateConversationRequest, request: Request):
    return await request.app.state.agent_service.create_conversation(
        payload.user_id, payload.session_id
    )
