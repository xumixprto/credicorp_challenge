import hashlib
import json
import re
from importlib.metadata import version
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from backend.app.evaluation.config import EvaluationConfig
from backend.app.investment.config import InvestmentConfig
from shared.settings import PROJECT_ROOT


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StorageConfig(ConfigModel):
    root: Path = Path("data/documents")
    sqlite_timeout_seconds: float = Field(default=30, gt=0)
    lock_timeout_seconds: float = Field(default=5, ge=0)


class IngestionConfig(ConfigModel):
    max_files: int = Field(default=20, ge=1, le=1000)
    max_file_mb: float = Field(default=50, gt=0)
    max_batch_mb: float = Field(default=200, gt=0)
    skip_failed_files: bool = True


class PdfConfig(ConfigModel):
    do_ocr: bool = True
    ocr_engine: Literal["rapidocr", "easyocr", "tesseract_cli"] = "rapidocr"
    ocr_languages: list[str] = Field(default_factory=list)
    force_full_page_ocr: bool = False
    do_table_structure: bool = True
    table_mode: Literal["fast", "accurate"] = "accurate"
    do_cell_matching: bool = True
    images_scale: float = Field(default=1, gt=0)
    generate_page_images: bool = False
    generate_picture_images: bool = False


class ParsingConfig(ConfigModel):
    format_map: dict[str, str] = Field(
        default_factory=lambda: {
            "pdf": "pdf",
            "docx": "docx",
            "md": "md",
            "markdown": "md",
            "html": "html",
            "htm": "html",
            "pptx": "pptx",
            "xlsx": "xlsx",
            "csv": "csv",
            "txt": "md",
            "png": "image",
            "jpg": "image",
            "jpeg": "image",
            "tiff": "image",
        }
    )
    max_pages: int = Field(default=500, ge=1)
    timeout_seconds: float = Field(default=180, gt=0)
    accelerator: Literal["auto", "cpu", "cuda", "mps", "xpu"] = "auto"
    num_threads: int = Field(default=4, ge=1)
    artifacts_path: Path | None = None
    pdf: PdfConfig = Field(default_factory=PdfConfig)

    @field_validator("format_map")
    @classmethod
    def validate_formats(cls, value: dict[str, str]) -> dict[str, str]:
        from docling.datamodel.base_models import InputFormat

        if not value:
            raise ValueError("parsing.format_map must contain at least one format")
        for extension, format_name in value.items():
            if not re.fullmatch(r"[a-z0-9]+", extension):
                raise ValueError("Extensions must be lowercase and omit the leading dot")
            InputFormat(format_name)
        return value


class ChunkingConfig(ConfigModel):
    tokenizer: str = "cl100k_base"
    max_tokens: int = Field(default=512, ge=32, le=8191)
    merge_peers: bool = True
    contextualize: bool = True
    repeat_table_header: bool = True
    omit_header_on_overflow: bool = False
    always_emit_headings: bool = False
    table_strategy: Literal["contextual_rows", "docling"] = "contextual_rows"
    infer_table_headers: bool = True
    table_header_max_rows: int = Field(default=8, ge=1, le=50)
    table_context_items: int = Field(default=6, ge=0, le=30)
    table_context_max_tokens: int = Field(default=128, ge=0, le=2048)

    @field_validator("tokenizer")
    @classmethod
    def validate_tokenizer(cls, value: str) -> str:
        import tiktoken

        if value not in tiktoken.list_encoding_names():
            raise ValueError("Unknown tiktoken encoding")
        return value


class EmbeddingConfig(ConfigModel):
    model: str = "text-embedding-3-small"
    dimensions: int = Field(default=1536, ge=1, le=3072)
    base_url: HttpUrl = HttpUrl("https://api.openai.com/v1")
    batch_size: int = Field(default=64, ge=1, le=2048)
    max_batch_tokens: int = Field(default=200000, ge=1, le=300000)
    max_input_tokens: int = Field(default=8191, ge=1, le=8191)
    max_concurrency: int = Field(default=4, ge=1, le=32)
    timeout_seconds: float = Field(default=60, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)
    cache_enabled: bool = True

    def signature(self) -> dict:
        return self.model_dump(mode="json", include={"model", "dimensions", "base_url"})


class HnswConfig(ConfigModel):
    space: Literal["cosine", "l2", "ip"] = "cosine"
    ef_construction: int = Field(default=200, ge=1)
    ef_search: int = Field(default=100, ge=1)
    max_neighbors: int = Field(default=16, ge=2)
    num_threads: int = Field(default=4, ge=1)
    batch_size: int = Field(default=100, ge=3)
    sync_threshold: int = Field(default=1000, ge=3)
    resize_factor: float = Field(default=1.2, gt=1)

    @model_validator(mode="after")
    def validate_threshold(self) -> Self:
        if self.sync_threshold < self.batch_size:
            raise ValueError("HNSW sync_threshold must be at least batch_size")
        return self


class ChromaConfig(ConfigModel):
    persist_directory: Path = Path("data/chroma")
    collection_prefix: str = Field(default="documents", pattern=r"^[a-z][a-z0-9_-]{2,40}$")
    write_batch_size: int = Field(default=512, ge=1, le=5000)
    hnsw: HnswConfig = Field(default_factory=HnswConfig)


class RetrievalConfig(ConfigModel):
    top_k: int = Field(default=5, ge=1)
    max_top_k: int = Field(default=50, ge=1, le=1000)
    max_distance: float | None = Field(default=None, ge=0)


class ReindexConfig(ConfigModel):
    force_reparse: bool = False
    force_reembed: bool = False


class DocumentConfig(ConfigModel):
    investment: InvestmentConfig = Field(default_factory=InvestmentConfig)
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    parsing: ParsingConfig = Field(default_factory=ParsingConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    embeddings: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    chroma: ChromaConfig = Field(default_factory=ChromaConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    reindex: ReindexConfig = Field(default_factory=ReindexConfig)

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        if self.chunking.max_tokens > self.embeddings.max_input_tokens:
            raise ValueError("chunking.max_tokens exceeds embeddings.max_input_tokens")
        if self.embeddings.max_input_tokens > self.embeddings.max_batch_tokens:
            raise ValueError("max_batch_tokens must accommodate one max_input_tokens input")
        if self.retrieval.top_k > self.retrieval.max_top_k:
            raise ValueError("retrieval.top_k exceeds max_top_k")
        if self.embeddings.model == "text-embedding-3-small" and self.embeddings.dimensions > 1536:
            raise ValueError("text-embedding-3-small supports at most 1536 dimensions")
        return self

    def parser_signature(self) -> str:
        return fingerprint(
            {"parsing": self.parsing.model_dump(mode="json"), "docling": version("docling")}
        )

    def index_signature(self) -> str:
        return fingerprint(
            {
                "parser": self.parser_signature(),
                "chunking": self.chunking.model_dump(),
                "docling_core": version("docling-core"),
                "embeddings": self.embeddings.signature(),
                "hnsw": self.chroma.hnsw.model_dump(),
                "prefix": self.chroma.collection_prefix,
            }
        )


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def resolve_path(path: Path) -> Path:
    return (PROJECT_ROOT / path).resolve()


def load_config(path: Path) -> DocumentConfig:
    with resolve_path(path).open(encoding="utf-8") as handle:
        return DocumentConfig.model_validate(yaml.safe_load(handle) or {})
