from datetime import date, timedelta
from math import sqrt
from statistics import mean, stdev

from backend.app.investment.config import InvestmentConfig
from backend.app.investment.data import FinancialContext, summarize
from backend.app.investment.models import Analysis, MarketData, Metric, RiskAnalysis


def price_returns(prices, as_of: date, config: InvestmentConfig):
    points = sorted([p for p in prices if p.date <= as_of], key=lambda p: p.date)
    adjusted = bool(points) and all(p.adjusted_price is not None for p in points)
    values = [p.adjusted_price if adjusted else p.price for p in points]
    returns = {
        (a.date, b.date): values[i + 1] / values[i] - 1
        for i, (a, b) in enumerate(zip(points, points[1:], strict=False))
        if (b.date - a.date).days <= config.max_return_gap_days
    }
    if len(points) >= 2:
        weekdays = sum(
            (points[0].date + timedelta(days=i)).weekday() < 5
            for i in range((points[-1].date - points[0].date).days + 1)
        )
        # Weekly/monthly or heavily missing bars must not be annualized as daily returns.
        if len(points) / max(weekdays, 1) < config.min_daily_price_coverage:
            returns = {}
    return points, values, returns, adjusted


def assess_financial_risk(
    ctx: FinancialContext, fundamentals: Analysis, trends: Analysis, market: MarketData
) -> RiskAnalysis:
    config = ctx.config
    business_metrics = {
        name: fundamentals.metrics.get(
            name, trends.metrics.get(name, Metric(reason="Missing data"))
        )
        for name in config.business_risk_bands
    }
    business = summarize(business_metrics, config.business_risk_bands)
    business.positive, business.negative = business.negative, business.positive
    severe = []
    equity = ctx.latest("equity")
    if equity and equity.value <= 0:
        severe.append("Nonpositive book equity: solvency/financial-quality review required.")
    interest = fundamentals.metrics["interest_coverage"].value
    if interest is not None and interest < config.severe_interest_coverage:
        severe.append("Interest coverage is below the configured severe-risk threshold.")
    current = fundamentals.metrics["current_ratio"].value
    if current is not None and current < config.severe_current_ratio:
        severe.append("Current ratio is below the configured severe-risk threshold.")
    warnings = []
    metrics = {
        name: Metric(reason="Insufficient daily market history")
        for name in config.market_risk_bands
    }
    points, values, returns, adjusted = price_returns(market.prices, ctx.as_of, config)
    _, _, benchmark, benchmark_adjusted = price_returns(market.benchmark_prices, ctx.as_of, config)
    if not adjusted and points:
        warnings.append("Unadjusted prices may distort returns around splits/dividends.")
    if len(points) > config.min_price_returns and not returns:
        warnings.append("Price observations are too sparse to treat as a daily return series.")
    if market.adjustment_as_of_date and market.adjustment_as_of_date > ctx.as_of:
        warnings.append("Excluded prices adjusted using information after the analysis date.")
        returns, benchmark = {}, {}
    if adjusted and market.adjustment_as_of_date is None:
        warnings.append(
            "Adjustment knowledge date is unspecified; historical adjustments are unverified."
        )
    if points and (ctx.as_of - points[-1].date).days > config.max_quote_age_days:
        warnings.append("Historical price series is stale relative to the analysis date.")
    if len(returns) >= config.min_price_returns:
        rs = list(returns.values())
        source = [market.source.id] if market.source else []
        annualizer = sqrt(config.trading_days_per_year)
        peak, drawdown = values[0], 0.0
        for price in values:
            peak = max(peak, price)
            drawdown = max(drawdown, 1 - price / peak)
        for name, value in {
            "volatility": stdev(rs) * annualizer,
            "downside_volatility": sqrt(mean(min(r, 0) ** 2 for r in rs)) * annualizer,
            "max_drawdown": drawdown,
            "extreme_move_frequency": sum(abs(r) >= config.extreme_daily_move for r in rs)
            / len(rs),
        }.items():
            metrics[name] = Metric(value=value, evidence=source)
        keys = sorted(set(returns) & set(benchmark))
        if len(keys) >= config.min_price_returns and adjusted == benchmark_adjusted:
            stock, spy = [returns[k] for k in keys], [benchmark[k] for k in keys]
            stock_mean, spy_mean = mean(stock), mean(spy)
            variance = sum((r - spy_mean) ** 2 for r in spy)
            if variance > 1e-15:
                beta = (
                    sum((a - stock_mean) * (b - spy_mean) for a, b in zip(stock, spy, strict=True))
                    / variance
                )
                metrics["beta"] = Metric(value=beta, evidence=[*source, market.benchmark_source.id])
        elif benchmark:
            warnings.append(
                "Beta needs enough matching stock/SPY return intervals and adjustment bases."
            )
    result = summarize(metrics, config.market_risk_bands)
    result.positive, result.negative = result.negative, result.positive
    result.warnings = warnings
    return RiskAnalysis(
        business=business,
        market=result,
        severe_warnings=severe,
        market_window_start=points[0].date if points else None,
        market_window_end=points[-1].date if points else None,
        usable_stock_returns=len(returns),
    )
