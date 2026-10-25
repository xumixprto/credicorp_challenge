import json
from typing import Any

from openai import AsyncOpenAI

from backend.app.evaluation.config import EvaluationConfig
from backend.app.evaluation.errors import JudgeOutputError
from backend.app.evaluation.models import BatchJudgment, RunJudgment
from backend.app.evaluation.prompts import JUDGE_INSTRUCTIONS
from backend.app.evaluation.schema import batch_inputs, build_batch_schema, decode_batch
from shared.evaluations import RunRecord, RunReference


def unique_hits(retrieval: dict) -> list[dict]:
    return list(
        {hit["chunk_id"]: hit for hit in retrieval.get("raw_response", {}).get("hits", [])}.values()
    )


def prepare_input(run: RunRecord, reference: RunReference | None) -> dict[str, Any]:
    evidence = []

    # Prior tool results support follow-up questions and approval continuations.
    def history_evidence(value: object) -> None:
        if isinstance(value, dict):
            if value.get("type") == "function_result" and not value.get("exception"):
                evidence.append({"evidence_id": f"history:{len(evidence)}", "content": value})
                return
            for item in value.values():
                history_evidence(item)
        elif isinstance(value, list):
            for item in value:
                history_evidence(item)

    history_evidence(run.context_json)
    for retrieval in run.retrievals_json:
        for chunk in retrieval.get("visible_evidence", {}).get("results", []):
            evidence.append(
                {
                    "evidence_id": f"{retrieval['retrieval_id']}:{chunk['chunk_id']}",
                    "content": chunk,
                }
            )
    for tool in run.tools_json:
        if tool["name"] != "retrieve_documents" and tool["status"] == "completed":
            evidence.append({"evidence_id": f"tool:{tool['tool_call_id']}", "content": tool})
    return {
        "run_id": run.run_id,
        "question": run.query,
        "input": run.input_json,
        "history": run.context_json,
        "run_status": run.status,
        "tool_calls": run.tools_json,
        "agent_response": run.response_json,
        "available_tools": run.configuration_json.get("available_tools"),
        "behavior_evidence_ids": [
            "question",
            "input",
            "history",
            "final_answer",
            "agent_response",
            *[f"tool_call:{tool['tool_call_id']}" for tool in run.tools_json],
            *(["available_tools"] if "available_tools" in run.configuration_json else []),
        ],
        "final_answer": run.answer,
        "evidence": evidence,
        "reference_answer": reference.expected_answer if reference else None,
        "retrievals": [
            {
                "retrieval_id": item["retrieval_id"],
                "query": item["query"],
                "chunks": unique_hits(item),
            }
            for item in run.retrievals_json
            if "raw_response" in item
        ],
    }


def normalize_evidence_ids(value: RunJudgment, payload: dict) -> None:
    """Resolve only exact, unambiguous aliases from evidence actually supplied to the judge."""

    def aliases(behavior: bool) -> dict[str, set[str]]:
        candidates: dict[str, set[str]] = {}

        def add(alias: object, target: str) -> None:
            if isinstance(alias, str) and alias:
                candidates.setdefault(alias, set()).add(target)

        for evidence in payload["evidence"]:
            content = evidence["content"]
            if isinstance(content, dict):
                keys = (
                    ("citation_id", "chunk_id")
                    if behavior
                    else ("citation_id", "chunk_id", "tool_call_id")
                )
                for key in keys:
                    add(content.get(key), evidence["evidence_id"])
        if behavior:
            for tool in payload["tool_calls"]:
                add(tool["tool_call_id"], f"tool_call:{tool['tool_call_id']}")
        return candidates

    answer_ids = {e["evidence_id"] for e in payload["evidence"]}
    if payload["reference_answer"] is not None:
        answer_ids.add("reference_answer")
    for judgments, known, mapping in (
        (value.claims, answer_ids, aliases(False)),
        (
            [value.tool_usage, value.intention_success],
            answer_ids | set(payload["behavior_evidence_ids"]),
            aliases(True),
        ),
    ):
        for judgment in judgments:
            judgment.evidence_ids = [
                next(iter(mapping[ref]))
                if ref not in known and len(mapping.get(ref, set())) == 1
                else ref
                for ref in judgment.evidence_ids
            ]


def validate_judgment(value: RunJudgment, payload: dict) -> None:
    expected = {
        item["retrieval_id"]: {c["chunk_id"] for c in item["chunks"]}
        for item in payload["retrievals"]
    }
    if len(value.retrievals) != len(expected) or {r.retrieval_id for r in value.retrievals} != set(
        expected
    ):
        raise JudgeOutputError("Judge omitted, duplicated, or invented retrieval IDs.")
    for retrieval in value.retrievals:
        ids = [item.chunk_id for item in retrieval.chunks]
        if len(ids) != len(set(ids)) or set(ids) != expected[retrieval.retrieval_id]:
            raise JudgeOutputError("Judge omitted, duplicated, or invented chunk IDs.")
    evidence_ids = {item["evidence_id"] for item in payload["evidence"]}
    if payload["reference_answer"] is not None:
        evidence_ids.add("reference_answer")
    for index, claim in enumerate(value.claims):
        if set(claim.evidence_ids) - evidence_ids:
            raise JudgeOutputError(
                f"claims[{index}].evidence_ids contains IDs outside the supplied answer evidence."
            )
        if claim.verdict != "unsupported" and not claim.evidence_ids:
            raise JudgeOutputError(
                f"claims[{index}] is supported/contradicted but has no evidence IDs."
            )
    behavior_ids = evidence_ids | set(payload["behavior_evidence_ids"])
    for name in ("tool_usage", "intention_success"):
        assessment = getattr(value, name)
        if set(assessment.evidence_ids) - behavior_ids:
            raise JudgeOutputError(
                f"{name}.evidence_ids contains IDs outside the supplied behavior/answer evidence."
            )
        if assessment.grade is not None and not assessment.evidence_ids:
            raise JudgeOutputError(f"{name} has a grade but no trace evidence IDs.")


class OpenAIJudge:
    def __init__(self, config: EvaluationConfig, api_key: str) -> None:
        self.config = config
        self.api_key = api_key

    async def assess(self, payloads: list[dict]) -> BatchJudgment:
        if not payloads:
            return BatchJudgment(runs=[])
        # The model fills required, named slots. It never generates the database run IDs.
        # Match by field name, not the order of the model's response.
        slots = {f"run_{index}": payload for index, payload in enumerate(payloads, start=1)}
        output_model = build_batch_schema(slots)
        inputs = batch_inputs(slots)
        async with AsyncOpenAI(
            api_key=self.api_key,
            timeout=self.config.timeout_seconds,
            max_retries=self.config.max_retries,
        ) as client:
            response = await client.responses.parse(
                model=self.config.judge_model,
                instructions=JUDGE_INSTRUCTIONS,
                input=json.dumps({"runs": inputs}, ensure_ascii=False),
                text_format=output_model,
                max_output_tokens=self.config.max_output_tokens,
                store=False,
            )
        if response.status != "completed":
            if (
                response.incomplete_details
                and response.incomplete_details.reason == "max_output_tokens"
            ):
                raise JudgeOutputError(
                    "Judge output reached evaluation.max_output_tokens; "
                    "increase it or reduce batch_size."
                )
            raise JudgeOutputError(
                "Judge response was incomplete or failed; no scores were accepted."
            )
        if response.output_parsed is None:
            raise JudgeOutputError("Judge returned no structured assessment (possibly a refusal).")
        return decode_batch(response.output_parsed, slots)
