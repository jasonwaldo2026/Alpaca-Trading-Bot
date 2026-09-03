# Alert on new scanner matches (Pushover)

**Status:** done — `core/alerts.py`, `scanner/alerts.py`
**Touches:** new `scanner/alerts.py`, `scanner/cli.py`

## Why

A scheduled scan that prints to a terminal nobody is watching is not a
signal. The VWAP-trend setup (`docs/specs/studio/vwap-trend-strategy.md`)
is time-sensitive: three 5-minute bars above VWAP is a condition you want to
know about within minutes, not at the end of the day.

## How a scenario is defined

A scenario is one JSON file in `rules/`: the detection *and* the message
together, so adding something you want to be told about is adding a file —
no code change. Studio writes these, with a live phone preview.

```json
{
  "name": "vwap hold",
  "conditions": [
    {"field": "close", "op": ">", "field2": "vwap", "for_bars": 3}
  ],
  "alert": {
    "title": "Strong above VWAP",
    "message": "{symbol} stock is strong and trading above VWAP\n\nPrice ${price}  •  VWAP ${vwap}\nSuggested limit: ${limit_price}",
    "priority": 0,
    "limit_offset_pct": 0.001
  }
}
```

Placeholders are `{symbol}`, `{price}`, `{limit_price}`, `{rule}`,
`{session}`, `{time}`, plus every indicator column the rule's params produce
(`vwap`, `rsi`, `ema_9`, `macd_hist`, …). A typo is caught by
`Rule.validate()` when the scenario is saved, not at 4am when the alert
should have fired.

Omitting the `alert` block means "detect but do not notify" — useful while a
scenario is still being tuned.

Shipped scenarios: `rules/vwap-hold.json` and
`rules/vwap-hold-confirmed.json` (the same signal with volume and EMA
momentum agreeing, at high priority).

## The Robinhood link — what it does and does not do

Tapping the notification opens `https://robinhood.com/us/en/stocks/{symbol}/`
— the stock detail page, which on iOS opens the Robinhood app via universal
link.

**The stock page is as deep as it goes.** There is no published Robinhood
link to a buy screen or an order ticket, pre-filled or otherwise, and their
third-party connections policy is explicit that outside apps cannot take
action in the app. Robinhood's own "buying a stock" article describes the
flow as: stock detail page → **Trade** → **Buy** → choose Shares / order
type. So the notification saves the symbol search; it cannot save the taps.

The suggested limit price goes in the *message text* instead, so it is one
glance rather than a calculation.

### Why the locale prefix

`/us/en/stocks/AAPL/` is Robinhood's canonical path, and using it is
deliberate. iOS matches a tapped URL against the app's associated-domain
paths **before** following any redirect, so a URL that only reaches the
right page by redirecting can open Safari instead of the app.

### Trying alternatives

`link_template` is configurable, and `core.alerts.ALTERNATIVE_LINK_TEMPLATES`
lists undocumented forms worth testing on a real device — the short path and
two `robinhood://` custom-scheme guesses. None of them is published by
Robinhood, so any may do nothing, open Safari, or break without notice.
Studio shows them under "Other link forms to try": paste one in and tap a
real alert to see what your phone actually does. That is a test only you can
run.

## What

**Channel: Pushover.** Purpose-built for phone push, delivers as a real
notification with priority levels, and needs only two credentials.

- Reads `PUSHOVER_TOKEN` (application) and `PUSHOVER_USER` (user key) from
  the environment. Either absent → alerting is off, logged once at startup,
  not an error and not a crash. State is still recorded in that case, so
  turning alerts on later does not dump every standing setup at once.
- Send when a `(rule, symbol)` pair matches that did **not** match on the
  previous run. Never re-alert a standing match — a setup that stays true
  for an hour is one alert, not twelve.
- Batch a run's new matches into one message rather than N messages.
- Message includes rule name, symbol, price, and the values that triggered
  it (`Match.values` already carries VWAP, EMAs, MACD, RSI, ATR).
- A delivery failure logs an error and does not interrupt the scan loop.
- Respect Pushover's rate limits; a scan that suddenly matches 200 symbols
  must not fire 200 requests. Cap the batch and summarise the remainder.

## Done

- [x] New matches alert exactly once
- [x] Standing matches never re-alert across runs; a setup that lapses and
      returns alerts again, because that is a new signal
- [x] State persists, so a restart does not re-announce what is currently true
- [x] A corrupt state file is discarded with a warning rather than fatal
- [x] Missing credentials disable alerting without crashing
- [x] Delivery failures are logged and swallowed
- [x] A run beyond the cap (8) sends one summarised low-priority message
- [x] Tests use a fake transport — no live requests, no credentials needed

## Not done

- [ ] `--watch` currently sleeps on a fixed interval rather than firing at
      `core.sessions.scan_times()`. See `scheduled-runs.md`.
- [ ] No market-hours gating on the loop beyond the scanner's own
      `skip_closed`, so overnight it scans, finds nothing, and sleeps.

## Explicitly out of scope

Slack, email, and SMS. Add them behind the same interface once Pushover
works; the "what changed since last run" logic is the hard part and is
shared.

## Note on credentials

`PUSHOVER_TOKEN` and `PUSHOVER_USER` go in `.env`, which is gitignored. Add
them to `.env.example` as empty placeholders — never with real values.
