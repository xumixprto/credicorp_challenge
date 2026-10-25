"""Document ingestion/reindex contracts shared by FastAPI and Streamlit."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class IngestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str
    source_id: str
    document_id: str | None = None
    status: Literal["ingested", "updated", "unchanged", "duplicate", "failed"]
    chunks: int = Field(default=0, ge=0)
    error: str | None = None


class IngestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection: str | None
    results: list[IngestResult]
    embedded_texts: int = Field(default=0, ge=0)
    cached_texts: int = Field(default=0, ge=0)


class ReindexResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection: str
    previous_collection: str | None
    documents: int = Field(ge=0)
    chunks: int = Field(ge=0)
    embedded_texts: int = Field(ge=0)
    cached_texts: int = Field(ge=0)
