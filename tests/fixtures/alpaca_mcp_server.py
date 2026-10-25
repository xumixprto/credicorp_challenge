"""Local MCP protocol fixture. No Alpaca credentials, network, or account actions."""

import os
import sys

from mcp.server import MCPServer

server = MCPServer("Test Alpaca")


@server.tool()
def get_stock_latest_quote(symbol: str, feed: str = "iex") -> dict:
    """Return a deterministic test quote for a stock symbol."""
    if symbol == "FAIL":
        raise ValueError("Simulated market data outage")
    return {
        "symbol": symbol,
        "ask_price": 100.25,
        "timestamp": "2026-10-24T15:00:00Z",
        "feed": feed,
        "process_id": os.getpid(),
    }


if __name__ == "__main__":
    print("Alpaca fixture started", file=sys.stderr, flush=True)
    server.run(transport="stdio")
