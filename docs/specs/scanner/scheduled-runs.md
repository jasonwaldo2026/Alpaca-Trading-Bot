# Scheduled scanner runs

**Status:** proposed
**Touches:** `scanner/cli.py`, new `scanner/schedule.py`

## Why
The scanner runs only when invoked by hand. A setup that fires at 09:45 or
on the hour is the entire point of a scanner — matches are time-sensitive
and a rule you have to remember to run is a rule you will not run.

## What
- `python -m scanner.cli --watch --every 15m` loops on an interval.
- Skip runs when the relevant market is closed: use Alpaca's clock endpoint
  for equities; crypto runs 24/7. A rule whose universe is all-crypto should
  keep running overnight.
- Track which `(rule, symbol)` pairs matched on the previous run so output
  can distinguish **new** matches from ones still standing. Persist to
  `.scanner-state.json` so a restart does not re-announce everything.
- On SIGINT, exit cleanly with a summary of the session.

## Done when
- [ ] `--watch --every` runs on schedule and honors market hours per asset class
- [ ] Second run of an unchanged market reports zero *new* matches
- [ ] State file survives restart; a corrupt state file is discarded with a
      warning rather than crashing
- [ ] Tests use a fake clock — no `time.sleep` in tests

## Explicitly out of scope
Alerting (Slack/email). That is a separate spec, and it depends on this one
producing a clean "new matches" signal first.
