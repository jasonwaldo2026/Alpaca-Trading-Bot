# VWAP-trend strategy

**Status:** entry rule and backtest done; live trading and alerts not built
**Touches:** `rules/vwap-hold.json`, `core/backtest.py`, `core/persistence.py`,
`studio/app.py`

## The idea

Price holding above VWAP is a statement about where the day's volume is
transacting: buyers are willing to pay more than the session's average.
Holding there for a while distinguishes a trend from a touch.

- **Alert / entry trigger** — close above VWAP for **three consecutive
  5-minute bars**.
- **Entry** — as soon as possible after that, on the next available bar.
- **Exit** — close below the 9 EMA, managed on **1-minute** bars so an
  adverse move is caught within a minute rather than up to five.
- **Floor** — an ATR stop under the entry, so a gap cannot turn one trade
  into an unbounded loss.

Expressed as a rule (`rules/vwap-hold.json`):

```json
{"field": "close", "op": ">", "field2": "vwap", "for_bars": 3}
```

`for_bars` is the persistence operator added to `core/rules.py`; a NaN or a
single failing bar resets the run, so a warming-up indicator never counts
as satisfying the condition.

## The dual timeframe, and what it costs

Entries are found on 5-minute bars; exits are managed on 1-minute bars. The
finer frame buys reaction time — a backtest asserts the exit lands on the
very next minute bar after the breakdown rather than up to five minutes
later.

**It also changes what the 9 EMA means, and this is the most important
decision in the design.** The EMA is read on the management timeframe:

| Managed on | EMA(9) spans | Effect |
|---|---|---|
| 1-minute bars | 9 minutes | Tight. Exits fast, cuts trends short. |
| 5-minute bars | 45 minutes | Loose. Rides further, gives back more. |

These are different strategies, not the same strategy at different speeds.
On synthetic data the 1-minute exit produced roughly 5x the trades at a
much lower win rate and a fraction of the holding time — the structural
effect is real even though the synthetic numbers say nothing about edge.

**Open question:** whether the intent is "react within a minute using the
1-minute 9 EMA" (tight) or "react within a minute, but against the
5-minute 9 EMA" (fast reaction, original exit level). The second is not yet
implementable — the backtest reads the exit EMA from the management frame.
Supporting it means carrying the coarse EMA forward onto the fine frame.
Decide this before trading it; the backtest can compare both today by
switching the management timeframe.

## Entry filters — not yet decided

The entry is currently the VWAP persistence condition alone. Candidate
filters, none of them applied:

- `volume > vol_sma` — participation. Session-aware already, so it works
  pre-market.
- `ema_9 > ema_12` — short-term momentum agrees.
- `close > ema_200` — only take longs in an uptrend.
- `rsi < 70` — avoid entering an already-extended move.

Each is one condition in Studio, so they can be added and backtested
individually rather than guessed at. Adding filters will reduce trade count;
whether it improves the average is exactly what the backtest is for.

## Backtest assumptions

Stated because they flatter the strategy if forgotten:

- Entries fill at the **next bar's open** after the signal bar closes.
- The ATR stop assumes a resting stop order **filled at its trigger price**.
  A real gap through the level fills worse, so stop-exit results are
  optimistic.
- No commission, no slippage, one position at a time, fully invested.
- A position still open when data ends is closed at the last price and
  marked `end_of_data`, so it is visible rather than dropped.

Treat `total_return_pct` as an upper bound, and a few dozen trades as a
sample, not an edge.

## Done

- [x] `for_bars` persistence operator on rule conditions
- [x] Lookahead-free backtest with dual-timeframe exits
- [x] ATR stop floor beneath the EMA exit
- [x] Studio panel: configure the exit, run over history, see trades and stats

## Not done

- [ ] Decide the exit-EMA timeframe question above
- [ ] Choose entry filters on backtest evidence rather than intuition
- [ ] A live strategy class that trades this (`bot/strategies.py`)
- [ ] Phone alerts — see `docs/specs/scanner/match-alerts.md`
- [ ] Backtest across a universe rather than one symbol at a time
