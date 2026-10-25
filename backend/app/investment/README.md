# Investment assessment

`build_investment_assessment` is the only new agent-facing tool. It accepts extracted financial
observations and market data, runs deterministic Python calculations, and returns an assessment.
It has no ChromaDB, Alpaca, OpenAI, database or trading client. Existing retrieval and Alpaca tools
remain independently registered. The agent retrieves evidence, extracts observations, calls this
tool, and explains its output. An assessment never submits an order or bypasses existing approvals.

All scores, bands and recommendation thresholds are **unvalidated heuristics**. They are not
probabilities, calibrated forecasts, sector-adjusted benchmarks or assurances of returns.

## Structure

| File | Responsibility |
| --- | --- |
| `models.py` | Pydantic inputs, observations, periods, provenance and outputs |
| `config.py` | Three risk profiles, score bands, weights, thresholds and DCF assumptions |
| `data.py` | Point-in-time filtering, unit normalization, period matching and derived inputs |
| `fundamentals.py` | `analyze_fundamentals` and deterministic financial ratios |
| `trends.py` | `analyze_financial_trends`, slopes, acceleration and rolling variation |
| `risk.py` | `assess_financial_risk`, separate business and market risk measurements |
| `valuation.py` | `analyze_valuation`, multiples and optional five-year FCFF DCF |
| `service.py` | Coverage, suitability, composite scoring and recommendation rules |
| `../tools/investment.py` | Agent Framework adapter registering the single tool |

## Tool input

`ticker`, `financial_data`, and `market_data` are required. Both data objects carry the same ticker
and share class. `risk_tolerance` accepts only integers **1, 2, 3**, default **2 (Moderate)**.
`horizon_months` accepts integers **6–36**, default **12**. `as_of_date` defaults to the UTC date.
The agent discloses the default investor assumptions. Missing data is allowed; malformed data is
rejected. No shared cached-data-reference interface is supported by the current tools.

Minimal valid input (returns NO TRADE with missing-data reasons):

```json
{
  "ticker": "AAPL",
  "risk_tolerance": 2,
  "horizon_months": 12,
  "as_of_date": "2026-03-31",
  "financial_data": {"ticker": "AAPL", "observations": []},
  "market_data": {"ticker": "AAPL", "currency": "USD"}
}
```

Each financial observation has this shape (illustrative values, not actual company results):

```json
{
  "metric": "revenue",
  "value": 1000,
  "units": "currency",
  "scale": "millions",
  "currency": "USD",
  "period": {"kind": "annual", "start": "2025-01-01", "end": "2025-12-31"},
  "filing_date": "2026-02-15",
  "source": {"id": "original-citation-id", "kind": "10-K", "filename": "filing.pdf", "page": 42},
  "is_estimate": false
}
```

- Units: `currency` or `currency_per_share` (EPS only). Scale: `ones`, `thousands`, `millions`,
  `billions`; EPS must use `ones`. Values normalize to base currency units. No FX conversion.
- Period kinds: `annual` (330–380 days), `quarterly` (70–110), `ytd` (70–380), or `instant`.
  Flow observations require start/end; balance-sheet observations require an instant end date.
  Different fiscal year-ends are accepted. Annual and YTD figures are never mixed in ratios.
- Metric names: `revenue`, `gross_profit`, `operating_income`, `net_income`, `diluted_eps`,
  `operating_cash_flow`, `capital_expenditure`, `free_cash_flow`, `ebitda`,
  `depreciation_amortization`, `interest_expense`, `pretax_income`, `income_tax`, `equity`,
  `total_debt`, `cash`, `current_assets`, `current_liabilities`, `total_assets`,
  `total_liabilities`, `invested_capital`, `nopat`, `change_noncash_working_capital`,
  `free_cash_flow_to_firm`.
- Expenses, capex and debt/asset/liability balances use positive amounts; profits, cash flows,
  equity and changes in working capital can be negative. Do not turn a negative cash-flow
  presentation of capex into negative expenditure: supply the positive amount spent.
- Source references require `id` and `kind` (`10-K`, `10-Q`, `market`, `other`), with optional
  `filename`, `page`, `section`, `url`. Preserve original citations. A 10-Q mentioning a 10-K
  is not proof that the 10-K was retrieved. Data is supplied by the caller; citations are retained,
  not independently authenticated by this calculation-only tool.

`market_data` accepts:

| Field | Meaning |
| --- | --- |
| `ticker`, `currency` | Matching share class; currency defaults to USD |
| `source` | Source reference for stock prices, e.g. an Alpaca tool result |
| `quote` | `{ "date": "2026-03-31", "price": 150 }`; valuation uses the unadjusted quote/close |
| `prices` | Up to 1,500 dated daily `{date, price, adjusted_price?}` observations, unique dates |
| `benchmark_ticker`, `benchmark_prices`, `benchmark_source` | SPY daily series and its source |
| `adjustment_as_of_date` | Knowledge date of the price adjustments; avoid future corporate-action adjustments |
| `shares_outstanding` | `{value, date, source}` in actual shares, not millions; same share class and split basis as price |
| `market_capitalization` | Optional `{value, date, source}` in base currency units |
| `position_quantity` | Optional `{value, date, source}` proving existing holdings; omitted means unknown |

Request bounded histories (typically 6–12 months of daily prices) from the existing tools, rather
than filling context with entire filings or maximum-size arrays. There is no new API endpoint.
Returned `evidence` lists source references; individual metrics contain the IDs used in calculations.

## Formulas and period rules

| Metric | Calculation |
| --- | --- |
| Revenue / NI / EPS / FCF growth | Latest comparable value / positive year-ago value − 1; same period kind and similar duration |
| Revenue CAGR | `(last / first) ** (1 / elapsed_years) - 1`, positive annual endpoints |
| Gross / operating / net / FCF margin | Corresponding same-period amount / positive revenue |
| ROE | Annual/TTM net income / average positive opening and closing equity |
| ROIC | Annual/TTM NOPAT / average positive opening and closing invested capital |
| Cash conversion | Same-period operating cash flow / positive net income |
| Debt/equity | Same-date debt / positive equity |
| Net debt/EBITDA | Same-period-end `(debt - cash)` / positive annual/TTM EBITDA |
| Interest coverage | Same-period operating income / positive interest expense |
| Current ratio | Same-date current assets / positive current liabilities |
| FCF | Supplied FCF, otherwise operating cash flow − positive capex |
| EBITDA | Supplied EBITDA, otherwise operating income + D&A |
| NOPAT | Supplied NOPAT, otherwise EBIT × `(1 - income_tax / positive_pretax_income)`; tax rate must be 0–1 |
| Invested capital | Supplied capital, otherwise equity + debt − cash |
| FCFF | Supplied FCFF, otherwise NOPAT + D&A − capex − change in noncash working capital |
| P/E, P/FCF, P/sales | Market capitalization / positive annual or TTM denominator |
| FCF yield | Positive annual/TTM FCF / market capitalization |
| Market capitalization | Supplied cap, otherwise price × fresh share count; conflicting supplied/calculated values block valuation |
| Enterprise value | Market cap + debt − cash; this simplified model excludes minority/preferred interests |
| EV/EBITDA | Positive EV / positive annual/TTM EBITDA, with matching debt/cash balance date |

Annual/TTM denominators use an annual observation or exactly four contiguous quarters. The tool
does not multiply one quarter by four, extrapolate YTD, sum EPS into TTM, or infer missing quarters.
Period-end and filing dates after `as_of_date`, estimates, and incompatible currencies are excluded.
For restated periods, use the latest filing available as of the analysis date. Conflicting values
within the same latest filing, a failed assets/liabilities/equity reconciliation, or inconsistent
reported FCF block supported recommendations. Values must be finite. Zero/nonpositive denominators
produce unavailable metrics, not infinity or invented neutral values.

Trends use the longest comparable cohort (annual, quarterly, matching-duration YTD, or instant),
bounded by `trend_window`. Regression uses elapsed years; `endpoint` uses endpoint change/year.
Absolute-amount slopes divide by average absolute amount; ratio slopes are ratio points/year.
Acceleration is the change between two available YoY growth rates. Earnings/FCF instability uses
sample standard deviation / absolute mean. Short histories yield missing trends; quarterly
patterns may be seasonal. No five-year history is assumed from two available filings.

Market returns use adjusted prices only when every observation has one, otherwise consistently
unadjusted prices with a warning. Daily returns with excessive gaps are excluded. Annual volatility
uses sample standard deviation × square root of trading days/year. Downside volatility uses
`sqrt(mean(min(return, 0)^2))` with the same annualizer. Drawdown is the largest historical
peak-to-trough fraction in the supplied window. Extreme-move frequency counts returns exceeding
the configured absolute threshold. Beta is covariance(stock, SPY) / variance(SPY), using identical
date intervals and matching adjustment bases; insufficient observations or constant SPY returns
produce no beta. Adjustments known only after the historical analysis date are excluded.

Optional DCF explicitly forecasts **five years of FCFF**, discounts them and terminal value at the
scenario WACC, subtracts net debt, then divides by fresh shares. It requires positive reliable FCFF
and complete inputs. Ordinary CFO-minus-capex FCF is not silently treated as FCFF. Bear/base/bull
rates are hypothetical configurable assumptions, not analyst estimates. `upside = value/price - 1`;
`margin_of_safety = 1 - price/value` (nonpositive equity value has no positive safety margin).

Formula references: [Damodaran's financial measures](https://pages.stern.nyu.edu/~adamodar/New_Home_Page/definitions.html),
[CFA Institute's FCFF/FCFE valuation overview](https://www.cfainstitute.org/insights/professional-learning/refresher-readings/2026/free-cash-flow-valuation),
and [NSE's beta explanation](https://www.nse.in/static/products-services/indices-beta).

## Scores, suitability and recommendations

Each score band maps its low/high endpoints linearly to 0/100, with optional inversion. Missing
metrics contribute no score and reduce coverage. Applicable component weights are renormalized
for the composite; reported coverage retains missing components. Fundamentals, trends and
valuation score higher for favorable values; business/market risk score higher for more risk.
The composite risk component is 100 minus mean available risk scores.

Objective metrics and risk scores do not depend on investor risk tolerance. Profiles affect
suitability limits, optional component weight overrides, evidence requirements and decision rules.

| Profile | BUY / SELL score | Coverage | Max business / market risk | DCF safety | FCF yield floor | Minimum fundamentals |
| --- | --- | --- | --- | --- | --- | --- |
| 1 Conservative | 80 / 35 | 0.80 | 30 / 35 | 30% (required) | 6% | 65 |
| 2 Moderate | 70 / 30 | 0.65 | 50 / 60 | 20% | 4% | 55 |
| 3 Aggressive | 65 / 25 | 0.55 | 65 / 80 | 10% | 2.5% | 45 |

BUY needs adequate coverage, current data, usable annual filing evidence, acceptable financial
quality, known risk suitability, and valuation support. Conservative defaults require an available
DCF safety margin, so enabling DCF and supplying its inputs is necessary for its BUY. Other profiles
can use their FCF yield floor when DCF is unavailable; that is a relative-valuation heuristic, not
proof of intrinsic value. A shorter-than-12-month horizon raises BUY score and valuation requirements.
Severe negative/zero equity, weak interest coverage or critical liquidity cannot be overridden by
a high score. Missing recent interim evidence blocks decisions when the annual period is old.

SELL and HOLD require a fresh, positive supplied holding. With unknown/zero holdings, a failed
purchase assessment is NO TRADE. Inadequate or inconsistent data yields NO TRADE even for a
known holding, because no supported buy/hold/sell decision can be made. The agent must explain
"There is not enough information" where appropriate and identify missing filings or observations.

## Configuration

Override the `investment` section in root `config.yaml`; full defaults are in `config.py`.
No document reindex is required. Restart FastAPI to apply changes consistently.

| Parameter | Default / purpose |
| --- | --- |
| `investment.weights.fundamentals` | 0.35 |
| `investment.weights.trends` | 0.25 |
| `investment.weights.valuation` | 0.25 |
| `investment.weights.risk` | 0.15; weights must sum to 1 |
| `investment.profiles` | Exactly keys 1, 2, 3; thresholds in the table above |
| `investment.fundamentals_bands` | Metric-name → `{low, high, higher_is_better}`; financial quality/growth bands |
| `investment.trend_bands` | Same shape; slopes, acceleration and instability |
| `investment.business_risk_bands` | Same shape; leverage, liquidity, interest coverage and instability |
| `investment.market_risk_bands` | Same shape; volatility, beta, drawdown, downside volatility, extreme frequency |
| `investment.valuation_bands` | Same shape; multiples and FCF yield |
| `investment.dcf.enabled` | false |
| `investment.dcf.scenarios` | Exactly bear/base/bull; each has `growth`, `discount_rate`, `terminal_growth` |
| `investment.min_trend_observations` | 3; minimum history for slopes/rolling variability |
| `investment.trend_window` | 8; most recent observations per comparable cohort |
| `investment.trend_method` | `regression` or `endpoint` |
| `investment.min_price_returns` | 60; minimum usable stock returns and aligned beta pairs |
| `investment.trading_days_per_year` | 252; daily volatility annualizer |
| `investment.max_return_gap_days` | 7; maximum calendar gap between daily observations |
| `investment.min_daily_price_coverage` | 0.8; minimum observed bars / weekdays in the supplied window; prevents annualizing weekly data as daily |
| `investment.extreme_daily_move` | 0.10; absolute return threshold |
| `investment.max_financial_age_days` | 450; latest financial and annual/TTM denominator freshness |
| `investment.max_quote_age_days` | 7; quote, market cap, history end and holding freshness |
| `investment.max_share_age_days` | 120; share count freshness |
| `investment.interim_required_after_days` | 150; require 10-Q evidence beyond this annual period age |
| `investment.severe_interest_coverage` | 1; lower values trigger severe-risk block |
| `investment.severe_current_ratio` | 0.5; lower values trigger severe-risk block |
| `investment.consistency_tolerance` | 0.02; relative reconciliation tolerance |
| `investment.short_horizon_months` | 12; horizons below this need extra caution |
| `investment.short_horizon_score_penalty` | 5; added to BUY threshold for short horizons |
| `investment.short_horizon_safety_extra` | 0.05; extra DCF safety margin; one tenth added to FCF yield floor |

Each profile accepts `name`, `buy_score`, `sell_score`, `min_coverage`, `max_business_risk`,
`max_market_risk`, `margin_of_safety`, `min_fcf_yield`, `min_fundamentals_score`,
`require_dcf_margin`, and optional `weights` (same four-component schema). When overriding profile
or band maps, provide the complete map; these settings are validated, not implicitly deep-merged.

Default DCF rates `(growth, discount, terminal)` are bear `(0%, 12%, 2%)`, base `(5%, 10%, 2.5%)`,
bull `(10%, 9%, 3%)`. Discount must exceed terminal growth. This is an enterprise FCFF model,
so the discount rate is an assumed WACC, not a cost of equity applied to CFO-minus-capex.

Tests use mocked AAPL, GOOG/GOOGL and NVDA observations, never real financial claims. Formula,
missing-data, risk-profile, period, look-ahead, valuation and agent-orchestration tests run without
calling OpenAI, Alpaca or executing trades.
