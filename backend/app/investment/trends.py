from statistics import mean, stdev

from backend.app.investment.data import FinancialContext, combined, metric, ratio, summarize
from backend.app.investment.fundamentals import growth_series
from backend.app.investment.models import Analysis


def analyze_financial_trends(ctx: FinancialContext) -> Analysis:
    config = ctx.config
    metrics = {}
    warnings = []

    def trend(series, relative=True):
        series = series[-config.trend_window :]
        if len(series) < config.min_trend_observations:
            return None
        if any(f.period.kind == "quarterly" for f in series):
            warnings.append("Quarterly trends may reflect seasonality; they are not YoY forecasts.")
        xs = [(f.period.end - series[0].period.end).days / 365.25 for f in series]
        ys = [f.value for f in series]
        if xs[-1] <= 0:
            return None
        if config.trend_method == "endpoint":
            slope = (ys[-1] - ys[0]) / xs[-1]
        else:
            x_mean, y_mean = mean(xs), mean(ys)
            variance = sum((x - x_mean) ** 2 for x in xs)
            if variance == 0:
                return None
            slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True)) / variance
        if relative:
            denominator = mean(abs(y) for y in ys)
            if denominator <= 1e-12:
                return None
            slope /= denominator
        return combined(slope, series[-1].period, *series)

    for name, source in {
        "revenue_slope": "revenue",
        "earnings_slope": "net_income",
        "fcf_slope": "free_cash_flow",
        "debt_slope": "total_debt",
    }.items():
        metrics[name] = metric(
            trend(ctx.cohort(source)),
            "fraction_per_year",
            "Insufficient comparable history for trend",
        )
    for name, numerator, denominator in (
        ("operating_margin_slope", "operating_income", "revenue"),
        ("net_margin_slope", "net_income", "revenue"),
        ("liquidity_slope", "current_assets", "current_liabilities"),
        ("cash_conversion_slope", "operating_cash_flow", "net_income"),
    ):
        series = [
            value
            for d in ctx.cohort(denominator)
            if (value := ratio(ctx.get(numerator, d.period), d)) is not None
        ]
        metrics[name] = metric(trend(series, relative=False), "ratio_points_per_year")
    rates = growth_series(ctx.cohort("revenue"))
    acceleration = (
        combined(rates[-1].value - rates[-2].value, rates[-1].period, *rates[-2:])
        if len(rates) >= 2
        else None
    )
    metrics["revenue_acceleration"] = metric(
        acceleration, "growth_rate_change", "Need two comparable YoY growth rates"
    )
    for name, source in (("earnings_cv", "net_income"), ("fcf_cv", "free_cash_flow")):
        series = ctx.cohort(source)[-config.trend_window :]
        value = None
        if len(series) >= config.min_trend_observations:
            values = [f.value for f in series]
            if abs(mean(values)) > 1e-12:
                value = combined(stdev(values) / abs(mean(values)), series[-1].period, *series)
        metrics[name] = metric(value, reason="Need enough comparable observations and nonzero mean")
    result = summarize(metrics, config.trend_bands)
    result.warnings = sorted(set(warnings))
    return result
