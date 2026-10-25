from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile

from backend.app.core.errors import ApplicationError
from backend.app.documents.models import (
    IngestResponse,
    ReindexRequest,
    ReindexResponse,
    RetrieveRequest,
    RetrieveResponse,
    UploadedDocument,
)
from backend.app.documents.service import DocumentService

router = APIRouter(prefix="/docs", tags=["Documents"])


def service_for(request: Request) -> DocumentService:
    return request.app.state.document_service


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    request: Request,
    files: Annotated[list[UploadFile], File(description="One or more original documents")],
    source_ids: Annotated[list[str] | None, Form()] = None,
    source_uris: Annotated[list[str] | None, Form()] = None,
) -> IngestResponse:
    service = service_for(request)
    limits = service.config.ingestion
    if not files or len(files) > limits.max_files:
        raise ApplicationError(422, "File count exceeds ingestion.max_files.")
    if any(
        values is not None and len(values) != len(files) for values in (source_ids, source_uris)
    ):
        raise ApplicationError(
            422, "source_ids and source_uris must each match the number of files."
        )
    uploads = []
    total_bytes = 0
    try:
        for index, file in enumerate(files):
            filename = Path((file.filename or "").replace("\\", "/")).name
            if not filename:
                raise ApplicationError(422, "Each upload must have a filename.")
            source_id = source_ids[index].strip() if source_ids is not None else filename
            if not source_id or len(source_id) > 512:
                raise ApplicationError(422, "Each source_id must contain 1 to 512 characters.")
            uri = source_uris[index].strip() if source_uris is not None else None
            if uri and len(uri) > 2048:
                raise ApplicationError(422, "source_uri exceeds 2048 characters.")
            content = bytearray()
            while block := await file.read(1024 * 1024):
                total_bytes += len(block)
                content.extend(block)
                if len(content) > limits.max_file_mb * 1024 * 1024:
                    raise ApplicationError(413, "A file exceeds ingestion.max_file_mb.")
                if total_bytes > limits.max_batch_mb * 1024 * 1024:
                    raise ApplicationError(413, "The batch exceeds ingestion.max_batch_mb.")
            uploads.append(UploadedDocument(filename, source_id, bytes(content), uri or None))
        return await service.ingest(uploads)
    finally:
        for file in files:
            await file.close()


@router.post("/reindex", response_model=ReindexResponse)
async def reindex(request: Request, options: ReindexRequest | None = None) -> ReindexResponse:
    return await service_for(request).reindex(options or ReindexRequest())


@router.post("/retrieve", response_model=RetrieveResponse)
async def retrieve(request: Request, payload: RetrieveRequest) -> RetrieveResponse:
    return await service_for(request).retrieve(payload)
