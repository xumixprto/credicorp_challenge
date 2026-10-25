"""Run from the repository root: python -m streamlit run frontend/app.py."""

import logging
import sys
from pathlib import Path
from uuid import uuid4

import streamlit as st

# The standalone Streamlit launcher adds frontend/, but not always the repository root.
if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from frontend.components.chat import render_chat
from frontend.components.documents import render_documents
from frontend.components.reindex import render_reindex
from frontend.components.sidebar import render_navigation, render_sidebar
from frontend.core.config import get_settings
from frontend.services.agent_client import AgentAPIError, AgentClient
from frontend.services.document_client import DocumentClient
from frontend.state.chat import initialize_state
from shared.conversations import ConversationDetail
from shared.logging_config import configure_logging


def main() -> None:
    st.set_page_config(
        page_title="Credicorp Workspace",
        page_icon="◈",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    settings = get_settings()
    configure_logging("frontend", settings)
    css = Path(__file__).parent / "assets" / "styles.css"
    st.html(f"<style>{css.read_text(encoding='utf-8')}</style>")
    initialize_state(str(settings.chat_user_id))
    if "session_logged" not in st.session_state:
        logging.getLogger("credicorp.frontend").info("browser_session_started")
        st.session_state.session_logged = True
    workspace = render_navigation()
    if workspace in {"Document Ingestion", "Reindex"}:
        render_page = render_documents if workspace == "Document Ingestion" else render_reindex
        render_page(
            DocumentClient(
                str(settings.api_base_url), timeout=float(settings.document_api_timeout_seconds)
            )
        )
        return
    client = AgentClient(str(settings.api_base_url), timeout=float(settings.api_timeout_seconds))
    try:
        conversations = client.list_conversations(st.session_state.user_id)
        if st.session_state.active_session_id is None:
            st.session_state.active_session_id = (
                conversations[0].session_id if conversations else str(uuid4())
            )
        render_sidebar(client, conversations)
        session_id = st.session_state.active_session_id
        conversation = (
            client.get_conversation(st.session_state.user_id, session_id)
            if any(item.session_id == session_id for item in conversations)
            else ConversationDetail(user_id=settings.chat_user_id, session_id=session_id)
        )
    except AgentAPIError as exc:
        st.title("Agent Chat")
        st.error(f"Unable to load your saved chats. {exc}")
        st.caption(f"Reference: {exc.request_id}")
        st.button("Reconnect", key="reconnect")
        st.chat_input("Reconnect to continue chatting...", disabled=True)
        return
    render_chat(client, conversation)


if __name__ == "__main__":
    main()
