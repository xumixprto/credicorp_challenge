import asyncio
import io
import json
import logging
import sys
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from backend.app.core.config import Settings
from backend.app.main import create_app
from backend.app.tools.alpaca import (
    ALPACA_SERVER_PACKAGE,
    AlpacaStdioTool,
    alpaca_connection,
    create_alpaca_tool,
)
from shared.logging_config import JsonFormatter


def configured(settings):
    return settings.model_copy(
        update={
            "alpaca_mcp_enabled": True,
            "alpaca_api_key": SecretStr("test-alpaca-key"),
            "alpaca_secret_key": SecretStr("test-alpaca-secret"),
        }
    )


def test_process_configuration_pins_server_and_passes_only_required_settings(app_settings):
    settings = configured(app_settings)
    settings.alpaca_toolsets = "stock-data,trading,news,account"
    parameters = create_alpaca_tool(settings)
    assert parameters.command == sys.executable
    assert parameters.args == ["-m", "uv", "tool", "run", ALPACA_SERVER_PACKAGE]
    assert parameters.env["ALPACA_API_KEY"] == "test-alpaca-key"
    assert parameters.env["ALPACA_SECRET_KEY"] == "test-alpaca-secret"
    assert parameters.env["ALPACA_PAPER_TRADE"] == "true"
    assert parameters.env["ALPACA_TOOLSETS"] == settings.alpaca_toolsets
    assert "OPENAI_API_KEY" not in parameters.env
    assert "test-alpaca-key" not in " ".join(parameters.args)
    settings.alpaca_paper_trade = False
    assert create_alpaca_tool(settings).env["ALPACA_PAPER_TRADE"] == "false"


@pytest.mark.parametrize("toolsets", ["", "stock-data,", "unknown", "all"])
def test_invalid_toolsets_do_not_silently_enable_every_tool(toolsets):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, alpaca_toolsets=toolsets)


def test_toolset_whitespace_and_duplicates_are_normalized():
    assert (
        Settings(_env_file=None, alpaca_toolsets=" stock-data,news,stock-data ").alpaca_toolsets
        == "stock-data,news"
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_disabled_or_unconfigured_alpaca_does_not_launch(app_settings, monkeypatch, enabled):
    def unexpected(settings):
        pytest.fail("MCP must not launch without enabled credentials")

    monkeypatch.setattr("backend.app.tools.alpaca.create_alpaca_tool", unexpected)
    settings = app_settings.model_copy(
        update={
            "alpaca_mcp_enabled": enabled,
            "alpaca_api_key": SecretStr(""),
            "alpaca_secret_key": SecretStr(""),
        }
    )

    async def run():
        async with alpaca_connection(settings) as tool:
            assert tool is None

    asyncio.run(run())


def test_incomplete_credentials_fail_with_actionable_error(app_settings):
    settings = configured(app_settings)
    settings.alpaca_secret_key = SecretStr("")

    async def run():
        async with alpaca_connection(settings):
            pytest.fail("Incomplete credentials must fail startup")

    with pytest.raises(RuntimeError, match="Set both ALPACA_API_KEY and ALPACA_SECRET_KEY"):
        asyncio.run(run())


def test_startup_failure_closes_document_service_and_hides_exception_details(
    app_settings, monkeypatch
):
    class BrokenClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            raise RuntimeError("private provider details test-alpaca-secret")

        async def __aexit__(self, *args):
            pass

    class Documents:
        closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        "backend.app.tools.alpaca.create_alpaca_tool", lambda settings: BrokenClient()
    )
    documents = Documents()
    app = create_app(configured(app_settings), document_service=documents)
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JsonFormatter("backend"))
    logger = logging.getLogger("credicorp.backend.alpaca")
    logger.addHandler(handler)
    try:
        with pytest.raises(RuntimeError, match="Alpaca MCP startup failed") as error:
            with TestClient(app):
                pytest.fail("Application must not report successful startup")
    finally:
        logger.removeHandler(handler)
        handler.close()
    assert "test-alpaca-secret" not in str(error.value)
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    failure = next(event for event in events if event["message"] == "alpaca_mcp_start_failed")
    assert failure["reason"] == "private provider details [redacted]"
    assert "test-alpaca-secret" not in output.getvalue()
    assert documents.closed


@pytest.mark.parametrize("fails", [False, True])
def test_agent_mcp_round_trip_lifecycle_and_function_logging(
    app_settings, model_stub, monkeypatch, fails, tmp_path, capfd
):
    launches = []
    stderr_path = tmp_path / "logs" / "alpaca-mcp.log"

    def parameters(settings):
        launches.append(True)
        return AlpacaStdioTool(
            stderr_path=stderr_path,
            name="alpaca",
            tool_name_prefix="alpaca",
            load_prompts=False,
            command=sys.executable,
            args=[str(Path(__file__).parents[1] / "fixtures" / "alpaca_mcp_server.py")],
        )

    monkeypatch.setattr("backend.app.tools.alpaca.create_alpaca_tool", parameters)
    original_respond = model_stub.respond

    async def respond(request):
        response = await original_respond(request)
        # Each turn starts without tool output from that turn. Alternate tool call/final answer.
        if len(model_stub.requests) % 2:
            body = response.json()
            body["output"] = [
                {
                    "type": "function_call",
                    "id": f"fc_{len(model_stub.requests)}",
                    "call_id": f"call_{len(model_stub.requests)}",
                    "status": "completed",
                    "name": "alpaca_get_stock_latest_quote",
                    "arguments": json.dumps({"symbol": "FAIL" if fails else "NVDA", "feed": "iex"}),
                }
            ]
            return httpx2.Response(200, json=body)
        return response

    monkeypatch.setattr(model_stub, "respond", respond)
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JsonFormatter("backend"))
    logger = logging.getLogger("credicorp.backend")
    app = create_app(configured(app_settings))
    logger.addHandler(handler)
    try:
        with TestClient(app) as client:
            tool = app.state.agent_service.runtime.alpaca
            assert tool.is_connected
            for index in range(2):
                response = client.post(
                    "/agent",
                    json={
                        "userId": "d5bbf9d1-5945-44d8-bb10-0065164a8356",
                        "sessionId": f"alpaca-{index}",
                        "query": "Latest NVDA quote?",
                        "messageType": "TEXT",
                    },
                    headers={"X-Request-ID": f"alpaca-request-{index}"},
                )
                assert response.status_code == 200
            assert launches == [True]  # One process for multiple independent chat sessions.
        assert not tool.is_connected
        assert app.state.agent_service.runtime.alpaca is None
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert len(model_stub.requests) == 4
    names = [tool["name"] for tool in model_stub.requests[0]["tools"]]
    assert "retrieve_documents" in names
    assert "alpaca_get_stock_latest_quote" in names
    results = []
    for request in model_stub.requests[1::2]:
        result = next(
            item["output"]
            for item in request["input"]
            if item.get("type") == "function_call_output"
        )
        results.append(result)
        assert ("100.25" if not fails else "error") in result.lower()
    if not fails:
        assert results[0] == results[1]  # Both calls ran in the same server process.
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    calls = [event for event in events if event["message"] == "function_call_started"]
    assert len(calls) == 2
    assert all(event["function_name"] == "alpaca_get_stock_latest_quote" for event in calls)
    assert {event["request_id"] for event in calls} == {"alpaca-request-0", "alpaca-request-1"}
    assert any(event["message"] == "alpaca_mcp_connected" for event in events)
    assert any(event["message"] == "alpaca_mcp_stopped" for event in events)
    assert "test-alpaca-secret" not in output.getvalue()
    assert "Alpaca fixture started" in stderr_path.read_text(encoding="utf-8")
    assert "Alpaca fixture started" not in capfd.readouterr().err
    # On Windows this also verifies the inherited file handle was released on shutdown.
    stderr_path.rename(stderr_path.with_suffix(".closed.log"))


@pytest.mark.parametrize("approved", [True, False])
def test_actual_mcp_write_requires_approval(
    app_settings, model_stub, monkeypatch, tmp_path, approved
):
    orders = tmp_path / "orders.jsonl"

    def parameters(settings):
        return AlpacaStdioTool(
            stderr_path=tmp_path / "alpaca-mcp.log",
            name="alpaca",
            tool_name_prefix="alpaca",
            load_prompts=False,
            command=sys.executable,
            args=[str(Path(__file__).parents[1] / "fixtures" / "alpaca_mcp_server.py")],
            env={"TEST_ALPACA_ORDER_LOG": str(orders)},
        )

    monkeypatch.setattr("backend.app.tools.alpaca.create_alpaca_tool", parameters)
    original = model_stub.respond

    async def respond(request):
        response = await original(request)
        if len(model_stub.requests) == 1:
            body = response.json()
            body["output"] = [
                {
                    "type": "function_call",
                    "id": "fc_trade",
                    "call_id": "call_trade",
                    "name": "alpaca_place_stock_order",
                    "status": "completed",
                    "arguments": json.dumps({"symbol": "AAPL", "qty": 1, "side": "buy"}),
                }
            ]
            return httpx2.Response(200, json=body)
        return response

    monkeypatch.setattr(model_stub, "respond", respond)
    payload = {
        "userId": "d5bbf9d1-5945-44d8-bb10-0065164a8356",
        "sessionId": "mcp-approval",
        "query": "Buy one AAPL",
    }
    with TestClient(create_app(configured(app_settings))) as client:
        response = client.post("/agent", json=payload)
        assert response.status_code == 200, response.text
        confirmation = response.json()["confirmations"][0]
        assert not orders.exists()
        reply = {
            **payload,
            "query": "",
            "messageType": "HUMAN_FLOW_REPLY",
            "confirmationId": confirmation["confirmationId"],
            "approved": approved,
        }
        response = client.post("/agent", json=reply)
        assert response.status_code == 200, response.text
        assert client.post("/agent", json=reply).json() == response.json()
        if approved:
            assert len(orders.read_text(encoding="utf-8").splitlines()) == 1
        else:
            assert not orders.exists()
