"""Use Agent Framework's native approval continuation for Alpaca writes."""

import logging
from collections.abc import Awaitable, Callable

from agent_framework import Content, FunctionInvocationContext, FunctionMiddleware

logger = logging.getLogger("credicorp.backend.approvals")


def is_alpaca_write(name: str) -> bool:
    # Unknown Alpaca actions require approval too. Retrieval and market reads stay automatic.
    return name.startswith("alpaca_") and not name.removeprefix("alpaca_").startswith(
        ("get_", "list_", "search_", "fetch_")
    )


class ToolApprovalMiddleware(FunctionMiddleware):
    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        if not is_alpaca_write(context.function.name):
            await call_next()
            return

        # Framework binds this response to the pending call and its original arguments.
        approval = context.metadata.get("approval_response")
        if approval is not None and approval.approved is True:
            await call_next()
            return

        occurrence_id = context.metadata["function_call_occurrence_id"]
        context.result = Content.from_function_approval_request(
            id=occurrence_id,
            function_call=Content.from_function_call(
                call_id=context.metadata["call_id"],
                id=occurrence_id,
                name=context.function.name,
                arguments=context.arguments,
            ),
        )
        logger.info(
            "tool_approval_requested",
            extra={
                "function_name": context.function.name,
                "session_id": context.session.session_id if context.session else None,
            },
        )
        # No call_next: the tool never runs. The SDK returns the approval request to the API.
