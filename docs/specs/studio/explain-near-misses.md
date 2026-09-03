# Show why a symbol did not match

**Status:** proposed
**Touches:** `core/rules.py`, `studio/app.py`

## Why
An empty scan result gives no information about *why* it is empty. Was the
RSI threshold slightly too tight, or is the whole rule nonsense? Today the
only way to find out is to loosen conditions one at a time and re-run.

## What
- Add `Rule.explain(bars) -> List[ConditionResult]` returning, per
  condition, whether it passed and the actual values compared.
- Studio: for symbols that failed, show which conditions passed and which
  failed, sorted so near-misses (all but one condition satisfied) come first.
- Highlight the single blocking condition when exactly one failed.

## Done when
- [ ] `explain()` returns a result for every condition, pass or fail
- [ ] Values reported are the real compared numbers, including NaN cases
- [ ] Studio lists near-misses ahead of total misses
- [ ] `explain()` and `matches()` never disagree — property test over random
      frames asserting `all(r.passed for r in explain(b)) == matches(b)`

## Explicitly out of scope
Suggesting threshold changes automatically. Show the data; let the user
decide.
