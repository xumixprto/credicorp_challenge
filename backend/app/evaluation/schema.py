"""Bind all run/retrieval/chunk identities to required fields, not model-generated IDs."""

from typing import Literal

from pydantic import Field, create_model

from backend.app.evaluation.models import (
    BatchJudgment,
    BehaviorJudgment,
    ChunkAssessment,
    ClaimJudgment,
    RunAssessment,
    RunJudgment,
)
from shared.evaluations import EvaluationModel


def build_batch_schema(slots: dict[str, dict]) -> type[EvaluationModel]:
    runs = {}
    for slot, payload in slots.items():
        retrievals = {}
        for index, retrieval in enumerate(payload["retrievals"], start=1):
            retrievals[f"retrieval_{index}"] = (
                create_model(
                    f"{slot}_retrieval_{index}",
                    __base__=EvaluationModel,
                    **{
                        f"chunk_{i}": (ChunkAssessment, ...)
                        for i in range(1, len(retrieval["chunks"]) + 1)
                    },
                ),
                ...,
            )
        retrieval_model = create_model(f"{slot}_retrievals", __base__=EvaluationModel, **retrievals)
        evidence_ids = [e["evidence_id"] for e in payload["evidence"]]
        if payload["reference_answer"] is not None:
            evidence_ids.append("reference_answer")
        # With no supplied evidence only an empty claim-evidence list is valid.
        claim_ids = (
            (list[Literal[tuple(evidence_ids)]], ...)
            if evidence_ids
            else (list[str], Field(max_length=0))
        )
        claim_model = create_model(f"{slot}_claim", __base__=ClaimJudgment, evidence_ids=claim_ids)
        behavior_ids = list(dict.fromkeys([*evidence_ids, *payload["behavior_evidence_ids"]]))
        behavior_model = create_model(
            f"{slot}_behavior",
            __base__=BehaviorJudgment,
            evidence_ids=(list[Literal[tuple(behavior_ids)]], ...),
        )
        runs[slot] = (
            create_model(
                f"{slot}_assessment",
                __base__=RunAssessment,
                retrievals=(retrieval_model, ...),
                claims=(list[claim_model], ...),
                tool_usage=(behavior_model, ...),
                intention_success=(behavior_model, ...),
            ),
            ...,
        )
    return create_model("JudgeBatchOutput", __base__=EvaluationModel, **runs)


def batch_inputs(slots: dict[str, dict]) -> dict:
    return {
        slot: {
            **{k: v for k, v in payload.items() if k not in {"run_id", "retrievals"}},
            "retrievals": {
                f"retrieval_{i}": {
                    "query": retrieval["query"],
                    "chunks": {
                        f"chunk_{j}": chunk for j, chunk in enumerate(retrieval["chunks"], 1)
                    },
                }
                for i, retrieval in enumerate(payload["retrievals"], 1)
            },
        }
        for slot, payload in slots.items()
    }


def decode_batch(output: EvaluationModel, slots: dict[str, dict]) -> BatchJudgment:
    runs = []
    for slot, payload in slots.items():
        assessment = getattr(output, slot).model_dump()
        retrievals = assessment.pop("retrievals")
        runs.append(
            RunJudgment(
                run_id=payload["run_id"],
                **assessment,
                retrievals=[
                    {
                        "retrieval_id": retrieval["retrieval_id"],
                        "chunks": [
                            {
                                "chunk_id": chunk["chunk_id"],
                                **retrievals[f"retrieval_{i}"][f"chunk_{j}"],
                            }
                            for j, chunk in enumerate(retrieval["chunks"], 1)
                        ],
                    }
                    for i, retrieval in enumerate(payload["retrievals"], 1)
                ],
            )
        )
    return BatchJudgment(runs=runs)
