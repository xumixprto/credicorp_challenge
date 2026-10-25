from uuid import uuid4

import streamlit as st

from frontend.services.agent_client import AgentAPIError, AgentClient
from frontend.state.chat import select_conversation
from shared.conversations import ConversationSummary


def render_navigation() -> str:
    with st.sidebar:
        st.markdown("### ◈ Credicorp")
        st.caption("AGENT WORKSPACE")
        selected = st.radio(
            "Workspace",
            ["Agent Chat", "Document Ingestion", "Reindex"],
            key="workspace",
            label_visibility="collapsed",
        )
        st.divider()
    return selected


def render_sidebar(client: AgentClient, conversations: list[ConversationSummary]) -> None:
    with st.sidebar:
        if st.button(
            "New chat",
            icon=":material/add:",
            key="new_chat",
            width="stretch",
        ):
            try:
                conversation = client.create_conversation(st.session_state.user_id, str(uuid4()))
                select_conversation(conversation.session_id)
                st.rerun()
            except AgentAPIError as exc:
                st.error(str(exc))
        st.caption("YOUR CHATS")
        for conversation in conversations:
            active = conversation.session_id == st.session_state.active_session_id
            st.button(
                conversation.title,
                icon=":material/chat_bubble_outline:",
                key=f"conversation_{conversation.session_id}",
                type="primary" if active else "secondary",
                width="stretch",
                on_click=select_conversation,
                args=(conversation.session_id,),
            )
        st.divider()
        st.button("Refresh chats", icon=":material/refresh:", key="refresh_chats")
        st.caption("Your conversations are saved. Pick up where you left off.")
