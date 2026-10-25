"""Normalize units and retain only point-in-time, comparable financial evidence."""

from dataclasses import dataclass
from datetime import date

from backend.app.investment.config import InvestmentConfig, ScoreBand
from backend.app.investment.models import Analysis, FinancialData, Metric, ReportingPeriod


@dataclass(frozen=True)
class Fact:
    value: float
    period: ReportingPeriod
    evidence: tuple[str, ...]


def combined(value: float, period: ReportingPeriod, *facts: Fact) -> Fact:
    return Fact(value, period, tuple(dict.fromkeys(ref for f in facts for ref in f.evidence)))


def period_key(period: ReportingPeriod) -> tuple:
    return (period.kind, period.start if period.kind != "instant" else None, period.end)


def duration(period: ReportingPeriod) -> int:
    return (period.end - period.start).days + 1 if period.start else 0


class FinancialContext:
    def __init__(self, data: FinancialData, as_of: date, currency: str, config: InvestmentConfig):
        self.config, self.as_of, self.currency = config, as_of, currency
        self.facts: dict[tuple, Fact] = {}
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.excluded = 0
        eligible = []
        grouped: dict[tuple, list] = {}
        scales = {"ones": 1, "thousands": 1e3, "millions": 1e6, "billions": 1e9}
        for obs in data.observations:
            if obs.filing_date > as_of or obs.period.end > as_of or obs.is_estimate:
                self.excluded += 1
                continue
            if obs.currency != currency:
                self.excluded += 1
                self.errors.append(f"Excluded {obs.metric}: currency differs from market currency.")
                continue
            eligible.append(obs)
            grouped.setdefault((obs.metric, *period_key(obs.period)), []).append(obs)
        for key, group in grouped.items():
            latest_date = max(o.filing_date for o in group)
            latest = [o for o in group if o.filing_date == latest_date]
            values = [o.value * scales[o.scale] for o in latest]
            if max(values) - min(values) > config.consistency_tolerance * max(
                abs(v) for v in values
            ):
                self.errors.append(f"Conflicting {key[0]} observations for {key[-1]}.")
                self.excluded += len(group)
                continue
            self.facts[key] = Fact(values[0], latest[0].period, tuple(o.source.id for o in latest))
        self.observations = eligible
        self.sources = {o.source.id: o.source for o in eligible}
        if self.excluded:
            self.warnings.append(
                f"Excluded {self.excluded} future, estimated, or inconsistent observations."
            )
        for assets in self.series("total_assets"):
            liabilities, equity = (
                self.get("total_liabilities", assets.period),
                self.get("equity", assets.period),
            )
            if (
                liabilities
                and equity
                and abs(assets.value - liabilities.value - equity.value)
                > (config.consistency_tolerance * max(abs(assets.value), 1))
            ):
                self.errors.append(
                    f"Assets do not reconcile to liabilities plus equity at {assets.period.end}."
                )
        for fcf in self.series("free_cash_flow"):
            cash, capex = (
                self.get("operating_cash_flow", fcf.period),
                self.get("capital_expenditure", fcf.period),
            )
            if (
                cash
                and capex
                and abs(fcf.value - (cash.value - capex.value))
                > (config.consistency_tolerance * max(abs(cash.value), abs(capex.value), 1))
            ):
                self.errors.append(
                    f"Reported FCF differs from CFO minus capex at {fcf.period.end}."
                )

    def series(self, metric: str, kind: str | None = None) -> list[Fact]:
        return sorted(
            [
                f
                for key, f in self.facts.items()
                if key[0] == metric and (kind is None or f.period.kind == kind)
            ],
            key=lambda f: f.period.end,
        )

    def get(self, metric: str, period: ReportingPeriod) -> Fact | None:
        direct = self.facts.get((metric, *period_key(period)))
        if direct:
            return direct
        recipes = {
            "free_cash_flow": ("operating_cash_flow", "capital_expenditure", -1),
            "ebitda": ("operating_income", "depreciation_amortization", 1),
        }
        if metric in recipes:
            left, right, sign = recipes[metric]
            a, b = self.get(left, period), self.get(right, period)
            if a and b:
                return combined(a.value + sign * b.value, period, a, b)
        if metric == "invested_capital":
            equity, debt, cash = (self.get(m, period) for m in ("equity", "total_debt", "cash"))
            if equity and debt and cash:
                return combined(equity.value + debt.value - cash.value, period, equity, debt, cash)
        if metric == "nopat":
            ebit, pretax, tax = (
                self.get(m, period) for m in ("operating_income", "pretax_income", "income_tax")
            )
            if ebit and pretax and tax and pretax.value > 0 and 0 <= tax.value / pretax.value <= 1:
                return combined(
                    ebit.value * (1 - tax.value / pretax.value), period, ebit, pretax, tax
                )
        if metric == "free_cash_flow_to_firm":
            parts = [
                self.get(m, period)
                for m in (
                    "nopat",
                    "depreciation_amortization",
                    "capital_expenditure",
                    "change_noncash_working_capital",
                )
            ]
            if all(p is not None for p in parts):
                a, b, c, d = parts
                return combined(a.value + b.value - c.value - d.value, period, *parts)
        return None

    def periods(self, kind: str | None = None) -> list[ReportingPeriod]:
        unique = {
            period_key(f.period): f.period
            for f in self.facts.values()
            if kind is None or f.period.kind == kind
        }
        return sorted(unique.values(), key=lambda p: p.end)

    def latest(self, metric: str, kind: str | None = None) -> Fact | None:
        found = [f for p in self.periods(kind) if (f := self.get(metric, p)) is not None]
        return max(found, key=lambda f: (f.period.end, duration(f.period))) if found else None

    def annual(self, metric: str) -> Fact | None:
        # Do not multiply a single quarter/YTD by 4/12: only annual or four contiguous quarters.
        candidates = [f for p in self.periods("annual") if (f := self.get(metric, p)) is not None]
        quarters = [f for p in self.periods("quarterly") if (f := self.get(metric, p)) is not None]
        if metric != "diluted_eps":
            for end in range(4, len(quarters) + 1):
                four = quarters[end - 4 : end]
                if (
                    all(
                        0 < (b.period.start - a.period.end).days <= 7
                        for a, b in zip(four, four[1:], strict=False)
                    )
                    and 330 <= (four[-1].period.end - four[0].period.start).days + 1 <= 380
                ):
                    period = ReportingPeriod(
                        kind="annual", start=four[0].period.start, end=four[-1].period.end
                    )
                    candidates.append(combined(sum(f.value for f in four), period, *four))
        candidates = [
            f
            for f in candidates
            if (self.as_of - f.period.end).days <= self.config.max_financial_age_days
        ]
        return max(candidates, key=lambda f: f.period.end) if candidates else None

    def cohort(self, metric: str) -> list[Fact]:
        groups = []
        for kind in ("annual", "quarterly", "ytd", "instant"):
            series = [f for p in self.periods(kind) if (f := self.get(metric, p)) is not None]
            if series and kind == "ytd":
                series = [
                    f for f in series if abs(duration(f.period) - duration(series[-1].period)) <= 14
                ]
            if series:
                groups.append(series)
        return max(groups, key=lambda g: (len(g), g[-1].period.end)) if groups else []


def metric(
    fact: Fact | None, units: str = "ratio", reason: str = "Missing comparable inputs"
) -> Metric:
    return (
        Metric(value=fact.value, units=units, period=fact.period, evidence=list(fact.evidence))
        if fact
        else Metric(units=units, reason=reason)
    )


def ratio(a: Fact | None, b: Fact | None) -> Fact | None:
    if a is None or b is None or b.value <= 0 or period_key(a.period) != period_key(b.period):
        return None
    return combined(a.value / b.value, a.period, a, b)


def score(value: float, band: ScoreBand) -> float:
    normalized = max(0.0, min(1.0, (value - band.low) / (band.high - band.low)))
    return 100 * (normalized if band.higher_is_better else 1 - normalized)


def summarize(metrics: dict[str, Metric], bands: dict[str, ScoreBand]) -> Analysis:
    available = {
        name: score(metrics[name].value, band)
        for name, band in bands.items()
        if name in metrics and metrics[name].value is not None
    }
    return Analysis(
        metrics=metrics,
        score=sum(available.values()) / len(available) if available else None,
        coverage=len(available) / len(bands) if bands else 0,
        positive=[name for name, value in available.items() if value >= 70],
        negative=[name for name, value in available.items() if value <= 30],
    )
