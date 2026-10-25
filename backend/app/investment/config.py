"""Central scoring heuristics. Metric formulas live in the adjacent analysis modules."""

from pathlib import Path

import yaml
from pydantic import Field, model_validator

from backend.app.investment.models import Model, Number
from shared.settings import PROJECT_ROOT


class ScoreBand(Model):
    low: Number
    high: Number
    higher_is_better: bool = True

    @model_validator(mode="after")
    def ordered(self):
        if self.low >= self.high:
            raise ValueError("Score band low must be below high")
        return self


def band(low: float, high: float, higher: bool = True) -> ScoreBand:
    return ScoreBand(low=low, high=high, higher_is_better=higher)


class Weights(Model):
    fundamentals: float = Field(default=0.35, ge=0, le=1)
    trends: float = Field(default=0.25, ge=0, le=1)
    valuation: float = Field(default=0.25, ge=0, le=1)
    risk: float = Field(default=0.15, ge=0, le=1)

    @model_validator(mode="after")
    def total(self):
        if abs(sum(self.model_dump().values()) - 1) > 1e-9:
            raise ValueError("Component weights must sum to 1")
        return self


class RiskProfile(Model):
    name: str
    buy_score: float = Field(ge=0, le=100)
    sell_score: float = Field(ge=0, le=100)
    min_coverage: float = Field(ge=0, le=1)
    max_business_risk: float = Field(ge=0, le=100)
    max_market_risk: float = Field(ge=0, le=100)
    margin_of_safety: float = Field(ge=0, lt=1)
    min_fcf_yield: float = Field(ge=0, le=1)
    min_fundamentals_score: float = Field(default=50, ge=0, le=100)
    require_dcf_margin: bool = False
    weights: Weights | None = None

    @model_validator(mode="after")
    def thresholds(self):
        if self.sell_score >= self.buy_score:
            raise ValueError("sell_score must be below buy_score")
        return self


class ScenarioAssumptions(Model):
    growth: float = Field(gt=-1, le=1)
    discount_rate: float = Field(gt=0, le=1)
    terminal_growth: float = Field(ge=-0.1, le=0.1)

    @model_validator(mode="after")
    def discount_exceeds_growth(self):
        if self.discount_rate <= self.terminal_growth:
            raise ValueError("DCF discount rate must exceed terminal growth")
        return self


class DCFConfig(Model):
    enabled: bool = False
    scenarios: dict[str, ScenarioAssumptions] = Field(
        default_factory=lambda: {
            "bear": ScenarioAssumptions(growth=0, discount_rate=0.12, terminal_growth=0.02),
            "base": ScenarioAssumptions(growth=0.05, discount_rate=0.10, terminal_growth=0.025),
            "bull": ScenarioAssumptions(growth=0.10, discount_rate=0.09, terminal_growth=0.03),
        }
    )

    @model_validator(mode="after")
    def names(self):
        if set(self.scenarios) != {"bear", "base", "bull"}:
            raise ValueError("DCF scenarios must be bear, base, and bull")
        return self


class InvestmentConfig(Model):
    weights: Weights = Field(default_factory=Weights)
    profiles: dict[int, RiskProfile] = Field(
        default_factory=lambda: {
            1: RiskProfile(
                name="Conservative",
                buy_score=80,
                sell_score=35,
                min_coverage=0.8,
                max_business_risk=30,
                max_market_risk=35,
                margin_of_safety=0.30,
                min_fcf_yield=0.06,
                min_fundamentals_score=65,
                require_dcf_margin=True,
            ),
            2: RiskProfile(
                name="Moderate",
                buy_score=70,
                sell_score=30,
                min_coverage=0.65,
                max_business_risk=50,
                max_market_risk=60,
                margin_of_safety=0.20,
                min_fcf_yield=0.04,
                min_fundamentals_score=55,
            ),
            3: RiskProfile(
                name="Aggressive",
                buy_score=65,
                sell_score=25,
                min_coverage=0.55,
                max_business_risk=65,
                max_market_risk=80,
                margin_of_safety=0.10,
                min_fcf_yield=0.025,
                min_fundamentals_score=45,
            ),
        }
    )
    fundamentals_bands: dict[str, ScoreBand] = Field(
        default_factory=lambda: {
            **{
                name: band(-0.1, 0.2)
                for name in (
                    "revenue_yoy_growth",
                    "revenue_cagr",
                    "net_income_growth",
                    "eps_growth",
                    "fcf_growth",
                )
            },
            "gross_margin": band(0.1, 0.6),
            "operating_margin": band(0, 0.3),
            "net_margin": band(0, 0.25),
            "roe": band(0, 0.25),
            "roic": band(0, 0.2),
            "fcf_margin": band(0, 0.25),
            "cash_conversion": band(0.5, 1.5),
            "debt_to_equity": band(0, 2, False),
            "net_debt_to_ebitda": band(0, 4, False),
            "interest_coverage": band(1, 8),
            "current_ratio": band(0.5, 2),
        }
    )
    trend_bands: dict[str, ScoreBand] = Field(
        default_factory=lambda: {
            "revenue_slope": band(-0.1, 0.2),
            "revenue_acceleration": band(-0.1, 0.1),
            "earnings_slope": band(-0.1, 0.2),
            "operating_margin_slope": band(-0.05, 0.05),
            "net_margin_slope": band(-0.05, 0.05),
            "fcf_slope": band(-0.1, 0.2),
            "debt_slope": band(-0.1, 0.2, False),
            "liquidity_slope": band(-0.2, 0.2),
            "cash_conversion_slope": band(-0.2, 0.2),
            "earnings_cv": band(0, 1, False),
            "fcf_cv": band(0, 1, False),
        }
    )
    business_risk_bands: dict[str, ScoreBand] = Field(
        default_factory=lambda: {
            "debt_to_equity": band(0, 3),
            "net_debt_to_ebitda": band(0, 5),
            "current_ratio": band(0.5, 2, False),
            "interest_coverage": band(1, 8, False),
            "earnings_cv": band(0, 1),
            "fcf_cv": band(0, 1),
            "operating_margin_slope": band(-0.1, 0, False),
        }
    )
    market_risk_bands: dict[str, ScoreBand] = Field(
        default_factory=lambda: {
            "volatility": band(0.1, 0.6),
            "beta": band(0.5, 2),
            "max_drawdown": band(0.1, 0.6),
            "downside_volatility": band(0.05, 0.4),
            "extreme_move_frequency": band(0, 0.05),
        }
    )
    valuation_bands: dict[str, ScoreBand] = Field(
        default_factory=lambda: {
            "pe": band(10, 40, False),
            "price_to_fcf": band(10, 40, False),
            "fcf_yield": band(0.01, 0.08),
            "price_to_sales": band(1, 10, False),
            "ev_to_ebitda": band(5, 25, False),
        }
    )
    dcf: DCFConfig = Field(default_factory=DCFConfig)
    min_trend_observations: int = Field(default=3, ge=3, le=20)
    trend_window: int = Field(default=8, ge=3, le=40)
    trend_method: str = "regression"
    min_price_returns: int = Field(default=60, ge=20, le=1000)
    trading_days_per_year: int = Field(default=252, ge=200, le=366)
    max_return_gap_days: int = Field(default=7, ge=3, le=14)
    min_daily_price_coverage: float = Field(default=0.8, gt=0, le=1)
    extreme_daily_move: float = Field(default=0.10, gt=0, lt=1)
    max_financial_age_days: int = Field(default=450, ge=90)
    max_quote_age_days: int = Field(default=7, ge=0)
    max_share_age_days: int = Field(default=120, ge=0)
    interim_required_after_days: int = Field(default=150, ge=0)
    severe_interest_coverage: float = Field(default=1, ge=0)
    severe_current_ratio: float = Field(default=0.5, ge=0)
    consistency_tolerance: float = Field(default=0.02, gt=0, lt=0.2)
    short_horizon_months: int = Field(default=12, ge=6, le=36)
    short_horizon_score_penalty: float = Field(default=5, ge=0, le=20)
    short_horizon_safety_extra: float = Field(default=0.05, ge=0, le=0.2)

    @model_validator(mode="after")
    def choices(self):
        if set(self.profiles) != {1, 2, 3}:
            raise ValueError("Exactly risk profiles 1, 2, and 3 are required")
        if self.trend_method not in {"regression", "endpoint"}:
            raise ValueError("trend_method must be regression or endpoint")
        if self.trend_window < self.min_trend_observations:
            raise ValueError("trend_window must cover min_trend_observations")
        return self


def load_investment_config(path: Path) -> InvestmentConfig:
    with (PROJECT_ROOT / path).open(encoding="utf-8") as handle:
        return InvestmentConfig.model_validate((yaml.safe_load(handle) or {}).get("investment", {}))
