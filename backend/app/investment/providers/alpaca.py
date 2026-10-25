"""Fetch daily history and an independent unadjusted price using the existing MCP connection."""

import asyncio
import json
from datetime import UTC, datetime, time, timedelta

from agent_framework import Content, MCPStdioTool

from backend.app.investment.config import InvestmentConfig
from backend.app.investment.models import MarketData, PriceObservation, SourceReference
from backend.app.investment.providers import fetch_recorded


def decode_result(result) -> dict:
    if isinstance(result, list):
        blocks = [b.to_dict() if isinstance(b, Content) else b for b in result]
        result = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    if isinstance(result, str):
        result = json.loads(result)
    if not isinstance(result, dict) or result.get("error") or result.get("isError"):
        raise ValueError("Invalid MCP response")
    data = result.get("data", result)
    if not isinstance(data, dict) or data.get("error"):
        raise ValueError("Invalid Alpaca data")
    return data


async def fetch_market(
    ticker: str, alpaca: MCPStdioTool | None, config: InvestmentConfig, now: datetime
) -> tuple[MarketData, list[str]]:
    market = MarketData(ticker=ticker)
    warnings = []
    if alpaca is None:
        return market, ["Alpaca is unavailable; no market data was fetched."]

    async def call(name, **args):
        result = await fetch_recorded(
            f"alpaca_{name}",
            args,
            alpaca.call_tool(name, **args),
            config.data_timeout_seconds,
        )
        return decode_result(result)

    # Exclude today's incomplete daily bar. The latest trade is acquired separately.
    end = datetime.combine(now.date(), time.min, tzinfo=UTC) - timedelta(microseconds=1)
    history, trade = await asyncio.gather(
        call(
            "get_stock_bars",
            symbols=",".join(dict.fromkeys([ticker, "SPY"])),
            timeframe="1Day",
            start=(end - timedelta(days=config.history_days)).isoformat(),
            end=end.isoformat(),
            limit=2000,
            adjustment="all",
            feed=config.market_feed,
            currency="USD",
            sort="asc",
        ),
        call("get_stock_latest_trade", symbols=ticker, feed=config.market_feed, currency="USD"),
        return_exceptions=True,
    )
    source = SourceReference(
        id=f"alpaca:{ticker}:history:{now.isoformat()}",
        kind="market",
        section=f"1Day; adjustment=all; feed={config.market_feed}",
    )
    try:
        if isinstance(history, BaseException):
            raise ValueError("History request failed") from history
        if history.get("next_page_token"):
            raise ValueError("Incomplete history page")
        series = {}
        for symbol in (ticker, "SPY"):
            series[symbol] = [
                PriceObservation(
                    date=datetime.fromisoformat(bar["t"].replace("Z", "+00:00")).date(),
                    price=bar["c"],
                    adjusted_price=bar["c"],
                )
                for bar in history["bars"][symbol]
            ]
            if not series[symbol] or any(p.date >= now.date() for p in series[symbol]):
                raise ValueError("Empty or incomplete daily history")
        market = MarketData(
            ticker=ticker,
            source=source,
            history_source=source,
            prices=series[ticker],
            benchmark_prices=series["SPY"],
            benchmark_source=source,
            adjustment_as_of_date=now.date(),
        )
    except (ValueError, TypeError, KeyError) as exc:
        warnings.append(f"Alpaca daily history unavailable or invalid ({type(exc).__name__}).")
    try:
        if isinstance(trade, BaseException):
            raise ValueError("Latest trade request failed") from trade
        value = trade["trades"][ticker]
        stamp = datetime.fromisoformat(value["t"].replace("Z", "+00:00"))
        if stamp > datetime.now(UTC):
            raise ValueError("Future trade timestamp")
        market.quote = PriceObservation(date=stamp.date(), price=value["p"])
        market.source = SourceReference(
            id=f"alpaca:{ticker}:trade:{stamp.isoformat()}",
            kind="market",
            section=f"Latest trade at {stamp.isoformat()}; feed={config.market_feed}",
        )
    except (ValueError, TypeError, KeyError) as exc:
        warnings.append(f"Alpaca latest trade unavailable or invalid ({type(exc).__name__}).")
    return market, warnings
