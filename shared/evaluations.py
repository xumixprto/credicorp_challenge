"""Transportable run and evaluation records; nested CSV cells contain JSON."""

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from shared.conversations import utc_now


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunRecord(EvaluationModel):
    schema_version: int = 1
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    request_id: str
    user_id: str
    session_id: str
    message_type: str
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    duration_ms: float | None = None
    model: str
    status: Literal["running", "completed", "approval_required", "error"] = "running"
    query: str
    answer: str = ""
    input_json: dict[str, Any] = Field(default_factory=dict)
    context_json: dict[str, Any] = Field(default_factory=dict)
    response_json: list[dict[str, Any]] = Field(default_factory=list)
    tools_json: list[dict[str, Any]] = Field(default_factory=list)
    retrievals_json: list[dict[str, Any]] = Field(default_factory=list)
    usage_json: dict[str, Any] = Field(default_factory=dict)
    configuration_json: dict[str, Any] = Field(default_factory=dict)
    error_type: str | None = None


class RetrievalReference(EvaluationModel):
    retrieval_id: str
    # Includes ALL known relevant chunks, including ones the retriever missed.
    relevance: dict[str, StrictInt]
    complete: bool = True

    @model_validator(mode="after")
    def valid_grades(self):
        if any(isinstance(v, bool) or v not in range(4) for v in self.relevance.values()):
            raise ValueError("Relevance grades must be integers from 0 to 3")
        return self


class RunReference(EvaluationModel):
    run_id: str
    expected_answer: str | None = None
    retrievals: list[RetrievalReference] = Field(default_factory=list)


class JudgeRequest(EvaluationModel):
    run_ids: list[str] | None = Field(default=None, min_length=1)
    missing_only: bool = True
    k: int | None = Field(default=None, ge=1, le=100)
    references: list[RunReference] = Field(default_factory=list)


class EvaluationRecord(EvaluationModel):
    schema_version: int = 2
    evaluation_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    evaluated_at: datetime = Field(default_factory=utc_now)
    judge_model: str
    rubric_version: str
    k: int
    status: Literal["completed", "not_applicable", "error"]
    precision_at_k: float | None = None
    recall_at_k: float | None = None
    ndcg_at_k: float | None = None
    hallucination: float | None = None
    groundedness: float | None = None
    tool_usage: float | None = Field(default=None, ge=0, le=1)
    intention_success: float | None = Field(default=None, ge=0, le=1)
    claim_count: int = 0
    summary: str = ""
    retrieval_metrics_json: list[dict[str, Any]] = Field(default_factory=list)
    assessment_json: dict[str, Any] = Field(default_factory=dict)
    reference_json: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float | None = None
    error_type: str | None = None


class JudgeResponse(EvaluationModel):
    selected: int
    skipped: int
    evaluations: list[EvaluationRecord]


class EvaluationRow(EvaluationModel):
    run_id: str
    user_id: str
    session_id: str
    started_at: datetime
    message_type: str
    run_status: str
    query: str
    answer: str
    evaluation_status: str = "missing"
    evaluation: EvaluationRecord | None = None


class EvaluationTable(EvaluationModel):
    total: int
    missing: int
    rows: list[EvaluationRow]
