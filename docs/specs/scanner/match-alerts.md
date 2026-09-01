# Alert on new scanner matches

**Status:** proposed — blocked on `scheduled-runs.md`
**Touches:** new `scanner/alerts.py`, `scanner/cli.py`

## Why
A scheduled scan that prints to a terminal nobody is watching is not a
signal. Matches need to reach a phone.

## What
- Send a message when a `(rule, symbol)` pair matches that did not match on
  the previous run. Never re-alert a standing match.
- Read `SLACK_WEBHOOK_URL` from the environment. Absent → alerting is off,
  logged once at startup, not an error.
- Message includes: rule name, symbol, price, and the indicator values that
  triggered it (`Match.values` already carries these).
- Batch a run's matches into one message rather than N messages.
- A webhook failure logs an error and does not interrupt the scan loop.

## Done when
- [ ] New matches alert exactly once
- [ ] Standing matches never re-alert
- [ ] Missing webhook disables alerting without crashing
- [ ] Webhook failures are logged and swallowed
- [ ] Tests assert payload shape against a fake transport — no live webhook

## Explicitly out of scope
Email and SMS. Add them behind the same interface once Slack works.
