import streamlit as st

from frontend.components.confirmations import render_confirmations
from frontend.services.agent_client import AgentAPIError, AgentClient
from frontend.state.chat import PendingTurn, queue_message
from shared.conversations import ConversationDetail


def request_reply(session_id: str, client: AgentClient, turn: PendingTurn) -> None:
    with st.spinner("Thinking..."):
        try:
            client.send_message(
                user_id=st.session_state.user_id,
                query=turn.query,
                session_id=session_id,
                request_id=turn.request_id,
            )
            st.session_state.pending_turns.pop(session_id, None)
        except AgentAPIError as exc:
            turn.error = str(exc)


def render_chat(client: AgentClient, conversation: ConversationDetail) -> None:
    st.title("Agent Chat")
    st.caption("A space for your questions and ideas.")

    if not conversation.messages:
        with st.container(key="welcome"):
            st.markdown("## What would you like to explore?")
            st.markdown("Send a message to start a conversation.")

    for message in conversation.messages:
        with st.chat_message(message.role):
            st.markdown(message.content)

    turn = st.session_state.pending_turns.get(conversation.session_id)
    if turn and any(message.request_id == turn.request_id for message in conversation.messages):
        st.session_state.pending_turns.pop(conversation.session_id, None)
        turn = None
    if turn:
        with st.chat_message("user"):
            st.markdown(turn.query)
        st.error(turn.error or "This message has not been sent yet.")
        st.caption(f"Reference: {turn.request_id}")
        if st.button("Try again", icon=":material/refresh:", key="retry_reply"):
            request_reply(conversation.session_id, client, turn)
            st.rerun()
        if st.button("Discard unsent message", key="discard_pending"):
            st.session_state.pending_turns.pop(conversation.session_id, None)
            st.rerun()

    awaiting_approval = render_confirmations(client, conversation)
    if prompt := st.chat_input(
        "Message the agent...", max_chars=20000, disabled=turn is not None or awaiting_approval
    ):
        query = prompt.strip()
        if not query:
            st.warning("Please enter a message before sending.")
            return
        turn = queue_message(conversation.session_id, query)
        with st.chat_message("user"):
            st.markdown(query)
        request_reply(conversation.session_id, client, turn)
        st.rerun()
