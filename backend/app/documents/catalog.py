"""Transactional index manifest and reusable embedding cache (SQLite/WAL)."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from backend.app.documents.models import ActiveIndex, DocumentRecord


class Catalog:
    def __init__(self, root: Path, timeout: float = 30) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "catalog.sqlite3"
        self.timeout = timeout
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS active_index (
                    id INTEGER PRIMARY KEY, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY, source_id TEXT UNIQUE NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS embeddings (
                    cache_key TEXT PRIMARY KEY, vector TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=self.timeout)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def snapshot(self) -> tuple[ActiveIndex | None, list[DocumentRecord]]:
        # Read the active collection and source revisions from the same committed snapshot.
        with self.connect() as connection:
            connection.execute("BEGIN")
            active = connection.execute("SELECT data FROM active_index WHERE id=1").fetchone()
            rows = connection.execute("SELECT data FROM documents ORDER BY source_id").fetchall()
        return (
            ActiveIndex.model_validate_json(active[0]) if active else None,
            [DocumentRecord.model_validate_json(row[0]) for row in rows],
        )

    def commit(self, active: ActiveIndex, documents: list[DocumentRecord]) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO active_index VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (active.model_dump_json(),),
            )
            connection.executemany(
                "INSERT INTO documents VALUES (?, ?, ?) ON CONFLICT(document_id) DO UPDATE SET "
                "source_id=excluded.source_id, data=excluded.data",
                [(doc.document_id, doc.source_id, doc.model_dump_json()) for doc in documents],
            )

    def get_embeddings(self, keys: list[str]) -> dict[str, list[float]]:
        results = {}
        with self.connect() as connection:
            for start in range(0, len(keys), 500):
                batch = keys[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"SELECT cache_key, vector FROM embeddings WHERE cache_key IN ({placeholders})",
                    batch,
                ).fetchall()
                results.update((key, json.loads(vector)) for key, vector in rows)
        return results

    def put_embeddings(self, vectors: dict[str, list[float]]) -> None:
        with self.connect() as connection:
            connection.executemany(
                "INSERT INTO embeddings VALUES (?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET vector=excluded.vector",
                [(key, json.dumps(vector, allow_nan=False)) for key, vector in vectors.items()],
            )
