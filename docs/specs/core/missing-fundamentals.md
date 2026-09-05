# Float and prior-spike screening

**Status:** float done via FMP; prior-spike screening still open
**Touches:** would need a new data source

## Why this is a spec and not a feature

Two of the four rules of engagement cannot be evaluated from Alpaca's market
data API:

### Low float — solved

Float is fundamental data. Alpaca's market data API serves bars, quotes and
trades, not shares outstanding, so it has to come from elsewhere.

**Financial Modeling Prep** provides it, on the free tier, and — critically —
has a **bulk endpoint**. `stable/shares-float-all` (paged, 1,000 per page) returns the whole market
in a few paged requests, which is the only shape that works when the scan
universe is 11,000 symbols. One lookup per symbol would be 11,000 requests a
scan.

```bash
export FMP_API_KEY=...        # free tier is enough
python -m scanner.cli --watch
```

`core/fundamentals.py` has three providers, chosen in this order:

| Provider | Source | When |
|---|---|---|
| `StaticFloatProvider` | a JSON file you maintain | present — explicit and cannot fail |
| `FMPFloatProvider` | FMP, bulk + cached daily | `FMP_API_KEY` is set |
| `YahooFloatProvider` | yfinance, one symbol at a time | `--yahoo-floats` |

**Everything fails closed.** Unknown float becomes NaN in the scan frame,
and a NaN comparison is False, so a low-float condition *skips* a symbol
whose float is unknown rather than matching it. Silently matching everything
because a lookup failed is the dangerous direction, and a test pins it.
`ScanResult.missing_float` records the symbols that had no data.

The FMP key travels in the query string, so `_scrub()` removes it from any
logged error message.

### Spiked in the previous 12 months

This is computable, but not from the 5-minute frame the scanner runs on. It
needs ~250 daily bars per symbol and a definition of "spiked" — for example,
any 5-day window with a move above some threshold, or a 12-month high more
than N times the 12-month low.

It is a **universe filter**, not a per-bar condition: it changes rarely, so
it should be computed once a day and used to narrow the symbol list, rather
than evaluated on every 5-minute scan.

## What is implemented instead

`rules/strong-above-vwap.json` covers three of the four:

| Rule of engagement | Condition |
|---|---|
| Up ~10% in 10 minutes | `roc >= 10` with `roc_period: 2` at 5-minute bars |
| High relative volume | `rvol >= 3` |
| (added) holding above VWAP | `close > vwap` |
| Low float | `float_millions <= 50`, from FMP |
| Spiked in last 12 months | **not checked** |

Only the 12-month prior spike is still unchecked. It is a universe filter
rather than a per-bar condition — it changes rarely, so it should be computed
once a day from daily bars and used to narrow the symbol list.

## A caveat on `rvol`

`core.indicators.relative_volume` is volume *per bar* against a rolling
baseline of recent bars, session-aware. That is not what "RVOL" means on a
scanner screen, which is the day's cumulative volume against the same time
of day across previous days.

The per-bar version answers "is this bar unusually busy", which is the right
question for a 10-minute move. The cumulative version answers "is today
unusually busy overall". They disagree most at the open, when cumulative
RVOL is noisy and per-bar RVOL is not.

Implementing true time-of-day RVOL needs several days of intraday history per
symbol, bucketed by minute-of-session. Worth doing; not done.

## Done when

- [x] Float via FMP, with a static file as the explicit alternative
- [ ] Prior-spike computed daily from daily bars, applied as a universe filter
- [ ] Time-of-day relative volume, or a documented decision to keep the
      per-bar version
