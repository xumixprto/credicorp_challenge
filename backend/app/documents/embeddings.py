"""Token-aware embedding batches, bounded parallelism, and durable content caching."""

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Protocol

import tiktoken
from openai import APIError, AsyncOpenAI

from backend.app.core.errors import ApplicationError
from backend.app.documents.catalog import Catalog
from backend.app.documents.config import EmbeddingConfig, fingerprint

logger = logging.getLogger("credicorp.backend.documents.embeddings")


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]
    embedded: int = 0
    cached: int = 0


class Embedder(Protocol):
    async def embed(
        self, texts: list[str], config: EmbeddingConfig, tokenizer: str, *, force: bool = False
    ) -> EmbeddingResult: ...


class OpenAIEmbedder:
    def __init__(self, catalog: Catalog, api_key: str) -> None:
        self.catalog = catalog
        self.api_key = api_key

    async def embed(
        self, texts: list[str], config: EmbeddingConfig, tokenizer: str, *, force: bool = False
    ) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult([])
        encoding = await asyncio.to_thread(tiktoken.get_encoding, tokenizer)
        keys = [fingerprint({"provider": config.signature(), "text": text}) for text in texts]
        unique = dict(zip(keys, texts, strict=True))
        token_counts = {
            key: len(encoding.encode(text, disallowed_special=())) for key, text in unique.items()
        }
        if any(
            not text.strip() or token_counts[key] > config.max_input_tokens
            for key, text in unique.items()
        ):
            raise ApplicationError(
                422, "An embedding input is empty or exceeds the configured token limit."
            )
        cached = (
            await asyncio.to_thread(self.catalog.get_embeddings, list(unique))
            if config.cache_enabled and not force
            else {}
        )
        missing = [(key, text) for key, text in unique.items() if key not in cached]
        if missing and not self.api_key.strip():
            raise ApplicationError(
                503, "Set OPENAI_API_KEY in .env and restart FastAPI to generate embeddings."
            )
        batches: list[list[tuple[str, str]]] = []
        batch: list[tuple[str, str]] = []
        tokens = 0
        for key, text in missing:
            count = token_counts[key]
            if batch and (
                len(batch) >= config.batch_size or tokens + count > config.max_batch_tokens
            ):
                batches.append(batch)
                batch, tokens = [], 0
            batch.append((key, text))
            tokens += count
        if batch:
            batches.append(batch)

        if batches:
            semaphore = asyncio.Semaphore(config.max_concurrency)
            async with AsyncOpenAI(
                api_key=self.api_key,
                base_url=str(config.base_url),
                timeout=config.timeout_seconds,
                max_retries=config.max_retries,
            ) as client:

                async def run_batch(items: list[tuple[str, str]]) -> dict[str, list[float]]:
                    async with semaphore:
                        try:
                            response = await client.embeddings.create(
                                model=config.model,
                                dimensions=config.dimensions,
                                input=[text for _, text in items],
                                encoding_format="float",
                            )
                        except APIError as exc:
                            logger.warning(
                                "embedding_request_failed", extra={"error_type": type(exc).__name__}
                            )
                            raise ApplicationError(
                                502,
                                "OpenAI embedding request failed. Check the key, quota, and model.",
                            ) from None
                        ordered = sorted(response.data, key=lambda item: item.index)
                        if [item.index for item in ordered] != list(range(len(items))):
                            raise ApplicationError(
                                502, "OpenAI returned an incomplete embedding batch."
                            )
                        vectors = {
                            key: item.embedding
                            for (key, _), item in zip(items, ordered, strict=True)
                        }
                        if any(
                            len(vector) != config.dimensions
                            or not all(math.isfinite(value) for value in vector)
                            for vector in vectors.values()
                        ):
                            raise ApplicationError(
                                502, "OpenAI returned invalid embedding dimensions or values."
                            )
                        if config.cache_enabled:
                            await asyncio.to_thread(self.catalog.put_embeddings, vectors)
                        logger.info("embedding_batch_completed", extra={"chunk_count": len(items)})
                        return vectors

                # Wait for every request before closing the shared HTTP client, even on failure.
                results = await asyncio.gather(
                    *(run_batch(batch) for batch in batches), return_exceptions=True
                )
                for result in results:
                    if isinstance(result, BaseException):
                        raise result
                    cached.update(result)
        return EmbeddingResult(
            vectors=[cached[key] for key in keys],
            embedded=len(missing),
            cached=len(unique) - len(missing),
        )
