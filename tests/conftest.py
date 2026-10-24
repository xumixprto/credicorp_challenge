import asyncio
import json

import httpx2
import pytest
from openai import AsyncOpenAI

from backend.app.core.config import Settings
from backend.app.main import create_app


class ModelStub:
    """Only the OpenAI HTTP boundary is mocked; Agent Framework runs normally."""

    def __init__(self):
        self.requests = []
        self.status_code = 200
        self.delay = 0

    async def respond(self, request):
        payload = json.loads(request.content)
        self.requests.append(payload)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.status_code != 200:
            return httpx2.Response(
                self.status_code,
                json={"error": {"message": "private provider error", "type": "test_error"}},
            )
        text = "Alice" if "My name is Alice." in json.dumps(payload["input"]) else "Hello World"
        return httpx2.Response(
            200,
            json={
                "id": f"resp_{len(self.requests)}",
                "object": "response",
                "created_at": 1700000000,
                "status": "completed",
                "model": payload["model"],
                "output": [
                    {
                        "type": "message",
                        "id": "msg_test",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": text, "annotations": []}],
                    }
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "total_tokens": 12,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )


@pytest.fixture
def model_stub(monkeypatch):
    stub = ModelStub()

    def make_client(**kwargs):
        return AsyncOpenAI(
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(stub.respond)), **kwargs
        )

    monkeypatch.setattr("backend.app.agent.runtime.AsyncOpenAI", make_client)
    return stub


@pytest.fixture
def app_settings(tmp_path):
    return Settings(openai_api_key="test-key", sessions_dir=tmp_path / "sessions", _env_file=None)


@pytest.fixture
def backend_app(app_settings, model_stub):
    return create_app(app_settings)
