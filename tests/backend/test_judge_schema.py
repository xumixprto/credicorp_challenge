import copy

import pytest
from pydantic import ValidationError

from backend.app.evaluation.judge import prepare_input, validate_judgment
from backend.app.evaluation.schema import batch_inputs, build_batch_schema, decode_batch
from shared.evaluations import RunRecord


def payload():
    run = RunRecord(
        run_id="saved-run",
        request_id="request",
        user_id="user",
        session_id="chat",
        message_type="TEXT",
        model="test",
        query="What are the risks?",
        retrievals_json=[
            {
                "retrieval_id": f"saved-retrieval-{i}",
                "query": f"Risk query {i}",
                "raw_response": {
                    "hits": [{"chunk_id": f"shared-{j}", "text": f"Evidence {j}"} for j in range(5)]
                },
                "visible_evidence": {"results": [{"chunk_id": "shared-0", "text": "Evidence 0"}]},
            }
            for i in range(3)
        ],
    )
    return prepare_input(run, None)


def output_for(inputs):
    return {
        slot: {
            "retrievals": {
                rid: {
                    cid: {"relevance": i % 4, "reason": "Query-specific grade"}
                    for cid in retrieval["chunks"]
                }
                for i, (rid, retrieval) in enumerate(run["retrievals"].items())
            },
            "claims": [],
            "tool_usage": {"grade": 2, "reason": "Synthetic", "evidence_ids": ["input"]},
            "intention_success": {"grade": None, "reason": "Unknown", "evidence_ids": []},
            "summary": "Synthetic assessment",
        }
        for slot, run in inputs.items()
    }


def test_overlapping_queries_keep_all_15_grades_and_restore_ids_by_name():
    original = payload()
    before = copy.deepcopy(original)
    slots = {"run_1": original}
    inputs = batch_inputs(slots)
    output = output_for(inputs)
    # Reordering fields must not swap queries or chunk ranks.
    output["run_1"]["retrievals"] = dict(reversed(list(output["run_1"]["retrievals"].items())))
    for rid, chunks in output["run_1"]["retrievals"].items():
        output["run_1"]["retrievals"][rid] = dict(reversed(list(chunks.items())))
    parsed = build_batch_schema(slots).model_validate(output)
    judgment = decode_batch(parsed, slots).runs[0]
    validate_judgment(judgment, original)
    assert judgment.run_id == "saved-run"
    assert [r.retrieval_id for r in judgment.retrievals] == [
        f"saved-retrieval-{i}" for i in range(3)
    ]
    for i, retrieval in enumerate(judgment.retrievals):
        assert [c.chunk_id for c in retrieval.chunks] == [f"shared-{j}" for j in range(5)]
        assert [c.relevance for c in retrieval.chunks] == [i] * 5
    assert original == before


@pytest.mark.parametrize("failure", ["merge", "missing_chunk", "invented_chunk", "citation"])
def test_missing_merged_or_invented_results_fail_schema_validation(failure):
    slots = {"run_1": payload()}
    output = output_for(batch_inputs(slots))
    run = output["run_1"]
    if failure == "merge":
        # The reported failure: three sets of five merged into one set of six.
        run["retrievals"].pop("retrieval_2")
        run["retrievals"].pop("retrieval_3")
        run["retrievals"]["retrieval_1"]["chunk_6"] = {"relevance": 1, "reason": "Merged"}
    elif failure == "missing_chunk":
        run["retrievals"]["retrieval_1"].pop("chunk_1")
    elif failure == "invented_chunk":
        run["retrievals"]["retrieval_1"]["invented"] = {"relevance": 1, "reason": "Invented"}
    else:
        run["claims"] = [
            {
                "claim": "Unverified",
                "verdict": "supported",
                "evidence_ids": ["saved-retrieval-0:shared-4"],
                "reason": "Hidden chunk",
            }
        ]
    with pytest.raises(ValidationError):
        build_batch_schema(slots).model_validate(output)


def test_empty_retrievals_and_no_answer_evidence_are_supported():
    original = payload()
    original["retrievals"] = []
    original["evidence"] = []
    slots = {"run_1": original}
    output = output_for(batch_inputs(slots))
    parsed = build_batch_schema(slots).model_validate(output)
    judgment = decode_batch(parsed, slots).runs[0]
    assert judgment.retrievals == []
    validate_judgment(judgment, original)
    output["run_1"]["claims"] = [
        {
            "claim": "No evidence",
            "verdict": "unsupported",
            "evidence_ids": [],
            "reason": "No evidence",
        }
    ]
    build_batch_schema(slots).model_validate(output)
    output["run_1"]["claims"][0]["evidence_ids"] = ["input"]
    with pytest.raises(ValidationError):
        build_batch_schema(slots).model_validate(output)
