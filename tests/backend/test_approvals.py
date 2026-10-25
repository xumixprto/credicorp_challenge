import json
from uuid import uuid4

import httpx2
import pytest
from agent_framework import tool
from fastapi.testclient import TestClient

from backend.app.main import create_app

PAYLOAD = {
    "userId": "d5bbf9d1-5945-44d8-bb10-0065164a8356",
    "sessionId": "approval-chat",
    "query": "Buy one AAPL",
}


@pytest.fixture
def trade_stub(monkeypatch, model_stub):
    calls = []

    @tool
    def alpaca_place_stock_order(symbol: str, qty: int, side: str) -> str:
        """Place a test order without accessing any account."""
        calls.append({"symbol": symbol, "qty": qty, "side": side})
        return "Test order accepted"

    monkeypatch.setattr(
        "backend.app.agent.runtime.get_tools", lambda settings: [alpaca_place_stock_order]
    )
    original = model_stub.respond

    async def respond(request):
        response = await original(request)
        if len(model_stub.requests) == 1:
            body = response.json()
            body["output"] = [
                {
                    "type": "function_call",
                    "id": "fc_order",
                    "call_id": "call_order",
                    "name": "alpaca_place_stock_order",
                    "arguments": json.dumps({"symbol": "AAPL", "qty": 1, "side": "buy"}),
                    "status": "completed",
                }
            ]
            return httpx2.Response(200, json=body)
        return response

    monkeypatch.setattr(model_stub, "respond", respond)
    return calls


def decision(confirmation_id, approved=True, **updates):
    return {
        **PAYLOAD,
        "query": "",
        "messageType": "HUMAN_FLOW_REPLY",
        "confirmationId": confirmation_id,
        "approved": approved,
        **updates,
    }


def propose(client):
    response = client.post("/agent", json=PAYLOAD, headers={"X-Request-ID": "proposal"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmation_required"
    return response.json()["confirmations"][0]["confirmationId"]


@pytest.mark.parametrize("approved", [True, False])
def test_resume_after_restart_and_retry(
    backend_app, app_settings, model_stub, trade_stub, approved
):
    with TestClient(backend_app) as client:
        confirmation_id = propose(client)
        assert trade_stub == []
        assert len(model_stub.requests) == 1
        repeat = client.post("/agent", json=PAYLOAD, headers={"X-Request-ID": "proposal"})
        assert repeat.json()["confirmations"][0]["confirmationId"] == confirmation_id
        assert client.post("/agent", json=PAYLOAD).status_code == 409
        assert (
            client.post("/agent", json={**PAYLOAD, "sessionId": "another-chat"}).status_code == 200
        )
        assert (
            client.post(
                "/agent", json=decision(confirmation_id, sessionId="another-chat")
            ).status_code
            == 404
        )
        assert (
            client.post("/agent", json=decision(confirmation_id, userId=str(uuid4()))).status_code
            == 404
        )
        public = client.get(
            "/conversations/detail", params={k: PAYLOAD[k] for k in ("userId", "sessionId")}
        ).json()
        assert "approvals" not in public
        assert public["confirmations"][0]["arguments"] == {
            "symbol": "AAPL",
            "qty": 1,
            "side": "buy",
        }
    with TestClient(create_app(app_settings)) as client:
        response = client.post("/agent", json=decision(confirmation_id, approved))
        assert response.status_code == 200, response.text
        assert response.json() == {"message": "Hello World"}
        assert len(trade_stub) == int(approved)
        count = len(model_stub.requests)
        assert (
            client.post("/agent", json=decision(confirmation_id, approved)).json()
            == response.json()
        )
        assert len(model_stub.requests) == count
        assert len(trade_stub) == int(approved)
        assert (
            client.post("/agent", json=decision(confirmation_id, not approved)).status_code == 409
        )
        assert client.post("/agent", json=PAYLOAD).status_code == 200
        forwarded = json.dumps(model_stub.requests[-2]["input"])
        assert "function_call_output" in forwarded


def test_failed_continuation_never_reexecutes(backend_app, model_stub, trade_stub):
    with TestClient(backend_app) as client:
        confirmation_id = propose(client)
        model_stub.status_code = 500
        response = client.post("/agent", json=decision(confirmation_id))
        assert response.status_code == 502
        assert len(trade_stub) == 1
        model_stub.status_code = 200
        assert client.post("/agent", json=decision(confirmation_id)).status_code == 409
        assert client.post("/agent", json=PAYLOAD).status_code == 409
        assert (
            client.post("/agent", json={**PAYLOAD, "sessionId": "another-chat"}).status_code == 200
        )
        assert len(trade_stub) == 1


def test_account_mode_change_requires_new_review(backend_app, app_settings, trade_stub):
    with TestClient(backend_app) as client:
        confirmation_id = propose(client)
        app_settings.alpaca_paper_trade = not app_settings.alpaca_paper_trade
        assert client.post("/agent", json=decision(confirmation_id)).status_code == 409
        assert trade_stub == []
        assert client.post("/agent", json=decision(confirmation_id, False)).status_code == 200


@pytest.mark.parametrize(
    "updates",
    [
        {"approved": "true"},
        {"query": "yes"},
        {"arguments": {"qty": 100}},
        {"confirmationId": str(uuid4())},
    ],
)
def test_invalid_or_unknown_decision(backend_app, trade_stub, updates):
    with TestClient(backend_app) as client:
        confirmation_id = propose(client)
        response = client.post("/agent", json={**decision(confirmation_id), **updates})
        assert response.status_code in (422, 404)
        assert trade_stub == []


def test_multiple_operations_require_separate_decisions(
    backend_app, model_stub, trade_stub, monkeypatch
):
    original = model_stub.respond

    async def respond(request):
        response = await original(request)
        if len(model_stub.requests) == 1:
            body = response.json()
            second = dict(
                body["output"][0],
                id="fc_second",
                call_id="call_second",
                arguments=json.dumps({"symbol": "NVDA", "qty": 2, "side": "sell"}),
            )
            body["output"].append(second)
            return httpx2.Response(200, json=body)
        return response

    monkeypatch.setattr(model_stub, "respond", respond)
    with TestClient(backend_app) as client:
        response = client.post("/agent", json=PAYLOAD)
        assert response.status_code == 200, response.text
        first, second = response.json()["confirmations"]
        assert trade_stub == []
        response = client.post("/agent", json=decision(first["confirmationId"]))
        assert response.status_code == 200, response.text
        assert len(trade_stub) == 1
        assert response.json()["confirmations"][0]["confirmationId"] == second["confirmationId"]
        assert client.post("/agent", json=PAYLOAD).status_code == 409
        response = client.post("/agent", json=decision(second["confirmationId"], False))
        assert response.status_code == 200, response.text
        assert len(trade_stub) == 1
        assert client.post("/agent", json=PAYLOAD).status_code == 200


def test_tool_error_is_not_reported_as_completed(backend_app, trade_stub, monkeypatch):
    @tool
    def alpaca_place_stock_order(symbol: str, qty: int, side: str) -> str:
        """Simulate an ambiguous transport failure after submitting an order."""
        trade_stub.append(symbol)
        raise ConnectionError("Disconnected after submitting")

    monkeypatch.setattr(
        "backend.app.agent.runtime.get_tools", lambda settings: [alpaca_place_stock_order]
    )
    with TestClient(backend_app) as client:
        confirmation_id = propose(client)
        response = client.post("/agent", json=decision(confirmation_id))
        assert response.status_code == 502, response.text
        assert client.post("/agent", json=decision(confirmation_id)).status_code == 409
        assert len(trade_stub) == 1


def test_claim_must_be_saved_before_executing(backend_app, trade_stub, monkeypatch):
    from backend.app.core.errors import ApplicationError

    with TestClient(backend_app) as client:
        confirmation_id = propose(client)

        def unavailable(record):
            raise ApplicationError(503, "Storage unavailable")

        monkeypatch.setattr(backend_app.state.agent_service.repository, "save", unavailable)
        assert client.post("/agent", json=decision(confirmation_id)).status_code == 503
        assert trade_stub == []


def test_disconnected_mcp_write_is_never_retried(app_settings, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, Mock

    from agent_framework.exceptions import ToolExecutionException
    from anyio import ClosedResourceError

    from backend.app.tools.alpaca import create_alpaca_tool

    mcp = create_alpaca_tool(app_settings)
    connection = Mock()
    connection.call_tool = AsyncMock(side_effect=ClosedResourceError)
    monkeypatch.setattr(mcp, "_require_connection", lambda: connection)
    recovery = AsyncMock()
    monkeypatch.setattr(mcp, "_recover_connection_after_loss", recovery)
    with pytest.raises(ToolExecutionException, match="outcome is unknown"):
        asyncio.run(
            mcp._call_tool_with_retries("place_stock_order", {"symbol": "AAPL"}, None, str, Mock())
        )
    assert connection.call_tool.await_count == 1
    recovery.assert_not_called()


@pytest.mark.parametrize("approved", [True, False])
def test_streamlit_confirmation_locks_only_current_chat(
    backend_app, trade_stub, monkeypatch, approved
):
    from functools import partial

    import httpx
    from streamlit.testing.v1 import AppTest

    from frontend.services.agent_client import AgentClient
    from shared.settings import PROJECT_ROOT

    decisions = []
    with TestClient(backend_app) as backend:

        def handle(request):
            if request.url.path == "/agent":
                payload = json.loads(request.content)
                if payload.get("messageType") == "HUMAN_FLOW_REPLY":
                    decisions.append((payload, request.headers["X-Request-ID"]))
                    if len(decisions) == 1:
                        raise httpx.ConnectError("Offline", request=request)
            response = backend.request(
                request.method,
                str(request.url),
                content=request.content,
                headers=dict(request.headers),
            )
            return httpx.Response(response.status_code, json=response.json())

        monkeypatch.setattr(
            "frontend.services.agent_client.AgentClient",
            partial(AgentClient, transport=httpx.MockTransport(handle)),
        )
        path = str(PROJECT_ROOT / "frontend" / "app.py")
        app = AppTest.from_file(path).run()
        app.chat_input[0].set_value("Buy one AAPL").run()
        assert not app.exception
        assert app.chat_input[0].disabled
        first_session = app.session_state.active_session_id
        confirmation_id = next(
            button.key.removeprefix("confirm_")
            for button in app.button
            if button.key.startswith("confirm_")
        )
        assert trade_stub == []
        app.button(key="new_chat").click().run()
        assert not app.chat_input[0].disabled
        app.chat_input[0].set_value("Hello in another chat").run()
        assert not app.exception
        app.button(key=f"conversation_{first_session}").click().run()
        assert app.chat_input[0].disabled
        # A fresh browser loads the same approval from conversation JSON.
        app = AppTest.from_file(path).run()
        app.button(key=f"conversation_{first_session}").click().run()
        assert app.chat_input[0].disabled
        action = "confirm" if approved else "reject"
        app.button(key=f"{action}_{confirmation_id}").click().run()
        assert not app.exception
        assert app.chat_input[0].disabled
        app.button(key=f"retry_{confirmation_id}").click().run()
        assert not app.exception
        assert not app.chat_input[0].disabled
        assert len(trade_stub) == int(approved)
        assert decisions[0] == decisions[1]
        assert decisions[0][0]["approved"] is approved
        assert decisions[0][0]["sessionId"] == first_session


def test_failed_rejection_can_be_retried(backend_app, trade_stub, model_stub):
    with TestClient(backend_app) as client:
        confirmation_id = propose(client)
        model_stub.status_code = 500
        assert client.post("/agent", json=decision(confirmation_id, False)).status_code == 502
        assert trade_stub == []
        model_stub.status_code = 200
        assert client.post("/agent", json=decision(confirmation_id, False)).status_code == 200
        assert trade_stub == []
