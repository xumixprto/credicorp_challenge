"""Agent-facing assessment: fetch real inputs, then run deterministic calculations."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Annotated

from agent_framework import FunctionTool, MCPStdioTool, tool
from pydantic import Field

from backend.app.core.config import Settings
from backend.app.investment.config import load_investment_config
from backend.app.investment.models import (
    AssessmentInput,
    Horizon,
    MarketValue,
    RiskTolerance,
    SourceReference,
)
from backend.app.investment.providers import fetch_recorded
from backend.app.investment.providers.alpaca import fetch_market
from backend.app.investment.providers.yahoo import fetch_financials
from backend.app.investment.service import build_assessment

logger = logging.getLogger("credicorp.backend.tools.investment")


def create_investment_tool(settings: Settings, alpaca: MCPStdioTool | None = None) -> FunctionTool:
    config = load_investment_config(settings.docs_config_path)

    @tool
    async def build_investment_assessment(
        ticker: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9.-]{0,15}$")],
        risk_tolerance: RiskTolerance = 2,
        horizon_months: Horizon = 12,
    ) -> dict:
        """Fetch Yahoo financial statements and Alpaca market data and assess a USD-listed stock.

        Pass ONLY the ticker, risk level (1 Conservative, 2 Moderate, 3 Aggressive), and investment
        horizon (6-36 months). Defaults: Moderate, 12 months. Data is fetched internally on every
        call, including follow-ups; do not copy financial observations, prices or source IDs.
        Use retrieval and other relevant read-only tools to research filings, risks and context
        separately, then combine that evidence with this tool's calculated results in your answer.
        Returns sources, coverage and missing-data reasons. ETFs and other non-equities are
        unsupported by corporate scoring. Current assessments only, not historical backtests.
        A recommendation never authorizes a trade; this tool only reads external data.
        """
        ticker = ticker.upper()
        now = datetime.now(UTC)
        try:
            snapshot = await fetch_recorded(
                "yfinance_financials",
                {"ticker": ticker, "retrieved_at": now.isoformat()},
                asyncio.to_thread(
                    fetch_financials, ticker, now.date(), config.data_timeout_seconds
                ),
                config.data_timeout_seconds,
            )
        except Exception as exc:
            return {
                "status": "data_unavailable",
                "ticker": ticker,
                "message": "Yahoo financial data could not be fetched; no assessment calculated.",
                "error_type": type(exc).__name__,
            }
        if snapshot.asset_type != "EQUITY":
            return {
                "status": "unsupported_asset" if snapshot.asset_type else "data_unavailable",
                "ticker": ticker,
                "asset_type": snapshot.asset_type,
                "message": "Corporate scoring requires an equity. Funds need a separate model.",
            }
        if snapshot.currency != "USD":
            return {
                "status": "unsupported_currency",
                "ticker": ticker,
                "message": "A verified USD listing is required; no FX conversion is performed.",
            }
        market, warnings = await fetch_market(ticker, alpaca, config, now)
        if snapshot.shares_outstanding:
            market.shares_outstanding = MarketValue(
                value=snapshot.shares_outstanding,
                date=now.date(),
                source=SourceReference(
                    id=f"yfinance:{ticker}:shares:{now.date()}",
                    kind="financial_data",
                    url=f"https://finance.yahoo.com/quote/{ticker}/key-statistics/",
                    section="Shares outstanding observed at retrieval; publication date unknown",
                ),
            )
        request = AssessmentInput(
            ticker=ticker,
            financial_data=snapshot.financial_data,
            market_data=market,
            risk_tolerance=risk_tolerance,
            horizon_months=horizon_months,
            as_of_date=now.date(),
        )
        result = await asyncio.to_thread(build_assessment, request, config)
        result.data_quality.warnings.extend([*snapshot.warnings, *warnings])
        payload = result.model_dump(mode="json")
        payload["data_collection"] = {
            "retrieved_at": now.isoformat(),
            "financial_provider": "yfinance",
            "market_provider": "Alpaca",
            "feed": config.market_feed,
            "financial_observations": len(snapshot.financial_data.observations),
            "stock_prices": len(market.prices),
            "benchmark_prices": len(market.benchmark_prices),
            "quote_date": market.quote.date.isoformat() if market.quote else None,
        }
        logger.info(
            "investment_assessment_completed",
            extra={
                "status": result.recommendation,
                "ticker": ticker,
                "stock_prices": len(market.prices),
                "benchmark_prices": len(market.benchmark_prices),
                "reason": "; ".join(result.reasons),
            },
        )
        return payload

    return build_investment_assessment
