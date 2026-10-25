import io
import json
import logging

import httpx2
import pytest
from agent_framework import tool
from fastapi.testclient import TestClient

from shared.logging_config import JsonFormatter


@pytest.mark.parametrize("fails", [False, True])
def test_agent_tool_calls_use_function_middleware(backend_app, model_stub, monkeypatch, fails):
    invoked = []

    @tool
    def probe(secret: str) -> str:
        """Exercise a tool call in the middleware integration test."""
        invoked.append(secret)
        if fails:
            raise RuntimeError("private exception text")
        return "private tool result"

    monkeypatch.setattr("backend.app.agent.runtime.get_tools", lambda settings: [probe])
    original_respond = model_stub.respond

    async def respond(request):
        response = await original_respond(request)
        if len(model_stub.requests) == 1:
            body = response.json()
            body["output"] = [
                {
                    "type": "function_call",
                    "id": "fc_probe",
                    "call_id": "call_probe",
                    "name": "probe",
                    "arguments": json.dumps({"secret": "private tool argument"}),
                    "status": "completed",
                }
            ]
            return httpx2.Response(200, json=body)
        return response

    monkeypatch.setattr(model_stub, "respond", respond)
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JsonFormatter("backend"))
    logger = logging.getLogger("credicorp.backend.functions")
    logger.addHandler(handler)
    try:
        with TestClient(backend_app) as client:
            response = client.post(
                "/agent",
                json={
                    "userId": "d5bbf9d1-5945-44d8-bb10-0065164a8356",
                    "sessionId": "tool-session",
                    "query": "Call the probe tool.",
                },
                headers={"X-Request-ID": "tool-request"},
            )
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert response.status_code == 200
    assert invoked == ["private tool argument"]
    assert len(model_stub.requests) == 2
    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [event["message"] for event in events] == [
        "function_call_started",
        "function_call_failed" if fails else "function_call_completed",
    ]
    assert all(event["function_name"] == "probe" for event in events)
    assert all(event["session_id"] == "tool-session" for event in events)
    assert all(event["request_id"] == "tool-request" for event in events)
    assert events[-1]["duration_ms"] >= 0
    if fails:
        assert events[-1]["error_type"]
    assert "private tool argument" not in output.getvalue()
    assert "private tool result" not in output.getvalue()
    assert "private exception text" not in output.getvalue()
    # The middleware preserves the framework's tool output/error handling.
    forwarded = json.dumps(model_stub.requests[1]["input"])
    assert "function_call_output" in forwarded
    if not fails:
        assert "private tool result" in forwarded
