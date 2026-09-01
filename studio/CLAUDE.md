# Scanner Studio

Streamlit app for authoring scan rules. Studio's whole job is to produce a
valid `core.rules.Rule` and write it to `rules/<name>.json`.

Run: `streamlit run studio/app.py`

## The one rule that matters

**Studio must never evaluate a rule with its own logic.** The preview calls
`scanner.engine.Scanner` — the same code path the scheduled scan uses. If
Studio ever grows a second evaluator, a rule will preview green and then
behave differently in production, which is the single worst failure mode
this app can have.

## Shape

- `app.py` — the whole UI. Session state holds a list of condition dicts;
  `_build_rule()` converts them to a real `Rule` on every rerun, so
  validation errors surface as you type.

The condition dicts are UI state (they carry a `mode` field for the
value/field radio); `Condition` is the domain object. Keep the conversion in
`_to_condition()` rather than scattering it.

## Rules of the road

- Field lists come from `core.indicators.INDICATOR_COLUMNS` — adding an
  indicator to core should make it selectable here with no edit.
- Operators come from `core.rules.VALID_OPS`. Same principle.
- Crossover operators force `mode="field"`, since comparing a crossover to a
  literal is meaningless. Keep that guard if you add operators.
- Saving writes exactly `Rule.to_json()`. Do not add Studio-only keys to the
  saved file — the scanner would ignore them and the two would drift.

## Not yet built

See `docs/specs/studio/`. Notable: backtesting a rule over history before
saving it, and showing why a near-miss symbol failed.
