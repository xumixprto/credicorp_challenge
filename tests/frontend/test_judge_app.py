import json
from functools import partial
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from backend.app.evaluation.models import BatchJudgment, RunJudgment
from frontend.services.agent_client import AgentClient
from frontend.services.judge_client import JudgeClient
from shared.evaluations import RunRecord
from shared.settings import PROJECT_ROOT


def test_judge_tab_selection_missing_and_no_resubmit_on_navigation(backend_app, monkeypatch):
    service = backend_app.state.judge_service
    for index in range(2):
        service.store.save_run(
            RunRecord(
                request_id=str(uuid4()),
                user_id="test",
                session_id="test",
                message_type="TEXT",
                model="test",
                query=f"Hello {index}",
                answer="Hello",
                status="completed",
            )
        )

    async def assess(payloads):
        return BatchJudgment(
            runs=[
                RunJudgment(
                    run_id=item["run_id"],
                    retrievals=[],
                    claims=[],
                    summary="No factual claims.",
                    tool_usage={
                        "grade": 2,
                        "reason": "No tool needed for a greeting.",
                        "evidence_ids": ["input"],
                    },
                    intention_success={
                        "grade": 2,
                        "reason": "Returned the greeting.",
                        "evidence_ids": ["final_answer"],
                    },
                )
                for item in payloads
            ]
        )

    service.judge.assess = AsyncMock(side_effect=assess)
    requests = []
    with TestClient(backend_app) as backend:

        def handle(request):
            if request.url.path == "/judge" and request.method == "POST":
                requests.append(json.loads(request.content))
            response = backend.request(
                request.method,
                str(request.url),
                content=request.content,
                headers=dict(request.headers),
            )
            return httpx.Response(
                response.status_code, content=response.content, headers=dict(response.headers)
            )

        transport = httpx.MockTransport(handle)
        monkeypatch.setattr(
            "frontend.services.agent_client.AgentClient", partial(AgentClient, transport=transport)
        )
        monkeypatch.setattr(
            "frontend.services.judge_client.JudgeClient", partial(JudgeClient, transport=transport)
        )
        app = AppTest.from_file(str(PROJECT_ROOT / "frontend/app.py")).run()
        app.sidebar.radio[0].set_value("Judge").run()
        assert not app.exception
        assert len(app.dataframe) == 1
        assert {button.proto.label for button in app.get("download_button")} == {
            "Download evaluations CSV",
            "Download runs CSV",
        }
        assert app.button(key="judge_evaluate_selected").disabled
        assert not app.button(key="judge_evaluate_missing").disabled
        assert app.metric[1].value == "2"
        displayed_id = app.dataframe[0].value.iloc[0]["run_id"]
        app.session_state["judge_selection_0"] = {
            "edited_rows": {0: {"Select": True}},
            "added_rows": [],
            "deleted_rows": [],
        }
        app.run()
        assert not app.exception
        assert not app.button(key="judge_evaluate_selected").disabled
        app.button(key="judge_evaluate_selected").click().run()
        assert not app.exception
        assert requests[0] == {"run_ids": [displayed_id], "missing_only": False}
        assert app.metric[1].value == "1"
        evaluated = app.dataframe[0].value.query("run_id == @displayed_id").iloc[0]
        assert evaluated["tool_usage"] == evaluated["intention_success"] == 1
        app.button(key="judge_evaluate_missing").click().run()
        assert not app.exception
        assert requests[1] == {"run_ids": None, "missing_only": True}
        assert app.metric[1].value == "0"
        assert app.button(key="judge_evaluate_missing").disabled
        assert b"tool_usage,intention_success" in service.store.export_csv("evaluations")
        app.sidebar.radio[0].set_value("Agent Chat").run()
        app.sidebar.radio[0].set_value("Judge").run()
        assert len(requests) == 2
        assert not app.exception
        client = JudgeClient("http://testserver", transport=transport)
        assert displayed_id.encode() in client.download_csv("evaluations")
        assert displayed_id.encode() in client.download_csv("runs")
        assert len(requests) == 2  # Downloads never invoke the judge.
