from datetime import timedelta

from backend.app.investment.data import (
    FinancialContext,
    combined,
    duration,
    metric,
    ratio,
    summarize,
)
from backend.app.investment.models import Analysis, Metric, ReportingPeriod


def growth_series(series):
    result = []
    for latest in series:
        previous = [
            f
            for f in series
            if 350 <= (latest.period.end - f.period.end).days <= 380
            and abs(duration(latest.period) - duration(f.period)) <= 14
            and f.period.kind == latest.period.kind
        ]
        if previous:
            prior = min(previous, key=lambda f: abs((latest.period.end - f.period.end).days - 365))
            if prior.value > 0:
                result.append(
                    combined(latest.value / prior.value - 1, latest.period, latest, prior)
                )
    return result


def analyze_fundamentals(ctx: FinancialContext) -> Analysis:
    metrics: dict[str, Metric] = {}
    for name, source in {
        "revenue_yoy_growth": "revenue",
        "net_income_growth": "net_income",
        "eps_growth": "diluted_eps",
        "fcf_growth": "free_cash_flow",
    }.items():
        series = ctx.cohort(source)
        growth = growth_series(series)
        # A stale earlier comparison must not masquerade as growth of the latest period.
        latest = growth[-1] if growth and growth[-1].period == series[-1].period else None
        metrics[name] = metric(
            latest, reason="Need comparable year-ago period and positive prior value"
        )
    annual = ctx.series("revenue", "annual")
    cagr = None
    if len(annual) >= 2 and annual[0].value > 0 and annual[-1].value > 0:
        years = (annual[-1].period.end - annual[0].period.end).days / 365.25
        if years >= 0.9:
            cagr = combined(
                (annual[-1].value / annual[0].value) ** (1 / years) - 1,
                annual[-1].period,
                annual[0],
                annual[-1],
            )
    metrics["revenue_cagr"] = metric(cagr)
    revenue = ctx.latest("revenue")
    for name, numerator in {
        "gross_margin": "gross_profit",
        "operating_margin": "operating_income",
        "net_margin": "net_income",
        "fcf_margin": "free_cash_flow",
    }.items():
        metrics[name] = metric(
            ratio(ctx.get(numerator, revenue.period), revenue) if revenue else None
        )
    income = ctx.latest("net_income")
    metrics["cash_conversion"] = metric(
        ratio(ctx.get("operating_cash_flow", income.period), income) if income else None,
        reason="Need positive net income and same-period CFO",
    )
    for name, flow_metric, balance_metric in (
        ("roe", "net_income", "equity"),
        ("roic", "nopat", "invested_capital"),
    ):
        flow = ctx.annual(flow_metric)
        result = None
        if flow:
            beginning = ctx.get(
                balance_metric,
                ReportingPeriod(kind="instant", end=flow.period.start - timedelta(days=1)),
            )
            ending = ctx.get(balance_metric, ReportingPeriod(kind="instant", end=flow.period.end))
            if beginning and ending and beginning.value > 0 and ending.value > 0:
                result = combined(
                    flow.value / ((beginning.value + ending.value) / 2),
                    flow.period,
                    flow,
                    beginning,
                    ending,
                )
        metrics[name] = metric(
            result, reason="Need annual/TTM income and positive opening/closing capital"
        )
    equity = ctx.latest("equity")
    metrics["debt_to_equity"] = metric(
        ratio(ctx.get("total_debt", equity.period), equity) if equity else None
    )
    current_assets = ctx.latest("current_assets")
    metrics["current_ratio"] = metric(
        ratio(current_assets, ctx.get("current_liabilities", current_assets.period))
        if current_assets
        else None
    )
    ebit = ctx.latest("operating_income")
    metrics["interest_coverage"] = metric(
        ratio(ebit, ctx.get("interest_expense", ebit.period)) if ebit else None,
        reason="Need EBIT and positive same-period interest expense",
    )
    ebitda = ctx.annual("ebitda")
    leverage = None
    if ebitda and ebitda.value > 0:
        period = ReportingPeriod(kind="instant", end=ebitda.period.end)
        debt, cash = ctx.get("total_debt", period), ctx.get("cash", period)
        if debt and cash:
            leverage = combined(
                (debt.value - cash.value) / ebitda.value, ebitda.period, debt, cash, ebitda
            )
    metrics["net_debt_to_ebitda"] = metric(
        leverage, reason="Need annual/TTM positive EBITDA and matching debt/cash"
    )
    return summarize(metrics, ctx.config.fundamentals_bands)
