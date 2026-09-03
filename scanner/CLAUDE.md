# Scanner

Runs saved rules across a symbol universe and reports matches. No UI —
`scanner/engine.py` is pure logic, `scanner/cli.py` is the only I/O.

Rules come from `rules/*.json`, authored in Studio. The scanner does not
define rule semantics; `core/rules.py` does.

## Shape

- `engine.py` — `Scanner.scan(rules, symbols=None) -> ScanResult`
- `cli.py` — argparse entry point, `python -m scanner.cli`

## Rules of the road

- **Fetch once.** `scan()` takes the union of every rule's universe and makes
  one batched request per asset class. Do not move fetching inside the
  per-rule loop — that is the difference between 1 API call and 40.
- **Enriched frames are cached per `(symbol, params)`.** Two rules sharing
  indicator periods must not recompute indicators. `IndicatorParams` is a
  frozen dataclass so it can be a cache key; keep it that way.
- **A bad symbol is not a fatal error.** Record it in `ScanResult.skipped`
  and keep going — one delisted ticker must not kill a 500-symbol sweep.
- **Validate rules before fetching**, so a typo fails in a second rather than
  after a full sweep.

## Testing

Use `FakeFetcher` from `tests/test_scanner.py`. Never write a test that
requires credentials or network.

## Not yet built

See `docs/specs/scanner/`. Notable: scheduled runs, alerting on new matches,
and match history (so "new since last scan" is answerable).
