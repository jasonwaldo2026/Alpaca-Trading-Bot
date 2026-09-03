# Backtest a rule before saving it

**Status:** proposed
**Touches:** new `core/backtest.py`, `studio/app.py`

## Why
Studio shows whether a rule matches *right now*. That says nothing about
whether the rule is any good. Right now the only way to find out is to save
it, wait days, and watch — so rules get saved on intuition.

## What
- `backtest(rule, bars) -> BacktestResult` walks a historical frame bar by
  bar, evaluating the rule on each prefix. **No lookahead** — at bar `i` the
  rule may only see `bars[:i+1]`.
- Report: number of matches, and forward return at each horizon after a
  match, summarized as mean, median, and hit rate (% positive).
- **Horizons are wall-clock, never bar counts.** Use
  `core.sessions.horizon_to_bars(horizon, symbol, config)` to resolve
  `1h` / `4h` / `1d` / `5d` into a per-symbol bar count. "+20 bars" is ~20
  hours for crypto and ~3 trading days for a regular-hours equity, so a
  shared bar-count column compares unlike things across asset classes.
  See `docs/specs/core/market-sessions.md`.
- Label every outcome column with `core.sessions.describe_horizon()`, which
  renders both — `1d (7 bars)`. A bare bar count is not a valid column header.
- Studio shows this as a table plus a histogram of forward returns, next to
  the live preview.
- Lives in `core/` because the scanner will eventually want it too.

## Done when
- [ ] `backtest()` is lookahead-free — a test with a rule keyed to a future
      bar must produce zero matches
- [ ] Forward returns handle the tail correctly (last match has no +20 bar)
- [ ] Studio renders results for a rule over ≥1 year of daily bars
- [ ] Known-value test: a hand-computed 10-bar series gives exact expected
      match count and returns

## Explicitly out of scope
Position sizing, transaction costs, and portfolio simulation. This measures
whether a *signal* has edge, not whether a strategy is profitable — say so
in the UI so the number is not over-read.

## Depends on
`docs/specs/bot/closed-bar-signals.md`. A backtest replays closed bars by
construction, so if live signals fire on partial bars the backtest will
measure a strategy the bot does not actually run.
