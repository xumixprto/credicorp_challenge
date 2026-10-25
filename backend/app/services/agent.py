import asyncio
import logging
from uuid import UUID

from agent_framework import AgentSession
from filelock import Timeout

from backend.app.agent.runtime import AgentRuntime
from backend.app.core.errors import ApplicationError
from backend.app.repositories.conversations import ConversationRepository
from backend.app.schemas.agent import AgentRequest, AgentResponse
from shared.conversations import ConversationDetail, ConversationMessage, utc_now

logger = logging.getLogger("credicorp.backend.agent")


class AgentService:
    def __init__(self, repository: ConversationRepository, runtime: AgentRuntime) -> None:
        self.repository = repository
        self.runtime = runtime

    async def create_conversation(self, user_id: UUID, session_id: str) -> ConversationDetail:
        try:
            async with self.repository.lock(user_id, session_id):
                record = await asyncio.to_thread(self.repository.load, user_id, session_id)
                if record is None:
                    record = self.repository.new(user_id, session_id)
                    await asyncio.to_thread(self.repository.save, record)
                return record.public_detail()
        except Timeout:
            raise ApplicationError(409, "This conversation is busy. Please try again.") from None

    async def create_reply(self, request: AgentRequest, request_id: str) -> AgentResponse:
        if request.message_type == "HUMAN_FLOW_REPLY":
            # Reserved for the upcoming human-flow implementation. No model or history mutation.
            return AgentResponse(message="")
        try:
            async with self.repository.lock(request.user_id, request.session_id):
                record = await asyncio.to_thread(
                    self.repository.load, request.user_id, request.session_id
                )
                if record is None:
                    record = self.repository.new(request.user_id, request.session_id)

                # A retry after a lost HTTP response returns the previously committed turn.
                previous = [
                    message for message in record.messages if message.request_id == request_id
                ]
                if previous:
                    if len(previous) != 2 or previous[0].content != request.query:
                        raise ApplicationError(
                            409, "This request ID was already used for another message."
                        )
                    return AgentResponse(message=previous[1].content)

                session = AgentSession.from_dict(record.agent_session)
                reply = await self.runtime.run(request.query, session)
                if not record.messages:
                    title = " ".join(request.query.split())
                    record.title = title[:57] + "..." if len(title) > 60 else title
                record.messages.extend(
                    [
                        ConversationMessage(
                            role="user", content=request.query, request_id=request_id
                        ),
                        ConversationMessage(role="assistant", content=reply, request_id=request_id),
                    ]
                )
                record.agent_session = session.to_dict()
                record.updated_at = utc_now()
                await asyncio.to_thread(self.repository.save, record)
                logger.info("agent_reply_created", extra={"session_id": request.session_id})
                return AgentResponse(message=reply)
        except Timeout:
            raise ApplicationError(409, "This conversation is busy. Please try again.") from None
