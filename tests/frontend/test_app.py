import json
import os
import subprocess
import sys
from functools import partial

import httpx
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from frontend.services.agent_client import AgentClient
from shared.settings import PROJECT_ROOT


def test_app_starts_without_repository_on_python_path(tmp_path):
    app_path = str(PROJECT_ROOT / "frontend" / "app.py")
    script = (
        "from streamlit.testing.v1 import AppTest; "
        f"app = AppTest.from_file({app_path!r}).run(); "
        "assert not app.exception, [error.message for error in app.exception]; "
        "assert len(app.chat_input) == 1"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "API_BASE_URL": "http://127.0.0.1:1", "API_TIMEOUT_SECONDS": "0.1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_chat_round_trip_and_separate_conversations(monkeypatch, backend_app):
    requests = []
    with TestClient(backend_app) as backend:

        def handle(request):
            if request.url.path == "/agent":
                requests.append(json.loads(request.content))
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
        app = AppTest.from_file(str(PROJECT_ROOT / "frontend" / "app.py")).run()
        assert not app.exception
        assert app.sidebar.radio[0].options == ["Agent Chat"]
        assert len(app.chat_input) == 1

        app.chat_input[0].set_value("thanks!").run()
        assert not app.exception
        assert app.chat_message[0].markdown[0].value == "thanks!"
        assert app.chat_message[1].markdown[0].value == "Hello World"
        first_session = requests[0]["sessionId"]

        app.chat_input[0].set_value("One more message").run()
        assert requests[1]["sessionId"] == first_session
        app.button(key="new_chat").click().run()
        assert len(app.chat_message) == 0
        app.chat_input[0].set_value("A different chat").run()
        assert requests[2]["sessionId"] != first_session
        assert requests[2]["userId"] == requests[0]["userId"]

        app.button(key=f"conversation_{first_session}").click().run()
        assert len(app.chat_message) == 4
        assert app.chat_message[0].markdown[0].value == "thanks!"
        assert len(requests) == 3  # Switching chats must not resend messages.

        # A new browser session reads its chats and transcript back from the API.
        refreshed = AppTest.from_file(str(PROJECT_ROOT / "frontend" / "app.py")).run()
        assert not refreshed.exception
        refreshed.button(key=f"conversation_{first_session}").click().run()
        assert len(refreshed.chat_message) == 4
        assert refreshed.chat_message[0].markdown[0].value == "thanks!"


def test_chat_retry_keeps_one_user_message(monkeypatch, backend_app):
    attempts = []

    with TestClient(backend_app) as backend:

        def handle(request):
            if request.url.path == "/agent":
                attempts.append((json.loads(request.content), request.headers["X-Request-ID"]))
                if len(attempts) == 1:
                    raise httpx.ConnectError("offline", request=request)
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
        app = AppTest.from_file(str(PROJECT_ROOT / "frontend" / "app.py")).run()
        app.chat_input[0].set_value("Please reply").run()
        assert not app.exception
        assert "Unable to reach" in app.error[0].value
        app.button(key="retry_reply").click().run()
        assert not app.exception
        assert len(app.error) == 0
        assert len(app.chat_message) == 2
        assert app.chat_message[1].markdown[0].value == "Hello World"
        assert attempts[0] == attempts[1]
