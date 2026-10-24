import asyncio
import json
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient

from backend.app.core.errors import ApplicationError
from backend.app.main import create_app
from backend.app.repositories.conversations import ConversationRepository

USER_ID = "d5bbf9d1-5945-44d8-bb10-0065164a8356"
SESSION_ID = "test-session-1001"


def payload(query="Hello", user_id=USER_ID, session_id=SESSION_ID):
    return {"userId": user_id, "sessionId": session_id, "query": query}


def test_framework_context_survives_restart(backend_app, app_settings, model_stub):
    with TestClient(backend_app) as client:
        response = client.post("/agent", json=payload("My name is Alice."))
        assert response.status_code == 200

    files = list(app_settings.resolved_sessions_dir.rglob("*.json"))
    assert len(files) == 1
    stored = json.loads(files[0].read_text(encoding="utf-8"))
    assert stored["userId"] == USER_ID
    assert stored["sessionId"] == SESSION_ID
    assert stored["agentSession"]["state"]
    assert len(stored["messages"]) == 2
    assert "test-key" not in files[0].read_text(encoding="utf-8")

    with TestClient(create_app(app_settings)) as restarted:
        reply = restarted.post("/agent", json=payload("What is my name?"))
        assert reply.json() == {"message": "Alice"}
        detail = restarted.get(
            "/conversations/detail", params={"userId": USER_ID, "sessionId": SESSION_ID}
        ).json()
        assert len(detail["messages"]) == 4
        assert "agentSession" not in detail

    sent = model_stub.requests[1]
    assert "My name is Alice." in json.dumps(sent["input"])
    assert "What is my name?" in json.dumps(sent["input"])
    assert sent["store"] is False
    assert not sent.get("previous_response_id")


def test_conversations_are_isolated_by_user_and_session(backend_app, model_stub):
    other_user = str(uuid4())
    with TestClient(backend_app) as client:
        client.post("/agent", json=payload("My name is Alice."))
        client.post("/agent", json=payload("Who am I?", session_id="different-session"))
        assert "My name is Alice." not in json.dumps(model_stub.requests[-1]["input"])
        client.post("/agent", json=payload("Who am I?", user_id=other_user))
        assert "My name is Alice." not in json.dumps(model_stub.requests[-1]["input"])
        mine = client.get("/conversations", params={"userId": USER_ID}).json()
        theirs = client.get("/conversations", params={"userId": other_user}).json()
        assert len(mine) == 2
        assert len(theirs) == 1
        assert all(item["userId"] == USER_ID for item in mine)
        assert theirs[0]["userId"] == other_user
        assert (
            client.get(
                "/conversations/detail",
                params={"userId": other_user, "sessionId": "different-session"},
            ).status_code
            == 404
        )


def test_create_empty_conversation_is_persisted_and_idempotent(backend_app):
    with TestClient(backend_app) as client:
        assert client.get("/conversations", params={"userId": USER_ID}).json() == []
        body = {"userId": USER_ID, "sessionId": SESSION_ID}
        created = client.post("/conversations", json=body)
        assert created.status_code == 201
        assert created.json()["messages"] == []
        assert created.json()["title"] == "New chat"
        assert client.post("/conversations", json=body).json() == created.json()
        assert len(client.get("/conversations", params={"userId": USER_ID}).json()) == 1


def test_retry_with_same_request_id_does_not_duplicate_turn(backend_app, model_stub):
    with TestClient(backend_app) as client:
        headers = {"X-Request-ID": "same-request"}
        first = client.post("/agent", json=payload(), headers=headers)
        second = client.post("/agent", json=payload(), headers=headers)
        assert first.json() == second.json()
        assert len(model_stub.requests) == 1
        detail = client.get(
            "/conversations/detail", params={"userId": USER_ID, "sessionId": SESSION_ID}
        ).json()
        assert len(detail["messages"]) == 2
        assert client.post("/agent", json=payload("Different"), headers=headers).status_code == 409


@pytest.mark.parametrize("provider_status,expected", [(401, 502), (429, 429), (500, 502)])
def test_failed_model_call_preserves_saved_context(
    backend_app, app_settings, model_stub, provider_status, expected
):
    with TestClient(backend_app) as client:
        client.post("/agent", json=payload("My name is Alice."))
        path = next(app_settings.resolved_sessions_dir.rglob("*.json"))
        original = path.read_bytes()
        model_stub.status_code = provider_status
        response = client.post("/agent", json=payload("What is my name?"))
        assert response.status_code == expected
        assert "private provider error" not in response.text
        assert path.read_bytes() == original


def test_missing_key_does_not_create_conversation(app_settings):
    settings = app_settings.model_copy(
        update={"openai_api_key": app_settings.openai_api_key.__class__("")}
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200
        response = client.post("/agent", json=payload())
        assert response.status_code == 503
        assert "OPENAI_API_KEY" in response.json()["detail"]
    assert list(app_settings.resolved_sessions_dir.rglob("*.json")) == []


def test_timeout_does_not_save_partial_turn(app_settings, model_stub):
    settings = app_settings.model_copy(update={"agent_timeout_seconds": 0.05})
    model_stub.delay = 0.3
    with TestClient(create_app(settings)) as client:
        response = client.post("/agent", json=payload())
        assert response.status_code == 504
    assert list(app_settings.resolved_sessions_dir.rglob("*.json")) == []


def test_corrupt_json_is_not_silently_overwritten(backend_app, app_settings):
    repository = ConversationRepository(app_settings.resolved_sessions_dir)
    path = repository.path_for(UUID(USER_ID), SESSION_ID)
    path.parent.mkdir(parents=True)
    path.write_text("broken-json", encoding="utf-8")
    with TestClient(backend_app) as client:
        assert client.post("/agent", json=payload()).status_code == 500
    assert path.read_text(encoding="utf-8") == "broken-json"


def test_failed_atomic_write_preserves_original(app_settings, monkeypatch):
    repository = ConversationRepository(app_settings.resolved_sessions_dir)
    record = repository.new(UUID(USER_ID), SESSION_ID)
    repository.save(record)
    path = repository.path_for(UUID(USER_ID), SESSION_ID)
    original = path.read_bytes()

    def fail_replace(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr("backend.app.repositories.conversations.os.replace", fail_replace)
    record.title = "Updated title"
    with pytest.raises(ApplicationError):
        repository.save(record)
    assert path.read_bytes() == original
    assert list(path.parent.glob("*.tmp")) == []


def test_session_id_cannot_escape_storage_directory(backend_app, app_settings):
    with TestClient(backend_app) as client:
        response = client.post("/agent", json=payload(session_id="../../CON:outside"))
        assert response.status_code == 200
    files = list(app_settings.resolved_sessions_dir.rglob("*.json"))
    assert len(files) == 1
    assert files[0].parent == app_settings.resolved_sessions_dir / "conversations" / USER_ID


def test_concurrent_messages_keep_both_turns(backend_app, model_stub):
    model_stub.delay = 0.05

    async def send():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=backend_app), base_url="http://testserver"
        ) as client:
            replies = await asyncio.gather(
                client.post("/agent", json=payload("First")),
                client.post("/agent", json=payload("Second")),
            )
            assert all(response.status_code == 200 for response in replies)
            response = await client.get(
                "/conversations/detail", params={"userId": USER_ID, "sessionId": SESSION_ID}
            )
            assert len(response.json()["messages"]) == 4

    asyncio.run(send())
    assert len(model_stub.requests) == 2
    assert "First" in json.dumps(model_stub.requests[-1]["input"])
    assert "Second" in json.dumps(model_stub.requests[-1]["input"])
