"""
Scanner Studio
==============
Run with:  streamlit run studio/app.py

Builds scan rules visually and saves them as JSON in rules/, which is
exactly what `python -m scanner.cli` reads. Studio never evaluates rules
with its own logic — it calls core.rules and scanner.engine, so a rule that
previews here behaves identically when the scanner runs it on a schedule.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

from core.backtest import ExitPolicy, backtest
from core.client import AlpacaClient, Credentials
from core.data import MarketDataFetcher
from core.sessions import session_day_series
from core.indicators import IndicatorParams, add_indicators, indicator_columns
from core.alerts import (
    ALTERNATIVE_LINK_TEMPLATES,
    DEFAULT_LINK_TEMPLATE,
    AlertError,
    AlertTemplate,
    build_context,
)
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
    return {"field": "close", "op": ">", "mode": "field", "value": 0.0,
            "field2": "vwap", "for_bars": 3}


def _to_condition(row: dict) -> Condition:
    for_bars = row.get("for_bars") or None
    if row["mode"] == "value":
        return Condition(field=row["field"], op=row["op"],
                         value=float(row["value"]), for_bars=for_bars)
    return Condition(field=row["field"], op=row["op"],
                     field2=row["field2"], for_bars=for_bars)


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


def _current_alert() -> Optional[AlertTemplate]:
    if not st.session_state.get("alert_enabled", True):
        return None
    return AlertTemplate(
        title=st.session_state.alert_title,
        message=st.session_state.alert_message,
        priority=int(st.session_state.alert_priority),
        limit_offset_pct=float(st.session_state.alert_limit_offset) / 100,
        link_template=st.session_state.alert_link,
    )


def _build_rule() -> Rule:
    return Rule(
        name=st.session_state.rule_name,
        description=st.session_state.rule_description,
        universe=st.session_state.rule_universe,
        conditions=[_to_condition(r) for r in st.session_state.conditions],
        params=_current_params(),
        alert=_current_alert(),
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
    c1, c2, c3, c4, c6, c5 = st.columns([3, 2, 2, 3, 1.4, 0.8])
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

    if is_cross:
        # A crossover is a single-bar event; persistence is meaningless on it.
        row["for_bars"] = 1
        c6.markdown("&nbsp;\n\n—")
    else:
        row["for_bars"] = c6.number_input(
            "For bars", min_value=1, max_value=200,
            value=int(row.get("for_bars") or 1), key=f"fb{i}",
            help="Consecutive bars the comparison must hold. 3 = three bars in a row.",
        )

    if c5.button("✕", key=f"del{i}", help="Remove this condition"):
        st.session_state.conditions.pop(i)
        st.rerun()

if st.button("➕ Add condition"):
    st.session_state.conditions.append(_blank_condition())
    st.rerun()

# ── Alert ────────────────────────────────────────────────────────────────────

st.divider()
st.subheader("Alert")
st.caption(
    "What gets sent to your phone when this scenario fires. Placeholders in "
    "`{braces}` are filled from the match."
)

st.checkbox(
    "Send a notification when this matches", value=True, key="alert_enabled",
    help="Leave off to detect a scenario while you are still tuning it.",
)

if st.session_state.alert_enabled:
    a1, a2 = st.columns([1, 1])
    with a1:
        st.text_input("Title", value="Strong above VWAP", key="alert_title")
        st.text_area(
            "Message",
            value=(
                "{symbol} stock is strong and trading above VWAP\n\n"
                "Price ${price}  •  VWAP ${vwap}\n"
                "Suggested limit: ${limit_price}"
            ),
            key="alert_message", height=150,
        )
    with a2:
        st.selectbox(
            "Priority", [-1, 0, 1], index=1, key="alert_priority",
            format_func=lambda p: {
                -1: "Quiet — no sound",
                0: "Normal",
                1: "High — bypasses quiet hours",
            }[p],
        )
        st.number_input(
            "Suggested limit, % above price", 0.0, 5.0, 0.1, step=0.05,
            key="alert_limit_offset",
            help="Puts a marketable limit price in the message. It is a "
                 "suggestion in text — nothing is ordered for you.",
        )
        st.text_input(
            "Link template", value=DEFAULT_LINK_TEMPLATE, key="alert_link",
            help="Tapping the notification opens this. Robinhood publishes no "
                 "deep link to a buy or order screen, so this lands on the "
                 "stock page: Trade → Buy → set order type to Limit.",
        )
        with st.expander("Other link forms to try"):
            st.caption(
                "None of these is published by Robinhood, so any may do "
                "nothing or open Safari. Paste one above and tap a real "
                "alert to see what your phone does."
            )
            for label, template in ALTERNATIVE_LINK_TEMPLATES.items():
                st.code(f"{label}:  {template}", language="text")

    with st.expander("Available placeholders"):
        st.code(
            "  ".join(sorted(_build_rule().available_fields()
                             | {"symbol", "price", "limit_price", "rule",
                                "session", "time"})),
            language="text",
        )

# ── Validation and preview ───────────────────────────────────────────────────

st.divider()

try:
    rule = _build_rule()
    rule.validate()
except (RuleError, AlertError) as exc:
    st.error(f"Not valid: {exc}")
    st.stop()

st.success(f"**{rule.name}** — `{rule.describe()}`")

if rule.alert:
    sample = {name: 100.0 for name in rule.available_fields()}
    sample.update({"vwap": 183.64, "rsi": 61.2, "atr": 1.43,
                   "ema_9": 184.02, "ema_12": 183.71, "ema_200": 179.80})
    preview = rule.alert.render(build_context(
        "NVDA", 184.21, rule.name, rule.alert, sample,
        session="regular", time_label="14:35 UTC",
    ))
    with st.container(border=True):
        st.caption("Phone preview")
        st.markdown(f"**{preview['title']}**")
        st.text(preview["message"])
        st.caption(f"🔗 {preview['url_title']} → {preview['url']}")

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

# ── Backtest ─────────────────────────────────────────────────────────────────

st.divider()
st.subheader("Backtest")
st.caption(
    "Replays the rule over history bar by bar with no lookahead. Entries fill "
    "at the next bar's open; exits are managed on a finer timeframe."
)

bt1, bt2 = st.columns([1, 1])

with bt1:
    bt_symbol = st.text_input("Symbol", value="AAPL", key="bt_symbol").strip().upper()
    bt_days = st.slider("Days of history", 5, 60, 20, key="bt_days")
    manage_minutes = st.selectbox(
        "Manage exits on", [1, 5, 15],
        index=0, key="bt_manage",
        format_func=lambda m: f"{m}-minute bars",
        help=(
            "The finer this is, the sooner an exit is noticed. It also changes "
            "what the exit EMA means: a 9-period EMA is 9 minutes on 1-minute "
            "bars but 45 minutes on 5-minute bars."
        ),
    )

with bt2:
    exit_ema = st.number_input("Exit EMA period", 2, 200, 9, key="bt_exit_ema")
    use_stop = st.checkbox("ATR stop floor", value=True, key="bt_use_stop")
    atr_mult = st.number_input(
        "ATR multiple", 0.5, 10.0, 1.5, step=0.25, key="bt_atr_mult",
        disabled=not use_stop,
        help="Stop = entry price - ATR at entry x this. Caps gap risk.",
    )

st.caption(
    f"Exit EMA({exit_ema}) on {manage_minutes}-minute bars spans "
    f"{exit_ema * manage_minutes} minutes. On the {st.session_state.bar_minutes}-minute "
    f"entry chart the same number would span {exit_ema * st.session_state.bar_minutes} "
    f"minutes — a materially different exit, not just a faster one."
)

if st.button("📉 Run backtest", disabled=get_client() is None):
    entry_minutes = st.session_state.bar_minutes
    if manage_minutes > entry_minutes:
        st.error(
            f"The management timeframe ({manage_minutes} min) must be no coarser "
            f"than the entry timeframe ({entry_minutes} min) — otherwise exits "
            f"are noticed later than entries."
        )
    else:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=bt_days)
        client = get_client()

        with st.spinner(f"Fetching {bt_days} days of {bt_symbol}…"):
            entry_raw = MarketDataFetcher(client, entry_minutes).get_bars_between(
                [bt_symbol], start, end
            ).get(bt_symbol)
            manage_raw = (
                entry_raw if manage_minutes == entry_minutes
                else MarketDataFetcher(client, manage_minutes).get_bars_between(
                    [bt_symbol], start, end
                ).get(bt_symbol)
            )

        if entry_raw is None or entry_raw.empty:
            st.warning(f"No bars returned for {bt_symbol} over that range.")
        elif len(entry_raw) < rule.params.min_bars():
            st.warning(
                f"Only {len(entry_raw)} bars returned; this rule needs "
                f"{rule.params.min_bars()} before its indicators are valid. "
                f"Widen the range or shorten the periods."
            )
        else:
            entry_bars = add_indicators(
                entry_raw, rule.params,
                anchor=session_day_series(entry_raw.index, bt_symbol),
            )
            # The exit EMA lives on the management frame, so that frame needs
            # its own params at its own resolution.
            manage_params = IndicatorParams(
                ema_periods=(exit_ema,), atr_period=rule.params.atr_period,
                bar_minutes=manage_minutes,
            )
            manage_bars = add_indicators(
                manage_raw, manage_params,
                anchor=session_day_series(manage_raw.index, bt_symbol),
            )

            result = backtest(
                entry_bars, rule,
                ExitPolicy(exit_ema, atr_mult if use_stop else None),
                manage_bars, symbol=bt_symbol,
            )

            summary = result.summary()
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Trades", summary["trades"])
            m2.metric("Win rate", f"{summary['win_rate_pct']}%")
            m3.metric("Avg P&L", f"{summary['avg_pnl_pct']:+.3f}%")
            m4.metric("Max drawdown", f"{summary['max_drawdown_pct']:.2f}%")

            m5, m6, m7 = st.columns(3)
            m5.metric("Total return", f"{summary['total_return_pct']:+.2f}%")
            m6.metric("Median P&L", f"{summary['median_pnl_pct']:+.3f}%")
            m7.metric("Avg hold", f"{summary['avg_holding_minutes']:.0f} min")

            if result.trades:
                st.caption(f"Exits: {result.exit_reasons()}")
                st.dataframe(
                    result.trades_frame(), use_container_width=True, hide_index=True
                )
                equity = (
                    1 + result.trades_frame()["pnl_pct"] / 100
                ).cumprod()
                st.line_chart(equity, height=180)
            else:
                st.info(
                    f"No trades over {bt_days} days. The rule never matched, or "
                    f"matched only where there were no bars left to fill."
                )

            st.warning(
                "Signal quality only — no commission, no slippage, one position "
                "at a time, fully invested. Treat the return as an upper bound, "
                "and a few dozen trades as a small sample rather than an edge."
            )

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
                    "field2": c.field2 or "vwap",
                    "for_bars": c.for_bars or 1,
                }
                for c in loaded.conditions
            ]
            st.session_state.rule_name = loaded.name
            st.session_state.rule_description = loaded.description
            st.session_state.alert_enabled = loaded.alert is not None
            if loaded.alert:
                st.session_state.alert_title = loaded.alert.title
                st.session_state.alert_message = loaded.alert.message
                st.session_state.alert_priority = loaded.alert.priority
                st.session_state.alert_limit_offset = (
                    loaded.alert.limit_offset_pct * 100
                )
                st.session_state.alert_link = loaded.alert.link_template
            st.rerun()
