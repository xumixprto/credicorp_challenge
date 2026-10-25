"""Persisted approval bubbles; UI state and input locks belong to one conversation."""

from uuid import uuid4

import streamlit as st

from frontend.components.operation_summary import render_operation_summary
from frontend.services.agent_client import AgentAPIError, AgentClient
from shared.conversations import ConversationDetail


def render_confirmations(client: AgentClient, conversation: ConversationDetail) -> bool:
    decisions = st.session_state.setdefault("approval_decisions", {})
    blocked = any(
        c.status in {"pending", "processing", "outcome_unknown"} for c in conversation.confirmations
    )
    uncertain = any(
        c.status in {"processing", "outcome_unknown"} for c in conversation.confirmations
    )
    for confirmation in conversation.confirmations:
        if confirmation.status in {"completed", "rejected"}:
            continue
        confirmation_id = str(confirmation.confirmation_id)
        key = f"{conversation.user_id}:{conversation.session_id}:{confirmation_id}"
        with (
            st.chat_message("assistant", avatar=":material/fact_check:"),
            st.container(border=True, key=f"approval_card_{confirmation_id}"),
        ):
            render_operation_summary(confirmation)
            with st.expander("Technical details", expanded=False):
                st.caption(f"Tool: {confirmation.tool_name}")
                st.caption(f"Confirmation: {confirmation_id}")
                st.json(confirmation.arguments, expanded=True)
            if uncertain:
                st.warning(
                    "An operation is processing or its outcome is unknown. Check Alpaca orders "
                    "before taking further action. Other chats remain available."
                )
                if st.button("Refresh status", key=f"refresh_{confirmation_id}"):
                    st.rerun()
                continue

            decision = decisions.get(key)
            submitted = False
            if decision:
                st.error(decision.get("error") or "The decision has not been acknowledged yet.")
                submitted = st.button("Retry decision", key=f"retry_{confirmation_id}")
            else:
                st.caption(
                    "Uses your paper trading account."
                    if confirmation.paper_trading
                    else "Uses your live trading account."
                )
                confirm, reject = st.columns(2, gap="small")
                with confirm:
                    if st.button(
                        "Confirm",
                        key=f"confirm_{confirmation_id}",
                        type="primary",
                        icon=":material/check:",
                        width="stretch",
                    ):
                        decision = {"approved": True, "request_id": str(uuid4())}
                with reject:
                    if st.button(
                        "Reject",
                        key=f"reject_{confirmation_id}",
                        icon=":material/close:",
                        width="stretch",
                    ):
                        decision = {"approved": False, "request_id": str(uuid4())}
                submitted = decision is not None
            if submitted:
                decisions[key] = decision
                with st.spinner("Continuing the conversation..."):
                    try:
                        client.confirm_operation(
                            user_id=str(conversation.user_id),
                            session_id=conversation.session_id,
                            confirmation_id=confirmation_id,
                            approved=decision["approved"],
                            request_id=decision["request_id"],
                        )
                        decisions.pop(key, None)
                    except AgentAPIError as exc:
                        decision["error"] = str(exc)
                st.rerun()
    return blocked
