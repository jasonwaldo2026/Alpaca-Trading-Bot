# Bot backtesting mode

**Status:** proposed — overlaps `docs/specs/studio/rule-backtest.md`
**Touches:** `bot/runner.py`, `core/backtest.py`

## Why
Strategy changes ship untested against history. There is no way to compare
`EnhancedSMAStrategy` against `SMAcrossoverStrategy` other than running both
live and waiting.

## What
- `TradingBot.backtest(start, end)` replays historical bars through the
  configured strategy and risk manager without placing orders.
- No lookahead: at each step the strategy sees only bars up to that point.
- Track simulated PnL, win rate, max drawdown, and trade count.
- Print a summary comparable across strategies.

## Done when
- [ ] Lookahead-free (test: a strategy peeking ahead must fail)
- [ ] Risk manager participates — sizing and exposure caps apply as in live
- [ ] Two strategies over the same window produce comparable summaries
- [ ] No Alpaca order call is reachable from the backtest path

## Note on overlap
Studio's rule backtest measures whether a *signal* has forward edge; this
measures whether a *strategy* — signal plus sizing plus risk — makes money.
Build the shared bar-replay loop in `core/backtest.py` once and let both
call it.
