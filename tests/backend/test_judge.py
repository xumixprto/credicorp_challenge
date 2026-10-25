import csv
from math import log2
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app.evaluation.errors import JudgeOutputError
from backend.app.evaluation.judge import normalize_evidence_ids, prepare_input, validate_judgment
from backend.app.evaluation.metrics.behavior import behavior_score
from backend.app.evaluation.metrics.grounding import grounding_scores
from backend.app.evaluation.metrics.retrieval import ndcg_at_k, precision_at_k, recall_at_k
from backend.app.evaluation.models import BatchJudgment
from backend.app.evaluation.storage import EvaluationStore
from shared.evaluations import EvaluationRecord, RunRecord


def sample_run(**updates):
    return RunRecord(
        request_id=str(uuid4()),
        user_id="user",
        session_id="chat",
        message_type="TEXT",
        model="test",
        status="completed",
        query="Cost of revenue?",
        answer="Cost was 24,079 million.",
        retrievals_json=[
            {
                "retrieval_id": "r1",
                "query": "NVDA cost",
                "status": "ok",
                "raw_response": {
                    "hits": [
                        {"chunk_id": "noise", "text": "Other data", "distance": 0.1},
                        {"chunk_id": "answer", "text": "Cost was 24,079 million.", "distance": 0.2},
                    ]
                },
                "visible_evidence": {
                    "results": [{"chunk_id": "answer", "text": "Cost was 24,079 million."}]
                },
            }
        ],
        **updates,
    )


async def assess(payloads):
    return BatchJudgment.model_validate(
        {
            "runs": [
                {
                    "run_id": p["run_id"],
                    "retrievals": [
                        {
                            "retrieval_id": r["retrieval_id"],
                            "chunks": [
                                {
                                    "chunk_id": c["chunk_id"],
                                    "relevance": 3 if c["chunk_id"] == "answer" else 0,
                                    "reason": "Test assessment",
                                }
                                for c in r["chunks"]
                            ],
                        }
                        for r in p["retrievals"]
                    ],
                    "claims": [
                        {
                            "claim": p["final_answer"],
                            "verdict": "supported",
                            "evidence_ids": ["r1:answer"],
                            "reason": "Matching amount and units.",
                        }
                    ],
                    "summary": "The answer is supported.",
                    "tool_usage": {
                        "grade": 2,
                        "reason": "Retrieved evidence for the requested fact.",
                        "evidence_ids": ["r1:answer"],
                    },
                    "intention_success": {
                        "grade": 2,
                        "reason": "Answered the requested amount.",
                        "evidence_ids": ["input", "final_answer", "r1:answer"],
                    },
                }
                for p in payloads
            ]
        }
    )


def configure_judge(app):
    judge = app.state.judge_service
    judge.judge.assess = AsyncMock(side_effect=assess)
    return judge


def test_metric_formulas_with_known_labels():
    labels = {"noise": 0, "answer": 3, "missed": 2}
    assert precision_at_k(["noise", "answer"], labels, 3) == pytest.approx(1 / 3)
    assert recall_at_k(["noise", "answer"], labels, 2) == 0.5
    expected = (7 / log2(3)) / (7 + 3 / log2(3))
    assert ndcg_at_k(["noise", "answer"], labels, 2, list(labels.values())) == pytest.approx(
        expected
    )
    assert ndcg_at_k([], {}, 5, []) == 0
    assert recall_at_k([], {}, 5) is None
    assert grounding_scores(["supported", "unsupported", "contradicted"]) == {
        "claim_count": 3,
        "groundedness": 1 / 3,
        "hallucination": 2 / 3,
    }
    assert grounding_scores([])["hallucination"] is None


def test_csv_roundtrip_multiline_unicode_and_large_context(tmp_path):
    store = EvaluationStore(tmp_path)
    run = sample_run(context_json={"text": "a" * 150000, "message": 'Hola, "Diego"\n¿Qué tal?'})
    run.query = 'Hola, "Diego"\n¿Qué tal?'
    store.save_run(run)
    run.answer = "Updated"
    store.save_run(run)
    evaluation = EvaluationRecord(
        run_id=run.run_id, judge_model="test", rubric_version="1", k=5, status="completed"
    )
    store.save_evaluations([evaluation])
    runs, evaluations = EvaluationStore(tmp_path).snapshot()
    assert runs == [run]
    assert evaluations == [evaluation]
    with (tmp_path / "runs.csv").open(encoding="utf-8", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 1


def test_recorded_runs_are_visible_and_cached_replies_do_not_duplicate(backend_app):
    with TestClient(backend_app) as client:
        payload = {
            "userId": "d5bbf9d1-5945-44d8-bb10-0065164a8356",
            "sessionId": "trace",
            "query": "Hello",
        }
        for _ in range(2):
            assert (
                client.post("/agent", json=payload, headers={"X-Request-ID": "same"}).status_code
                == 200
            )
        table = client.get("/judge").json()
        assert table["total"] == table["missing"] == 1
        assert table["rows"][0]["evaluation_status"] == "missing"
        runs, _ = backend_app.state.judge_service.store.snapshot()
        assert runs[0].status == "completed"
        assert runs[0].response_json and runs[0].answer == "Hello World"
        assert runs[0].usage_json
        assert runs[0].duration_ms > 0
        tools = runs[0].configuration_json["available_tools"]
        assert tools[0]["name"] == "retrieve_documents"
        assert tools[0]["description"] and tools[0]["parameters"]


def test_selected_all_missing_and_replacement(backend_app):
    judge = configure_judge(backend_app)
    runs = [sample_run() for _ in range(7)]
    for run in runs:
        judge.store.save_run(run)
    with TestClient(backend_app) as client:
        first = client.post("/judge", json={"run_ids": [runs[0].run_id]}).json()
        assert first["selected"] == 1
        score = first["evaluations"][0]
        assert score["precision_at_k"] == 0.2
        assert score["recall_at_k"] is None
        assert score["ndcg_at_k"] == pytest.approx(1 / log2(3))
        assert score["groundedness"] == 1 and score["hallucination"] == 0
        assert score["tool_usage"] == score["intention_success"] == 1
        assert client.post("/judge", json={}).json()["selected"] == 6
        assert judge.judge.assess.await_count == 3  # One selected + two batches of three.
        assert client.post("/judge", json={}).json()["selected"] == 0
        assert client.get("/judge").json()["missing"] == 0
        assert (
            client.post("/judge", json={"run_ids": [runs[0].run_id], "missing_only": False}).json()[
                "selected"
            ]
            == 1
        )
        assert len(judge.store.snapshot()[1]) == 7
        assert judge.store.snapshot()[1][0].evaluation_id != score["evaluation_id"]
        assert client.post("/judge", json={"run_ids": ["unknown"]}).status_code == 404
        assert client.post("/judge", json={"run_ids": []}).status_code == 422


def test_reference_recall_and_ndcg_persist_for_future_evaluations(backend_app):
    judge = configure_judge(backend_app)
    run = sample_run()
    judge.store.save_run(run)
    reference = {
        "run_id": run.run_id,
        "retrievals": [
            {"retrieval_id": "r1", "relevance": {"answer": 3, "missed": 2}, "complete": True}
        ],
    }
    with TestClient(backend_app) as client:
        result = client.post(
            "/judge", json={"run_ids": [run.run_id], "k": 2, "references": [reference]}
        ).json()["evaluations"][0]
        assert result["recall_at_k"] == 0.5
        assert result["precision_at_k"] == 0.5
        assert result["retrieval_metrics_json"][0]["ndcg_scope"] == "reference_corpus"
        assert client.post("/judge", json={"run_ids": [run.run_id], "k": 2}).json()["selected"] == 0
        assert (
            client.get("/judge").json()["rows"][0]["evaluation_status"] == "stale"
        )  # configured k=5
        result = client.post("/judge", json={}).json()["evaluations"][0]
        assert result["recall_at_k"] == 0.5
        assert result["reference_json"]["retrievals"][0]["relevance"] == {"answer": 3, "missed": 2}


@pytest.mark.parametrize(
    "failure",
    [
        "provider",
        "invented_evidence",
        "missing_run",
        "missing_chunk",
        "invented_behavior_evidence",
        "missing_behavior_evidence",
    ],
)
def test_failed_evaluations_keep_null_scores_and_can_retry(backend_app, failure):
    judge = configure_judge(backend_app)
    run = sample_run()
    judge.store.save_run(run)

    async def invalid(payloads):
        result = await assess(payloads)
        if failure == "provider":
            raise TimeoutError()
        if failure == "invented_evidence":
            result.runs[0].claims[0].evidence_ids = ["not-provided"]
        if failure == "missing_run":
            result.runs = []
        if failure == "missing_chunk":
            result.runs[0].retrievals[0].chunks = []
        if failure == "invented_behavior_evidence":
            result.runs[0].tool_usage.evidence_ids = ["tool_call:invented"]
        if failure == "missing_behavior_evidence":
            result.runs[0].intention_success.evidence_ids = []
        return result

    judge.judge.assess.side_effect = invalid
    with TestClient(backend_app) as client:
        result = client.post("/judge", json={}).json()["evaluations"][0]
        assert result["status"] == "error" and result["groundedness"] is None
        assert result["tool_usage"] is result["intention_success"] is None
        if failure == "invented_behavior_evidence":
            assert result["summary"].startswith("tool_usage.evidence_ids")
            assert "rejected_judgment" in result["assessment_json"]
        if failure == "missing_behavior_evidence":
            assert result["summary"] == "intention_success has a grade but no trace evidence IDs."
        assert client.get("/judge").json()["missing"] == 1
        judge.judge.assess.side_effect = assess
        assert client.post("/judge", json={}).json()["evaluations"][0]["status"] == "completed"


def test_unevaluable_runs_do_not_call_model_and_large_traces_are_not_truncated(backend_app):
    judge = configure_judge(backend_app)
    run = sample_run()
    run.status = "approval_required"
    judge.store.save_run(run)
    large = sample_run(context_json={"large": "context " * 50000})
    judge.store.save_run(large)
    with TestClient(backend_app) as client:
        results = client.post("/judge", json={}).json()["evaluations"]
        assert {e["status"] for e in results} == {"error", "not_applicable"}
        assert any(e["error_type"] == "ContextTooLarge" for e in results)
        judge.judge.assess.assert_not_called()


def test_judge_lock_prevents_duplicate_concurrent_batches(backend_app):
    judge = configure_judge(backend_app)
    judge.store.save_run(sample_run())
    from filelock import FileLock

    with TestClient(backend_app) as client, FileLock(judge.store.directory / ".judge.lock"):
        assert client.post("/judge", json={}).status_code == 409
        judge.judge.assess.assert_not_called()


def test_only_visible_evidence_supports_answer(backend_app):
    run = sample_run()
    payload = prepare_input(run, None)
    assert [e["evidence_id"] for e in payload["evidence"]] == ["r1:answer"]
    assert len(payload["retrievals"][0]["chunks"]) == 2


def test_behavior_input_includes_failures_and_approval_decision_without_factual_support():
    run = sample_run(
        tools_json=[
            {
                "tool_call_id": "failed",
                "name": "place_order",
                "status": "error",
                "arguments": {"symbol": "NVDA"},
                "error_type": "TimeoutError",
            }
        ],
        input_json={"messageType": "HUMAN_FLOW_REPLY", "approved": False},
        response_json=[{"role": "assistant", "contents": []}],
        configuration_json={"available_tools": [{"name": "place_order"}]},
    )
    payload = prepare_input(run, None)
    assert payload["tool_calls"] == run.tools_json
    assert payload["input"]["approved"] is False
    assert payload["agent_response"] == run.response_json
    assert payload["available_tools"] == [{"name": "place_order"}]
    assert "tool_call:failed" in payload["behavior_evidence_ids"]
    assert [item["evidence_id"] for item in payload["evidence"]] == ["r1:answer"]
    assert prepare_input(sample_run(), None)["available_tools"] is None


@pytest.mark.parametrize("grade,score", [(0, 0), (1, 0.5), (2, 1), (None, None)])
def test_behavior_scores_do_not_depend_on_retrieval_or_claims(backend_app, grade, score):
    judge = configure_judge(backend_app)
    run = sample_run()
    run.query, run.answer, run.retrievals_json = "Hello", "Hello", []
    judge.store.save_run(run)

    async def behavior_only(payloads):
        return BatchJudgment.model_validate(
            {
                "runs": [
                    {
                        "run_id": run.run_id,
                        "retrievals": [],
                        "claims": [],
                        "summary": "Behavior only.",
                        "tool_usage": {
                            "grade": 2,
                            "reason": "No tools needed for a greeting.",
                            "evidence_ids": ["input"],
                        },
                        "intention_success": {
                            "grade": grade,
                            "reason": "Synthetic rubric assessment.",
                            "evidence_ids": ["final_answer"] if grade is not None else [],
                        },
                    }
                ]
            }
        )

    judge.judge.assess.side_effect = behavior_only
    with TestClient(backend_app) as client:
        result = client.post("/judge", json={}).json()["evaluations"][0]
        assert result["status"] == "completed"
        assert result["tool_usage"] == 1
        assert result["intention_success"] == score == behavior_score(grade)
        assert result["recall_at_k"] is result["groundedness"] is None
        assert client.post("/judge", json={}).json()["selected"] == 0
        assert judge.store.snapshot()[1][0].intention_success == score


def test_legacy_evaluation_csv_is_readable_and_replaced_on_reevaluation(backend_app):
    import io
    import json

    judge = configure_judge(backend_app)
    run = sample_run()
    judge.store.save_run(run)
    old = EvaluationRecord(
        schema_version=1,
        run_id=run.run_id,
        judge_model=judge.config.judge_model,
        rubric_version="1.0",
        k=judge.config.k,
        status="completed",
        precision_at_k=0.2,
        assessment_json={"summary": "Original assessment"},
    )
    fields = [
        f for f in EvaluationRecord.model_fields if f not in {"tool_usage", "intention_success"}
    ]
    path = judge.store.directory / "evaluations.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(
            {
                k: json.dumps(v) if k.endswith("_json") else v
                for k, v in old.model_dump(mode="json").items()
                if k in fields
            }
        )
    before = path.read_bytes()
    with TestClient(backend_app) as client:
        table = client.get("/judge").json()
        assert table["rows"][0]["evaluation_status"] == "stale"
        assert table["rows"][0]["evaluation"]["tool_usage"] is None
        assert table["missing"] == 1 and path.read_bytes() == before
        assert client.post("/judge", json={}).json()["selected"] == 1
        assert client.get("/judge").json()["missing"] == 0
        records = judge.store.snapshot()[1]
        assert len(records) == 1 and records[0].evaluation_id != old.evaluation_id
        assert records[0].schema_version == 2 and records[0].tool_usage == 1
        exported = client.get("/judge/export/evaluations")
        rows = list(csv.DictReader(io.StringIO(exported.text)))
        assert len(rows) == 1 and rows[0]["tool_usage"] == "1.0"


def test_unknown_csv_schemas_still_rejected(tmp_path):
    (tmp_path / "evaluations.csv").write_text("run_id,unexpected\nrun,value\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Unexpected CSV schema"):
        EvaluationStore(tmp_path).snapshot()


def test_table_shows_first_50_but_missing_evaluates_every_run(backend_app):
    judge = configure_judge(backend_app)
    for _ in range(53):
        judge.store.save_run(sample_run())
    with TestClient(backend_app) as client:
        table = client.get("/judge").json()
        assert len(table["rows"]) == 50 and table["total"] == 53
        assert client.post("/judge", json={}).json()["selected"] == 53
        assert client.get("/judge").json()["missing"] == 0
        for table_name in ("runs", "evaluations"):
            import io

            exported = client.get(f"/judge/export/{table_name}")
            assert exported.status_code == 200
            assert exported.headers["content-type"].startswith("text/csv")
            assert exported.headers["content-disposition"] == (
                f'attachment; filename="{table_name}.csv"'
            )
            assert len(list(csv.DictReader(io.StringIO(exported.text)))) == 53
            assert exported.content == (judge.store.directory / f"{table_name}.csv").read_bytes()


def test_empty_csv_downloads_include_headers_and_only_known_tables(backend_app):
    with TestClient(backend_app) as client:
        for table_name, model in (("runs", RunRecord), ("evaluations", EvaluationRecord)):
            response = client.get(f"/judge/export/{table_name}")
            assert response.status_code == 200
            assert response.text.strip().split(",") == list(model.model_fields)
        assert client.get("/judge/export/secret").status_code == 422


def test_csv_atomic_failure_preserves_previous_data(tmp_path, monkeypatch):
    from unittest.mock import patch

    store = EvaluationStore(tmp_path)
    first = sample_run()
    store.save_run(first)
    before = (tmp_path / "runs.csv").read_bytes()
    with patch(
        "backend.app.evaluation.storage.os.replace", side_effect=OSError("disk unavailable")
    ):
        with pytest.raises(OSError):
            store.save_run(sample_run())
    assert (tmp_path / "runs.csv").read_bytes() == before
    assert store.snapshot()[0] == [first]


def test_concurrent_csv_writers_preserve_all_runs(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = EvaluationStore(tmp_path)
    runs = [sample_run() for _ in range(12)]
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(store.save_run, runs))
    assert {r.run_id for r in store.snapshot()[0]} == {r.run_id for r in runs}


def test_failed_agent_is_recorded_and_trace_write_failure_does_not_mask_reply(
    backend_app, model_stub, monkeypatch
):
    store = backend_app.state.judge_service.store
    with TestClient(backend_app) as client:
        payload = {
            "userId": "d5bbf9d1-5945-44d8-bb10-0065164a8356",
            "sessionId": "failed-trace",
            "query": "Hello",
        }
        model_stub.status_code = 500
        assert client.post("/agent", json=payload).status_code == 502
        runs, _ = store.snapshot()
        assert runs[0].status == "error" and runs[0].error_type
        model_stub.status_code = 200
        original = store.save_run

        def fail_final(record):
            if record.status != "running":
                raise OSError("disk unavailable")
            original(record)

        monkeypatch.setattr(store, "save_run", fail_final)
        response = client.post("/agent", json=payload, headers={"X-Request-ID": "safe-retry"})
        assert response.status_code == 200
        count = len(model_stub.requests)
        assert (
            client.post("/agent", json=payload, headers={"X-Request-ID": "safe-retry"}).json()
            == response.json()
        )
        assert len(model_stub.requests) == count


@pytest.mark.parametrize("run_count", [1, 2])
@pytest.mark.parametrize("invalid", [None, "missing_slot", "extra_slot", "model_run_id"])
def test_openai_judge_uses_structured_batch_output(monkeypatch, run_count, invalid):
    import asyncio
    import json

    import httpx2
    from openai import AsyncOpenAI
    from pydantic import ValidationError

    from backend.app.evaluation.config import EvaluationConfig
    from backend.app.evaluation.judge import OpenAIJudge

    seen = []

    async def handle(request):
        body = json.loads(request.content)
        seen.append(body)
        runs = json.loads(body["input"])["runs"]
        assert all("run_id" not in payload for payload in runs.values())
        output = {}
        for index, (slot, payload) in enumerate(runs.items(), start=1):
            output[slot] = {
                "retrievals": {
                    rid: {
                        cid: {
                            "relevance": 3 if chunk["chunk_id"] == "answer" else 0,
                            "reason": "Synthetic assessment",
                        }
                        for cid, chunk in retrieval["chunks"].items()
                    }
                    for rid, retrieval in payload["retrievals"].items()
                },
                "claims": [
                    {
                        "claim": payload["final_answer"],
                        "verdict": "supported",
                        "evidence_ids": ["r1:answer"],
                        "reason": "Matching evidence",
                    }
                ],
                "tool_usage": {
                    "grade": index,
                    "reason": "Synthetic assessment",
                    "evidence_ids": ["input"],
                },
                "intention_success": {
                    "grade": 2,
                    "reason": "Synthetic assessment",
                    "evidence_ids": ["r1:answer"],
                },
                "summary": "Synthetic assessment",
            }
        # Model response order is irrelevant: assignment must use the required field names.
        output = dict(reversed(list(output.items())))
        if invalid == "missing_slot":
            output.pop("run_1")
        if invalid == "extra_slot":
            output["invented_run"] = output["run_1"]
        if invalid == "model_run_id":
            output["run_1"]["run_id"] = "uuid-copied-from-history"
        return httpx2.Response(
            200,
            json={
                "id": "judge-response",
                "object": "response",
                "created_at": 1700000000,
                "status": "completed",
                "model": body["model"],
                "output": [
                    {
                        "type": "message",
                        "id": "judged",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(output),
                                "annotations": [],
                            }
                        ],
                    }
                ],
            },
        )

    def client(**kwargs):
        return AsyncOpenAI(
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)), **kwargs
        )

    monkeypatch.setattr("backend.app.evaluation.judge.AsyncOpenAI", client)
    payloads = [prepare_input(sample_run(), None) for _ in range(run_count)]
    payloads[0]["history"] = {"run_id": "uuid-copied-from-history"}
    judge = OpenAIJudge(EvaluationConfig(), "test-key")
    if invalid:
        with pytest.raises(ValidationError):
            asyncio.run(judge.assess(payloads))
    else:
        result = asyncio.run(judge.assess(payloads))
        assert [r.run_id for r in result.runs] == [p["run_id"] for p in payloads]
        assert [r.tool_usage.grade for r in result.runs] == list(range(1, run_count + 1))
    assert len(seen) == 1
    assert seen[0]["store"] is False
    assert seen[0]["text"]["format"]["type"] == "json_schema"
    assert seen[0]["text"]["format"]["strict"] is True
    schema = seen[0]["text"]["format"]["schema"]
    assert schema["required"] == [f"run_{i}" for i in range(1, run_count + 1)]
    assert schema["additionalProperties"] is False
    assert "run_id" not in schema["$defs"]["run_1_assessment"]["properties"]
    assert "tools" not in seen[0]


def test_invalid_old_run_does_not_discard_valid_sibling_and_logs_reason(backend_app, monkeypatch):
    judge = configure_judge(backend_app)
    old, new = sample_run(), sample_run(configuration_json={"available_tools": []})
    for run in (old, new):
        judge.store.save_run(run)
    logged = []
    monkeypatch.setattr(
        "backend.app.evaluation.service.logger.warning",
        lambda message, **kwargs: logged.append((message, kwargs["extra"])),
    )

    async def mixed(payloads):
        result = await assess(payloads)
        # Older traces still get judged; a fabricated citation must fail only its own run.
        result.runs[0].tool_usage.evidence_ids = ["tool_call:nonexistent"]
        return result

    judge.judge.assess.side_effect = mixed
    with TestClient(backend_app) as client:
        result = client.post("/judge", json={}).json()
        by_id = {item["run_id"]: item for item in result["evaluations"]}
        assert by_id[old.run_id]["status"] == "error"
        assert by_id[new.run_id]["status"] == "completed"
        assert by_id[new.run_id]["tool_usage"] == 1
        assert len(logged) == 1 and logged[0][1]["run_id"] == old.run_id
        assert logged[0][1]["reason"] == by_id[old.run_id]["summary"]
        assert "tool_usage.evidence_ids" in logged[0][1]["reason"]
        assert "nonexistent" not in logged[0][1]["reason"]
        assert by_id[old.run_id]["assessment_json"]["rejected_judgment"]["tool_usage"][
            "evidence_ids"
        ] == ["tool_call:nonexistent"]
        judge.judge.assess.side_effect = assess
        assert client.post("/judge", json={}).json()["selected"] == 1
        saved = judge.store.snapshot()[1]
        assert len(saved) == 2 and all(row.status == "completed" for row in saved)


def test_provider_exception_text_is_not_exposed(backend_app, monkeypatch):
    judge = configure_judge(backend_app)
    judge.store.save_run(sample_run())
    judge.judge.assess.side_effect = ValueError("private-provider-content")
    logged = []
    monkeypatch.setattr(
        "backend.app.evaluation.service.logger.warning",
        lambda message, **kwargs: logged.append(kwargs["extra"]),
    )
    with TestClient(backend_app) as client:
        response = client.post("/judge", json={})
        assert response.json()["evaluations"][0]["status"] == "error"
        assert "private-provider-content" not in response.text
        assert "private-provider-content" not in str(logged)


def test_evaluation_upsert_compacts_duplicates_and_failure_replaces_success(tmp_path):
    store = EvaluationStore(tmp_path)
    first = EvaluationRecord(
        run_id="one",
        judge_model="test",
        rubric_version="2.0",
        k=5,
        status="completed",
        tool_usage=1,
    )
    duplicate = first.model_copy(update={"evaluation_id": "newer"})
    other = first.model_copy(update={"run_id": "two"})
    store._write("evaluations.csv", [first, duplicate, other], EvaluationRecord)
    failed = first.model_copy(
        update={"status": "error", "tool_usage": None, "evaluation_id": "latest"}
    )
    store.save_evaluations([failed])
    rows = {item.run_id: item for item in store.snapshot()[1]}
    assert len(rows) == 2 and rows["two"] == other
    assert rows["one"].evaluation_id == "latest"
    assert rows["one"].status == "error" and rows["one"].tool_usage is None


def test_agent_citation_and_actual_tool_call_id_are_accepted(backend_app):
    judge = configure_judge(backend_app)
    call_id = "af-call-9db1b52e91d943cf9f6614e54719fa7b"
    run = sample_run(
        tools_json=[{"tool_call_id": call_id, "name": "retrieve_documents", "status": "completed"}]
    )
    run.retrievals_json[0]["visible_evidence"]["results"][0]["citation_id"] = "S845d4fb2ba20"
    judge.store.save_run(run)

    async def agent_labels(payloads):
        result = await assess(payloads)
        item = result.runs[0]
        item.claims[0].evidence_ids = ["S845d4fb2ba20"]
        item.tool_usage.evidence_ids = [call_id]
        item.intention_success.evidence_ids = ["S845d4fb2ba20"]
        return result

    judge.judge.assess.side_effect = agent_labels
    with TestClient(backend_app) as client:
        evaluation = client.post("/judge", json={}).json()["evaluations"][0]
        assert evaluation["status"] == "completed"
        assert (
            evaluation["groundedness"]
            == evaluation["tool_usage"]
            == evaluation["intention_success"]
            == 1
        )
        assessment = evaluation["assessment_json"]
        assert assessment["claims"][0]["evidence_ids"] == ["r1:answer"]
        assert assessment["tool_usage"]["evidence_ids"] == [f"tool_call:{call_id}"]
        assert assessment["intention_success"]["evidence_ids"] == ["r1:answer"]


@pytest.mark.parametrize("alias", ["Sunknown", "noise", "Sambiguous"])
def test_unknown_hidden_and_ambiguous_citations_still_fail(alias):
    import asyncio

    run = sample_run()
    payload = prepare_input(run, None)
    payload["evidence"][0]["content"]["citation_id"] = "Sambiguous"
    payload["evidence"].append(
        {
            "evidence_id": "other-evidence",
            "content": {"citation_id": "Sambiguous", "text": "Other fact"},
        }
    )
    judgment = asyncio.run(assess([payload])).runs[0]
    judgment.claims[0].evidence_ids = [alias]
    normalize_evidence_ids(judgment, payload)
    with pytest.raises(JudgeOutputError, match="claims"):
        validate_judgment(judgment, payload)


@pytest.mark.parametrize("status", ["completed", "error"])
def test_tool_aliases_respect_factual_and_behavior_evidence_scopes(status):
    import asyncio

    run = sample_run(
        tools_json=[
            {
                "tool_call_id": "order-call",
                "name": "place_order",
                "status": status,
                "result": {"status": "submitted"},
            }
        ]
    )
    payload = prepare_input(run, None)
    judgment = asyncio.run(assess([payload])).runs[0]
    judgment.tool_usage.evidence_ids = ["order-call"]
    judgment.claims[0].evidence_ids = ["order-call"]
    normalize_evidence_ids(judgment, payload)
    assert judgment.tool_usage.evidence_ids == ["tool_call:order-call"]
    if status == "completed":
        assert judgment.claims[0].evidence_ids == ["tool:order-call"]
        validate_judgment(judgment, payload)
    else:
        with pytest.raises(JudgeOutputError, match="claims"):
            validate_judgment(judgment, payload)
