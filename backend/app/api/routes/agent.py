from fastapi import APIRouter, Request

from backend.app.schemas.agent import AgentRequest, AgentResponse

router = APIRouter(tags=["Agent"])


@router.post("/agent", response_model=AgentResponse)
async def agent(payload: AgentRequest, request: Request) -> AgentResponse:
    return await request.app.state.agent_service.create_reply(payload, request.state.request_id)
