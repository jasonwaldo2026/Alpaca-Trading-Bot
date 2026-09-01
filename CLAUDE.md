# Alpaca Trading Platform

Four apps over one shared core. Paper trading only.

| Directory | What it is | Run it with |
|---|---|---|
| `core/` | Shared library. Credentials, bar fetching, indicator math, scan-rule model. | — (imported) |
| `bot/` | Live trading bot — strategies, risk, orders, poll loop. | `python trading_bot.py` |
| `dashboard/` → `dashboard.py` | Streamlit read-only view of account, positions, charts. | `streamlit run dashboard.py` |
| `scanner/` | Runs saved rules across a symbol universe. | `python -m scanner.cli` |
| `studio/` | Streamlit rule builder. Authors the rules the scanner runs. | `streamlit run studio/app.py` |

## Invariants — do not break these

1. **Indicator math lives only in `core/indicators.py`.** Never re-implement
   SMA/RSI/ATR inside an app. If a chart and a signal disagree about RSI, the
   whole system is untrustworthy. Need a new indicator? Add it to `core/`
   with a test, then use it.
2. **`core/` never imports from `bot/`, `dashboard`, `scanner/`, or `studio/`.**
   The dependency arrow points one way. A test asserts this.
3. **`paper=True` stays true.** Nothing in this repo should flip Alpaca to
   live trading. If a task seems to require it, stop and ask.
4. **Studio and Scanner share `core/rules.py`.** Studio writes rule JSON;
   Scanner reads it. Neither may define its own rule dialect or evaluation
   logic — both call `Rule.matches()`.
5. **Asset-class routing goes through `core/universe.py:is_crypto()`.**
   Never test for `"/" in symbol` inline.

## Conventions

- Indicator column names are constants in `core/indicators.py`
  (`COL_RSI`, `COL_ATR`, …). Reference those, not string literals, in new code.
- Indicator periods travel as an `IndicatorParams`. Get one from
  `BotConfig.indicator_params()` or a `Rule.params` — never construct one
  ad hoc in app code, or the apps will silently disagree.
- Strategies subclass `BaseStrategy` in `bot/strategies.py` and return
  `List[Signal]`.
- Credentials are resolved by `core/client.py:Credentials` — `.from_env()`
  for CLI apps, `.from_streamlit(st.secrets)` for Streamlit ones. Do not read
  `os.getenv("ALPACA_...")` directly in an app.

## Testing

```bash
pip install -r requirements-dev.txt
pytest                    # all tests, no network required
pytest tests/test_indicators.py -q
```

Tests never hit the network. The scanner is tested against a `FakeFetcher`
(see `tests/test_scanner.py`); follow that pattern for anything that would
otherwise call Alpaca.

`tests/test_indicators.py::test_matches_legacy_implementations` pins the
indicator math against the original pre-refactor implementations. If you
change indicator behavior deliberately, that test is the one to update — and
updating it means every app's numbers change, so say so explicitly.

## Working on this repo

- One app per session and per branch. `core/` changes land first, in their
  own PR, so the apps can build on them.
- Feature specs live in `docs/specs/<app>/*.md`. Read the spec before
  implementing; if the spec and the code disagree, say so rather than
  guessing.
