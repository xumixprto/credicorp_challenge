import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Query, Request, Response
from filelock import Timeout

from backend.app.core.errors import ApplicationError
from shared.evaluations import EvaluationTable, JudgeRequest, JudgeResponse, RunRecord

router = APIRouter(tags=["Evaluation"])
logger = logging.getLogger("credicorp.backend.judge")


@router.get("/judge/export/{table_name}", response_class=Response)
async def export_table(table_name: Literal["runs", "evaluations"], request: Request) -> Response:
    try:
        content = await asyncio.to_thread(
            request.app.state.judge_service.store.export_csv, table_name
        )
    except (OSError, ValueError, Timeout):
        raise ApplicationError(503, "The CSV table could not be downloaded.") from None
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{table_name}.csv"'},
    )


@router.get("/judge", response_model=EvaluationTable)
async def evaluation_table(
    request: Request, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)
) -> EvaluationTable:
    try:
        return await asyncio.to_thread(request.app.state.judge_service.table, limit, offset)
    except (OSError, ValueError, Timeout):
        raise ApplicationError(503, "Evaluation tables could not be read.") from None


@router.get("/judge/runs/{run_id}", response_model=RunRecord)
async def run_detail(run_id: str, request: Request) -> RunRecord:
    try:
        runs, _ = await asyncio.to_thread(request.app.state.judge_service.store.snapshot)
    except (OSError, ValueError, Timeout):
        raise ApplicationError(503, "Run traces could not be read.") from None
    run = next((item for item in runs if item.run_id == run_id), None)
    if run is None:
        raise ApplicationError(404, "Run not found.")
    return run


@router.post("/judge", response_model=JudgeResponse)
async def judge(payload: JudgeRequest, request: Request) -> JudgeResponse:
    try:
        return await request.app.state.judge_service.evaluate(payload)
    except (OSError, ValueError):
        logger.error("evaluation_storage_failed")
        raise ApplicationError(
            503, "Evaluation storage is unavailable. Completed batches remain saved."
        ) from None
