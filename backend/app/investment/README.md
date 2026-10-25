# Investment assessment

`build_investment_assessment` fetches actual data before running deterministic Python calculations.
The agent researches filings, risks and market/news context through its other relevant tools, then
uses this tool for the numerical assessment. No copied price arrays, financial observations,
chat-state caches or run-history lookups are required. No trade is submitted by this tool.

## Tool input

```json
{"ticker": "MSFT", "risk_tolerance": 2, "horizon_months": 12}
```

Call once per ticker. Risk accepts strict integers **1 Conservative, 2 Moderate, 3 Aggressive**;
default **2**. Investment horizon accepts **6?36 months**, default **12**. This horizon is separate
from the daily-history lookback. Current assessments only: no historical as-of-date input.

## Data fetching

- **yfinance 1.7.0**, no API key: annual and quarterly income statements, balance sheets, cash
  flows, listing/financial currencies, equity classification and current shares outstanding.
- **Existing Alpaca MCP connection**, using the configured Alpaca credentials: fully adjusted
  daily stock/SPY bars and a separate unadjusted latest trade for valuation. Only the read-only
  `get_stock_bars` and `get_stock_latest_trade` methods are called internally. No extra MCP server
  is started per assessment. IEX is the default and is not consolidated exchange coverage.
- Provider calls and normalized Yahoo observations are captured in `runs.csv` tool traces for
  debugging and judging. Traces are an audit trail, never an input cache. `data_collection` in the
  response reports sources, timestamps, observation/bar counts and quote date. Backend logs include
  fetch status/duration and assessment coverage limitations. Follow-ups fetch again automatically.

The Yahoo adapter uses an explicit field mapping in `providers/yahoo.py`. It preserves currency,
EPS units, fiscal period ends, and annual versus quarterly periods; capex cash outflows become
positive expenditure amounts. Missing/NaN/infinite values are excluded, never replaced by zero.
Financial currency must match the USD market data; no implicit FX conversion is performed.
Yahoo does not provide a verified filing date for each observation: `filing_date` stays null and
`retrieved_date` is stored separately. Flow starts are inferred from adjacent fiscal period ends,
or calendar frequency when necessary; this limitation is returned explicitly. Sources are marked
`financial_data`, never falsely labeled 10-K/10-Q. Provider shares are dated at observation time,
not presented as a verified SEC publication date. Annual/interim coverage is checked by actual
observation periods rather than requiring a filing label on Yahoo data.

Incomplete market pages and invalid/duplicate/future bars are excluded. Today's unfinished daily
bar is excluded; adjusted closes support risk calculations, while the separate unadjusted trade
supports valuation. Failures remain explicit and do not produce fabricated data or trigger trades.
If Yahoo cannot identify the company, the tool returns `data_unavailable`. Non-equities (including
AGG and other ETFs) return `unsupported_asset`, not a corporate NO TRADE recommendation. Non-USD
listings return `unsupported_currency`. Missing Alpaca data allows partial financial calculations
with explicit missing market evidence and NO TRADE when coverage is inadequate.

No new document ingestion or reindex is required. Retrieved filings remain useful for qualitative
context and for identifying discrepancies with Yahoo's normalized statements. The agent must
report discrepancies, not silently mix incompatible periods/providers. yfinance is an unofficial
Yahoo integration; availability is not guaranteed. See its [project documentation](https://github.com/ranaroussi/yfinance)
for data-use terms. All scores remain unvalidated heuristics, not calibrated forecasts.

## Structure

| File | Responsibility |
| --- | --- |
| `providers/yahoo.py` | Fetch and normalize financial statements |
| `providers/alpaca.py` | Fetch and validate daily history and latest trade via existing MCP |
| `providers/__init__.py` | Provider timeouts, logging and audit records |
| `../tools/investment.py` | Simple tool input; coordinate providers and calculations |
| `models.py` | Internal typed observations, periods, provenance and output |
| `config.py` | Provider settings, risk profiles, score bands, thresholds and DCF assumptions |
| `data.py` | Units, period matching and derived inputs |
| `fundamentals.py`, `trends.py`, `risk.py`, `valuation.py` | Deterministic metric calculations |
| `service.py` | Coverage, suitability, composite scoring and recommendation rules |

The calculation layer still accepts typed `AssessmentInput` directly for offline tests. It does
not call providers. Only the agent-facing adapter gathers data. Existing approval middleware
continues to protect trading tools; the assessment makes no account mutations.

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

BUY needs adequate coverage, current data, usable annual financial observations, acceptable financial
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
| `investment.history_days` | 400; daily stock/SPY lookback, 90?730 calendar days |
| `investment.market_feed` | iex; Alpaca feed permitted by your subscription |
| `investment.data_timeout_seconds` | 60; timeout per provider operation, maximum 300 seconds |
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
| `investment.interim_required_after_days` | 150; require newer interim observations beyond this annual period age |
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
