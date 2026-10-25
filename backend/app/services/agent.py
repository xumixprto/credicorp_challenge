import asyncio
import logging
from hashlib import sha256
from uuid import UUID

from agent_framework import AgentResponse as FrameworkResponse
from agent_framework import AgentSession, Content, Message
from filelock import Timeout

from backend.app.agent.runtime import AgentRuntime
from backend.app.core.errors import ApplicationError
from backend.app.repositories.conversations import (
    ConversationRepository,
    StoredApproval,
    StoredConversation,
    StoredReply,
)
from backend.app.schemas.agent import AgentRequest, AgentResponse
from shared.conversations import (
    ConversationDetail,
    ConversationMessage,
    OperationConfirmation,
    utc_now,
)

logger = logging.getLogger("credicorp.backend.agent")
BLOCKING_STATUSES = {"pending", "processing", "outcome_unknown"}


class AgentService:
    def __init__(self, repository: ConversationRepository, runtime: AgentRuntime) -> None:
        self.repository = repository
        self.runtime = runtime

    def _account_fingerprint(self) -> str:
        settings = self.runtime.settings
        identity = "\0".join(
            [
                settings.alpaca_api_key.get_secret_value().strip(),
                settings.alpaca_secret_key.get_secret_value().strip(),
                str(settings.alpaca_paper_trade),
            ]
        )
        return sha256(identity.encode()).hexdigest()

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
        try:
            # Both the lock and approval state are scoped to userId + sessionId.
            async with self.repository.lock(request.user_id, request.session_id):
                record = await asyncio.to_thread(
                    self.repository.load, request.user_id, request.session_id
                )
                if record is None:
                    record = self.repository.new(request.user_id, request.session_id)
                cached = record.replies.get(request_id)
                if cached:
                    if cached.request != request.model_dump(mode="json"):
                        raise ApplicationError(
                            409, "This request ID was already used for another message."
                        )
                    return cached.response
                if request.message_type == "HUMAN_FLOW_REPLY":
                    return await self._continue(record, request, request_id)

                # Retain idempotency for conversations saved before response caching was added.
                previous = [m for m in record.messages if m.request_id == request_id]
                if previous:
                    if len(previous) != 2 or previous[0].content != request.query:
                        raise ApplicationError(
                            409, "This request ID was already used for another message."
                        )
                    return AgentResponse(message=previous[1].content)
                if any(c.status in BLOCKING_STATUSES for c in record.confirmations):
                    raise ApplicationError(
                        409,
                        "Resolve this conversation's pending operation before sending text. "
                        "Other chats remain available.",
                    )

                session = AgentSession.from_dict(record.agent_session)
                result = await self.runtime.run(request.query, session)
                response = self._response(record, result)
                if not record.messages:
                    title = " ".join(request.query.split())
                    record.title = title[:57] + "..." if len(title) > 60 else title
                record.messages.append(
                    ConversationMessage(
                        role="user",
                        content=request.query,
                        request_id=request_id,
                    )
                )
                await self._commit(record, session, request, request_id, response)
                return response
        except Timeout:
            raise ApplicationError(409, "This conversation is busy. Please try again.") from None

    async def _continue(
        self, record: StoredConversation, request: AgentRequest, request_id: str
    ) -> AgentResponse:
        confirmation = next(
            (c for c in record.confirmations if c.confirmation_id == request.confirmation_id), None
        )
        if confirmation is None:
            raise ApplicationError(404, "Confirmation not found in this conversation.")
        saved = record.approvals[str(confirmation.confirmation_id)]
        if confirmation.approved is not None and confirmation.approved != request.approved:
            raise ApplicationError(409, "This operation already has a different decision.")
        if saved.response is not None:
            return saved.response
        if confirmation.status != "pending" or any(
            c.status in {"processing", "outcome_unknown"} for c in record.confirmations
        ):
            raise ApplicationError(
                409,
                "This operation may already have run. Check Alpaca orders before taking "
                "further action; it will not be resubmitted.",
            )
        if request.approved and saved.account_fingerprint != self._account_fingerprint():
            raise ApplicationError(
                409,
                "Alpaca account or paper/live mode changed. "
                "Reject this proposal and request a new one.",
            )

        # RESTORE CONTINUATION HERE: session includes history and SDK pending-call bindings.
        session = AgentSession.from_dict(record.agent_session)
        approval_request = Content.from_dict(saved.framework_request)
        reply = Message(
            role="user",
            contents=[approval_request.to_function_approval_response(approved=request.approved)],
        )

        # Durable claim BEFORE executing. A crash/lost response cannot replay this operation.
        confirmation.status = "processing"
        confirmation.approved = request.approved
        await asyncio.to_thread(self.repository.save, record)
        logger.info(
            "tool_approval_decided",
            extra={
                "session_id": record.session_id,
                "confirmation_id": str(confirmation.confirmation_id),
                "approved": request.approved,
            },
        )
        try:
            # This is a NEW run with restored state; no coroutine was kept waiting.
            result = await self.runtime.run(reply, session)
        except (Exception, asyncio.CancelledError):
            if not request.approved:
                # Rejecting executes no write, so the same rejection can safely be retried.
                confirmation.status = "pending"
                confirmation.approved = None
                await asyncio.to_thread(self.repository.save, record)
                raise ApplicationError(
                    502,
                    "The rejection could not finish. "
                    "No operation was approved; retry the rejection.",
                ) from None
            confirmation.status = "outcome_unknown"
            await asyncio.to_thread(self.repository.save, record)
            logger.warning(
                "tool_approval_outcome_unknown",
                extra={
                    "session_id": record.session_id,
                    "confirmation_id": str(confirmation.confirmation_id),
                },
            )
            raise ApplicationError(
                502,
                "Continuation failed. The operation will not be retried automatically. "
                "Check Alpaca orders before placing another order.",
            ) from None

        confirmation.status = "completed" if request.approved else "rejected"
        response = self._response(record, result)
        saved.response = response.model_copy(deep=True)
        record.messages.append(
            ConversationMessage(
                role="user",
                content=f"{'Approved' if request.approved else 'Rejected'} operation "
                f"{confirmation.confirmation_id} ({confirmation.tool_name}).",
                request_id=request_id,
            )
        )
        await self._commit(record, session, request, request_id, response)
        return response

    def _response(self, record: StoredConversation, result: FrameworkResponse) -> AgentResponse:
        for item in result.user_input_requests:
            if item.type != "function_approval_request":
                continue
            if any(a.framework_request.get("id") == item.id for a in record.approvals.values()):
                continue
            call = item.function_call
            confirmation = OperationConfirmation(
                tool_name=call.name,
                arguments=call.parse_arguments() or {},
                paper_trading=self.runtime.settings.alpaca_paper_trade,
            )
            record.confirmations.append(confirmation)
            record.approvals[str(confirmation.confirmation_id)] = StoredApproval(
                framework_request=item.to_dict(),
                account_fingerprint=self._account_fingerprint(),
            )
        pending = [c.model_copy(deep=True) for c in record.confirmations if c.status == "pending"]
        if pending:
            return AgentResponse(
                message=result.text or "Please confirm or reject the proposed operation(s).",
                status="confirmation_required",
                confirmations=pending,
            )
        return AgentResponse(message=result.text)

    async def _commit(
        self,
        record: StoredConversation,
        session: AgentSession,
        request: AgentRequest,
        request_id: str,
        response: AgentResponse,
    ) -> None:
        record.messages.append(
            ConversationMessage(
                role="assistant",
                content=response.message,
                request_id=request_id,
            )
        )
        record.agent_session = session.to_dict()
        record.updated_at = utc_now()
        record.replies[request_id] = StoredReply(
            request=request.model_dump(mode="json"),
            response=response.model_copy(deep=True),
        )
        await asyncio.to_thread(self.repository.save, record)
        logger.info("agent_reply_created", extra={"session_id": record.session_id})
