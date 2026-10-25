from datetime import date

from backend.app.investment.data import FinancialContext, summarize
from backend.app.investment.models import (
    DCFScenario,
    MarketData,
    Metric,
    Valuation,
)


def quote_for(market: MarketData, as_of: date):
    quotes = [p for p in [market.quote, *market.prices] if p is not None and p.date <= as_of]
    return max(quotes, key=lambda p: p.date) if quotes else None


def analyze_valuation(ctx: FinancialContext, market: MarketData) -> Valuation:
    config = ctx.config
    metrics = {
        name: Metric(reason="Need price and positive annual/TTM financial denominator")
        for name in config.valuation_bands
    }
    metrics.update(
        market_capitalization=Metric(
            units=ctx.currency, reason="Need supplied market cap or price and fresh shares"
        ),
        enterprise_value=Metric(units=ctx.currency, reason="Need market cap, debt and cash"),
    )
    warnings = []
    quote = quote_for(market, ctx.as_of)
    shares = market.shares_outstanding
    if shares and (
        shares.date > ctx.as_of
        or (ctx.as_of - shares.date).days > config.max_share_age_days
        or shares.value <= 0
    ):
        shares = None
    supplied_cap = market.market_capitalization
    if supplied_cap and (
        supplied_cap.date > ctx.as_of
        or (ctx.as_of - supplied_cap.date).days > config.max_quote_age_days
        or supplied_cap.value <= 0
    ):
        supplied_cap = None
    cap = (
        supplied_cap.value
        if supplied_cap
        else (quote.price * shares.value if quote and shares else None)
    )
    cap_evidence = (
        [supplied_cap.source.id]
        if supplied_cap
        else ([market.source.id, shares.source.id] if quote and shares else [])
    )
    if (
        supplied_cap
        and quote
        and shares
        and abs(supplied_cap.value - quote.price * shares.value)
        > config.consistency_tolerance * supplied_cap.value
    ):
        warnings.append(
            "Market capitalization and price times shares disagree; valuation is unreliable."
        )
        cap = None
    if quote and (ctx.as_of - quote.date).days > config.max_quote_age_days:
        warnings.append("Market price is stale.")
    if cap is not None:
        metrics["market_capitalization"] = Metric(
            value=cap, units=ctx.currency, evidence=cap_evidence
        )
        for name, denominator in (
            ("pe", "net_income"),
            ("price_to_fcf", "free_cash_flow"),
            ("price_to_sales", "revenue"),
        ):
            fact = ctx.annual(denominator)
            if fact and fact.value > 0:
                metrics[name] = Metric(
                    value=cap / fact.value,
                    period=fact.period,
                    evidence=[*cap_evidence, *fact.evidence],
                )
                if denominator == "free_cash_flow":
                    metrics["fcf_yield"] = Metric(
                        value=fact.value / cap,
                        period=fact.period,
                        evidence=[*cap_evidence, *fact.evidence],
                    )
    debt = ctx.latest("total_debt")
    cash = ctx.get("cash", debt.period) if debt else None
    enterprise = cap + debt.value - cash.value if cap is not None and debt and cash else None
    if enterprise is not None:
        metrics["enterprise_value"] = Metric(
            value=enterprise,
            units=ctx.currency,
            period=debt.period,
            evidence=[*cap_evidence, *debt.evidence, *cash.evidence],
        )
        ebitda = ctx.annual("ebitda")
        if ebitda and ebitda.value > 0 and ebitda.period.end == debt.period.end and enterprise > 0:
            metrics["ev_to_ebitda"] = Metric(
                value=enterprise / ebitda.value,
                period=ebitda.period,
                evidence=[*metrics["enterprise_value"].evidence, *ebitda.evidence],
            )
    scenarios = {}
    if config.dcf.enabled:
        fcff = ctx.annual("free_cash_flow_to_firm")
        if (
            fcff
            and fcff.value > 0
            and quote
            and shares
            and debt
            and cash
            and not ctx.errors
            and debt.period.end == fcff.period.end
            and (ctx.as_of - fcff.period.end).days <= config.max_financial_age_days
        ):
            for name, assumptions in config.dcf.scenarios.items():
                growth, discount, terminal = (
                    assumptions.growth,
                    assumptions.discount_rate,
                    assumptions.terminal_growth,
                )
                cash_flows = [fcff.value * (1 + growth) ** year for year in range(1, 6)]
                terminal_value = cash_flows[-1] * (1 + terminal) / (discount - terminal)
                ev = (
                    sum(value / (1 + discount) ** year for year, value in enumerate(cash_flows, 1))
                    + terminal_value / (1 + discount) ** 5
                )
                equity_value = ev - debt.value + cash.value
                per_share = equity_value / shares.value
                scenarios[name] = DCFScenario(
                    **assumptions.model_dump(),
                    enterprise_value=ev,
                    equity_value=equity_value,
                    value_per_share=per_share,
                    upside=per_share / quote.price - 1,
                    margin_of_safety=1 - quote.price / per_share if per_share > 0 else -1,
                    evidence=list(
                        dict.fromkeys(
                            [
                                *fcff.evidence,
                                *debt.evidence,
                                *cash.evidence,
                                shares.source.id,
                                market.source.id,
                            ]
                        )
                    ),
                )
        else:
            warnings.append(
                "DCF inconclusive: needs reliable positive annual FCFF, "
                "matching debt/cash, price and fresh shares."
            )
    summary = summarize(metrics, config.valuation_bands)
    return Valuation(
        **summary.model_dump(exclude={"warnings"}),
        warnings=warnings,
        status="available" if summary.score is not None else "inconclusive",
        scenarios=scenarios,
        assumptions={
            "dcf_enabled": config.dcf.enabled,
            "dcf_years": 5,
            "dcf_basis": "FCFF discounted at assumed WACC; subtract net debt for equity value.",
            "rates": "Configured hypothetical scenarios, not market estimates or forecasts.",
            "multiples": "Annual or four contiguous quarters; never extrapolate one quarter/YTD.",
        },
    )
