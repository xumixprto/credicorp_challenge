from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.app.documents.config import DocumentConfig
from shared.documents import IngestResponse as IngestResponse
from shared.documents import IngestResult as IngestResult


class DataModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Chunk(DataModel):
    index: int
    text: str
    embedding_text: str
    token_count: int
    pages: list[int] = Field(default_factory=list)
    headings: list[str] = Field(default_factory=list)
    references: list[dict[str, Any]] = Field(default_factory=list)


class DocumentRecord(DataModel):
    document_id: str
    source_id: str
    filename: str
    source_uri: str | None = None
    content_hash: str
    original_path: str
    format: str
    revision: str
    chunk_count: int
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ActiveIndex(DataModel):
    collection: str
    signature: str
    config: DocumentConfig
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ReindexRequest(DataModel):
    force_reparse: bool | None = None
    force_reembed: bool | None = None


class ReindexResponse(DataModel):
    collection: str
    previous_collection: str | None
    documents: int
    chunks: int
    embedded_texts: int
    cached_texts: int


class RetrieveRequest(DataModel):
    query: str = Field(min_length=1, max_length=32000)
    top_k: int | None = Field(default=None, ge=1)
    document_ids: list[str] | None = None
    max_distance: float | None = Field(default=None, ge=0)


class CitationSource(DataModel):
    document_id: str
    source_id: str
    filename: str
    source_uri: str | None
    content_hash: str


class RetrievalHit(DataModel):
    chunk_id: str
    text: str
    distance: float
    pages: list[int]
    headings: list[str]
    references: list[dict[str, Any]]
    sources: list[CitationSource]


class RetrieveResponse(DataModel):
    collection: str | None
    distance_metric: str
    hits: list[RetrievalHit]


@dataclass(frozen=True)
class UploadedDocument:
    filename: str
    source_id: str
    content: bytes
    source_uri: str | None = None
