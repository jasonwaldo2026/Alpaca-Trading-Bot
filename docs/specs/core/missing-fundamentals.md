# Float and prior-spike screening

**Status:** blocked — the data is not available from Alpaca
**Touches:** would need a new data source

## Why this is a spec and not a feature

Two of the four rules of engagement cannot be evaluated from Alpaca's market
data API:

### Low float

Float — shares actually available to trade — is fundamental data. Alpaca's
market data API serves bars, quotes and trades; it does not serve shares
outstanding or float. There is no column to compute it from.

Float matters here precisely because it is not derivable from price and
volume: it is why a given amount of buying moves a small-cap far more than
a mega-cap, which is the whole premise of the setup.

Options, none free and all needing a decision:

- A fundamentals provider (Financial Modeling Prep, Polygon, Finnhub) —
  another API key, another rate limit, and float updates slowly so it can be
  cached daily rather than fetched per scan.
- A hand-maintained watchlist of low-float names, refreshed occasionally.
  Crude, but for a universe of a few dozen candidates it is honest and free.

The second is probably right to start: the scan universe is already a curated
list, so curate it for float.

### Spiked in the previous 12 months

This is computable, but not from the 5-minute frame the scanner runs on. It
needs ~250 daily bars per symbol and a definition of "spiked" — for example,
any 5-day window with a move above some threshold, or a 12-month high more
than N times the 12-month low.

It is a **universe filter**, not a per-bar condition: it changes rarely, so
it should be computed once a day and used to narrow the symbol list, rather
than evaluated on every 5-minute scan.

## What is implemented instead

`rules/momentum-runner.json` covers the two that are expressible:

| Rule of engagement | Condition |
|---|---|
| Up ~10% in 10 minutes | `roc >= 10` with `roc_period: 2` at 5-minute bars |
| High relative volume | `rvol >= 3` |
| (added) holding above VWAP | `close > vwap` |
| Low float | **not checked** |
| Spiked in last 12 months | **not checked** |

The alert message says so explicitly, so an alert is never mistaken for a
full screen:

> Check by hand: float, and whether it spiked in the last 12 months.

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

- [ ] A decision on the float source (provider vs curated list)
- [ ] Prior-spike computed daily from daily bars, applied as a universe filter
- [ ] Time-of-day relative volume, or a documented decision to keep the
      per-bar version
