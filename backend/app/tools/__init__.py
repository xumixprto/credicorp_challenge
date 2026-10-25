"""Agent Framework tool registry."""

from agent_framework import FunctionTool

from backend.app.core.config import Settings
from backend.app.tools.retrieval import create_retrieval_tool


def get_tools(settings: Settings) -> list[FunctionTool]:
    return [create_retrieval_tool(settings)]
