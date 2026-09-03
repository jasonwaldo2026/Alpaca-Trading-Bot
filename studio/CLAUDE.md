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

## Preview is wired like the CLI

`Scanner(MarketDataFetcher(client, bar_minutes), bar_minutes=..., fundamentals=get_floats())`
— the rule's own bar size (never the fetcher default), and the same float
source `load_provider()` gives the CLI. Two deliberate differences:
`skip_closed=False`, because a preview scans whatever the clock says, and
`SessionConfig.extended()`, because pre-market is when this app is open.
An `all_tradable` universe is resolved with the client here, as the CLI
does, since core cannot.

## Loading a saved rule

Streamlit forbids writing a widget's session value once that widget exists
on the current run. So the Load button only parks the parsed rule in
`st.session_state.pending_load` and reruns; `_apply_loaded()` at the top
of the script, ahead of every widget, writes it into the widget keys.
Params are restored too (`PARAM_WIDGETS`, plus `params_base` for any field
without a widget), so "load, tweak one condition, save" keeps the file's
periods and bar size rather than overwriting them with the sidebar's.

Condition-row widget keys carry a generation number (`conditions_gen`)
that a load or a delete bumps. Without it, the row at index *i* keeps
showing what the previous row *i* held — a deleted condition reappears,
and a loaded rule shows the old rule's first rows.

## Rules of the road

- Field lists come from `core.indicators.INDICATOR_COLUMNS` — adding an
  indicator to core should make it selectable here with no edit.
- Operators come from `core.rules.VALID_OPS`. Same principle.
- Crossover operators force `mode="field"`, since comparing a crossover to a
  literal is meaningless. Keep that guard if you add operators.
- Saving writes exactly `Rule.to_json()`. Do not add Studio-only keys to the
  saved file — the scanner would ignore them and the two would drift.

## Not yet built

See `docs/specs/studio/`. Notable: showing why a near-miss symbol failed
(`explain-near-misses.md`).
