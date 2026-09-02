# Act only on closed bars

**Status:** proposed — **live bug**, not a new feature
**Touches:** `bot/strategies.py`, `core/data.py`

## Why

`EnhancedSMAStrategy._signals_for_bars` reads:

```python
prev, curr = df.iloc[-2], df.iloc[-1]
```

`MarketDataFetcher` requests `limit=N` with no end timestamp, so Alpaca
returns the most recent bars **including the one currently forming**. During
regular hours `iloc[-1]` is therefore a partial bar.

The consequence: a golden cross can appear at 10:15 on a half-formed 10:00
bar, fire a BUY, and then the bar can close back below the slow SMA by 10:59.
The trade is already placed. The bot has acted on a signal that, on the
completed bar series, never existed — and nothing in the logs or the
dashboard will show why, because both re-read the closed bar afterwards.

The 60-second poll interval makes this worse: the bot gets ~60 chances per
hour to catch a transient crossing.

This affects `EnhancedSMAStrategy` and `SMAcrossoverStrategy` equally, and
`RiskManager`'s ATR sizing inherits it — `signal.atr` and
`signal.current_price` also come from the partial bar.

## What

- Evaluate signals on the last **closed** bar only.
- Two ways to do it; pick one and apply it consistently:
  1. Drop the trailing bar when its period has not elapsed (needs the bar
     timestamp and the timeframe — `core.sessions` already has the clock).
  2. Request bars with an explicit `end` at the last bar boundary.
- Option 2 is cleaner but costs a timestamp calculation per fetch; option 1
  keeps the fetch simple. Prefer 2 if it does not complicate `MarketDataFetcher`.
- Align the poll interval to bar close rather than 60 seconds — see
  `core.sessions.scan_times()`.

## Done when

- [ ] A crossover that appears and reverses within one bar produces no signal
- [ ] Test drives a frame whose final bar is explicitly in-progress and
      asserts the signal is computed from the prior two closed bars
- [ ] `signal.atr` and `signal.current_price` come from the closed bar
- [ ] Both strategies covered
- [ ] Crypto covered — 24/7 means there is always a forming bar

## Explicitly out of scope

Changing the strategy logic itself. This makes the existing logic act on the
data it always intended to.

## Note

This changes live trading behavior — it will make the bot trade **less**
often and later within the hour. That is the point, but it should be stated
in the PR rather than discovered from a change in fill counts.
