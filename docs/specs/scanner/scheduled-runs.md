# Scheduled scanner runs

**Status:** proposed
**Touches:** `scanner/cli.py`, new `scanner/schedule.py`

## Why
The scanner runs only when invoked by hand. A setup that fires at 09:45 or
on the hour is the entire point of a scanner — matches are time-sensitive
and a rule you have to remember to run is a rule you will not run.

## What
- `python -m scanner.cli --watch` loops, driven by
  `core.sessions.scan_times()` rather than a fixed interval — once per
  completed bar, ~2 minutes after it closes.
- Cadence follows from the bar interval and the enabled sessions:
  **7 scans/day** regular hours, **11** with after-hours, **24** for crypto.
  Anything faster re-reads a bar that cannot have changed.
- Session gating already exists: `Scanner(skip_closed=True)` (the default)
  drops symbols whose market is closed and records why in
  `ScanResult.skipped`. Crypto keeps running overnight.
- Populate `SessionCalendar` from Alpaca's `/v2/calendar` at startup so
  holidays and early closes are respected — currently an empty calendar
  treats every weekday as a full session.
- Track which `(rule, symbol)` pairs matched on the previous run so output
  can distinguish **new** matches from ones still standing. Persist to
  `.scanner-state.json` so a restart does not re-announce everything.
- On SIGINT, exit cleanly with a summary of the session.

## Done when
- [ ] `--watch` fires at `scan_times()`, not on a fixed interval
- [ ] Holidays and early closes come from Alpaca's calendar, not hardcoded
- [ ] Second run of an unchanged market reports zero *new* matches
- [ ] State file survives restart; a corrupt state file is discarded with a
      warning rather than crashing
- [ ] Tests use a fake clock — no `time.sleep` in tests

## Explicitly out of scope
Alerting (Slack/email). That is a separate spec, and it depends on this one
producing a clean "new matches" signal first.
