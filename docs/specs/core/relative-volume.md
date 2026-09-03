# Relative volume: what the baseline should be

**Status:** proposed
**Touches:** `core/indicators.py`, `core/enrich.py`, `IndicatorParams`

## Where it stands

`rvol` is `volume / vol_sma`, and `vol_sma` is a rolling mean over the
last `volume_sma_period` bars **within the current session**. Every app
gets that through `core/enrich.py`, so the baseline is per-session whether
or not the config enables extended hours — Alpaca returns pre- and
after-market bars for intraday timeframes regardless, and a flat window
across them turns `rvol >= 3` into "is it 09:30 yet".

The window restarts with each session and is allowed to be short
(`min_periods=1`), so there is a baseline from a session's first bar. That
was a bug fix: with a full 20 bars required, the first 95 minutes after the
open had no baseline and the strong-above-VWAP scenario could not fire at
the one time of day it matters most.

## What is still wrong with it

An intra-session baseline cannot see the open. The first bar of regular
hours compares against itself (`rvol = 1`), the second against two bars,
and the opening minutes are the highest-volume part of every day, so early
bars inflate the baseline for the rest of the morning. A stock doing three
times its *normal* 09:35 volume looks ordinary next to its own 09:30 print.

What a trading platform means by RVOL is different: **this bar's volume
against the average volume at this time of day over the last N sessions.**
That baseline knows that 09:35 is always busy and 13:00 is always quiet,
so a 3× reading means the same thing all day.

## What
- `time_of_day_volume_baseline(volume, bar_start_et, days)`: group bars by
  their clock time in Eastern (from `core/sessions.py`, never a fixed
  offset), and for each bar take the mean of the same slot over the
  previous `days` sessions, **excluding the current one** (`shift(1)` inside
  the group — the current bar must not be in its own baseline).
- `IndicatorParams.rvol_days: int = 5`. Zero keeps the intra-session
  baseline for callers that cannot fetch enough history.
- `min_bars()` grows: `rvol_days` sessions of bars at the configured
  resolution, from `core.sessions.bars_per_day()`. At 5-minute extended
  bars that is 192 × 5 = 960 bars plus one day, so `bar_limit` rises to
  about 1200. The scanner CLI's `--bars` default and `BotConfig.bar_limit`
  change with it; `BotConfig.validate()` already enforces the floor.
- Sparse pre-market slots (no bar at 04:35 on three of five days) average
  over the days that have one. A slot with no history is NaN, and NaN
  fails closed as everywhere else.

## Cost
Five sessions of 5-minute bars is ~1,000 bars per symbol per scan instead
of 300. For a watchlist that is nothing. For `all_tradable` it is another
reason a whole-market pass cannot run inside a 5-minute cadence — see
`docs/specs/scanner/universe-size.md`; the FMP screener route described
there is the fix for both.

## Done when
- [ ] A bar's baseline never includes the bar itself.
- [ ] Two days of identical volume profiles give `rvol == 1.0` on day two
      at every slot.
- [ ] A 3× print at 13:00 reads as 3× even though 09:30 that day was 10×.
- [ ] `min_bars()` reflects `rvol_days`, and `BotConfig.validate()` rejects
      a `bar_limit` below it.
- [ ] `rvol_days = 0` reproduces today's intra-session numbers exactly.
