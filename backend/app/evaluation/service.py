import asyncio
import json
import logging
from statistics import mean
from time import perf_counter

import tiktoken
from filelock import Timeout
from pydantic import ValidationError

from backend.app.core.errors import ApplicationError
from backend.app.evaluation.config import EvaluationConfig
from backend.app.evaluation.errors import JudgeOutputError
from backend.app.evaluation.judge import (
    OpenAIJudge,
    normalize_evidence_ids,
    prepare_input,
    unique_hits,
    validate_judgment,
)
from backend.app.evaluation.metrics.behavior import behavior_score
from backend.app.evaluation.metrics.grounding import grounding_scores
from backend.app.evaluation.metrics.retrieval import ndcg_at_k, precision_at_k, recall_at_k
from backend.app.evaluation.models import RunJudgment
from backend.app.evaluation.prompts import JUDGE_INSTRUCTIONS, RUBRIC_VERSION
from backend.app.evaluation.schema import build_batch_schema
from backend.app.evaluation.storage import EvaluationStore
from shared.evaluations import (
    EvaluationRecord,
    EvaluationRow,
    EvaluationTable,
    JudgeRequest,
    JudgeResponse,
    RunRecord,
    RunReference,
)

logger = logging.getLogger("credicorp.backend.judge")


class JudgeService:
    def __init__(
        self, store: EvaluationStore, config: EvaluationConfig, judge: OpenAIJudge
    ) -> None:
        self.store, self.config, self.judge = store, config, judge

    def table(self, limit: int = 50, offset: int = 0) -> EvaluationTable:
        runs, evaluations = self.store.snapshot()
        latest = {item.run_id: item for item in evaluations}
        current = {
            item.run_id: item
            for item in latest.values()
            if item.k == self.config.k
            and item.judge_model == self.config.judge_model
            and item.rubric_version == RUBRIC_VERSION
        }
        rows = []
        for run in sorted(runs, key=lambda item: item.started_at, reverse=True):
            evaluation = current.get(run.run_id) or latest.get(run.run_id)
            rows.append(
                EvaluationRow(
                    run_id=run.run_id,
                    user_id=run.user_id,
                    session_id=run.session_id,
                    started_at=run.started_at,
                    message_type=run.message_type,
                    run_status=run.status,
                    query=run.query,
                    answer=run.answer,
                    evaluation_status=(evaluation.status if run.run_id in current else "stale")
                    if evaluation
                    else "missing",
                    evaluation=evaluation,
                )
            )
        return EvaluationTable(
            total=len(rows),
            missing=sum(
                r.evaluation_status in {"missing", "error", "stale"} and r.run_status != "running"
                for r in rows
            ),
            rows=rows[offset : offset + limit],
        )

    async def evaluate(self, request: JudgeRequest) -> JudgeResponse:
        try:
            async with self.store.judge_lock():
                return await self._evaluate(request)
        except Timeout:
            raise ApplicationError(
                409, "An evaluation batch is already running. Refresh the table."
            ) from None

    async def _evaluate(self, request: JudgeRequest) -> JudgeResponse:
        runs, evaluations = await asyncio.to_thread(self.store.snapshot)
        run_map = {run.run_id: run for run in runs}
        requested = set(request.run_ids) if request.run_ids is not None else set(run_map)
        if requested - set(run_map):
            raise ApplicationError(404, "One or more run IDs were not found.")
        references = {ref.run_id: ref for ref in request.references}
        if len(references) != len(request.references) or set(references) - requested:
            raise ApplicationError(
                422, "Reference run IDs must be unique and included in the selection."
            )
        # Preserve supplied ground truth on later UI evaluations unless explicitly replaced.
        latest = {item.run_id: item for item in evaluations}
        for run_id in requested - set(references):
            if run_id in latest and latest[run_id].reference_json:
                references[run_id] = RunReference.model_validate(latest[run_id].reference_json)
        for run_id, reference in references.items():
            known = {r["retrieval_id"] for r in run_map[run_id].retrievals_json}
            ids = [r.retrieval_id for r in reference.retrievals]
            if len(ids) != len(set(ids)) or set(ids) - known:
                raise ApplicationError(
                    422, "Reference retrieval IDs must belong to the selected run."
                )
        k = request.k or self.config.k
        current = {
            e.run_id: e
            for e in latest.values()
            if e.k == k
            and e.judge_model == self.config.judge_model
            and e.rubric_version == RUBRIC_VERSION
        }
        done = {
            e.run_id
            for e in current.values()
            if e.status in {"completed", "not_applicable"}
            and e.k == k
            and e.judge_model == self.config.judge_model
            and e.rubric_version == RUBRIC_VERSION
            and e.reference_json
            == (references[e.run_id].model_dump(mode="json") if e.run_id in references else {})
        }
        selected = [
            run
            for run in runs
            if run.run_id in requested
            and run.status != "running"
            and (not request.missing_only or run.run_id not in done)
        ]
        results = []
        ready = []
        encoding = tiktoken.get_encoding("cl100k_base")
        overhead = len(encoding.encode(JUDGE_INSTRUCTIONS)) + 1500
        for run in selected:
            base = dict(
                run_id=run.run_id,
                judge_model=self.config.judge_model,
                rubric_version=RUBRIC_VERSION,
                k=k,
                reference_json=references[run.run_id].model_dump(mode="json")
                if run.run_id in references
                else {},
            )
            if run.status != "completed":
                results.append(
                    EvaluationRecord(
                        **base,
                        status="not_applicable",
                        summary=f"No final answer to score: run status is {run.status}.",
                    )
                )
                continue
            payload = prepare_input(run, references.get(run.run_id))
            size = len(
                encoding.encode(json.dumps(payload, ensure_ascii=False), disallowed_special=())
            )
            size += len(
                encoding.encode(
                    json.dumps(build_batch_schema({"run_1": payload}).model_json_schema()),
                    disallowed_special=(),
                )
            )
            if size + overhead > self.config.max_input_tokens:
                results.append(
                    EvaluationRecord(
                        **base,
                        status="error",
                        error_type="ContextTooLarge",
                        summary="Trace exceeds evaluation.max_input_tokens; "
                        "no evidence was truncated.",
                    )
                )
                continue
            ready.append((run, payload, size, base))
        if ready and not self.judge.api_key.strip():
            raise ApplicationError(503, "Set OPENAI_API_KEY before evaluating runs.")
        if results:
            await asyncio.to_thread(self.store.save_evaluations, results)
        batches, batch, size = [], [], overhead
        for item in ready:
            if batch and (
                len(batch) >= self.config.batch_size
                or size + item[2] > self.config.max_input_tokens
            ):
                batches.append(batch)
                batch, size = [], overhead
            batch.append(item)
            size += item[2]
        if batch:
            batches.append(batch)
        semaphore = asyncio.Semaphore(self.config.max_concurrency)

        async def assess(items):
            async with semaphore:
                started = perf_counter()
                try:
                    answer = await self.judge.assess([item[1] for item in items])
                    by_id = {item.run_id: item for item in answer.runs}
                    if len(by_id) != len(answer.runs) or set(by_id) != {
                        item[0].run_id for item in items
                    }:
                        raise JudgeOutputError("Judge omitted, duplicated, or invented run IDs.")
                except Exception as exc:
                    completed = [self._failure(item[3], exc) for item in items]
                else:
                    completed = []
                    for run, payload, _, base in items:
                        judgment = by_id[run.run_id]
                        try:
                            normalize_evidence_ids(judgment, payload)
                            validate_judgment(judgment, payload)
                            completed.append(
                                self._score(run, judgment, references.get(run.run_id), base)
                            )
                        except Exception as exc:
                            completed.append(self._failure(base, exc, judgment))
                for evaluation in completed:
                    evaluation.duration_ms = round((perf_counter() - started) * 1000, 2)
                await asyncio.to_thread(self.store.save_evaluations, completed)
                return completed

        groups = await asyncio.gather(*(assess(batch) for batch in batches), return_exceptions=True)
        # Keep the evaluation lock until every in-flight batch finishes, even on disk errors.
        for group in groups:
            if isinstance(group, BaseException):
                raise group
            results.extend(group)
        logger.info("judge_completed", extra={"batch_count": len(batches)})
        return JudgeResponse(
            selected=len(selected), skipped=len(requested) - len(selected), evaluations=results
        )

    @staticmethod
    def _failure(
        base: dict, exc: Exception, judgment: RunJudgment | None = None
    ) -> EvaluationRecord:
        # Only our own validation messages are safe to expose. Provider/Pydantic exceptions
        # can embed request contents, credentials or rejected model text.
        if isinstance(exc, JudgeOutputError):
            reason = str(exc)
        elif isinstance(exc, ValidationError):
            reason = "Judge output did not match the required assessment schema."
        else:
            reason = "Judge request or processing failed; retry this run."
        logger.warning(
            "judge_run_failed",
            extra={"run_id": base["run_id"], "error_type": type(exc).__name__, "reason": reason},
        )
        return EvaluationRecord(
            **base,
            status="error",
            error_type=type(exc).__name__,
            summary=reason,
            # Diagnostic only: rejected output is never used to calculate scores.
            assessment_json={"rejected_judgment": judgment.model_dump(mode="json")}
            if judgment is not None
            else {},
        )

    @staticmethod
    def _score(
        run: RunRecord, judgment: RunJudgment, reference: RunReference | None, base: dict
    ) -> EvaluationRecord:
        ref_by_id = {item.retrieval_id: item for item in reference.retrievals} if reference else {}
        judged = {item.retrieval_id: item for item in judgment.retrievals}
        metrics = []
        for retrieval in run.retrievals_json:
            if "raw_response" not in retrieval:
                metrics.append({"retrieval_id": retrieval["retrieval_id"], "status": "error"})
                continue
            rid = retrieval["retrieval_id"]
            ranked = [hit["chunk_id"] for hit in unique_hits(retrieval)]
            grades = {chunk.chunk_id: chunk.relevance for chunk in judged[rid].chunks}
            labels = ref_by_id.get(rid)
            if labels:
                grades = (
                    dict(labels.relevance) if labels.complete else {**grades, **labels.relevance}
                )
            complete = labels is not None and labels.complete
            metrics.append(
                {
                    "retrieval_id": rid,
                    "query": retrieval["query"],
                    "status": "completed",
                    "k": base["k"],
                    "returned_count": len(ranked),
                    "precision_at_k": precision_at_k(ranked, grades, base["k"]),
                    "recall_at_k": recall_at_k(ranked, labels.relevance, base["k"])
                    if complete
                    else None,
                    "ndcg_at_k": ndcg_at_k(
                        ranked,
                        grades,
                        base["k"],
                        list(grades.values())
                        if complete
                        else [grades.get(item, 0) for item in ranked],
                    ),
                    "relevance_source": "reference" if complete else "mixed" if labels else "llm",
                    "ndcg_scope": "reference_corpus" if complete else "retrieved_pool",
                    "recall_note": None
                    if complete
                    else "Complete relevance labels were not supplied.",
                }
            )
        aggregate = {}
        for name in ("precision_at_k", "recall_at_k", "ndcg_at_k"):
            values = [item[name] for item in metrics if item.get(name) is not None]
            aggregate[name] = mean(values) if values else None
        return EvaluationRecord(
            **base,
            status="completed",
            summary=judgment.summary,
            **aggregate,
            **grounding_scores([c.verdict for c in judgment.claims]),
            tool_usage=behavior_score(judgment.tool_usage.grade),
            intention_success=behavior_score(judgment.intention_success.grade),
            retrieval_metrics_json=metrics,
            assessment_json=judgment.model_dump(mode="json"),
        )
