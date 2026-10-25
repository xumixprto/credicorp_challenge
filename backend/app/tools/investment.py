"""The only new agent-facing investment tool; no external clients or trade execution."""

import asyncio
import logging
from datetime import date

from agent_framework import FunctionTool, tool

from backend.app.core.config import Settings
from backend.app.investment.config import load_investment_config
from backend.app.investment.models import (
    AssessmentInput,
    FinancialData,
    Horizon,
    MarketData,
    RiskTolerance,
)
from backend.app.investment.service import build_assessment

logger = logging.getLogger("credicorp.backend.tools.investment")


def create_investment_tool(settings: Settings) -> FunctionTool:
    config = load_investment_config(settings.docs_config_path)

    @tool
    async def build_investment_assessment(
        ticker: str,
        financial_data: FinancialData,
        market_data: MarketData,
        risk_tolerance: RiskTolerance = 2,
        horizon_months: Horizon = 12,
        as_of_date: date | None = None,
    ) -> dict:
        """Assess a stock using supplied structured filing observations and market data.

        Retrieve evidence with the existing document and Alpaca tools FIRST. Pass only extracted
        observations and bounded daily prices, not documents. This tool makes no external calls.
        Risk: 1 Conservative, 2 Moderate (default), 3 Aggressive. Horizon: 6-36 months, default 12.
        Empty observations/prices are accepted and yield missing-data reasons, not invented values.
        Uses positive capex/expense amounts, explicit units/scales, dates and source references.
        Supply adjusted daily stock and SPY prices when available; never mix tickers/share classes.
        Produces heuristic analysis, not a trade. A recommendation never authorizes an order.
        """
        request = AssessmentInput(
            ticker=ticker,
            financial_data=financial_data,
            market_data=market_data,
            risk_tolerance=risk_tolerance,
            horizon_months=horizon_months,
            as_of_date=as_of_date,
        )
        result = await asyncio.to_thread(build_assessment, request, config)
        logger.info("investment_assessment_completed", extra={"status": result.recommendation})
        return result.model_dump(mode="json")

    return build_investment_assessment
