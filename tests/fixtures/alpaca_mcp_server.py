"""Local MCP protocol fixture. No Alpaca credentials, network, or account actions."""

import json
import os
import sys
from pathlib import Path

from mcp.server import MCPServer

server = MCPServer("Test Alpaca")


@server.tool()
def place_stock_order(symbol: str, qty: int, side: str) -> dict:
    """Record a fake order locally; never contact a broker."""
    order = {"symbol": symbol, "qty": qty, "side": side, "status": "accepted"}
    with Path(os.environ["TEST_ALPACA_ORDER_LOG"]).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(order) + "\n")
    return order


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
