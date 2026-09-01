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
- Report: number of matches, and forward return at +1/+5/+20 bars after each
  match, summarized as mean, median, and hit rate (% positive).
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
