from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

Number = Annotated[float, Field(allow_inf_nan=False)]
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
RiskTolerance = Annotated[StrictInt, Field(ge=1, le=3)]
Horizon = Annotated[StrictInt, Field(ge=6, le=36)]
MetricName = Literal[
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "diluted_eps",
    "operating_cash_flow",
    "capital_expenditure",
    "free_cash_flow",
    "ebitda",
    "depreciation_amortization",
    "interest_expense",
    "pretax_income",
    "income_tax",
    "equity",
    "total_debt",
    "cash",
    "current_assets",
    "current_liabilities",
    "total_assets",
    "total_liabilities",
    "invested_capital",
    "nopat",
    "change_noncash_working_capital",
    "free_cash_flow_to_firm",
]
BALANCE_METRICS = {
    "equity",
    "total_debt",
    "cash",
    "current_assets",
    "current_liabilities",
    "total_assets",
    "total_liabilities",
    "invested_capital",
}


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SourceReference(Model):
    id: str = Field(min_length=1, description="Original citation_id or market tool reference.")
    kind: Literal["10-K", "10-Q", "financial_data", "market", "other"]
    filename: str | None = None
    page: int | None = Field(default=None, ge=1)
    section: str | None = None
    url: str | None = None


class ReportingPeriod(Model):
    kind: Literal["annual", "quarterly", "ytd", "instant"]
    start: date | None = None
    end: date

    @model_validator(mode="after")
    def dates(self):
        if self.kind == "instant":
            if self.start is not None and self.start != self.end:
                raise ValueError("An instant period cannot span dates")
        else:
            if self.start is None or self.start > self.end:
                raise ValueError("Flow periods require start <= end")
            days = (self.end - self.start).days + 1
            limits = {"annual": (330, 380), "quarterly": (70, 110), "ytd": (70, 380)}
            low, high = limits[self.kind]
            if not low <= days <= high:
                raise ValueError("Period length is inconsistent with its kind")
        return self


class FinancialObservation(Model):
    metric: MetricName
    value: Number
    units: Literal["currency", "currency_per_share"] = "currency"
    scale: Literal["ones", "thousands", "millions", "billions"] = "ones"
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    period: ReportingPeriod
    filing_date: date | None = None
    retrieved_date: date | None = None
    source: SourceReference
    is_estimate: bool = False

    @model_validator(mode="after")
    def consistency(self):
        if self.filing_date is None and self.retrieved_date is None:
            raise ValueError("A filing date or provider retrieval date is required")
        if self.filing_date is not None and self.filing_date < self.period.end:
            raise ValueError("Filing date cannot precede the reporting period end")
        if (self.metric in BALANCE_METRICS) != (self.period.kind == "instant"):
            raise ValueError(
                "Balance sheet metrics require instant periods; flows require durations"
            )
        if (self.metric == "diluted_eps") != (self.units == "currency_per_share"):
            raise ValueError("Only diluted_eps uses currency_per_share")
        if self.units == "currency_per_share" and self.scale != "ones":
            raise ValueError("EPS must be in currency per share, not millions")
        if (
            self.metric
            in {
                "total_debt",
                "cash",
                "current_assets",
                "current_liabilities",
                "total_assets",
                "total_liabilities",
                "capital_expenditure",
                "interest_expense",
                "depreciation_amortization",
            }
            and self.value < 0
        ):
            raise ValueError("Use positive balances/expense amounts, including capital expenditure")
        return self


class FinancialData(Model):
    ticker: str
    observations: list[FinancialObservation] = Field(default_factory=list, max_length=1000)


class PriceObservation(Model):
    date: date
    price: Positive
    adjusted_price: Positive | None = None


class MarketValue(Model):
    value: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    date: date
    source: SourceReference


class MarketData(Model):
    ticker: str
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    source: SourceReference | None = None
    history_source: SourceReference | None = None
    quote: PriceObservation | None = None
    prices: list[PriceObservation] = Field(default_factory=list, max_length=1500)
    benchmark_ticker: Literal["SPY"] = "SPY"
    benchmark_source: SourceReference | None = None
    benchmark_prices: list[PriceObservation] = Field(default_factory=list, max_length=1500)
    adjustment_as_of_date: date | None = None
    shares_outstanding: MarketValue | None = None
    market_capitalization: MarketValue | None = None
    position_quantity: MarketValue | None = None

    @field_validator("prices", "benchmark_prices")
    @classmethod
    def unique_dates(cls, values):
        if len({p.date for p in values}) != len(values):
            raise ValueError("Historical prices require unique dates")
        return sorted(values, key=lambda p: p.date)

    @model_validator(mode="after")
    def provenance(self):
        if (self.quote or self.prices) and self.source is None:
            raise ValueError("Market prices require a source reference")
        if self.benchmark_prices and self.benchmark_source is None:
            raise ValueError("Benchmark prices require a source reference")
        return self


class AssessmentInput(Model):
    ticker: str = Field(min_length=1, max_length=16)
    financial_data: FinancialData
    market_data: MarketData
    risk_tolerance: RiskTolerance = 2
    horizon_months: Horizon = 12
    as_of_date: date | None = None

    @model_validator(mode="after")
    def matching_tickers(self):
        self.ticker = self.ticker.strip().upper()
        if not self.ticker or any(
            data.ticker.strip().upper() != self.ticker
            for data in (self.financial_data, self.market_data)
        ):
            raise ValueError("All supplied data must refer to the requested ticker/share class")
        return self


class Metric(Model):
    value: Number | None = None
    units: str = "ratio"
    period: ReportingPeriod | None = None
    evidence: list[str] = Field(default_factory=list)
    reason: str | None = None


class Analysis(Model):
    metrics: dict[str, Metric]
    score: Number | None = None
    coverage: Annotated[float, Field(ge=0, le=1)] = 0
    positive: list[str] = Field(default_factory=list)
    negative: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RiskAnalysis(Model):
    business: Analysis
    market: Analysis
    severe_warnings: list[str] = Field(default_factory=list)
    market_window_start: date | None = None
    market_window_end: date | None = None
    usable_stock_returns: int = 0


class DCFScenario(Model):
    growth: Number
    discount_rate: Number
    terminal_growth: Number
    enterprise_value: Number
    equity_value: Number
    value_per_share: Number
    upside: Number
    margin_of_safety: Number
    evidence: list[str]


class Valuation(Analysis):
    status: Literal["available", "inconclusive"]
    scenarios: dict[str, DCFScenario] = Field(default_factory=dict)
    assumptions: dict[str, str | float | bool] = Field(default_factory=dict)


class DataQuality(Model):
    coverage: Annotated[float, Field(ge=0, le=1)]
    reliable: bool
    included_observations: int
    excluded_observations: int
    financial_age_days: int | None
    quote_age_days: int | None
    filing_types: list[str]
    missing_metrics: list[str]
    warnings: list[str]


class InvestmentAssessment(Model):
    ticker: str
    as_of_date: date
    risk_tolerance: RiskTolerance
    risk_profile: str
    horizon_months: Horizon
    recommendation: Literal["BUY", "HOLD", "SELL", "NO TRADE"]
    composite_score: Number | None
    component_scores: dict[str, Number | None]
    fundamentals: Analysis
    trends: Analysis
    risk: RiskAnalysis
    suitability: Literal["suitable", "unsuitable", "unknown"]
    valuation: Valuation
    bullish_factors: list[str]
    bearish_factors: list[str]
    thesis_risks: list[str]
    data_quality: DataQuality
    evidence: list[SourceReference]
    reasons: list[str]
    methodology: str = (
        "Unvalidated heuristic scores, not calibrated forecasts or trade instructions."
    )
