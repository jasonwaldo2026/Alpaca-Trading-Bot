"""
Scanner Studio
==============
Run with:  streamlit run studio/app.py

Builds scan rules visually and saves them as JSON in rules/, which is
exactly what `python -m scanner.cli` reads. Studio never evaluates rules
with its own logic — it calls core.rules and scanner.engine, so a rule that
previews here behaves identically when the scanner runs it on a schedule.
"""

from pathlib import Path

import pandas as pd
import streamlit as st

from core.client import AlpacaClient, Credentials
from core.data import MarketDataFetcher
from core.indicators import IndicatorParams, indicator_columns
from core.rules import VALID_OPS, Condition, Rule, RuleError
from scanner.engine import Scanner

RULES_DIR = Path("rules")

# Fields a condition can reference: raw OHLCV plus every indicator column
# the current params produce (EMA columns are named after their periods).
OHLCV_FIELDS = ["close", "open", "high", "low", "volume"]

st.set_page_config(page_title="Scanner Studio", page_icon="🛠️", layout="wide")


@st.cache_resource
def get_client():
    creds = Credentials.from_streamlit(st.secrets, paper=True)
    return AlpacaClient(creds) if creds.is_complete() else None


def _blank_condition() -> dict:
    return {"field": "rsi", "op": "<", "mode": "value", "value": 30.0, "field2": "vol_sma"}


def _to_condition(row: dict) -> Condition:
    if row["mode"] == "value":
        return Condition(field=row["field"], op=row["op"], value=float(row["value"]))
    return Condition(field=row["field"], op=row["op"], field2=row["field2"])


def _parse_ema_periods(text: str):
    """Read '9, 12, 200' into (9, 12, 200). Invalid entries fall back."""
    try:
        periods = tuple(int(p.strip()) for p in text.split(",") if p.strip())
    except ValueError:
        return None
    return periods or None


def _current_params() -> IndicatorParams:
    periods = _parse_ema_periods(st.session_state.get("ema_periods_text", "9, 12, 200"))
    return IndicatorParams(
        sma_fast=st.session_state.sma_fast,
        sma_slow=st.session_state.sma_slow,
        ema_periods=periods or (9, 12, 200),
        rsi_period=st.session_state.rsi_period,
        volume_sma_period=st.session_state.vol_sma_period,
        atr_period=st.session_state.atr_period,
        macd_fast=st.session_state.macd_fast,
        macd_slow=st.session_state.macd_slow,
        macd_signal=st.session_state.macd_signal,
        bar_minutes=st.session_state.bar_minutes,
    )


def _build_rule() -> Rule:
    return Rule(
        name=st.session_state.rule_name,
        description=st.session_state.rule_description,
        universe=st.session_state.rule_universe,
        conditions=[_to_condition(r) for r in st.session_state.conditions],
        params=_current_params(),
    )


# ── State ────────────────────────────────────────────────────────────────────

if "conditions" not in st.session_state:
    st.session_state.conditions = [_blank_condition()]

# ── Sidebar: rule identity, universe, indicator periods ──────────────────────

with st.sidebar:
    st.title("🛠️ Scanner Studio")
    st.caption("Build a rule, preview it, save it. The scanner runs what you save.")
    st.divider()

    st.text_input("Rule name", value="my scan", key="rule_name")
    st.text_area("Description", value="", key="rule_description", height=70)

    st.selectbox(
        "Universe",
        ["default_stocks", "default_crypto", "sp500_liquid", "major_crypto"],
        index=2,
        key="rule_universe",
    )

    st.divider()
    st.subheader("Indicator periods")
    st.caption("These travel with the rule, so the scanner reproduces this preview exactly.")
    st.slider("Fast SMA", 5, 50, 10, key="sma_fast")
    st.slider("Slow SMA", 10, 100, 30, key="sma_slow")
    st.slider("RSI period", 5, 30, 14, key="rsi_period")
    st.slider("Volume SMA", 5, 50, 20, key="vol_sma_period")
    st.slider("ATR period", 5, 30, 14, key="atr_period")
    st.text_input(
        "EMA periods", value="9, 12, 200", key="ema_periods_text",
        help="Comma-separated. Each becomes a selectable field: ema_9, ema_12, …",
    )
    st.slider("MACD fast", 3, 40, 12, key="macd_fast")
    st.slider("MACD slow", 5, 80, 26, key="macd_slow")
    st.slider("MACD signal", 2, 30, 9, key="macd_signal")

    st.divider()
    st.selectbox(
        "Bar size", [5, 15, 30, 60], index=0, key="bar_minutes",
        format_func=lambda m: f"{m} min" if m < 60 else "1 hour",
        help="Periods above are bar counts, so their wall-clock span scales with this.",
    )

# ── Condition builder ────────────────────────────────────────────────────────

st.title("Rule builder")

if get_client() is None:
    st.warning(
        "No Alpaca credentials found — you can still build and save rules, "
        "but the live preview is disabled. Add ALPACA_API_KEY and "
        "ALPACA_API_SECRET to your .env or Streamlit secrets."
    )

st.subheader("Conditions")
st.caption("A symbol matches when **every** condition holds on the latest bar.")

if _parse_ema_periods(st.session_state.ema_periods_text) is None:
    st.error("EMA periods must be comma-separated whole numbers, e.g. `9, 12, 200`.")
    st.stop()

# Selectable fields follow the current params, so changing the EMA periods
# immediately changes what a condition can reference.
FIELDS = OHLCV_FIELDS + list(indicator_columns(_current_params()))

for i, row in enumerate(st.session_state.conditions):
    c1, c2, c3, c4, c5 = st.columns([3, 2, 2, 3, 1])
    if row["field"] not in FIELDS:
        FIELDS.append(row["field"])      # keep a loaded rule's field selectable
    row["field"] = c1.selectbox("Field", FIELDS, index=FIELDS.index(row["field"]), key=f"f{i}")
    row["op"] = c2.selectbox("Operator", VALID_OPS, index=VALID_OPS.index(row["op"]), key=f"o{i}")

    is_cross = row["op"] in ("crosses_above", "crosses_below")
    if is_cross:
        # Crossovers compare two series; a literal has no meaning here.
        row["mode"] = "field"
        c3.markdown("&nbsp;\n\ncompared to")
    else:
        row["mode"] = c3.radio(
            "Compare to", ["value", "field"],
            index=0 if row["mode"] == "value" else 1,
            key=f"m{i}", horizontal=True,
        )

    if row["mode"] == "value":
        row["value"] = c4.number_input("Value", value=float(row["value"]), key=f"v{i}")
    else:
        if row["field2"] not in FIELDS:
            FIELDS.append(row["field2"])
        row["field2"] = c4.selectbox(
            "Field", FIELDS, index=FIELDS.index(row["field2"]), key=f"f2{i}"
        )

    if c5.button("✕", key=f"del{i}", help="Remove this condition"):
        st.session_state.conditions.pop(i)
        st.rerun()

if st.button("➕ Add condition"):
    st.session_state.conditions.append(_blank_condition())
    st.rerun()

# ── Validation and preview ───────────────────────────────────────────────────

st.divider()

try:
    rule = _build_rule()
    rule.validate()
except RuleError as exc:
    st.error(f"Rule is not valid: {exc}")
    st.stop()

st.success(f"**{rule.name}** — `{rule.describe()}`")

left, right = st.columns([1, 1])

with left:
    st.subheader("Preview")
    bar_limit = st.slider("Bars to fetch", 60, 500, 120)
    if st.button("▶ Run scan", type="primary", disabled=get_client() is None):
        with st.spinner("Scanning…"):
            result = Scanner(
                MarketDataFetcher(get_client()), bar_limit=bar_limit
            ).scan([rule])

        st.caption(f"Scanned {result.scanned} symbols.")
        if result.matches:
            st.dataframe(
                pd.DataFrame([
                    {"symbol": m.symbol, "price": m.price, **m.values}
                    for m in result.matches
                ]),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No matches right now. Loosen a condition, or try another universe.")

        if result.skipped:
            with st.expander(f"Skipped {len(result.skipped)} symbol(s)"):
                st.json(result.skipped)

with right:
    st.subheader("Saved form")
    st.caption("This is the exact file the scanner reads.")
    st.code(rule.to_json(), language="json")

    filename = st.text_input(
        "Filename",
        value=f"{rule.name.strip().lower().replace(' ', '-')}.json",
    )
    if st.button("💾 Save to rules/"):
        RULES_DIR.mkdir(exist_ok=True)
        path = RULES_DIR / filename
        path.write_text(rule.to_json())
        st.success(f"Saved {path} — run it with `python -m scanner.cli {path}`")

# ── Load an existing rule ────────────────────────────────────────────────────

st.divider()
existing = sorted(RULES_DIR.glob("*.json")) if RULES_DIR.exists() else []
if existing:
    st.subheader("Open a saved rule")
    choice = st.selectbox("Rule file", [p.name for p in existing])
    if st.button("📂 Load"):
        try:
            loaded = Rule.from_json((RULES_DIR / choice).read_text())
        except RuleError as exc:
            st.error(f"Could not load {choice}: {exc}")
        else:
            st.session_state.conditions = [
                {
                    "field": c.field,
                    "op": c.op,
                    "mode": "value" if c.field2 is None else "field",
                    "value": c.value if c.value is not None else 0.0,
                    "field2": c.field2 or "vol_sma",
                }
                for c in loaded.conditions
            ]
            st.session_state.rule_name = loaded.name
            st.session_state.rule_description = loaded.description
            st.rerun()
