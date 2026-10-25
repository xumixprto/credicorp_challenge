"""Illustrative synthetic AAPL/GOOGL/NVDA data; these are not company financial figures."""

from datetime import date, timedelta
from math import sin, sqrt
from statistics import stdev

import pytest
from pydantic import ValidationError

from backend.app.investment.config import InvestmentConfig
from backend.app.investment.data import FinancialContext
from backend.app.investment.fundamentals import analyze_fundamentals
from backend.app.investment.models import (
    AssessmentInput,
    FinancialData,
    FinancialObservation,
    MarketData,
)
from backend.app.investment.service import build_assessment
from backend.app.investment.valuation import analyze_valuation

AS_OF = date(2026, 3, 31)


def observation(name, value, year=2025, kind="annual", scale="ones", currency="USD"):
    return {
        "metric": name,
        "value": value,
        "units": "currency_per_share" if name == "diluted_eps" else "currency",
        "scale": scale,
        "currency": currency,
        "period": {
            "kind": kind,
            "start": None if kind == "instant" else f"{year}-01-01",
            "end": f"{year}-12-31",
        },
        "filing_date": f"{year + 1}-02-15",
        "source": {"id": f"filing-{year}", "kind": "10-K", "filename": "synthetic.pdf"},
    }


def request_data(ticker="AAPL", amplitude=0.01):
    observations = []
    for year in range(2021, 2026):
        equity = 500 * 1.1 ** (year - 2021)
        for name, value in {
            "equity": equity,
            "total_debt": 100,
            "cash": 150,
            "current_assets": 400,
            "current_liabilities": 100,
            "total_assets": equity + 200,
            "total_liabilities": 200,
        }.items():
            observations.append(observation(name, value, year, "instant"))
        if year >= 2022:
            revenue = 1000 * 1.15 ** (year - 2022)
            for name, value in {
                "revenue": revenue,
                "gross_profit": 0.6 * revenue,
                "operating_income": 0.35 * revenue,
                "net_income": 0.25 * revenue,
                "diluted_eps": 0.25 * revenue / 10,
                "operating_cash_flow": 0.35 * revenue,
                "capital_expenditure": 0.05 * revenue,
                "depreciation_amortization": 0.05 * revenue,
                "interest_expense": 0.01 * revenue,
                "pretax_income": 0.34 * revenue,
                "income_tax": 0.068 * revenue,
                "change_noncash_working_capital": 0.01 * revenue,
            }.items():
                observations.append(observation(name, value, year))
    days = [
        AS_OF - timedelta(days=i)
        for i in range(140, -1, -1)
        if (AS_OF - timedelta(days=i)).weekday() < 5
    ]
    stock, benchmark = [], []
    a = b = 100
    for i, day in enumerate(days):
        a *= 1 + 0.001 + amplitude * sin(i)
        b *= 1 + 0.0005 + 0.005 * sin(i)
        stock.append({"date": day.isoformat(), "price": a, "adjusted_price": a})
        benchmark.append({"date": day.isoformat(), "price": b, "adjusted_price": b})
    return {
        "ticker": ticker,
        "as_of_date": AS_OF.isoformat(),
        "financial_data": {"ticker": ticker, "observations": observations},
        "market_data": {
            "ticker": ticker,
            "source": {"id": "alpaca-stock", "kind": "market"},
            "quote": {"date": AS_OF.isoformat(), "price": 150},
            "prices": stock,
            "benchmark_prices": benchmark,
            "benchmark_source": {"id": "alpaca-SPY", "kind": "market"},
            "adjustment_as_of_date": AS_OF.isoformat(),
            "shares_outstanding": {
                "value": 10,
                "date": AS_OF.isoformat(),
                "source": {"id": "shares", "kind": "market"},
            },
        },
    }


def context(data):
    request = AssessmentInput.model_validate(data)
    return FinancialContext(
        request.financial_data, request.as_of_date, request.market_data.currency, InvestmentConfig()
    )


@pytest.mark.parametrize("ticker", ["AAPL", "GOOG", "GOOGL", "NVDA"])
def test_fundamental_formulas_and_provenance(ticker):
    ctx = context(request_data(ticker))
    result = analyze_fundamentals(ctx)
    m = result.metrics
    assert m["revenue_yoy_growth"].value == pytest.approx(0.15)
    assert m["gross_margin"].value == pytest.approx(0.6)
    assert m["operating_margin"].value == pytest.approx(0.35)
    assert m["net_margin"].value == pytest.approx(0.25)
    assert m["fcf_margin"].value == pytest.approx(0.30)
    assert m["cash_conversion"].value == pytest.approx(1.4)
    assert m["interest_coverage"].value == pytest.approx(35)
    assert m["current_ratio"].value == 4
    assert m["roe"].value == pytest.approx(
        (1000 * 1.15**3 * 0.25) / ((500 * 1.1**3 + 500 * 1.1**4) / 2)
    )
    assert m["roic"].value == pytest.approx(
        (1000 * 1.15**3 * 0.35 * 0.8) / ((500 * 1.1**3 - 50 + 500 * 1.1**4 - 50) / 2)
    )
    assert "filing-2025" in m["fcf_margin"].evidence


def test_units_normalize_and_currency_conflict_blocks_buy():
    data = request_data()
    for obs in data["financial_data"]["observations"]:
        if obs["metric"] != "diluted_eps":
            obs["value"] /= 1000
            obs["scale"] = "thousands"
    scaled = build_assessment(AssessmentInput.model_validate(data), InvestmentConfig())
    ordinary = build_assessment(AssessmentInput.model_validate(request_data()), InvestmentConfig())
    assert scaled.fundamentals.metrics["roe"].value == pytest.approx(
        ordinary.fundamentals.metrics["roe"].value
    )
    data["financial_data"]["observations"][-1]["currency"] = "EUR"
    result = build_assessment(AssessmentInput.model_validate(data), InvestmentConfig())
    assert not result.data_quality.reliable and result.recommendation == "NO TRADE"


@pytest.mark.parametrize("risk", [True, False, "2", 2.0, 0, 4])
def test_risk_profile_accepts_only_three_integers(risk):
    data = request_data()
    data["risk_tolerance"] = risk
    with pytest.raises(ValidationError):
        AssessmentInput.model_validate(data)


def test_profiles_change_suitability_not_objective_metrics():
    data = request_data(amplitude=0.04)
    results = [
        build_assessment(
            AssessmentInput.model_validate({**data, "risk_tolerance": n}), InvestmentConfig()
        )
        for n in (1, 2, 3)
    ]
    assert results[0].fundamentals == results[1].fundamentals == results[2].fundamentals
    assert results[0].risk == results[1].risk == results[2].risk
    assert results[0].suitability == "unsuitable"
    assert results[2].suitability == "suitable"


def test_price_risk_formulas_and_dates():
    data = request_data()
    result = build_assessment(AssessmentInput.model_validate(data), InvestmentConfig())
    prices = [p["price"] for p in data["market_data"]["prices"]]
    returns = [b / a - 1 for a, b in zip(prices, prices[1:], strict=False)]
    m = result.risk.market.metrics
    assert m["volatility"].value == pytest.approx(stdev(returns) * sqrt(252))
    assert m["beta"].value == pytest.approx(2)
    expected_drawdown = max(1 - p / max(prices[: i + 1]) for i, p in enumerate(prices))
    assert m["max_drawdown"].value == pytest.approx(expected_drawdown)
    assert m["downside_volatility"].value == pytest.approx(
        sqrt(sum(min(r, 0) ** 2 for r in returns) / len(returns)) * sqrt(252)
    )


def test_future_and_estimated_information_excluded():
    data = request_data()
    baseline = build_assessment(AssessmentInput.model_validate(data), InvestmentConfig())
    future = observation("revenue", 999999)
    future["filing_date"] = "2026-04-01"
    estimated = {**observation("net_income", 999999), "is_estimate": True}
    data["financial_data"]["observations"].extend([future, estimated])
    data["market_data"]["prices"].append(
        {"date": "2026-04-01", "price": 0.01, "adjusted_price": 0.01}
    )
    result = build_assessment(AssessmentInput.model_validate(data), InvestmentConfig())
    assert result.fundamentals == baseline.fundamentals
    assert result.risk == baseline.risk
    assert result.data_quality.excluded_observations == 2


def test_partial_data_does_not_create_neutral_scores_or_buy():
    request = AssessmentInput(
        ticker="NVDA",
        financial_data=FinancialData(ticker="NVDA"),
        market_data=MarketData(ticker="NVDA"),
    )
    result = build_assessment(request, InvestmentConfig())
    assert result.recommendation == "NO TRADE" and result.composite_score is None
    assert result.data_quality.coverage == 0
    assert result.trends.score is None and result.suitability == "unknown"
    assert any("10-K" in w for w in result.data_quality.warnings)


def test_negative_earnings_zero_denominators_and_inconsistent_accounts():
    data = request_data()
    for obs in data["financial_data"]["observations"]:
        if obs["period"]["end"] == "2025-12-31":
            if obs["metric"] == "net_income":
                obs["value"] = -100
            if obs["metric"] in {"current_liabilities", "equity"}:
                obs["value"] = 0
    result = build_assessment(AssessmentInput.model_validate(data), InvestmentConfig())
    assert result.valuation.metrics["pe"].value is None
    assert result.fundamentals.metrics["current_ratio"].value is None
    assert result.fundamentals.metrics["roe"].value is None
    assert result.risk.severe_warnings and result.recommendation == "NO TRADE"
    assert not result.data_quality.reliable


def test_dcf_five_year_fcff_and_not_ordinary_fcf():
    data = request_data()
    config = InvestmentConfig(dcf={"enabled": True})
    result = build_assessment(AssessmentInput.model_validate(data), config)
    fcff = 1000 * 1.15**3 * (0.35 * 0.8 + 0.05 - 0.05 - 0.01)
    expected_ev = (
        sum(fcff * 1.05**y / 1.1**y for y in range(1, 6))
        + (fcff * 1.05**5 * 1.025 / 0.075) / 1.1**5
    )
    assert result.valuation.scenarios["base"].enterprise_value == pytest.approx(expected_ev)
    assert result.valuation.scenarios["base"].value_per_share == pytest.approx(
        (expected_ev + 50) / 10
    )
    data["financial_data"]["observations"] = [
        o
        for o in data["financial_data"]["observations"]
        if o["metric"] != "change_noncash_working_capital"
    ]
    assert build_assessment(AssessmentInput.model_validate(data), config).valuation.scenarios == {}
    with pytest.raises(ValidationError):
        InvestmentConfig(
            dcf={
                "scenarios": {
                    "base": {"growth": 0.05, "discount_rate": 0.02, "terminal_growth": 0.02}
                }
            }
        )


def test_sell_requires_evidence_of_holdings_and_horizon_affects_buy():
    data = request_data()
    config = InvestmentConfig(short_horizon_score_penalty=20)
    long = build_assessment(AssessmentInput.model_validate({**data, "horizon_months": 36}), config)
    short = build_assessment(AssessmentInput.model_validate({**data, "horizon_months": 6}), config)
    assert long.recommendation == "BUY" and short.recommendation == "NO TRADE"
    risky = request_data(amplitude=0.08)
    without = build_assessment(AssessmentInput.model_validate(risky), InvestmentConfig())
    risky["market_data"]["position_quantity"] = {
        "value": 10,
        "date": AS_OF.isoformat(),
        "source": {"id": "position", "kind": "market"},
    }
    with_holding = build_assessment(AssessmentInput.model_validate(risky), InvestmentConfig())
    assert without.recommendation == "NO TRADE" and with_holding.recommendation == "SELL"


def test_quarterly_ytd_and_fiscal_year_periods_are_not_mixed():
    observations = []
    for year in (2024, 2025):
        annual = observation("revenue", 100 if year == 2024 else 120, year)
        annual["period"].update(start=f"{year - 1}-10-01", end=f"{year}-09-30")
        annual["filing_date"] = f"{year}-11-15"
        observations.append(annual)
        ytd = observation("revenue", 60 if year == 2024 else 90, year)
        ytd["period"].update(kind="ytd", start=f"{year}-01-01", end=f"{year}-09-30")
        observations.append(ytd)
    ctx = FinancialContext(
        FinancialData(ticker="AAPL", observations=observations), AS_OF, "USD", InvestmentConfig()
    )
    assert ctx.annual("revenue").value == 120
    assert len(ctx.cohort("revenue")) == 2
    # A single YTD observation must not be used as annual revenue for valuation.
    ctx = FinancialContext(
        FinancialData(ticker="AAPL", observations=[observations[-1]]),
        AS_OF,
        "USD",
        InvestmentConfig(),
    )
    assert ctx.annual("revenue") is None
    assert analyze_valuation(ctx, MarketData(ticker="AAPL")).score is None


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_nonfinite_observations_rejected(value):
    with pytest.raises(ValidationError):
        FinancialObservation.model_validate(observation("revenue", value))


def test_weekly_prices_are_not_annualized_as_daily():
    data = request_data()
    data["market_data"]["prices"] = [
        {
            "date": (AS_OF - timedelta(weeks=i)).isoformat(),
            "price": 100 + i,
            "adjusted_price": 100 + i,
        }
        for i in range(70)
    ]
    result = build_assessment(AssessmentInput.model_validate(data), InvestmentConfig())
    assert result.risk.market.metrics["volatility"].value is None
    assert result.risk.usable_stock_returns == 0
    assert result.recommendation == "NO TRADE"


def test_contiguous_quarters_sum_but_overlapping_ytd_does_not():
    obs = []
    for start, end in (
        ("2025-01-01", "2025-03-31"),
        ("2025-04-01", "2025-06-30"),
        ("2025-07-01", "2025-09-30"),
        ("2025-10-01", "2025-12-31"),
    ):
        item = observation("revenue", 100)
        item["period"] = {"kind": "quarterly", "start": start, "end": end}
        obs.append(item)
    ctx = FinancialContext(
        FinancialData(ticker="AAPL", observations=obs), AS_OF, "USD", InvestmentConfig()
    )
    assert ctx.annual("revenue").value == 400
    obs.pop(1)
    ctx = FinancialContext(
        FinancialData(ticker="AAPL", observations=obs), AS_OF, "USD", InvestmentConfig()
    )
    assert ctx.annual("revenue") is None


def test_horizon_and_ticker_mismatches_are_rejected():
    data = request_data()
    for horizon in (5, 37, True, "12"):
        with pytest.raises(ValidationError):
            AssessmentInput.model_validate({**data, "horizon_months": horizon})
    data["market_data"]["ticker"] = "MSFT"
    with pytest.raises(ValidationError):
        AssessmentInput.model_validate(data)
