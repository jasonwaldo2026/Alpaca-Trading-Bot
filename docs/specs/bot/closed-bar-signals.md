# Act only on closed bars

**Status:** done — fixed in `core/data.py:drop_forming_bars`
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

## What was done

The filter lives at the **fetcher**, not in each strategy, so every consumer
inherits it: bot, scanner, and Studio previews alike. A bar starting at T
covers `[T, T + bar_minutes)` and is dropped until `now >= T + bar_minutes`.

`MarketDataFetcher(drop_forming=True)` is the default. Backtests that supply
an explicit end timestamp have no forming bar and can pass `False`.

Strategies still read `iloc[-1]` — that is now correct, because the frame
they receive ends at the last closed bar.

## Done

- [x] A crossover that appears and reverses within one bar produces no signal
      (`tests/test_strategies.py::test_dropping_the_forming_bar_prevents_the_phantom_trade`)
- [x] Test drives a frame whose final bar is explicitly in-progress
- [x] `signal.atr` and `signal.current_price` come from the closed bar
- [x] Both strategies covered — the fetcher-level fix protects them equally
- [x] Crypto covered — 24/7 means there is always a forming bar
- [x] MultiIndex (multi-symbol) frames handled
- [x] tz-naive frames treated as UTC, not local time

## Explicitly out of scope

Changing the strategy logic itself. This makes the existing logic act on the
data it always intended to.

## Note

This changes live trading behavior — the bot trades **less** often, and
later within each bar. That is the point, but it is a real change in fill
counts, not a no-op refactor.

## Remaining

The poll interval is still a flat 60 seconds. It should be driven by
`core.sessions.scan_times()` so the bot wakes just after each bar close
rather than 60 times per bar. Tracked in
`docs/specs/scanner/scheduled-runs.md`.
