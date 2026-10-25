"""One disk-backed Chroma client, owned by the FastAPI application lifespan."""

import json
from typing import Any
from uuid import uuid4

import chromadb
from chromadb.config import Settings as ChromaSettings

from backend.app.documents.config import DocumentConfig, resolve_path
from backend.app.documents.models import Chunk


class ChromaIndex:
    def __init__(self, config: DocumentConfig) -> None:
        self.client = chromadb.PersistentClient(
            path=str(resolve_path(config.chroma.persist_directory)),
            settings=ChromaSettings(anonymized_telemetry=False),
        )

    def close(self) -> None:
        self.client.close()

    def create(self, config: DocumentConfig) -> str:
        name = f"{config.chroma.collection_prefix}_{uuid4().hex}"
        self.client.create_collection(
            name=name,
            embedding_function=None,
            configuration={"hnsw": config.chroma.hnsw.model_dump()},
        )
        return name

    def delete_staging(self, name: str) -> None:
        self.client.delete_collection(name)

    def upsert(
        self,
        name: str,
        revisions: list[tuple[str, str, Chunk]],
        embeddings: list[list[float]],
        batch_size: int,
    ) -> None:
        collection = self.client.get_collection(name=name, embedding_function=None)
        size = min(batch_size, self.client.get_max_batch_size())
        for start in range(0, len(revisions), size):
            batch = revisions[start : start + size]
            collection.upsert(
                ids=[f"{revision}:{chunk.index}" for revision, _, chunk in batch],
                embeddings=embeddings[start : start + size],
                documents=[chunk.text for _, _, chunk in batch],
                metadatas=[
                    {
                        "revision": revision,
                        "content_hash": content_hash,
                        "citation_json": json.dumps(
                            chunk.model_dump(exclude={"text", "embedding_text"})
                        ),
                    }
                    for revision, content_hash, chunk in batch
                ],
            )

    def query(
        self, name: str, embedding: list[float], revisions: list[str], top_k: int
    ) -> dict[str, Any]:
        collection = self.client.get_collection(name=name, embedding_function=None)
        return collection.query(
            query_embeddings=[embedding],
            n_results=top_k,
            where={"revision": {"$in": revisions}},
            include=["documents", "metadatas", "distances"],
        )
