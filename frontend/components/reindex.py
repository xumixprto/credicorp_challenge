"""Explicit, configurable document-index rebuilds from Streamlit."""

import streamlit as st

from frontend.services.api_client import APIError
from frontend.services.document_client import DocumentClient


def render_reindex(client: DocumentClient) -> None:
    st.title("Reindex")
    st.caption("Rebuild the document library from the saved originals using the current settings.")
    force_reparse = st.checkbox(
        "Force reparse",
        key="reindex_force_reparse",
        value=False,
        help="Extract text and tables again and rebuild chunks, bypassing saved parsing results.",
    )
    force_reembed = st.checkbox(
        "Force reembed",
        key="reindex_force_reembed",
        value=False,
        help="Generate all embeddings again. This makes new OpenAI API calls with usage costs.",
    )
    st.caption(
        "Leave both unchecked to reuse cached work. "
        "Existing searches stay available during rebuilding."
    )
    if st.button("Reindex documents", key="reindex_documents", type="primary"):
        st.session_state.pop("document_reindex_result", None)
        st.session_state.pop("document_reindex_error", None)
        with st.spinner("Rebuilding the document library…", show_time=True):
            try:
                st.session_state.document_reindex_result = client.reindex(
                    force_reparse=force_reparse, force_reembed=force_reembed
                )
            except APIError as exc:
                st.session_state.document_reindex_error = (str(exc), exc.request_id)
    if error := st.session_state.get("document_reindex_error"):
        st.error(error[0])
        st.caption(f"Reference: {error[1]}")
    if result := st.session_state.get("document_reindex_result"):
        st.subheader("Last rebuild")
        st.success(f"Reindex complete. {result.documents} document(s) are ready to search.")
        columns = st.columns(3)
        columns[0].metric("Chunks indexed", result.chunks)
        columns[1].metric("Embeddings generated", result.embedded_texts)
        columns[2].metric("Embeddings reused", result.cached_texts)
