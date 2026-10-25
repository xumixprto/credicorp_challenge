import asyncio
import json

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient

from backend.app.tools import get_tools
from backend.app.tools.investment import create_investment_tool
from backend.app.tools.retrieval import create_retrieval_tool
from tests.backend.test_investment import AS_OF, request_data
from tests.backend.test_investment_providers import FakeAlpaca, FakeYahoo
from tests.backend.test_retrieval_tool import evidence_response


def test_registry_exposes_only_simple_assessment_parameters(app_settings):
    registered = get_tools(app_settings)
    assert [t.name for t in registered] == ["retrieve_documents", "build_investment_assessment"]
    assert set(registered[1].parameters()["properties"]) == {
        "ticker",
        "risk_tolerance",
        "horizon_months",
    }


def test_followups_fetch_fresh_data_without_agent_supplied_numbers(app_settings, monkeypatch):
    monkeypatch.setattr("backend.app.investment.providers.yahoo.yf.Ticker", FakeYahoo)
    alpaca = FakeAlpaca()
    tool = create_investment_tool(app_settings, alpaca)
    for _ in range(2):
        result = asyncio.run(tool.invoke(arguments={"ticker": "MSFT"}, skip_parsing=True))
        assert result["data_collection"]["stock_prices"] == 101
        assert result["data_collection"]["benchmark_prices"] == 101
        assert result["risk"]["usable_stock_returns"] == 100
        assert result["risk"]["market"]["metrics"]["beta"]["value"] == pytest.approx(2)
        assert result["fundamentals"]["metrics"]["gross_margin"]["value"] == 0.6
        assert result["risk_tolerance"] == 2 and result["horizon_months"] == 12
        assert not any("Annual financial observations" in r for r in result["reasons"])
    assert len(alpaca.calls) == 4


def test_provider_failure_and_unsupported_asset_are_not_scored(app_settings, monkeypatch):
    monkeypatch.setattr("backend.app.investment.providers.yahoo.yf.Ticker", FakeYahoo)
    alpaca = FakeAlpaca()
    tool = create_investment_tool(app_settings, alpaca)
    result = asyncio.run(tool.invoke(arguments={"ticker": "AGG"}, skip_parsing=True))
    assert result["status"] == "unsupported_asset"
    assert "recommendation" not in result
    assert alpaca.calls == []

    def fail(*args, **kwargs):
        raise RuntimeError("private details")

    monkeypatch.setattr("backend.app.tools.investment.fetch_financials", fail)
    result = asyncio.run(tool.invoke(arguments={"ticker": "NVDA"}, skip_parsing=True))
    assert result["status"] == "data_unavailable"
    assert "private details" not in str(result)
    assert "recommendation" not in result


def test_missing_alpaca_keeps_financial_analysis_but_reports_missing_prices(
    app_settings, monkeypatch
):
    monkeypatch.setattr("backend.app.investment.providers.yahoo.yf.Ticker", FakeYahoo)
    result = asyncio.run(
        create_investment_tool(app_settings).invoke(
            arguments={"ticker": "NVDA"},
            skip_parsing=True,
        )
    )
    assert result["recommendation"] == "NO TRADE"
    assert result["data_collection"]["stock_prices"] == 0
    assert result["data_collection"]["financial_observations"] > 0
    assert "Alpaca is unavailable" in " ".join(result["data_quality"]["warnings"])


def test_agent_research_and_assessment_provider_calls_are_recorded(
    backend_app,
    model_stub,
    monkeypatch,
):
    monkeypatch.setattr("backend.app.investment.providers.yahoo.yf.Ticker", FakeYahoo)
    alpaca = FakeAlpaca()
    monkeypatch.setattr(
        "backend.app.agent.runtime.get_tools",
        lambda settings: [
            create_retrieval_tool(
                settings,
                httpx.MockTransport(
                    lambda request: httpx.Response(200, json=evidence_response().model_dump())
                ),
            ),
            create_investment_tool(settings, alpaca),
        ],
    )
    original = model_stub.respond

    async def respond(request):
        response = await original(request)
        step = len(model_stub.requests)
        calls = [
            ("retrieve_documents", {"query": "NVDA business risks"}),
            (
                "build_investment_assessment",
                {"ticker": "NVDA", "risk_tolerance": 2, "horizon_months": 12},
            ),
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
                "query": "Assess NVDA and its business risks",
                "messageType": "TEXT",
            },
        )
        assert response.status_code == 200
        runs, _ = backend_app.state.judge_service.store.snapshot()
    calls = runs[0].tools_json
    assert [c["name"] for c in calls] == [
        "retrieve_documents",
        "build_investment_assessment",
        "yfinance_financials",
        "alpaca_get_stock_bars",
        "alpaca_get_stock_latest_trade",
    ]
    assert all(c["status"] == "completed" for c in calls)
    assert calls[2]["result"]["financial_data"]["observations"]
    result = json.loads("".join(b["text"] for b in calls[1]["result"]))
    assert result["data_collection"]["financial_observations"] > 50
    assert result["data_collection"]["stock_prices"] == 101
    assert runs[0].retrievals_json[0]["visible_evidence"]["status"] == "ok"


def test_trace_serialization_keeps_dates_and_hides_unknown_objects():
    from backend.app.investment.models import AssessmentInput
    from backend.app.middleware.tool_trace import serialize_result

    request = AssessmentInput.model_validate(request_data())
    serialized = serialize_result(
        {"financial_data": request.financial_data, "date": AS_OF, "internal": object()}
    )
    assert serialized["date"] == AS_OF.isoformat()
    assert serialized["financial_data"] == request.financial_data.model_dump(mode="json")
    assert serialized["internal"] == "[unserializable]"
