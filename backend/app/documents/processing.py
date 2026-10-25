"""Docling adapters and content-addressed parsing/chunking artifacts."""

import json
import logging
from importlib.metadata import version
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Protocol

import tiktoken

from backend.app.core.errors import ApplicationError
from backend.app.documents.config import DocumentConfig, fingerprint, resolve_path
from backend.app.documents.files import write_json
from backend.app.documents.models import Chunk

if TYPE_CHECKING:
    from docling_core.types.doc import DoclingDocument

logger = logging.getLogger("credicorp.backend.documents.processing")


class Parser(Protocol):
    def parse(self, path: Path, format_name: str, config: DocumentConfig) -> "DoclingDocument": ...


class Chunker(Protocol):
    def chunk(self, document: "DoclingDocument", config: DocumentConfig) -> list[Chunk]: ...


class DoclingParser:
    def __init__(self) -> None:
        self._converter = None
        self._signature = None

    def parse(self, path: Path, format_name: str, config: DocumentConfig) -> "DoclingDocument":
        from docling.datamodel.base_models import ConversionStatus, FailureCategory, InputFormat
        from docling.datamodel.pipeline_options import (
            AcceleratorOptions,
            EasyOcrOptions,
            PdfPipelineOptions,
            RapidOcrOptions,
            TableFormerMode,
            TableStructureOptions,
            TesseractCliOcrOptions,
        )
        from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption
        from docling_core.types.io import DocumentStream

        signature = config.parser_signature()
        if self._signature != signature:
            pdf = config.parsing.pdf
            ocr_args = {"force_full_page_ocr": pdf.force_full_page_ocr}
            if pdf.ocr_languages:
                ocr_args["lang"] = pdf.ocr_languages
            ocr_class = {
                "rapidocr": RapidOcrOptions,
                "easyocr": EasyOcrOptions,
                "tesseract_cli": TesseractCliOcrOptions,
            }[pdf.ocr_engine]
            options = PdfPipelineOptions(
                do_ocr=pdf.do_ocr,
                ocr_options=ocr_class(**ocr_args),
                do_table_structure=pdf.do_table_structure,
                table_structure_options=TableStructureOptions(
                    mode=TableFormerMode(pdf.table_mode),
                    do_cell_matching=pdf.do_cell_matching,
                ),
                images_scale=pdf.images_scale,
                generate_page_images=pdf.generate_page_images,
                generate_picture_images=pdf.generate_picture_images,
                document_timeout=config.parsing.timeout_seconds,
                accelerator_options=AcceleratorOptions(
                    device=config.parsing.accelerator,
                    num_threads=config.parsing.num_threads,
                ),
                artifacts_path=(
                    resolve_path(config.parsing.artifacts_path)
                    if config.parsing.artifacts_path
                    else None
                ),
                enable_remote_services=False,
                allow_external_plugins=False,
            )
            self._converter = DocumentConverter(
                allowed_formats=list(
                    {InputFormat(value) for value in config.parsing.format_map.values()}
                ),
                format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=options),
                    InputFormat.IMAGE: ImageFormatOption(pipeline_options=options),
                },
            )
            self._signature = signature
        # Canonical extensions let .txt/.markdown/.htm aliases use the configured Docling backend.
        extension = {"md": ".md", "html": ".html"}.get(format_name, path.suffix)
        started = perf_counter()
        context = {"content_hash": path.stem, "format": format_name}
        logger.info(
            "docling_conversion_started",
            extra={**context, "timeout_seconds": config.parsing.timeout_seconds},
        )
        result = self._converter.convert(
            DocumentStream(name=f"document{extension}", stream=BytesIO(path.read_bytes())),
            raises_on_error=False,
            max_num_pages=config.parsing.max_pages,
            max_file_size=int(config.ingestion.max_file_mb * 1024 * 1024),
        )
        if result.status != ConversionStatus.SUCCESS:
            logger.warning(
                "docling_conversion_failed",
                extra={
                    **context,
                    "conversion_status": result.status.value,
                    "duration_ms": round((perf_counter() - started) * 1000, 2),
                    "conversion_errors": [
                        {
                            "category": error.category.value,
                            "component": error.component_type.value,
                            "module": error.module_name,
                            "page_no": error.page_no,
                        }
                        for error in result.errors
                    ],
                },
            )
            if any(error.category == FailureCategory.TIMEOUT for error in result.errors):
                raise ApplicationError(
                    422,
                    "Docling conversion timed out. Increase parsing.timeout_seconds "
                    f"(currently {config.parsing.timeout_seconds:g}s) in config.yaml, "
                    "restart FastAPI, and retry the upload.",
                )
            raise ApplicationError(
                422,
                f"Docling could not fully convert this document (status: {result.status.value}).",
            )
        logger.info(
            "docling_conversion_completed",
            extra={
                **context,
                "page_count": len(result.document.pages),
                "duration_ms": round((perf_counter() - started) * 1000, 2),
            },
        )
        return result.document


class DoclingChunker:
    def chunk(self, document: "DoclingDocument", config: DocumentConfig) -> list[Chunk]:
        from docling.chunking import HybridChunker
        from docling_core.transforms.chunker.tokenizer.openai import OpenAITokenizer

        from backend.app.documents.serialization import ProseSerializerProvider
        from backend.app.documents.tables import contextual_tables

        encoding = tiktoken.get_encoding(config.chunking.tokenizer)
        tables = (
            contextual_tables(document, config.chunking, encoding)
            if config.chunking.table_strategy == "contextual_rows"
            else {}
        )
        chunker = HybridChunker(
            serializer_provider=ProseSerializerProvider(set(tables)),
            tokenizer=OpenAITokenizer(tokenizer=encoding, max_tokens=config.chunking.max_tokens),
            merge_peers=config.chunking.merge_peers,
            repeat_table_header=config.chunking.repeat_table_header,
            omit_header_on_overflow=config.chunking.omit_header_on_overflow,
            always_emit_headings=config.chunking.always_emit_headings,
        )
        chunks = []
        for item in chunker.chunk(dl_doc=document):
            if not item.text.strip():
                continue
            text = chunker.contextualize(item) if config.chunking.contextualize else item.text
            pages: set[int] = set()
            references = []
            for source in item.meta.doc_items:
                provenance = [entry.model_dump(mode="json") for entry in source.prov]
                pages.update(entry.page_no for entry in source.prov)
                references.append(
                    {
                        "ref": source.self_ref,
                        "label": source.label.value,
                        "provenance": provenance,
                    }
                )
            chunks.append(
                Chunk(
                    index=len(chunks),
                    text=item.text,
                    embedding_text=text,
                    token_count=len(encoding.encode(text, disallowed_special=())),
                    pages=sorted(pages),
                    headings=item.meta.headings or [],
                    references=references,
                )
            )
        chunks.extend(chunk for table in tables.values() for chunk in table)
        positions = {
            item.self_ref: index for index, (item, _) in enumerate(document.iterate_items())
        }
        chunks.sort(
            key=lambda chunk: min(
                (
                    positions.get(ref["ref"], len(positions))
                    for ref in chunk.references
                    if ref.get("role") != "table_context"
                ),
                default=len(positions),
            )
        )
        for index, chunk in enumerate(chunks):
            chunk.index = index
        if not chunks:
            raise ApplicationError(422, "No text chunks could be extracted from this document.")
        return chunks


class DocumentProcessor:
    def __init__(
        self, root: Path, parser: Parser | None = None, chunker: Chunker | None = None
    ) -> None:
        self.root = root
        self.parser = parser or DoclingParser()
        self.chunker = chunker or DoclingChunker()

    def process(
        self,
        original: Path,
        content_hash: str,
        format_name: str,
        config: DocumentConfig,
        *,
        force_reparse: bool = False,
    ) -> tuple[str, list[Chunk]]:
        from docling_core.types.doc import DoclingDocument

        parse_key = fingerprint(
            {"content": content_hash, "format": format_name, "parser": config.parser_signature()}
        )
        revision = fingerprint(
            {
                "parsed": parse_key,
                "chunking": config.chunking.model_dump(),
                "core": version("docling-core"),
            }
        )
        parsed_path = self.root / "parsed" / f"{parse_key}.json"
        chunks_path = self.root / "chunks" / f"{revision}.json"
        if chunks_path.exists() and not force_reparse:
            logger.info("document_chunks_cache_hit", extra={"content_hash": content_hash})
            return revision, [
                Chunk.model_validate(item)
                for item in json.loads(chunks_path.read_text(encoding="utf-8"))
            ]
        if parsed_path.exists() and not force_reparse:
            logger.info("document_parse_cache_hit", extra={"content_hash": content_hash})
            document = DoclingDocument.model_validate_json(parsed_path.read_text(encoding="utf-8"))
        else:
            document = self.parser.parse(original, format_name, config)
            write_json(parsed_path, document.export_to_dict())
        logger.info("document_chunking_started", extra={"content_hash": content_hash})
        chunks = self.chunker.chunk(document, config)
        write_json(chunks_path, [chunk.model_dump() for chunk in chunks])
        logger.info(
            "document_chunked", extra={"content_hash": content_hash, "chunk_count": len(chunks)}
        )
        return revision, chunks
