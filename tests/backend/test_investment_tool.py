import json

import httpx
import httpx2
from agent_framework import tool
from fastapi.testclient import TestClient

from backend.app.tools import get_tools
from backend.app.tools.investment import create_investment_tool
from backend.app.tools.retrieval import create_retrieval_tool
from tests.backend.test_investment import request_data


def test_registry_adds_only_assessment_tool(app_settings):
    registered = get_tools(app_settings)
    assert [t.name for t in registered] == ["retrieve_documents", "build_investment_assessment"]
    parameters = registered[1].parameters()
    assert {
        "ticker",
        "financial_data",
        "market_data",
        "risk_tolerance",
        "horizon_months",
        "as_of_date",
    } <= set(parameters["properties"])


def test_agent_orchestrates_retrieval_market_and_analysis_without_trading(
    backend_app, app_settings, model_stub, monkeypatch
):
    supplied = request_data("NVDA")
    observed = []

    def retrieve(request):
        observed.append("retrieval")
        return httpx.Response(
            200,
            json={
                "collection": "test",
                "distance_metric": "cosine",
                "hits": [
                    {
                        "chunk_id": "synthetic-filings",
                        "text": json.dumps(supplied["financial_data"]),
                        "distance": 0.1,
                        "pages": [1],
                        "headings": ["Mock financial statements"],
                        "references": [],
                        "sources": [
                            {
                                "document_id": "test",
                                "source_id": "test",
                                "filename": "synthetic.pdf",
                                "source_uri": None,
                                "content_hash": "test",
                            }
                        ],
                    }
                ],
            },
        )

    @tool
    async def alpaca_get_stock_bars(symbol: str) -> dict:
        """Mock the existing Alpaca tool boundary; no market/trading API calls."""
        assert symbol == "NVDA"
        observed.append("market")
        return supplied["market_data"]

    # Three independent capabilities. The new tool receives data, never either provider.
    monkeypatch.setattr(
        "backend.app.agent.runtime.get_tools",
        lambda settings: [
            create_retrieval_tool(
                settings.model_copy(update={"retrieval_tool_max_tokens": 32000}),
                httpx.MockTransport(retrieve),
            ),
            create_investment_tool(settings),
            alpaca_get_stock_bars,
        ],
    )
    original = model_stub.respond

    async def respond(request):
        response = await original(request)
        step = len(model_stub.requests)
        calls = [
            ("retrieve_documents", {"query": "NVDA 10-K financial statements"}),
            ("alpaca_get_stock_bars", {"symbol": "NVDA"}),
            ("build_investment_assessment", supplied),
        ]
        if step <= len(calls):
            name, arguments = calls[step - 1]
            body = response.json()
            body["output"] = [
                {
                    "type": "function_call",
                    "id": f"fc_{step}",
                    "call_id": f"call_{step}",
                    "status": "completed",
                    "name": name,
                    "arguments": json.dumps(arguments),
                }
            ]
            return httpx2.Response(200, json=body)
        return response

    monkeypatch.setattr(model_stub, "respond", respond)
    with TestClient(backend_app) as client:
        response = client.post(
            "/agent",
            json={
                "userId": "d5bbf9d1-5945-44d8-bb10-0065164a8356",
                "sessionId": "assessment",
                "query": "Evaluate NVDA for 12 months",
                "messageType": "TEXT",
            },
        )
        assert response.status_code == 200
        runs, _ = backend_app.state.judge_service.store.snapshot()
    assert observed == ["retrieval", "market"]
    assert len(model_stub.requests) == 4
    assert [t["name"] for t in runs[0].tools_json] == [
        "retrieve_documents",
        "alpaca_get_stock_bars",
        "build_investment_assessment",
    ]
    assessment = runs[0].tools_json[-1]
    assert assessment["status"] == "completed"
    result = json.loads(
        "".join(block["text"] for block in assessment["result"] if block["type"] == "text")
    )
    assert result["ticker"] == "NVDA"
    assert result["risk_tolerance"] == 2
    assert result["fundamentals"]["metrics"]["gross_margin"]["value"] == 0.6
    assert runs[0].retrievals_json[0]["visible_evidence"]["status"] == "ok"
    assert runs[0].status == "completed"
