from dataclasses import dataclass
from uuid import uuid4

import streamlit as st


@dataclass
class PendingTurn:
    query: str
    request_id: str
    error: str | None = None


def initialize_state(user_id: str) -> None:
    if (
        st.session_state.get("chat_state_version") != 2
        or st.session_state.get("user_id") != user_id
    ):
        # Discard the old Hello World chats on an existing Streamlit browser session.
        st.session_state.pop("conversations", None)
        st.session_state.pop("active_conversation_id", None)
        st.session_state.user_id = user_id
        st.session_state.active_session_id = None
        st.session_state.pending_turns = {}
        st.session_state.chat_state_version = 2


def select_conversation(conversation_id: str) -> None:
    st.session_state.active_session_id = conversation_id


def queue_message(session_id: str, query: str) -> PendingTurn:
    turn = PendingTurn(query=query, request_id=str(uuid4()))
    st.session_state.pending_turns[session_id] = turn
    return turn
