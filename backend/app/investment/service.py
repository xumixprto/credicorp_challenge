from datetime import UTC, datetime

from backend.app.investment.config import InvestmentConfig
from backend.app.investment.data import FinancialContext
from backend.app.investment.fundamentals import analyze_fundamentals
from backend.app.investment.models import AssessmentInput, DataQuality, InvestmentAssessment
from backend.app.investment.risk import assess_financial_risk
from backend.app.investment.trends import analyze_financial_trends
from backend.app.investment.valuation import analyze_valuation, quote_for


def build_assessment(request: AssessmentInput, config: InvestmentConfig) -> InvestmentAssessment:
    as_of = request.as_of_date or datetime.now(UTC).date()
    ctx = FinancialContext(request.financial_data, as_of, request.market_data.currency, config)
    fundamentals = analyze_fundamentals(ctx)
    trends = analyze_financial_trends(ctx)
    risk = assess_financial_risk(ctx, fundamentals, trends, request.market_data)
    valuation = analyze_valuation(ctx, request.market_data)
    profile = config.profiles[request.risk_tolerance]
    weights = (profile.weights or config.weights).model_dump()
    risk_scores = [a.score for a in (risk.business, risk.market) if a.score is not None]
    components = {
        "fundamentals": fundamentals.score,
        "trends": trends.score,
        "valuation": valuation.score,
        "risk": 100 - sum(risk_scores) / len(risk_scores) if risk_scores else None,
    }
    denominator = sum(weights[name] for name, value in components.items() if value is not None)
    composite = (
        sum(weights[name] * value for name, value in components.items() if value is not None)
        / denominator
        if denominator
        else None
    )
    coverage = sum(
        weights[name] * available
        for name, available in {
            "fundamentals": fundamentals.coverage,
            "trends": trends.coverage,
            "valuation": valuation.coverage,
            "risk": (risk.business.coverage + risk.market.coverage) / 2,
        }.items()
    )
    quote = quote_for(request.market_data, as_of)
    quote_age = (as_of - quote.date).days if quote else None
    revenue = ctx.latest("revenue")
    financial_age = (as_of - revenue.period.end).days if revenue else None
    filing_types = sorted({o.source.kind for o in ctx.observations} & {"10-K", "10-Q"})
    provider_statements = any(o.source.kind == "financial_data" for o in ctx.observations)
    warnings = [
        *ctx.warnings,
        *ctx.errors,
        *trends.warnings,
        *risk.market.warnings,
        *valuation.warnings,
    ]
    for filing in ("10-K", "10-Q"):
        if filing not in filing_types and not provider_statements:
            warnings.append(
                f"No usable {filing} evidence supplied for {request.ticker}; "
                "do not imply it was reviewed."
            )
    reliable = (
        bool(ctx.facts)
        and not ctx.errors
        and not any("unreliable" in w for w in valuation.warnings)
    )
    suitability = "unknown"
    if risk.severe_warnings:
        suitability = "unsuitable"
    elif (
        risk.business.score is not None
        and risk.market.score is not None
        and min(risk.business.coverage, risk.market.coverage) >= profile.min_coverage
    ):
        suitability = (
            "suitable"
            if (
                risk.business.score <= profile.max_business_risk
                and risk.market.score <= profile.max_market_risk
            )
            else "unsuitable"
        )
    reasons = []
    if not ctx.facts:
        reasons.append("No usable financial observations were supplied to the assessment.")
    elif not reliable:
        reasons.append("Financial/market inputs are inconsistent; no supported recommendation.")
    if coverage < profile.min_coverage or any(v is None for v in components.values()):
        reasons.append("Insufficient metric coverage for this risk profile.")
    if financial_age is None or financial_age > config.max_financial_age_days:
        reasons.append("Financial evidence is missing or stale.")
    if quote_age is None or quote_age > config.max_quote_age_days:
        reasons.append("Price evidence is missing or stale.")
    if not any(o.period.kind == "annual" for o in ctx.observations):
        reasons.append(
            "Annual financial observations were not supplied or usable. "
            "There is not enough information for an investment decision."
        )
    annual_revenue = ctx.annual("revenue")
    if (
        annual_revenue
        and (as_of - annual_revenue.period.end).days > config.interim_required_after_days
        and not any(
            o.period.kind in {"quarterly", "ytd"} and o.period.end > annual_revenue.period.end
            for o in ctx.observations
        )
    ):
        reasons.append("Recent interim filing observations were not supplied or usable.")
    history = [p for p in request.market_data.prices if p.date <= as_of]
    if not history or (as_of - max(p.date for p in history)).days > config.max_quote_age_days:
        reasons.append("Market risk history is missing or stale.")
    adequate = not reasons
    short = request.horizon_months < config.short_horizon_months
    threshold = profile.buy_score + (config.short_horizon_score_penalty if short else 0)
    safety = profile.margin_of_safety + (config.short_horizon_safety_extra if short else 0)
    base = valuation.scenarios.get("base")
    fcf_yield = valuation.metrics["fcf_yield"].value
    valuation_support = (
        base.margin_of_safety >= safety
        if base
        else (
            fcf_yield is not None
            and fcf_yield
            >= profile.min_fcf_yield + (config.short_horizon_safety_extra / 10 if short else 0)
        )
    )
    if profile.require_dcf_margin and base is None:
        valuation_support = False
    position = request.market_data.position_quantity
    holding = bool(
        position
        and position.date <= as_of
        and (as_of - position.date).days <= config.max_quote_age_days
        and position.value > 0
    )
    recommendation = "NO TRADE"
    if adequate:
        if risk.severe_warnings:
            recommendation = "SELL" if holding else "NO TRADE"
            reasons.extend(risk.severe_warnings)
        elif composite is not None and (
            composite <= profile.sell_score
            or suitability == "unsuitable"
            or fundamentals.score < profile.min_fundamentals_score
        ):
            recommendation = "SELL" if holding else "NO TRADE"
            reasons.append("Score or measured risk is outside this investor profile's limits.")
        elif (
            composite is not None
            and composite >= threshold
            and suitability == "suitable"
            and valuation_support
        ):
            recommendation = "BUY"
            reasons.append(
                "Evidence, score, valuation and risk meet the configured heuristic thresholds."
            )
        else:
            recommendation = "HOLD" if holding and suitability == "suitable" else "NO TRADE"
            reasons.append(
                "The BUY score, valuation cushion, or risk suitability is not established."
            )
    if not holding and recommendation == "NO TRADE":
        reasons.append(
            "No holding was established; avoid initiating a purchase rather than sell shares."
        )
    if short:
        reasons.append("The shorter horizon requires a higher BUY score and valuation cushion.")
    sources = dict(ctx.sources)
    for source in [
        request.market_data.source,
        request.market_data.history_source,
        request.market_data.benchmark_source,
        *[
            v.source
            for v in (
                request.market_data.shares_outstanding,
                request.market_data.market_capitalization,
                position,
            )
            if v
        ],
    ]:
        if source:
            sources[source.id] = source
    analyses = {
        "fundamentals": fundamentals,
        "trends": trends,
        "valuation": valuation,
        "business_risk": risk.business,
        "market_risk": risk.market,
    }
    return InvestmentAssessment(
        ticker=request.ticker,
        as_of_date=as_of,
        risk_tolerance=request.risk_tolerance,
        risk_profile=profile.name,
        horizon_months=request.horizon_months,
        recommendation=recommendation,
        composite_score=composite,
        component_scores=components,
        fundamentals=fundamentals,
        trends=trends,
        risk=risk,
        suitability=suitability,
        valuation=valuation,
        bullish_factors=[
            f"{name}: {factor}" for name, a in analyses.items() for factor in a.positive
        ],
        bearish_factors=[
            f"{name}: {factor}" for name, a in analyses.items() for factor in a.negative
        ],
        thesis_risks=[
            *risk.severe_warnings,
            "Thesis requires continuing earnings/cash-flow support and acceptable leverage.",
            "Modeled valuation depends on hypothetical assumptions; market prices can diverge.",
            "Missing filings, stale data or inconsistent accounts can invalidate the assessment.",
        ],
        data_quality=DataQuality(
            coverage=coverage,
            reliable=reliable,
            included_observations=len(ctx.facts),
            excluded_observations=ctx.excluded,
            financial_age_days=financial_age,
            quote_age_days=quote_age,
            filing_types=filing_types,
            missing_metrics=[
                f"{name}.{m}"
                for name, a in analyses.items()
                for m, value in a.metrics.items()
                if value.value is None
            ],
            warnings=list(dict.fromkeys(warnings)),
        ),
        evidence=list(sources.values()),
        reasons=reasons,
    )
