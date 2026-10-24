from fastapi import APIRouter

from backend.app.api.routes import agent, conversations, health

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(agent.router)
api_router.include_router(conversations.router)
