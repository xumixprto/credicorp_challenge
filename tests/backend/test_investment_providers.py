import asyncio
import json
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from agent_framework import Content

from backend.app.investment.config import InvestmentConfig
from backend.app.investment.data import FinancialContext
from backend.app.investment.models import FinancialData
from backend.app.investment.providers.alpaca import fetch_market
from backend.app.investment.providers.yahoo import STATEMENTS, fetch_financials, normalize_statement
from tests.backend.test_investment import AS_OF, request_data


class FakeAlpaca:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    async def call_tool(self, name, **kwargs):
        self.calls.append((name, kwargs))
        ticker = kwargs["symbols"].split(",")[0]
        if name == "get_stock_bars":
            today = datetime.now(UTC).date()
            supplied = request_data(ticker)["market_data"]
            result = {
                "bars": {
                    symbol: [
                        {
                            "t": (
                                datetime.fromisoformat(p["date"]).date()
                                + (today - AS_OF)
                                - timedelta(days=1)
                            ).isoformat()
                            + "T04:00:00Z",
                            "c": p["adjusted_price"],
                        }
                        for p in supplied[key]
                    ]
                    for symbol, key in ((ticker, "prices"), ("SPY", "benchmark_prices"))
                },
                "next_page_token": None,
            }
            if self.failure == "pagination":
                result["next_page_token"] = "more"
            if self.failure == "symbol":
                del result["bars"][ticker]
            if self.failure == "duplicate":
                result["bars"][ticker].append(result["bars"][ticker][0])
        elif name == "get_stock_latest_trade":
            result = {"trades": {ticker: {"t": datetime.now(UTC).isoformat(), "p": 150}}}
            if self.failure == "trade":
                raise RuntimeError("private provider detail")
        else:
            raise AssertionError(f"Unexpected tool {name}")
        return [Content.from_text(json.dumps({"data": result}))]


class FakeYahoo:
    def __init__(self, ticker, session=None):
        self.ticker = ticker

    def get_info(self):
        return {
            "symbol": self.ticker,
            "quoteType": "ETF" if self.ticker == "AGG" else "EQUITY",
            "financialCurrency": "USD",
            "currency": "USD",
            "sharesOutstanding": 10,
        }

    def frame(self, statement, freq):
        if freq == "quarterly":
            return pd.DataFrame()
        observations = request_data(self.ticker)["financial_data"]["observations"]
        data = {}
        for o in observations:
            if o["metric"] not in STATEMENTS[statement]:
                continue
            row = STATEMENTS[statement][o["metric"]]
            end = pd.Timestamp(o["period"]["end"])
            value = o["value"]
            if o["metric"] == "capital_expenditure":
                value = -value
            data.setdefault(end, {})[row] = value
        return pd.DataFrame(data)

    def get_income_stmt(self, freq):
        return self.frame("income_stmt", freq)

    def get_balance_sheet(self, freq):
        return self.frame("balance_sheet", freq)

    def get_cashflow(self, freq):
        return self.frame("cashflow", freq)


def test_yahoo_normalization_real_adapter_preserves_periods_units_and_missing_values(monkeypatch):
    monkeypatch.setattr("backend.app.investment.providers.yahoo.yf.Ticker", FakeYahoo)
    result = fetch_financials("MSFT", AS_OF, 5)
    ctx = FinancialContext(result.financial_data, AS_OF, "USD", InvestmentConfig())
    revenue = ctx.latest("revenue")
    assert revenue.value == pytest.approx(1000 * 1.15**3)
    assert ctx.latest("capital_expenditure").value > 0
    assert ctx.latest("free_cash_flow").value == pytest.approx(revenue.value * 0.3)
    assert not ctx.errors
    assert result.shares_outstanding == 10
    assert all(
        o.filing_date is None and o.retrieved_date == AS_OF
        for o in result.financial_data.observations
    )
    assert all(o.source.kind == "financial_data" for o in result.financial_data.observations)
    assert not ctx.series("revenue", "quarterly")
    assert (
        FinancialContext(
            result.financial_data, AS_OF - timedelta(days=1), "USD", InvestmentConfig()
        ).facts
        == {}
    )
    assert fetch_financials("AGG", AS_OF, 5).asset_type == "ETF"


def test_quarterly_values_are_not_annualized_and_nonfinite_values_stay_missing():
    frame = pd.DataFrame(
        {
            pd.Timestamp("2025-09-30"): {"TotalRevenue": 50, "NetIncome": float("nan")},
            pd.Timestamp("2025-12-31"): {"TotalRevenue": 60, "NetIncome": float("inf")},
        }
    )
    observations = normalize_statement("MSFT", frame, "income_stmt", "quarterly", "USD", AS_OF)
    assert [o.value for o in observations] == [50, 60]
    assert str(observations[-1].period.start) == "2025-10-01"
    ctx = FinancialContext(
        FinancialData(ticker="MSFT", observations=observations), AS_OF, "USD", InvestmentConfig()
    )
    assert ctx.annual("revenue") is None


@pytest.mark.parametrize("failure", [None, "pagination", "symbol", "duplicate", "trade"])
def test_alpaca_fetches_read_only_data_without_any_run_history(failure):
    alpaca = FakeAlpaca(failure)
    result, warnings = asyncio.run(
        fetch_market("MSFT", alpaca, InvestmentConfig(), datetime.now(UTC))
    )
    assert {call[0] for call in alpaca.calls} == {"get_stock_bars", "get_stock_latest_trade"}
    args = alpaca.calls[0][1]
    assert args["symbols"] == "MSFT,SPY"
    assert args["adjustment"] == "all" and args["timeframe"] == "1Day"
    if failure in ("pagination", "symbol", "duplicate"):
        assert result.prices == result.benchmark_prices == []
        assert result.quote.price == 150
    else:
        assert len(result.prices) == len(result.benchmark_prices) == 101
        assert result.prices[0].adjusted_price == result.prices[0].price
        assert result.quote is None if failure == "trade" else result.quote.price == 150
    assert bool(warnings) == bool(failure)
    assert "private provider detail" not in str(warnings)
