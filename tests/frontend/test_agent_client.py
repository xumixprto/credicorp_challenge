import json
from uuid import UUID

import httpx
import pytest

from frontend.services.agent_client import AgentAPIError, AgentClient


def test_client_posts_expected_payload_and_request_id():
    captured = []

    def handle(request):
        captured.append(request)
        return httpx.Response(200, json={"message": "Hello World"})

    client = AgentClient("http://api.test/", transport=httpx.MockTransport(handle))
    reply = client.send_message(user_id="user-1", query="thanks!", session_id="session-1")
    assert reply == "Hello World"
    assert captured[0].method == "POST"
    assert str(captured[0].url) == "http://api.test/agent"
    assert UUID(captured[0].headers["X-Request-ID"])
    assert json.loads(captured[0].content) == {
        "userId": "user-1",
        "query": "thanks!",
        "sessionId": "session-1",
    }


@pytest.mark.parametrize("failure", ["timeout", "connection", "server", "json", "schema"])
def test_client_reports_actionable_errors(failure):
    def handle(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if failure == "connection":
            raise httpx.ConnectError("connection refused", request=request)
        if failure == "server":
            return httpx.Response(500, json={"detail": "private server information"})
        if failure == "json":
            return httpx.Response(200, text="not json")
        return httpx.Response(200, json={"message": 42})

    client = AgentClient("http://api.test", transport=httpx.MockTransport(handle))
    with pytest.raises(AgentAPIError) as raised:
        client.send_message(user_id="user-1", query="Hi", session_id="session-1")
    assert UUID(raised.value.request_id)
    assert "try again" in str(raised.value).lower()
    assert "private server information" not in str(raised.value)
