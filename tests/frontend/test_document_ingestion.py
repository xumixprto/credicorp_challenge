import json
from email.parser import BytesParser
from email.policy import default
from functools import partial
from io import BytesIO
from uuid import UUID

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from frontend.services.agent_client import AgentAPIError, AgentClient
from frontend.services.api_client import APIError
from frontend.services.document_client import DocumentClient, DocumentUpload
from shared.settings import PROJECT_ROOT


def response_payload(statuses=("ingested", "failed")):
    return {
        "collection": "documents_test",
        "results": [
            {
                "filename": f"document-{index}.md",
                "source_id": f"document-{index}.md",
                "status": status,
                "chunks": 0 if status == "failed" else 2,
                "error": "No text could be extracted." if status == "failed" else None,
            }
            for index, status in enumerate(statuses)
        ],
        "embedded_texts": 2,
        "cached_texts": 0,
    }


def test_document_client_sends_multipart_and_rewinds_uploads():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=response_payload())

    stream = BytesIO(b"# Policy\n\nAnnual leave.")
    stream.read()  # A previous attempt or preview may have consumed the uploaded stream.
    uploads = [
        DocumentUpload("policy.md", stream, "text/markdown"),
        DocumentUpload("handbook.pdf", BytesIO(b"pdf bytes"), "application/pdf"),
    ]
    client = DocumentClient("http://api.test/", timeout=600, transport=httpx.MockTransport(handle))
    result = client.ingest(uploads)
    assert result.results[1].status == "failed"
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "http://api.test/docs/ingest"
    assert UUID(requests[0].headers["X-Request-ID"])
    assert requests[0].extensions["timeout"]["read"] == 600
    message = BytesParser(policy=default).parsebytes(
        f"Content-Type: {requests[0].headers['Content-Type']}\r\n\r\n".encode()
        + requests[0].content
    )
    parts = list(message.iter_parts())
    assert [part.get_param("name", header="content-disposition") for part in parts] == [
        "files",
        "files",
    ]
    assert [part.get_filename() for part in parts] == ["policy.md", "handbook.pdf"]
    assert parts[0].get_payload(decode=True) == stream.getvalue()
    client.ingest(uploads)  # Explicit retries resend the full content.
    assert stream.getvalue() in requests[1].content


@pytest.mark.parametrize(
    "failure", ["timeout", "connection", "limit", "validation", "server", "schema"]
)
def test_document_client_reports_safe_errors(failure):
    def handle(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if failure == "connection":
            raise httpx.ConnectError("offline", request=request)
        if failure in {"limit", "validation"}:
            return httpx.Response(
                413 if failure == "limit" else 422,
                json={"detail": "Check the selected documents."},
            )
        if failure == "server":
            return httpx.Response(500, json={"detail": "private server detail"})
        return httpx.Response(200, json={"invalid": "response"})

    client = DocumentClient("http://api.test", transport=httpx.MockTransport(handle))
    with pytest.raises(APIError) as raised:
        client.ingest([DocumentUpload("policy.md", BytesIO(b"policy"))])
    assert UUID(raised.value.request_id)
    assert "private server detail" not in str(raised.value)
    if failure == "timeout":
        assert "may still finish" in str(raised.value)
    elif failure in {"limit", "validation"}:
        assert str(raised.value) == "Check the selected documents."


def document_app(monkeypatch, handle):
    def offline(*args):
        raise AgentAPIError("Chat history is unavailable.", "chat-reference")

    monkeypatch.setattr(AgentClient, "list_conversations", offline)
    monkeypatch.setattr(
        "frontend.services.document_client.DocumentClient",
        partial(DocumentClient, transport=httpx.MockTransport(handle)),
    )
    app = AppTest.from_file(str(PROJECT_ROOT / "frontend" / "app.py")).run()
    assert app.error  # Navigation must remain usable when chat history cannot load.
    return app.sidebar.radio[0].set_value("Document Ingestion").run()


def test_document_page_batch_results_survive_navigation_without_resubmitting(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=response_payload())

    app = document_app(monkeypatch, handle)
    assert not app.exception
    assert app.title[0].value == "Document Ingestion"
    assert app.button(key="ingest_documents").disabled
    app.file_uploader[0].set_value(
        [
            ("document-0.md", b"# Policy\n\nPaid leave.", "text/markdown"),
            ("document-1.md", b"", "text/markdown"),
        ]
    ).run()
    assert not requests  # Selecting files must not ingest until the user clicks.
    app.button(key="ingest_documents").click().run()
    assert not app.exception
    assert len(requests) == 1  # Use the backend's batch transaction.
    assert "1 could not be ingested" in app.warning[0].value
    rows = app.dataframe[0].value
    assert list(rows["Status"]) == ["Added", "Failed"]
    assert rows.iloc[1]["Details"] == "No text could be extracted."
    app.run()
    app.sidebar.radio[0].set_value("Agent Chat").run()
    app.sidebar.radio[0].set_value("Document Ingestion").run()
    assert not app.exception
    assert len(app.dataframe[0].value) == 2
    assert len(requests) == 1


def test_document_page_can_retry_after_connection_failure(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json=response_payload(("unchanged",)))

    app = document_app(monkeypatch, handle)
    app.file_uploader[0].set_value(("document-0.md", b"policy", "text/markdown")).run()
    app.button(key="ingest_documents").click().run()
    assert not app.exception
    assert "Unable to reach the document service" in app.error[0].value
    assert any("Reference:" in caption.value for caption in app.caption)
    app.button(key="ingest_documents").click().run()
    assert not app.exception
    assert not app.error
    assert app.success
    assert app.dataframe[0].value.iloc[0]["Status"] == "Already ingested"
    assert len(requests) == 2


def test_document_page_rejects_duplicate_filenames(monkeypatch):
    def handle(request):
        pytest.fail("Duplicate filenames should be rejected before sending a request")

    app = document_app(monkeypatch, handle)
    app.file_uploader[0].set_value(
        [
            ("policy.md", b"first", "text/markdown"),
            ("policy.md", b"second", "text/markdown"),
        ]
    ).run()
    assert app.button(key="ingest_documents").disabled
    assert "same name" in app.warning[0].value


@pytest.mark.parametrize(
    "reparse,reembed", [(False, False), (True, False), (False, True), (True, True)]
)
def test_reindex_page_sends_selected_flags_and_retains_results(monkeypatch, reparse, reembed):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "collection": "new",
                "previous_collection": "old",
                "documents": 2,
                "chunks": 10,
                "embedded_texts": 3,
                "cached_texts": 7,
            },
        )

    app = document_app(monkeypatch, handle).sidebar.radio[0].set_value("Reindex").run()
    assert not app.exception
    assert not app.checkbox(key="reindex_force_reparse").value
    assert not app.checkbox(key="reindex_force_reembed").value
    app.checkbox(key="reindex_force_reparse").set_value(reparse)
    app.checkbox(key="reindex_force_reembed").set_value(reembed).run()
    assert not requests
    app.button(key="reindex_documents").click().run()
    assert not app.exception
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/docs/reindex"
    assert json.loads(requests[0].content) == {
        "force_reparse": reparse,
        "force_reembed": reembed,
    }
    assert UUID(requests[0].headers["X-Request-ID"])
    assert requests[0].extensions["timeout"]["read"] == 600
    assert "2 document(s)" in app.success[0].value
    assert [metric.value for metric in app.metric] == ["10", "3", "7"]
    app.sidebar.radio[0].set_value("Document Ingestion").run()
    app.sidebar.radio[0].set_value("Reindex").run()
    assert app.success
    assert len(requests) == 1


def test_reindex_page_timeout_shows_reference_and_can_retry(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("private detail", request=request)
        return httpx.Response(
            200,
            json={
                "collection": "new",
                "previous_collection": "old",
                "documents": 0,
                "chunks": 0,
                "embedded_texts": 0,
                "cached_texts": 0,
            },
        )

    app = document_app(monkeypatch, handle).sidebar.radio[0].set_value("Reindex").run()
    app.button(key="reindex_documents").click().run()
    assert not app.exception
    assert "rebuild may still finish" in app.error[0].value
    assert "private detail" not in app.error[0].value
    assert any("Reference:" in caption.value for caption in app.caption)
    app.button(key="reindex_documents").click().run()
    assert not app.exception
    assert not app.error
    assert app.success
