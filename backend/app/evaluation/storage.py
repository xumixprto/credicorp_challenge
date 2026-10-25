"""Locked, atomic CSV tables. JSON-encoded cells preserve types and multiline content."""

import csv
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Literal, TypeVar

from filelock import AsyncFileLock, FileLock
from pydantic import BaseModel

from shared.evaluations import EvaluationRecord, RunRecord

Record = TypeVar("Record", bound=BaseModel)
# Retrieval text and history can be much larger than csv's default 128 KiB cell limit.
csv.field_size_limit(256 * 1024 * 1024)


class EvaluationStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _read(self, name: str, model: type[Record]) -> list[Record]:
        path = self.directory / name
        if not path.exists():
            return []
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = list(model.model_fields)
            # Explicit v1 compatibility: preserve old rows; the next atomic write adds columns.
            legacy_fields = [f for f in fields if f not in {"tool_usage", "intention_success"}]
            legacy = model is EvaluationRecord and reader.fieldnames == legacy_fields
            if reader.fieldnames != fields and not legacy:
                raise ValueError(f"Unexpected CSV schema in {name}")
            records = []
            for row in reader:
                decoded = {
                    key: json.loads(value) if key.endswith("_json") else value
                    for key, value in row.items()
                    if value != ""
                }
                records.append(model.model_validate(decoded))
            return records

    def _write(self, name: str, records: list[Record], model: type[Record]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=self.directory,
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                writer = csv.DictWriter(handle, fieldnames=list(model.model_fields))
                writer.writeheader()
                for record in records:
                    writer.writerow(
                        {
                            key: json.dumps(value, ensure_ascii=False)
                            if key.endswith("_json")
                            else value
                            for key, value in record.model_dump(mode="json").items()
                        }
                    )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.directory / name)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _lock(self) -> FileLock:
        self.directory.mkdir(parents=True, exist_ok=True)
        return FileLock(self.directory / ".tables.lock", timeout=5)

    def judge_lock(self) -> AsyncFileLock:
        self.directory.mkdir(parents=True, exist_ok=True)
        return AsyncFileLock(self.directory / ".judge.lock", timeout=0)

    def snapshot(self) -> tuple[list[RunRecord], list[EvaluationRecord]]:
        with self._lock():
            return self._read("runs.csv", RunRecord), self._read(
                "evaluations.csv", EvaluationRecord
            )

    def export_csv(self, table_name: Literal["runs", "evaluations"]) -> bytes:
        models = {"runs": RunRecord, "evaluations": EvaluationRecord}
        model = models[table_name]
        with self._lock():
            path = self.directory / f"{table_name}.csv"
            if path.exists():
                return path.read_bytes()
            # A table with no records can still be exported with its column headers.
            content = io.StringIO(newline="")
            csv.writer(content).writerow(model.model_fields)
            return content.getvalue().encode("utf-8")

    def save_run(self, record: RunRecord) -> None:
        with self._lock():
            records = self._read("runs.csv", RunRecord)
            records = [item for item in records if item.run_id != record.run_id]
            records.append(record)
            self._write("runs.csv", records, RunRecord)

    def save_evaluations(self, records: list[EvaluationRecord]) -> None:
        with self._lock():
            previous = self._read("evaluations.csv", EvaluationRecord)
            # One latest attempt per run, including failures. Also compact older append-only files.
            latest = {item.run_id: item for item in [*previous, *records]}
            self._write("evaluations.csv", list(latest.values()), EvaluationRecord)
