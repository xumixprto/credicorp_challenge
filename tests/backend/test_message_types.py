import pytest
from fastapi.testclient import TestClient

PAYLOAD = {
    "userId": "d5bbf9d1-5945-44d8-bb10-0065164a8356",
    "sessionId": "message-types",
    "query": "Hello",
}


def test_human_flow_requires_a_decision_without_mutating_history(
    backend_app, app_settings, model_stub
):
    with TestClient(backend_app) as client:
        response = client.post(
            "/agent", json={**PAYLOAD, "messageType": "HUMAN_FLOW_REPLY", "query": ""}
        )
        assert response.status_code == 422
        assert not model_stub.requests
        assert not list(app_settings.sessions_dir.rglob("*.json"))
        assert client.post("/agent", json={**PAYLOAD, "messageType": "TEXT"}).status_code == 200
        saved = {path: path.read_bytes() for path in app_settings.sessions_dir.rglob("*.json")}
        assert saved
        response = client.post("/agent", json={**PAYLOAD, "messageType": "HUMAN_FLOW_REPLY"})
        assert response.status_code == 422
        assert len(model_stub.requests) == 1
        assert saved == {path: path.read_bytes() for path in saved}


@pytest.mark.parametrize(
    "updates",
    [
        {"messageType": "other"},
        {"messageType": "text"},
        {"messageType": None},
        {"messageType": "TEXT", "query": "  "},
        {"query": ""},
    ],
)
def test_invalid_message_types_and_empty_text_are_rejected(backend_app, model_stub, updates):
    with TestClient(backend_app) as client:
        assert client.post("/agent", json={**PAYLOAD, **updates}).status_code == 422
    assert not model_stub.requests
