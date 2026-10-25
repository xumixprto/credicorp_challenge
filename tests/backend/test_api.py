import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from shared.logging_config import JsonFormatter

PAYLOAD = {
    "userId": "d5bbf9d1-5945-44d8-bb10-0065164a8356",
    "query": "thanks!",
    "sessionId": "test-session-1001",
}


@pytest.fixture
def client(backend_app):
    with TestClient(backend_app) as test_client:
        yield test_client


def test_health_and_agent_contract(client):
    assert client.get("/health").json() == {"status": "ok"}
    response = client.post("/agent", json=PAYLOAD)
    assert response.status_code == 200
    assert response.json() == {"message": "Hello World"}
    assert response.headers["X-Request-ID"]


@pytest.mark.parametrize(
    "changes",
    [
        {"userId": "not-a-uuid"},
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 20001},
        {"sessionId": ""},
        {"sessionId": "x" * 129},
        {"unexpected": "value"},
    ],
)
def test_invalid_payload_returns_422(client, changes):
    response = client.post("/agent", json={**PAYLOAD, **changes})
    assert response.status_code == 422
    assert response.headers["X-Request-ID"]


def test_missing_field_and_wrong_method(client):
    assert client.post("/agent", json={"query": "Hello"}).status_code == 422
    assert client.get("/agent").status_code == 405


def test_logs_correlate_requests_without_recording_query(client):
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JsonFormatter("backend"))
    logger = logging.getLogger("credicorp.backend")
    logger.addHandler(handler)
    try:
        response = client.post(
            "/agent",
            json={**PAYLOAD, "query": "private-message-do-not-log"},
            headers={"X-Request-ID": "test-correlation-123"},
        )
        second = client.get("/health")
        invalid = client.get("/health", headers={"X-Request-ID": "x" * 129})
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert response.headers["X-Request-ID"] == "test-correlation-123"
    assert second.headers["X-Request-ID"] != "test-correlation-123"
    assert len(invalid.headers["X-Request-ID"]) <= 128
    assert "private-message-do-not-log" not in output.getvalue()
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    agent_records = [
        record
        for record in records
        if record.get("path") == "/agent" and record["message"] == "request_completed"
    ]
    assert agent_records[0]["request_id"] == "test-correlation-123"
    assert agent_records[0]["status_code"] == 200
    assert agent_records[0]["duration_ms"] >= 0


def test_unexpected_error_has_safe_response_and_request_id(app_settings):
    app = create_app(app_settings)

    @app.get("/test-error")
    async def fail():
        raise RuntimeError("internal implementation detail")

    with TestClient(app) as client:
        response = client.get("/test-error", headers={"X-Request-ID": "error-123"})
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error", "requestId": "error-123"}
    assert response.headers["X-Request-ID"] == "error-123"


def test_upload_validation_logs_missing_field_without_request_body(client):
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JsonFormatter("backend"))
    logger = logging.getLogger("credicorp.backend")
    logger.addHandler(handler)
    try:
        response = client.post(
            "/docs/ingest",
            json={"files": "private-document-content"},
            headers={"X-Request-ID": "missing-upload-123"},
        )
    finally:
        logger.removeHandler(handler)
        handler.close()
    assert response.status_code == 422
    assert "private-document-content" not in output.getvalue()
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [record["message"] for record in records] == [
        "request_started",
        "request_validation_failed",
        "request_completed",
    ]
    assert records[1]["validation_errors"] == [{"location": ["body", "files"], "type": "missing"}]
    assert all(record["request_id"] == "missing-upload-123" for record in records)
