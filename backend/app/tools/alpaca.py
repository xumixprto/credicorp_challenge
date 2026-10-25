"""One Alpaca MCP subprocess per FastAPI worker, shared across agent turns."""

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from agent_framework import MCPStdioTool
from mcp.client.stdio import StdioServerParameters, stdio_client

from backend.app.core.config import Settings

logger = logging.getLogger("credicorp.backend.alpaca")
ALPACA_SERVER_PACKAGE = "alpaca-mcp-server==2.4.0"


class AlpacaStdioTool(MCPStdioTool):
    """Keep subprocess stderr off Git Bash's terminal; the SDK owns the connection."""

    def __init__(self, *, stderr_path: Path, **kwargs) -> None:
        super().__init__(**kwargs)
        self.stderr_path = stderr_path

    @asynccontextmanager
    async def get_mcp_client(self) -> AsyncIterator[tuple]:
        self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        server = StdioServerParameters(
            command=self.command,
            args=self.args,
            env=self.env,
            encoding=self.encoding or "utf-8",
            **self._client_kwargs,
        )
        # Keep the file open until the SDK finishes stopping the subprocess.
        with self.stderr_path.open("a", encoding="utf-8") as stderr:
            async with stdio_client(server, errlog=stderr) as streams:
                yield streams


def create_alpaca_tool(settings: Settings) -> MCPStdioTool:
    return AlpacaStdioTool(
        stderr_path=settings.resolved_log_dir / "alpaca-mcp.log",
        name="alpaca",
        tool_name_prefix="alpaca",
        command=sys.executable,
        args=["-m", "uv", "tool", "run", ALPACA_SERVER_PACKAGE],
        env={
            "ALPACA_API_KEY": settings.alpaca_api_key.get_secret_value().strip(),
            "ALPACA_SECRET_KEY": settings.alpaca_secret_key.get_secret_value().strip(),
            "ALPACA_PAPER_TRADE": str(settings.alpaca_paper_trade).lower(),
            "ALPACA_TOOLSETS": settings.alpaca_toolsets,
        },
        request_timeout=settings.alpaca_mcp_request_timeout_seconds,
        load_prompts=False,
    )


@asynccontextmanager
async def alpaca_connection(settings: Settings) -> AsyncIterator[MCPStdioTool | None]:
    """Open during application startup; close after requests finish on shutdown."""
    if not settings.alpaca_mcp_enabled:
        logger.info("alpaca_mcp_disabled", extra={"reason": "disabled_by_configuration"})
        yield None
        return
    credentials = [
        settings.alpaca_api_key.get_secret_value().strip(),
        settings.alpaca_secret_key.get_secret_value().strip(),
    ]
    if not any(credentials):
        logger.info("alpaca_mcp_disabled", extra={"reason": "credentials_not_configured"})
        yield None
        return
    if not all(credentials):
        raise RuntimeError("Set both ALPACA_API_KEY and ALPACA_SECRET_KEY, or disable Alpaca MCP.")

    connected = False
    logger.info("alpaca_mcp_starting")
    try:
        async with create_alpaca_tool(settings) as tool:
            if not tool.functions:
                raise RuntimeError("Alpaca returned no tools for the configured toolsets.")
            connected = True
            logger.info("alpaca_mcp_connected", extra={"tool_count": len(tool.functions)})
            yield tool
    except Exception as exc:
        if connected:
            raise
        reason = str(exc)
        for secret in (*credentials, settings.openai_api_key.get_secret_value()):
            if secret:
                reason = reason.replace(secret, "[redacted]")
        logger.error(
            "alpaca_mcp_start_failed",
            extra={"error_type": type(exc).__name__, "reason": reason[:2000]},
        )
        raise RuntimeError(
            f"Alpaca MCP startup failed. See backend.log and alpaca-mcp.log in "
            f"{settings.resolved_log_dir}. Set ALPACA_MCP_ENABLED=false to run without it."
        ) from None
    finally:
        logger.info("alpaca_mcp_stopped")
