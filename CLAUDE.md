# Alpaca Trading Platform

Four apps over one shared core. Paper trading only.

| Directory | What it is | Run it with |
|---|---|---|
| `core/` | Shared library. Credentials, bar fetching, indicator math, scan-rule model, market sessions, backtesting. | — (imported) |
| `bot/` | Live trading bot — strategies, risk, orders, poll loop. | `python trading_bot.py` |
| `dashboard/` → `dashboard.py` | Streamlit read-only view of account, positions, charts. | `streamlit run dashboard.py` |
| `scanner/` | Runs saved rules across a symbol universe. | `python -m scanner.cli` |
| `studio/` | Streamlit rule builder. Authors the rules the scanner runs. | `streamlit run studio/app.py` |

## Invariants — do not break these

1. **Indicator math lives only in `core/indicators.py`.** Never re-implement
   SMA/RSI/ATR inside an app. If a chart and a signal disagree about RSI, the
   whole system is untrustworthy. Need a new indicator? Add it to `core/`
   with a test, then use it.
2. **`core/` never imports from `bot/`, `dashboard`, `scanner/`, or `studio/`.**
   The dependency arrow points one way. A test asserts this.
3. **`paper=True` stays true.** Nothing in this repo should flip Alpaca to
   live trading. If a task seems to require it, stop and ask.
   `BotConfig.execution_mode` defaults to `"monitor"`, which computes and
   reports signals but never sends an order. Do not change that default:
   the bot is currently used to generate alerts for manual trading, not to
   trade. See `docs/specs/bot/execution-modes.md`.
4. **Studio and Scanner share `core/rules.py`.** Studio writes rule JSON;
   Scanner reads it. Neither may define its own rule dialect or evaluation
   logic — both call `Rule.matches()`. A strategy belongs in a rule wherever
   it can be expressed as one, so it stays editable and backtestable in
   Studio rather than hardcoded in an app.
12. **Backtests never look ahead.** At bar *i* a rule sees `bars[:i+1]` and
    nothing later, and a signal at a bar's close fills at the *next* bar's
    open. `core/backtest.py` enforces both; a test asserts the window handed
    to the rule ends at the current bar.
5. **Asset-class routing goes through `core/universe.py:is_crypto()`.**
   Never test for `"/" in symbol` inline.
6. **Horizons are wall-clock, never bar counts.** A bar is not a duration:
   "+20 bars" is ~20 hours for crypto and ~3 trading days for a
   regular-hours equity. Resolve with
   `core/sessions.py:horizon_to_bars()`, and label outcome columns with
   `describe_horizon()` so both units are visible.
7. **Session boundaries come from `core/sessions.py`.** Never hardcode
   09:30/16:00, never assume a fixed UTC offset (Eastern shifts with DST),
   and never treat a naive timestamp as local time — Alpaca returns UTC.
8. **Never act on a forming bar.** Alpaca includes the in-progress period in
   a `limit=N` request. `MarketDataFetcher` drops it — do not bypass the
   fetcher, and do not set `drop_forming=False` outside a backtest that
   supplies an explicit end timestamp.
9. **Bar size is a parameter, never a constant.** `bar_minutes` flows from
   config through `core/sessions.py` and `core/data.py`. Never hardcode
   `TimeFrame.Hour`.
10. **VWAP must be anchored.** It is a running average within a trading day,
    not a rolling window. Always pass
    `core/sessions.py:session_day_series()` as `add_indicators(anchor=...)`.
    An unanchored VWAP drifts further from price every day and is not the
    line any charting platform draws.
11. **Indicator periods are bar counts, and `IndicatorParams.bar_minutes`
    records which resolution they were written for.** Changing bar size
    without deciding what the periods mean is the silent way to change
    strategy. `BotConfig.indicator_period_basis` makes the choice explicit:
    unset means "12/26/9 bars at this resolution" (what a trader means);
    set means "rescale to preserve wall-clock lookback".

## Conventions

- Indicator column names are constants in `core/indicators.py`
  (`COL_RSI`, `COL_ATR`, `COL_VWAP`, `COL_MACD`, …). Reference those, not
  string literals, in new code. Studio's rule-field list and the scanner's
  reported values both derive from `INDICATOR_COLUMNS`, so a new indicator
  added to `core/` becomes selectable everywhere with no app edit.
- `IndicatorParams.min_bars()` is driven by MACD (`macd_slow + macd_signal`),
  not by the longest single period. `BotConfig.validate()` enforces that
  `bar_limit` covers it — call it before running anything, because fetching
  too few bars yields all-NaN indicators and a bot that silently never trades.
- Indicator periods travel as an `IndicatorParams`. Get one from
  `BotConfig.indicator_params()` or a `Rule.params` — never construct one
  ad hoc in app code, or the apps will silently disagree.
- Strategies subclass `BaseStrategy` in `bot/strategies.py` and return
  `List[Signal]`. `generate_signals()` takes optional `positions` and
  `manage_bars` for strategies that manage open positions on a finer
  timeframe.
- **A live strategy must share the objects the backtest measured.**
  `VwapTrendStrategy` takes a `core.rules.Rule` and a
  `core.backtest.ExitPolicy` rather than reimplementing the logic — a
  reimplementation could drift from the numbers that justified trading it,
  and nothing would catch the drift.
- Management bars are fetched for **held symbols only**. One or two symbols
  at 1-minute resolution is cheap; the whole watchlist would not be.
- Credentials are resolved by `core/client.py:Credentials` — `.from_env()`
  for CLI apps, `.from_streamlit(st.secrets)` for Streamlit ones. Do not read
  `os.getenv("ALPACA_...")` directly in an app.
- **Extended hours changes the order path.** Outside 09:30–16:00 ET, Alpaca
  requires a limit order with `TimeInForce.DAY` and `extended_hours=True`,
  and rejects fractional/notional quantities. A market order sent then is
  silently queued to the next open. `OrderManager` routes on session; do not
  bypass it. See `docs/specs/core/market-sessions.md`.
- **When extended hours are enabled, volume baselines must be per session.**
  Pass `core.sessions.session_series(...)` to `add_indicators()`. A flat
  rolling average across sessions turns `volume > vol_sma` into "is it
  regular hours".
- Cadence is one scan per *completed* bar. At hourly bars: 7/day regular
  hours, 11 with after-hours, 24 for crypto. At 5-minute bars: 78/day regular
  hours, 192 across the full extended session (04:06 → 20:01 ET), which is
  the shipped default. `core/sessions.py:scan_times()` generates them.
- **Alpaca builds bars from trades, so a window with no trades yields no
  bar.** Pre-market series are sparse, which stretches every rolling window
  beyond its nominal span. `core/data.py:bar_coverage()` measures it. The
  usual amplifier is the data feed: unset means IEX (one venue, thin
  pre-market); `DataFeed.SIP` is the consolidated tape and needs a paid plan.
- EMA periods are a tuple (`ema_periods=(9, 12, 200)`), one column each
  (`ema_9`, `ema_12`, `ema_200`). Column lists come from
  `indicators.indicator_columns(params)`, not the fixed constant, so a new
  period is selectable in Studio with no app edit.
- Persistence ("true for N bars in a row") is `Condition.for_bars`, backed by
  `core/persistence.py`. NaN breaks a run, so a warming-up indicator never
  counts as satisfying a condition.
- An exit EMA read on a finer management timeframe is a **different** exit,
  not a faster one: EMA(9) is 9 minutes on 1-minute bars and 45 on 5-minute.
  Say which is meant.
- Changing `bar_minutes` changes what the indicator periods mean —
  `sma_slow=30` is 30 hours of hourly bars but 150 minutes of 5-minute bars.
  Say so explicitly when changing it; do not treat it as a tuning knob.

## Testing

```bash
pip install -r requirements-dev.txt
pytest                    # all tests, no network required
pytest tests/test_indicators.py -q
```

Tests never hit the network. The scanner is tested against a `FakeFetcher`
(see `tests/test_scanner.py`); follow that pattern for anything that would
otherwise call Alpaca.

`tests/test_indicators.py::test_matches_legacy_implementations` pins the
indicator math against the original pre-refactor implementations. If you
change indicator behavior deliberately, that test is the one to update — and
updating it means every app's numbers change, so say so explicitly.

## Working on this repo

- One app per session and per branch. `core/` changes land first, in their
  own PR, so the apps can build on them.
- Feature specs live in `docs/specs/<app>/*.md`. Read the spec before
  implementing; if the spec and the code disagree, say so rather than
  guessing.
