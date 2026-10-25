"""Normalize Yahoo financial statements; never ask an LLM to copy numerical inputs."""

from datetime import date, timedelta
from math import isfinite

import pandas as pd
import yfinance as yf
from curl_cffi.requests import Session
from pydantic import Field, ValidationError

from backend.app.investment.models import (
    FinancialData,
    FinancialObservation,
    Model,
    ReportingPeriod,
    SourceReference,
)

# Explicit provider-to-calculator mapping. Unavailable rows remain missing, never zero-filled.
STATEMENTS = {
    "income_stmt": {
        "revenue": "TotalRevenue",
        "gross_profit": "GrossProfit",
        "operating_income": "OperatingIncome",
        "net_income": "NetIncome",
        "diluted_eps": "DilutedEPS",
        "interest_expense": "InterestExpense",
        "pretax_income": "PretaxIncome",
        "income_tax": "TaxProvision",
        "ebitda": "EBITDA",
    },
    "balance_sheet": {
        "equity": "StockholdersEquity",
        "total_debt": "TotalDebt",
        "cash": "CashAndCashEquivalents",
        "current_assets": "CurrentAssets",
        "current_liabilities": "CurrentLiabilities",
        "total_assets": "TotalAssets",
        "total_liabilities": "TotalLiabilitiesNetMinorityInterest",
    },
    "cashflow": {
        "operating_cash_flow": "OperatingCashFlow",
        "capital_expenditure": "CapitalExpenditure",
        "free_cash_flow": "FreeCashFlow",
        "depreciation_amortization": "DepreciationAndAmortization",
    },
}


class FinancialSnapshot(Model):
    financial_data: FinancialData
    asset_type: str | None = None
    currency: str | None = None
    shares_outstanding: float | None = None
    warnings: list[str] = Field(default_factory=list)


def finite_number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if isfinite(number) else None
    except (TypeError, ValueError):
        return None


def normalize_statement(
    ticker: str,
    frame: pd.DataFrame,
    statement: str,
    frequency: str,
    currency: str,
    retrieved_on: date,
) -> list[FinancialObservation]:
    observations = []
    columns = sorted(frame.columns)
    months = 12 if frequency == "yearly" else 3
    for index, column in enumerate(columns):
        end = pd.Timestamp(column).date()
        if end > retrieved_on:
            continue
        # Yahoo gives period ends. Prefer a contiguous previous period; otherwise derive a
        # calendar boundary and explicitly disclose that this is not an SEC-certified start date.
        start = (pd.Timestamp(column) - pd.DateOffset(months=months)).date() + timedelta(days=1)
        if index:
            previous = pd.Timestamp(columns[index - 1]).date()
            gap = (end - previous).days
            low, high = (330, 380) if months == 12 else (70, 110)
            if low <= gap <= high:
                start = previous + timedelta(days=1)
        period = ReportingPeriod(
            kind="instant"
            if statement == "balance_sheet"
            else "annual"
            if frequency == "yearly"
            else "quarterly",
            start=None if statement == "balance_sheet" else start,
            end=end,
        )
        for metric, row in STATEMENTS[statement].items():
            if row not in frame.index:
                continue
            value = finite_number(frame.at[row, column])
            if value is None:
                continue
            if metric in {"capital_expenditure", "interest_expense", "depreciation_amortization"}:
                value = abs(value)
            try:
                observations.append(
                    FinancialObservation(
                        metric=metric,
                        value=value,
                        currency=currency,
                        units="currency_per_share" if metric == "diluted_eps" else "currency",
                        period=period,
                        retrieved_date=retrieved_on,
                        source=SourceReference(
                            id=f"yfinance:{ticker}:{statement}:{frequency}:{end}:{row}",
                            kind="financial_data",
                            url=f"https://finance.yahoo.com/quote/{ticker}/financials/",
                            section=f"{statement}/{frequency}/{row}; retrieved {retrieved_on}",
                        ),
                    )
                )
            except ValidationError:
                continue
    return observations


def fetch_financials(ticker: str, retrieved_on: date, timeout: float) -> FinancialSnapshot:
    snapshot = FinancialSnapshot(financial_data=FinancialData(ticker=ticker))
    with Session(impersonate="chrome", timeout=timeout) as session:
        company = yf.Ticker(ticker, session=session)
        info = company.get_info()
        if str(info.get("symbol", "")).upper() != ticker:
            raise ValueError("Provider did not confirm the requested ticker")
        snapshot.asset_type = info.get("quoteType")
        snapshot.currency = info.get("currency")
        shares = finite_number(info.get("sharesOutstanding"))
        snapshot.shares_outstanding = shares if shares and shares > 0 else None
        if snapshot.asset_type != "EQUITY":
            return snapshot
        currency = info.get("financialCurrency")
        if not currency:
            snapshot.warnings.append(
                "Yahoo did not supply financial currency; statements excluded."
            )
            return snapshot
        for statement in STATEMENTS:
            for frequency in ("yearly", "quarterly"):
                try:
                    frame = getattr(company, f"get_{statement}")(freq=frequency)
                    observations = normalize_statement(
                        ticker,
                        frame,
                        statement,
                        frequency,
                        currency,
                        retrieved_on,
                    )
                    snapshot.financial_data.observations.extend(observations)
                    if not observations:
                        snapshot.warnings.append(
                            f"Yahoo {statement}/{frequency} has no usable rows."
                        )
                except Exception as exc:
                    snapshot.warnings.append(
                        f"Yahoo {statement}/{frequency} unavailable ({type(exc).__name__})."
                    )
    snapshot.warnings.append(
        "Yahoo financials are provider-normalized, not verified SEC filings. Filing dates are "
        "unknown; retrieval dates are recorded separately. Period starts are inferred from "
        "adjacent period ends/calendar frequency. Historical point-in-time analysis is unsupported."
    )
    return snapshot
