"""Coordinate incremental ingestion and publish complete replacement indexes atomically."""

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable
from contextlib import suppress
from pathlib import Path
from typing import TypeVar
from uuid import NAMESPACE_URL, uuid5

from filelock import AsyncFileLock, Timeout
from yaml import YAMLError

from backend.app.core.errors import ApplicationError
from backend.app.documents.catalog import Catalog
from backend.app.documents.config import DocumentConfig, load_config, resolve_path
from backend.app.documents.embeddings import Embedder, EmbeddingResult, OpenAIEmbedder
from backend.app.documents.files import atomic_write
from backend.app.documents.models import (
    ActiveIndex,
    Chunk,
    CitationSource,
    DocumentRecord,
    IngestResponse,
    IngestResult,
    ReindexRequest,
    ReindexResponse,
    RetrievalHit,
    RetrieveRequest,
    RetrieveResponse,
    UploadedDocument,
)
from backend.app.documents.processing import DocumentProcessor
from backend.app.documents.vectors import ChromaIndex

logger = logging.getLogger("credicorp.backend.documents")
T = TypeVar("T")


class DocumentService:
    def __init__(
        self,
        config_path: Path,
        api_key: str,
        *,
        config: DocumentConfig | None = None,
        processor: DocumentProcessor | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self.config_path = config_path
        self.config = config or load_config(config_path)
        self.root = resolve_path(self.config.storage.root)
        self.catalog = Catalog(self.root, self.config.storage.sqlite_timeout_seconds)
        self.processor = processor or DocumentProcessor(self.root)
        self.embedder = embedder or OpenAIEmbedder(self.catalog, api_key)
        self.vectors = ChromaIndex(self.config)
        logger.info("document_storage_opened")

    def close(self) -> None:
        self.vectors.close()
        logger.info("document_storage_closed")

    def _lock(self) -> AsyncFileLock:
        return AsyncFileLock(
            self.root / "writer.lock",
            timeout=self.config.storage.lock_timeout_seconds,
            preserve_lock_file=True,
        )

    @staticmethod
    async def _finish_mutation(operation: Awaitable[T]) -> T:
        # A disconnected HTTP client must not release the file lock while a worker still writes.
        task = asyncio.ensure_future(operation)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await task
            raise

    def _prepare(
        self, upload: UploadedDocument, config: DocumentConfig
    ) -> tuple[DocumentRecord, list[Chunk]]:
        extension = Path(upload.filename).suffix.lower().lstrip(".")
        format_name = config.parsing.format_map.get(extension)
        if format_name is None:
            raise ApplicationError(422, "Unsupported file extension; check parsing.format_map.")
        if not upload.content:
            raise ApplicationError(422, "The uploaded document is empty.")
        content_hash = hashlib.sha256(upload.content).hexdigest()
        original_path = Path("originals") / f"{content_hash}.{extension}"
        original = self.root / original_path
        if not original.exists():
            atomic_write(original, upload.content)
        logger.info(
            "document_processing_started",
            extra={
                "source_id": upload.source_id,
                "document_name": upload.filename,
                "content_hash": content_hash,
            },
        )
        revision, chunks = self.processor.process(original, content_hash, format_name, config)
        return DocumentRecord(
            document_id=str(uuid5(NAMESPACE_URL, upload.source_id)),
            source_id=upload.source_id,
            filename=upload.filename,
            source_uri=upload.source_uri,
            content_hash=content_hash,
            original_path=original_path.as_posix(),
            format=format_name,
            revision=revision,
            chunk_count=len(chunks),
        ), chunks

    async def _write_vectors(
        self,
        name: str,
        chunks: dict[str, tuple[str, list[Chunk]]],
        config: DocumentConfig,
        *,
        force_reembed: bool = False,
    ) -> EmbeddingResult:
        entries = [
            (revision, content_hash, chunk)
            for revision, (content_hash, items) in chunks.items()
            for chunk in items
        ]
        logger.info(
            "document_embeddings_started",
            extra={"collection": name, "chunk_count": len(entries)},
        )
        result = await self.embedder.embed(
            [chunk.embedding_text for _, _, chunk in entries],
            config.embeddings,
            config.chunking.tokenizer,
            force=force_reembed,
        )
        logger.info("document_index_write_started", extra={"collection": name})
        await asyncio.to_thread(
            self.vectors.upsert, name, entries, result.vectors, config.chroma.write_batch_size
        )
        logger.info("document_index_write_completed", extra={"collection": name})
        return result

    async def _discard_staging(self, name: str) -> None:
        try:
            await asyncio.to_thread(self.vectors.delete_staging, name)
        except Exception as exc:
            logger.warning(
                "staging_cleanup_failed",
                extra={"collection": name, "error_type": type(exc).__name__},
            )

    async def ingest(self, uploads: list[UploadedDocument]) -> IngestResponse:
        try:
            async with self._lock():
                return await self._finish_mutation(self._ingest(uploads))
        except Timeout:
            raise ApplicationError(
                409, "Another ingestion or reindex is running. Please retry."
            ) from None

    async def _ingest(self, uploads: list[UploadedDocument]) -> IngestResponse:
        logger.info("ingestion_started", extra={"document_count": len(uploads)})
        config = self.config
        active, existing = await asyncio.to_thread(self.catalog.snapshot)
        if active and active.signature != config.index_signature():
            raise ApplicationError(
                409, "Index settings changed. Run /docs/reindex before ingesting."
            )
        if len({upload.source_id for upload in uploads}) != len(uploads):
            raise ApplicationError(422, "Each file in a batch must have a unique source_id.")
        by_source = {doc.source_id: doc for doc in existing}
        known_revisions = {doc.revision for doc in existing}
        new_chunks: dict[str, tuple[str, list[Chunk]]] = {}
        updates = []
        results = []
        for upload in uploads:
            try:
                record, chunks = await asyncio.to_thread(self._prepare, upload, config)
                previous = by_source.get(record.source_id)
                if previous and previous.revision == record.revision:
                    status = "unchanged"
                elif previous:
                    status = "updated"
                elif record.revision in known_revisions:
                    status = "duplicate"
                else:
                    status = "ingested"
                if record.revision not in known_revisions:
                    new_chunks[record.revision] = (record.content_hash, chunks)
                known_revisions.add(record.revision)
                updates.append(record)
                results.append(
                    IngestResult(
                        filename=record.filename,
                        source_id=record.source_id,
                        document_id=record.document_id,
                        status=status,
                        chunks=len(chunks),
                    )
                )
            except Exception as exc:
                message = (
                    exc.detail
                    if isinstance(exc, ApplicationError)
                    else "Document parsing or chunking failed."
                )
                logger.warning(
                    "document_processing_failed",
                    extra={
                        "error_type": type(exc).__name__,
                        "document_name": upload.filename,
                        "source_id": upload.source_id,
                        "reason": message,
                    },
                )
                if not config.ingestion.skip_failed_files:
                    raise ApplicationError(422, message) from None
                results.append(
                    IngestResult(
                        filename=upload.filename,
                        source_id=upload.source_id,
                        status="failed",
                        error=message,
                    )
                )
        if not updates:
            return IngestResponse(collection=active.collection if active else None, results=results)

        is_new = active is None
        collection = (
            active.collection if active else await asyncio.to_thread(self.vectors.create, config)
        )
        next_index = active or ActiveIndex(
            collection=collection, signature=config.index_signature(), config=config
        )
        try:
            embedded = await self._write_vectors(collection, new_chunks, config)
            await asyncio.to_thread(self.catalog.commit, next_index, updates)
        except BaseException:
            if is_new:
                await self._discard_staging(collection)
            raise
        logger.info(
            "ingestion_committed", extra={"collection": collection, "document_count": len(updates)}
        )
        return IngestResponse(
            collection=collection,
            results=results,
            embedded_texts=embedded.embedded,
            cached_texts=embedded.cached,
        )

    async def reindex(self, options: ReindexRequest) -> ReindexResponse:
        try:
            async with self._lock():
                return await self._finish_mutation(self._reindex(options))
        except Timeout:
            raise ApplicationError(
                409, "Another ingestion or reindex is running. Please retry."
            ) from None

    async def _reindex(self, options: ReindexRequest) -> ReindexResponse:
        try:
            config = await asyncio.to_thread(load_config, self.config_path)
        except (ValueError, OSError, YAMLError) as exc:
            logger.warning("document_config_invalid", extra={"error_type": type(exc).__name__})
            raise ApplicationError(
                422, "config.yaml is invalid; the active index was not changed."
            ) from None
        if resolve_path(config.storage.root) != self.root or resolve_path(
            config.chroma.persist_directory
        ) != resolve_path(self.config.chroma.persist_directory):
            raise ApplicationError(
                409, "Storage paths cannot change during reindex. Restart the API to use new paths."
            )
        active, documents = await asyncio.to_thread(self.catalog.snapshot)
        force_parse = (
            options.force_reparse
            if options.force_reparse is not None
            else config.reindex.force_reparse
        )
        force_embed = (
            options.force_reembed
            if options.force_reembed is not None
            else config.reindex.force_reembed
        )
        prepared: dict[tuple[str, str], tuple[str, list[Chunk]]] = {}
        chunks: dict[str, tuple[str, list[Chunk]]] = {}
        updates = []
        for doc in documents:
            key = (doc.content_hash, doc.format)
            if key not in prepared:
                original = self.root / doc.original_path
                if not original.is_file():
                    raise ApplicationError(
                        409, "An original document is missing; the active index was not changed."
                    )
                content_hash = await asyncio.to_thread(
                    lambda p=original: hashlib.sha256(p.read_bytes()).hexdigest()
                )
                if content_hash != doc.content_hash:
                    raise ApplicationError(
                        409, "An original document was modified; upload it again before reindexing."
                    )
                prepared[key] = await asyncio.to_thread(
                    self.processor.process,
                    original,
                    doc.content_hash,
                    doc.format,
                    config,
                    force_reparse=force_parse,
                )
            revision, items = prepared[key]
            chunks[revision] = (doc.content_hash, items)
            updates.append(doc.model_copy(update={"revision": revision, "chunk_count": len(items)}))

        collection = await asyncio.to_thread(self.vectors.create, config)
        try:
            embedded = await self._write_vectors(
                collection, chunks, config, force_reembed=force_embed
            )
            next_index = ActiveIndex(
                collection=collection, signature=config.index_signature(), config=config
            )
            await asyncio.to_thread(self.catalog.commit, next_index, updates)
        except BaseException:
            await self._discard_staging(collection)
            raise
        # Committed generations are retained so requests already reading them remain valid.
        self.config = config
        self.catalog.timeout = config.storage.sqlite_timeout_seconds
        logger.info(
            "reindex_committed", extra={"collection": collection, "document_count": len(updates)}
        )
        return ReindexResponse(
            collection=collection,
            previous_collection=active.collection if active else None,
            documents=len(updates),
            chunks=sum(len(items) for _, items in chunks.values()),
            embedded_texts=embedded.embedded,
            cached_texts=embedded.cached,
        )

    async def retrieve(self, request: RetrieveRequest) -> RetrieveResponse:
        top_k = request.top_k or self.config.retrieval.top_k
        if top_k > self.config.retrieval.max_top_k or not request.query.strip():
            raise ApplicationError(422, "Query is blank or top_k exceeds retrieval.max_top_k.")
        active, documents = await asyncio.to_thread(self.catalog.snapshot)
        metric = active.config.chroma.hnsw.space if active else self.config.chroma.hnsw.space
        if request.document_ids is not None:
            documents = [doc for doc in documents if doc.document_id in request.document_ids]
        if not active or not documents:
            return RetrieveResponse(
                collection=active.collection if active else None, distance_metric=metric, hits=[]
            )
        sources: dict[str, list[DocumentRecord]] = {}
        for doc in documents:
            sources.setdefault(doc.revision, []).append(doc)
        # Query with the active generation's model/dimensions, including during reindexing.
        embedded = await self.embedder.embed(
            [request.query], active.config.embeddings, active.config.chunking.tokenizer
        )
        matches = await asyncio.to_thread(
            self.vectors.query, active.collection, embedded.vectors[0], list(sources), top_k
        )
        maximum = (
            request.max_distance
            if request.max_distance is not None
            else self.config.retrieval.max_distance
        )
        hits = []
        for chunk_id, text, metadata, distance in zip(
            matches["ids"][0],
            matches["documents"][0],
            matches["metadatas"][0],
            matches["distances"][0],
            strict=True,
        ):
            if maximum is not None and distance > maximum:
                continue
            citation = json.loads(metadata["citation_json"])
            hits.append(
                RetrievalHit(
                    chunk_id=chunk_id,
                    text=text,
                    distance=distance,
                    pages=citation["pages"],
                    headings=citation["headings"],
                    references=citation["references"],
                    sources=[
                        CitationSource.model_validate(
                            doc.model_dump(include=set(CitationSource.model_fields))
                        )
                        for doc in sources[metadata["revision"]]
                    ],
                )
            )
        return RetrieveResponse(collection=active.collection, distance_metric=metric, hits=hits)
