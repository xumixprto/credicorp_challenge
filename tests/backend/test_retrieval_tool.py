import asyncio
import json

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient

from backend.app.documents.models import RetrieveResponse
from backend.app.tools.retrieval import DocumentRetriever, create_retrieval_tool, format_evidence
from shared.logging_config import request_id_context


def evidence_response():
    return RetrieveResponse.model_validate(
        {
            "collection": "documents_test",
            "distance_metric": "cosine",
            "hits": [
                {
                    "chunk_id": "table-row",
                    "text": "NVIDIA | USD millions | Cost of revenue | Three months ended "
                    "Jul 26, 2026: 24,079 | Six months ended Jul 26, 2026: 44,538",
                    "distance": 0.1,
                    "pages": [4],
                    "headings": ["Statements of income"],
                    "references": [{"ref": "#/tables/0", "table_rows": [3], "bbox": [1, 2]}],
                    "sources": [
                        {
                            "document_id": "nvda",
                            "source_id": "nvda-10q",
                            "filename": "nvda.pdf",
                            "source_uri": "https://example.com/nvda.pdf",
                            "content_hash": "hash",
                        }
                    ],
                }
            ],
        }
    )


def test_evidence_preserves_financial_context_citations_and_rank():
    response = evidence_response()
    first = response.hits[0]
    response.hits = [
        first.model_copy(update={"chunk_id": "less-relevant", "distance": 0.6}),
        first,
        first,
    ]
    result = format_evidence(response, 8000)
    assert result["status"] == "ok"
    assert [hit["chunk_id"] for hit in result["results"]] == ["table-row", "less-relevant"]
    hit = result["results"][0]
    assert hit["text"] == first.text
    assert hit["pages"] == [4]
    assert hit["sources"][0]["filename"] == "nvda.pdf"
    assert hit["sources"][0]["source_uri"] == "https://example.com/nvda.pdf"
    assert hit["locations"] == [{"ref": "#/tables/0", "table_rows": [3]}]
    assert (
        hit["citation_id"]
        == format_evidence(evidence_response(), 8000)["results"][0]["citation_id"]
    )


def test_evidence_budget_omits_whole_chunks_instead_of_cutting_values():
    response = evidence_response()
    original = response.hits[0]
    response.hits.insert(
        0,
        original.model_copy(
            update={"chunk_id": "huge", "text": "long table " * 3000, "distance": 0.01}
        ),
    )
    result = format_evidence(response, 500)
    assert result["omitted_results"] == 1
    assert result["results"][0]["text"] == original.text
    response.hits = response.hits[:1]
    assert format_evidence(response, 500)["status"] == "context_limit"
    response.hits = []
    assert format_evidence(response, 500)["status"] == "no_results"


def test_retriever_posts_query_and_propagates_request_id(app_settings):
    def handle(request):
        assert request.method == "POST"
        assert str(request.url) == "http://127.0.0.1:8000/docs/retrieve"
        assert json.loads(request.content) == {"query": "NVDA cost of revenue July 2026"}
        assert request.headers["X-Request-ID"] == "retrieval-request"
        return httpx.Response(200, json=evidence_response().model_dump())

    token = request_id_context.set("retrieval-request")
    try:
        result = asyncio.run(
            DocumentRetriever(app_settings, httpx.MockTransport(handle)).retrieve(
                " NVDA cost of revenue July 2026 "
            )
        )
    finally:
        request_id_context.reset(token)
    assert result["status"] == "ok"


def test_retriever_forwards_document_filter(app_settings):
    def handle(request):
        assert json.loads(request.content) == {
            "query": "Annual cash flow operating activities capex",
            "document_ids": ["nvda"],
        }
        return httpx.Response(200, json=evidence_response().model_dump())

    result = asyncio.run(
        DocumentRetriever(app_settings, httpx.MockTransport(handle)).retrieve(
            "Annual cash flow operating activities capex", document_ids=["nvda"]
        )
    )
    assert result["status"] == "ok"
    assert "document_ids" in create_retrieval_tool(app_settings).parameters()["properties"]


@pytest.mark.parametrize("failure", ["timeout", "server", "schema", "json"])
def test_retrieval_errors_are_not_reported_as_no_results(app_settings, failure):
    def handle(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("private detail", request=request)
        if failure == "server":
            return httpx.Response(500, text="private detail")
        if failure == "schema":
            return httpx.Response(200, json={"private detail": True})
        return httpx.Response(200, text="private detail")

    result = asyncio.run(
        DocumentRetriever(app_settings, httpx.MockTransport(handle)).retrieve("NVDA")
    )
    assert result["status"] == "error"
    assert result["error"] == ("timeout" if failure == "timeout" else "retrieval_failed")
    assert result["results"] == []
    assert "private detail" not in json.dumps(result)


def test_framework_calls_retrieval_endpoint_and_forwards_evidence(
    backend_app, model_stub, monkeypatch
):
    queries = []

    async def retrieve(payload):
        queries.append(payload.query)
        return evidence_response()

    monkeypatch.setattr(
        "backend.app.agent.runtime.get_tools",
        lambda settings: [create_retrieval_tool(settings, httpx.ASGITransport(app=backend_app))],
    )
    original_respond = model_stub.respond

    async def respond(request):
        response = await original_respond(request)
        if len(model_stub.requests) == 1:
            body = response.json()
            body["output"] = [
                {
                    "type": "function_call",
                    "id": "fc_retrieve",
                    "call_id": "call_retrieve",
                    "name": "retrieve_documents",
                    "status": "completed",
                    "arguments": json.dumps({"query": "NVDA cost of revenue July 2026"}),
                }
            ]
            return httpx2.Response(200, json=body)
        return response

    monkeypatch.setattr(model_stub, "respond", respond)
    with TestClient(backend_app) as client:
        monkeypatch.setattr(backend_app.state.document_service, "retrieve", retrieve)
        response = client.post(
            "/agent",
            json={
                "userId": "d5bbf9d1-5945-44d8-bb10-0065164a8356",
                "sessionId": "retrieval",
                "query": "NVDA cost of revenue July 2026",
                "messageType": "TEXT",
            },
        )
    assert response.status_code == 200
    assert queries == ["NVDA cost of revenue July 2026"]
    assert len(model_stub.requests) == 2
    outputs = [
        item
        for item in model_stub.requests[1]["input"]
        if item.get("type") == "function_call_output"
    ]
    assert len(outputs) == 1
    evidence = json.loads(outputs[0]["output"])
    assert evidence["status"] == "ok"
    assert evidence["results"][0]["text"] == evidence_response().hits[0].text
    assert evidence["results"][0]["sources"][0]["filename"] == "nvda.pdf"
    runs, _ = backend_app.state.judge_service.store.snapshot()
    assert len(runs) == 1
    trace = runs[0]
    retrieval = trace.retrievals_json[0]
    assert retrieval["raw_response"]["collection"] == "documents_test"
    assert retrieval["raw_response"]["hits"][0]["text"] == evidence_response().hits[0].text
    assert retrieval["visible_evidence"] == evidence
    assert trace.tools_json[0]["name"] == "retrieve_documents"
    assert trace.tools_json[0]["status"] == "completed"
