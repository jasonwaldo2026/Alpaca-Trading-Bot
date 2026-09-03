# Alert on new scanner matches (Pushover)

**Status:** proposed — blocked on `scheduled-runs.md`
**Touches:** new `scanner/alerts.py`, `scanner/cli.py`

## Why

A scheduled scan that prints to a terminal nobody is watching is not a
signal. The VWAP-trend setup (`docs/specs/studio/vwap-trend-strategy.md`)
is time-sensitive: three 5-minute bars above VWAP is a condition you want to
know about within minutes, not at the end of the day.

## What

**Channel: Pushover.** Purpose-built for phone push, delivers as a real
notification with priority levels, and needs only two credentials.

- Read `PUSHOVER_TOKEN` (application) and `PUSHOVER_USER` (user key) from the
  environment. Either absent → alerting is off, logged once at startup, not
  an error and not a crash.
- Send when a `(rule, symbol)` pair matches that did **not** match on the
  previous run. Never re-alert a standing match — a setup that stays true
  for an hour is one alert, not twelve.
- Batch a run's new matches into one message rather than N messages.
- Message includes rule name, symbol, price, and the values that triggered
  it (`Match.values` already carries VWAP, EMAs, MACD, RSI, ATR).
- A delivery failure logs an error and does not interrupt the scan loop.
- Respect Pushover's rate limits; a scan that suddenly matches 200 symbols
  must not fire 200 requests. Cap the batch and summarise the remainder.

## Done when

- [ ] New matches alert exactly once
- [ ] Standing matches never re-alert across runs
- [ ] Missing credentials disable alerting without crashing
- [ ] Delivery failures are logged and swallowed
- [ ] A run with more matches than the cap sends one summarised message
- [ ] Tests assert payload shape against a fake transport — no live requests,
      and no credentials in the test environment

## Explicitly out of scope

Slack, email, and SMS. Add them behind the same interface once Pushover
works; the "what changed since last run" logic is the hard part and is
shared.

## Note on credentials

`PUSHOVER_TOKEN` and `PUSHOVER_USER` go in `.env`, which is gitignored. Add
them to `.env.example` as empty placeholders — never with real values.
