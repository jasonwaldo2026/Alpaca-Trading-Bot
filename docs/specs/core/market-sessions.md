# Market sessions, cadence, and horizons

**Status:** done — implemented in `core/sessions.py`
**Touches:** `core/sessions.py`, `core/indicators.py`, `core/client.py`, `bot/`, `scanner/`

## Why

Three separate problems all reduced to "nothing in this repo knew what time
it was":

1. The bot polled every 60 seconds against hourly bars — roughly 200x more
   often than a signal can change.
2. `volume > vol_sma` compared extended-hours volume against a rolling
   average dominated by regular-hours volume, so it measured *"is it
   09:30 yet"* rather than *"is volume unusual"*.
3. Outcome horizons were expressed in bars, and a bar is not a duration.

## Session boundaries (US Eastern)

| Session | Hours | Hourly bars |
|---|---|---|
| Pre-market | 04:00 – 09:30 | 6 |
| Regular | 09:30 – 16:00 | 7 |
| After-hours | 16:00 – 20:00 | 4 |
| **Regular only** | | **7 / day** |
| **Regular + after** | 09:30 – 20:00 | **11 / day** |
| **Full extended** | 04:00 – 20:00 | **16 / day** |
| **Crypto** | 24/7 | **24 / day** |

Enabling pre-market adds 5 bars, not 6: Alpaca aligns bars to the clock hour,
so 09:00–09:30 and 09:30–10:00 merge into a single 09:00 bar.
`_clock_hours_spanned()` returns an hour *set* rather than a count for
exactly this reason.

## Bar size

Bar size is a parameter, not a constant: `BotConfig.bar_minutes`,
`Scanner(bar_minutes=...)`, `--bar-minutes` on the CLI. It must divide evenly
into 1440. Every function in `core/sessions.py` takes it, so cadence and
horizons stay correct at any resolution.

| Bar size | Regular hours | Extended | Crypto |
|---|---|---|---|
| 1 hour | 7 | 16 | 24 |
| 30 min | 13 | 32 | 48 |
| 15 min | 26 | 64 | 96 |
| 5 min | 78 | 192 | 288 |

Hourly is the only size with a stub bar: 09:30 falls mid-hour, so the 09:00
bucket covers just 09:30–10:00. Sizes that divide the session evenly (5, 15,
30) have no stub — 390 minutes of regular session is exactly 78 five-minute
bars.

**Changing bar size changes what the indicators mean.** `sma_slow=30` spans
30 hours of hourly bars but only 150 minutes of 5-minute bars.

`IndicatorParams.bar_minutes` records the resolution the periods were written
for, and `.rescaled_to(target)` converts them preserving wall-clock lookback.
`BotConfig.indicator_period_basis` selects between the two readings:

| Setting | Meaning | `sma_slow=30` at 5-min |
|---|---|---|
| unset (default) | periods are bar counts at `bar_minutes` | 30 bars = 150 min |
| `60` | periods authored hourly, rescaled | 360 bars = 30 hours |

Neither is more correct. "MACD 12/26/9 on the 5-minute chart" conventionally
means 12/26/9 *five-minute* bars — a genuinely faster indicator, which is the
unset default. The basis exists for when you tuned a strategy hourly and want
the same strategy at finer resolution.

**`bar_limit` must grow with the periods.** At 5-minute bars with an hourly
basis, MACD alone needs 422 bars. Fetching fewer does not raise anywhere:
every indicator is NaN, every symbol is skipped, and the bot runs happily
without ever trading. `BotConfig.validate()` catches it at construction and
names the bar_limit that would work.

## Cadence — how many cycles per day

**Scan once per completed bar, ~2 minutes after it closes.** The bar interval
is the ceiling: a signal computed on hourly bars cannot change more than
once an hour, so anything faster is wasted API budget.

At hourly bars:

- Equities, regular hours: **7 scans/day** — 10:02, 11:02, … 16:02 ET
- Equities, regular + after-hours: **11 scans/day** — through 20:02 ET
- Crypto: **24 scans/day**, every hour

At 5-minute bars: **78 scans/day** regular hours (09:36 → 16:01 ET), or
**192** across the full extended session (04:06 → 20:01 ET) — the shipped
default.

The first pre-market scan is 04:06, not 04:00: the 04:00–04:05 bar does not
exist until 04:05, and acting on it before then is the phantom-signal bug.

`core.sessions.scan_times()` generates these, with a delay past each bar
close that scales with bar size — two minutes for hourly, one for 5-minute
(two would be 40% of the bar).

The delay is a convenience, not the safety mechanism. Bars that have not
closed are dropped in `core.data.drop_forming_bars` regardless of when the
scan runs. See `docs/specs/bot/closed-bar-signals.md`.

## Horizons — the outcomes-table fix

"+20 bars" is a row count, not a duration:

| Config | +20 bars means |
|---|---|
| Crypto, hourly | ~20 hours |
| Equity, hourly, regular hours only | ~3 trading days |
| Equity, hourly, full extended hours | ~1.3 days |
| Equity, 5-minute, regular hours | ~100 minutes |

An outcomes table with a shared "+20 bars" column compares a 20-hour result
against a 3-day one — and across a weekend, a 5-calendar-day one.

**Rule: horizons are wall-clock, resolved to bars per symbol.**
`core.sessions.horizon_to_bars(horizon, symbol, config, bar_minutes=...)` does
the conversion.
Valid horizons are `1h`, `2h`, `4h`, `1d`, `5d`, `20d`, where a "day" means
one *session* — 7 bars for a regular-hours equity, 24 for crypto. Unknown
horizons raise rather than guessing.

Outcome table columns must be labelled with both, via `describe_horizon()`:
`1d (7 bars)`. Never label a column with a bare bar count.

## Volume baselines

When extended hours are enabled, pass a session series to `add_indicators()`:

```python
from core.sessions import session_series
sessions = session_series(df.index, symbol, calendar)
df = add_indicators(df, params, sessions)
```

This computes `vol_sma` *within* each session. Without it, an after-hours bar
essentially never clears an average containing regular-hours bars — a real
after-hours volume spike is invisible. `tests/test_sessions.py::
test_session_baseline_detects_an_unusual_after_hours_bar` pins this.

With regular hours only, pass nothing — every bar is in one session and
grouping changes nothing.

## Order placement outside regular hours

Alpaca will not accept a market order outside 09:30–16:00. It is queued to
the next open and fills at an unknown price, which silently invalidates ATR
sizing computed from the signal-time price. Extended hours requires **all
three** of:

- a **limit** order (not market)
- `TimeInForce.DAY` (not GTC)
- `extended_hours=True`

Fractional and notional orders are regular-hours only, so extended-hours
orders must be **whole shares**. `OrderManager` routes on session
automatically; `AlpacaClient.place_extended_hours_order()` places a
*marketable* limit — slightly through the reference price in the direction
of the trade — because extended-hours liquidity is thin enough that a
mid-price limit often will not fill at all.

Consequences the bot handles explicitly rather than silently:
- A buy sized below one share is skipped and logged.
- A fractional holding cannot be sold after hours; it is skipped and logged.
- A signal with no reference price is skipped — there is no safe default limit.

## Data feed and sparse pre-market bars

**Alpaca builds bars from trades. A window with no trades produces no bar at
all** — not a zero-volume bar, a missing one. So a pre-market series of N
bars can span far more wall-clock time than `N * bar_minutes`, and every
rolling indicator over it reaches back further than its period suggests.

Two causes compound:

1. **The feed.** `StockBarsRequest` takes a `feed`; leaving it unset uses the
   account default, which on free and basic plans is **IEX** — a single venue
   carrying a small share of consolidated volume. Pre-market IEX activity is
   thin enough that many 5-minute windows are empty. `DataFeed.SIP` is the
   consolidated tape and needs a paid Alpaca data plan. Set it via
   `BotConfig.data_feed` or `--feed sip`.
2. **Genuine thinness.** Even on SIP, pre-market prints are intermittent for
   anything but the most active names.

`core.data.bar_coverage()` measures it: bars returned, wall-clock span,
density against expectation, and largest gap. `ScanResult.sparse` records
symbols below 80% density, and the CLI prints a note. This is diagnostic, not
a fix — the tape is what it is; the point is that thin data is visible rather
than mistaken for a stale bot.

## Indicators at 5-minute resolution

Default periods are bar counts at the data resolution:

| Indicator | Periods | Span at 5-min |
|---|---|---|
| EMA | 9, 12, 200 | 45 min, 60 min, **1000 min** |
| MACD | 12/26/9 | 60 / 130 / 45 min |
| SMA | 10, 30 | 50 / 150 min |

EMA(200) is the binding constraint on `min_bars()` — 202 bars, so
`bar_limit` must be at least 243. `BotConfig.validate()` enforces it.

At 1000 minutes of *trading* time, EMA(200) spans slightly more than one
960-minute extended session. Across sparse pre-market bars it reaches back
further still, since 200 bars there is more than 1000 minutes of clock.

## Holidays

Weekends are handled in `SessionCalendar`. Holidays and early closes are
**not hardcoded** — they move year to year, and Alpaca's `/v2/calendar`
endpoint is authoritative. Fetch them at startup and pass them in:

```python
SessionCalendar(holidays=frozenset(...), early_closes=frozenset(...))
```

An empty calendar treats every weekday as a full session, which is wrong on
roughly nine days a year. Wiring the calendar fetch is not yet done — see
"Open" below.

## Open

- [ ] Fetch the Alpaca calendar at startup and populate `SessionCalendar`.
      Until then, holiday scans return stale bars rather than being skipped.
- [ ] Scheduled runs (`docs/specs/scanner/scheduled-runs.md`) should drive
      off `scan_times()` rather than a fixed interval. The bot still polls
      every 60 seconds, which at hourly bars is ~60 wake-ups per usable scan.
- [x] Indicator periods and `bar_limit` — done. `indicator_period_basis`
      selects the interpretation; `BotConfig.validate()` rejects a bar_limit
      that cannot produce signals.
- [ ] The poll loop still wakes every 60 seconds regardless of bar size. Now
      harmless (forming bars are dropped, so it re-reads the same closed bar)
      but wasteful: ~60 wake-ups per usable scan hourly, 12 at 5-minute. It
      should skip the fetch when no new bar has closed since the last cycle.
