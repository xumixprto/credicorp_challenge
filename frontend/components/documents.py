"""Upload documents and display persistent, per-file ingestion outcomes."""

import streamlit as st

from frontend.services.api_client import APIError
from frontend.services.document_client import DocumentClient, DocumentUpload
from shared.documents import IngestResponse

STATUS_LABELS = {
    "ingested": "Added",
    "updated": "Updated",
    "unchanged": "Already ingested",
    "duplicate": "Duplicate content",
    "failed": "Failed",
}


def render_results(result: IngestResponse) -> None:
    st.subheader("Last ingestion")
    failed = sum(item.status == "failed" for item in result.results)
    ready = len(result.results) - failed
    if not result.results:
        st.warning("No document results were returned. Try uploading your files again.")
    elif failed == len(result.results):
        st.error("No documents were ingested. Check the details below and try again.")
    elif failed:
        st.warning(f"{ready} document(s) are ready. {failed} could not be ingested.")
    else:
        st.success(f"All {ready} document(s) are available in the document library.")
    st.dataframe(
        [
            {
                "Document": item.filename,
                "Status": STATUS_LABELS[item.status],
                "Details": item.error or "",
            }
            for item in result.results
        ],
        hide_index=True,
        width="stretch",
    )


def render_documents(client: DocumentClient) -> None:
    st.title("Document Ingestion")
    st.caption("Upload documents to your shared document library.")
    uploads = st.file_uploader(
        "Drag and drop documents here, or browse files",
        accept_multiple_files=True,
        key="document_uploads",
        help="PDF, DOCX, Markdown, HTML, and other formats supported by the document service.",
    )
    st.caption("Uploading a file with the same name updates the existing document.")
    if uploads:
        size_mb = sum(file.size for file in uploads) / (1024 * 1024)
        st.write(f"{len(uploads)} document(s) selected · {size_mb:.2f} MB")
    duplicate_names = uploads and len({file.name for file in uploads}) != len(uploads)
    if duplicate_names:
        st.warning("Two selected files have the same name. Rename one before ingesting.")

    if st.button(
        "Ingest documents",
        type="primary",
        icon=":material/upload_file:",
        key="ingest_documents",
        disabled=not uploads or bool(duplicate_names),
    ):
        st.session_state.pop("document_ingestion_result", None)
        st.session_state.pop("document_ingestion_error", None)
        # The batch endpoint returns only when committed; display activity without inventing a %.
        with st.spinner(
            f"Processing and saving {len(uploads)} document(s). "
            "Large files can take a few minutes…",
            show_time=True,
        ):
            try:
                result = client.ingest(
                    [
                        DocumentUpload(file.name, file, file.type or "application/octet-stream")
                        for file in uploads
                    ]
                )
                st.session_state.document_ingestion_result = result
            except APIError as exc:
                st.session_state.document_ingestion_error = (str(exc), exc.request_id)

    if error := st.session_state.get("document_ingestion_error"):
        st.error(error[0])
        st.caption(f"Reference: {error[1]}")
    if result := st.session_state.get("document_ingestion_result"):
        render_results(result)
