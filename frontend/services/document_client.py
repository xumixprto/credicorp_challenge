"""Multipart document uploads to the backend; document processing stays in FastAPI."""

from dataclasses import dataclass
from typing import BinaryIO

from frontend.services.api_client import APIClient
from shared.documents import IngestResponse, ReindexResponse


@dataclass(frozen=True)
class DocumentUpload:
    filename: str
    stream: BinaryIO
    content_type: str = "application/octet-stream"


class DocumentClient(APIClient):
    target_name = "document service"
    timeout_message = (
        "The upload timed out. Processing may still finish on the server. "
        "Retry the same files to check their status."
    )

    def ingest(self, uploads: list[DocumentUpload]) -> IngestResponse:
        if not uploads:
            raise ValueError("Select at least one document.")
        for upload in uploads:
            upload.stream.seek(0)
        return self._request(
            "POST",
            "/docs/ingest",
            IngestResponse.model_validate,
            files=[
                ("files", (upload.filename, upload.stream, upload.content_type))
                for upload in uploads
            ],
        )

    def reindex(self, *, force_reparse: bool, force_reembed: bool) -> ReindexResponse:
        return self._request(
            "POST",
            "/docs/reindex",
            ReindexResponse.model_validate,
            json={"force_reparse": force_reparse, "force_reembed": force_reembed},
            timeout_message=(
                "The reindex request timed out. The rebuild may still finish on the server. "
                "Check the backend logs before starting another rebuild."
            ),
        )
