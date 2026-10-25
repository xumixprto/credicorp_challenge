from typing import Literal

from pydantic import Field

from shared.evaluations import EvaluationModel


class ChunkAssessment(EvaluationModel):
    relevance: Literal[0, 1, 2, 3]
    reason: str


class ChunkJudgment(ChunkAssessment):
    chunk_id: str


class RetrievalJudgment(EvaluationModel):
    retrieval_id: str
    chunks: list[ChunkJudgment]


class ClaimJudgment(EvaluationModel):
    claim: str
    verdict: Literal["supported", "unsupported", "contradicted"]
    evidence_ids: list[str]
    reason: str


class BehaviorJudgment(EvaluationModel):
    grade: Literal[0, 1, 2] | None
    reason: str = Field(min_length=1)
    evidence_ids: list[str]


class RunAssessment(EvaluationModel):
    retrievals: list[RetrievalJudgment]
    claims: list[ClaimJudgment]
    tool_usage: BehaviorJudgment
    intention_success: BehaviorJudgment
    summary: str


class RunJudgment(RunAssessment):
    # Assigned by the application after matching a required response field to its input.
    run_id: str


class BatchJudgment(EvaluationModel):
    runs: list[RunJudgment]
