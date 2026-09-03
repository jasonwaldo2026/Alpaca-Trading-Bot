"""
Trading Bot Dashboard
=====================
Run with:  streamlit run dashboard.py

Shows live account data, open positions, indicator charts, and the bot log.
All data comes directly from Alpaca — no bot process needs to be running.
"""

import math
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

try:
    from core import universe
    from core.client import AlpacaClient, Credentials
    from core.data import MarketDataFetcher
    from core.indicators import (
        IndicatorParams,
        add_indicators,
        crossed_down,
        crossed_up,
    )
    from core.sessions import session_day_series
except ImportError:
    st.error("Missing packages — run:  pip install -r requirements.txt")
    st.stop()

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Trading Bot Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Colour palette ────────────────────────────────────────────────────────────

GREEN  = "#00c896"
RED    = "#ff4b4b"
BLUE   = "#4b8eff"
YELLOW = "#ffd700"
GREY   = "#888888"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _pct_color(value: float) -> str:
    return GREEN if value >= 0 else RED

def _arrow(value: float) -> str:
    return "▲" if value >= 0 else "▼"

# ── Alpaca connection (cached so it doesn't reconnect on every rerun) ─────────

@st.cache_resource
def get_client():
    """Shared AlpacaClient, or None when credentials are absent."""
    creds = Credentials.from_streamlit(st.secrets, paper=True)
    if not creds.is_complete():
        return None
    return AlpacaClient(creds)

client = get_client()
trading_client = client.trading if client else None

# ── Data fetchers (cached per symbol for 60 seconds) ─────────────────────────

@st.cache_data(ttl=60)
def fetch_account():
    return trading_client.get_account()

@st.cache_data(ttl=60)
def fetch_positions():
    return trading_client.get_all_positions()

@st.cache_data(ttl=60)
def fetch_orders(limit=20):
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=limit)
    return trading_client.get_orders(req)

@st.cache_data(ttl=300)
def fetch_bars(symbol: str, bar_limit: int = 100, bar_minutes: int = 60):
    """Hourly OHLCV for one symbol. Asset class is routed by core.universe."""
    try:
        frames = MarketDataFetcher(client, bar_minutes).get_bars(
            [symbol], limit=bar_limit
        )
        df = frames.get(symbol)
        if df is None or df.empty:
            return pd.DataFrame()
        # Keep the DatetimeIndex: VWAP's daily reset is derived from it.
        return df
    except Exception as e:
        st.warning(f"Could not fetch bars for {symbol}: {e}")
        return pd.DataFrame()

def compute_indicators(
    df: pd.DataFrame,
    symbol: str,
    sma_fast=10, sma_slow=30, rsi_period=14, vol_sma_period=20, atr_period=14,
    ema_periods=(4, 9, 12, 200),
    macd_fast=12, macd_slow=26, macd_signal=9,
    bar_minutes=60,
) -> pd.DataFrame:
    """
    Thin wrapper over core.indicators so the chart shows exactly the values
    the bot and scanner act on. Do not inline indicator math here.

    The VWAP anchor is derived from the frame's DatetimeIndex so VWAP resets
    each trading day, as it does on every charting platform.
    """
    params = IndicatorParams(
        sma_fast=sma_fast,
        sma_slow=sma_slow,
        ema_periods=tuple(ema_periods),
        rsi_period=rsi_period,
        volume_sma_period=vol_sma_period,
        atr_period=atr_period,
        macd_fast=macd_fast,
        macd_slow=macd_slow,
        macd_signal=macd_signal,
        bar_minutes=bar_minutes,
    )
    anchor = (
        session_day_series(df.index, symbol)
        if isinstance(df.index, pd.DatetimeIndex) else None
    )
    return add_indicators(df, params, anchor=anchor)


def read_log(path="bot.log", max_lines=200) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [l.rstrip() for l in lines[-max_lines:]][::-1]
    except FileNotFoundError:
        return ["bot.log not found — start the bot to generate logs."]

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("⚙️ Dashboard Settings")
    st.divider()

    all_symbols = list(universe.DEFAULT_STOCKS) + list(universe.DEFAULT_CRYPTO)

    selected_symbol = st.selectbox(
        "Inspect symbol",
        all_symbols,
        index=0,
    )

    st.divider()
    st.subheader("Indicator settings")
    sma_fast       = st.slider("Fast SMA period",   5,  50, 10)
    sma_slow       = st.slider("Slow SMA period",  10, 100, 30)
    rsi_period     = st.slider("RSI period",        5,  30, 14)
    vol_sma_period = st.slider("Volume SMA period", 5,  50, 20)
    atr_period     = st.slider("ATR period",        5,  30, 14)

    st.caption("EMA / MACD")
    ema_text       = st.text_input("EMA periods", value="4, 9, 12, 200",
                                   help="Comma-separated; one line per period.")
    try:
        ema_periods = tuple(int(p.strip()) for p in ema_text.split(",") if p.strip())
    except ValueError:
        ema_periods = ()
    if not ema_periods:
        st.error("EMA periods must be comma-separated whole numbers, "
                 "e.g. 4, 9, 12, 200")
        st.stop()
    macd_fast      = st.slider("MACD fast",         3,  40, 12)
    macd_slow      = st.slider("MACD slow",         5,  80, 26)
    macd_signal    = st.slider("MACD signal",       2,  30,  9)

    st.divider()
    bar_minutes = st.selectbox(
        "Bar size",
        [5, 15, 30, 60],
        index=3,
        format_func=lambda m: f"{m} min" if m < 60 else "1 hour",
        help=(
            "Periods above are bar counts, so their wall-clock meaning changes "
            "with this. SMA(30) is 30 hours of hourly bars but 150 minutes of "
            "5-minute bars."
        ),
    )
    bar_limit      = st.slider("Bars to load",     60, 1000, 120)

    st.divider()
    if st.button("🔄 Refresh data"):
        st.cache_data.clear()
        st.rerun()

    st.caption("Data refreshes automatically every 60 s.")

# ── Guard: credentials ────────────────────────────────────────────────────────

if client is None:
    st.error("No API credentials found. Add ALPACA_API_KEY and ALPACA_API_SECRET to your .env file.")
    st.stop()

# ── Header ────────────────────────────────────────────────────────────────────

st.title("📈 Trading Bot Dashboard")
st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  •  Paper trading mode")
st.divider()

# ── Account overview ──────────────────────────────────────────────────────────

try:
    acct = fetch_account()

    portfolio_value  = float(acct.portfolio_value)
    cash             = float(acct.cash)
    buying_power     = float(acct.buying_power)
    equity           = float(acct.equity)
    last_equity      = float(acct.last_equity)
    day_pnl          = equity - last_equity
    day_pnl_pct      = (day_pnl / last_equity * 100) if last_equity else 0

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric("💼 Portfolio Value",  f"${portfolio_value:,.2f}")
    with col2:
        st.metric("💵 Cash Available",   f"${cash:,.2f}")
    with col3:
        st.metric("⚡ Buying Power",     f"${buying_power:,.2f}")
    with col4:
        st.metric(
            "📊 Today's P&L",
            f"${day_pnl:+,.2f}",
            delta=f"{day_pnl_pct:+.2f}%",
            delta_color="normal",
        )

    # Allocation bar
    invested     = portfolio_value - cash
    invested_pct = (invested / portfolio_value * 100) if portfolio_value else 0
    st.progress(
        min(invested_pct / 100, 1.0),
        text=f"Portfolio allocated: {invested_pct:.1f}%  (${invested:,.2f} invested  /  ${cash:,.2f} cash)",
    )

except Exception as e:
    st.error(f"Could not load account data: {e}")

st.divider()

# ── Open positions ────────────────────────────────────────────────────────────

st.subheader("🏦 Open Positions")

try:
    positions = fetch_positions()

    if not positions:
        st.info("No open positions right now. The bot will open positions when signals fire.")
    else:
        rows = []
        for p in positions:
            qty        = float(p.qty)
            avg_entry  = float(p.avg_entry_price)
            mkt_val    = float(p.market_value)
            unrealized = float(p.unrealized_pl)
            unr_pct    = float(p.unrealized_plpc) * 100
            rows.append({
                "Symbol":        p.symbol,
                "Qty":           qty,
                "Avg Entry $":   avg_entry,
                "Market Value":  mkt_val,
                "Unrealized P&L":unrealized,
                "P&L %":         unr_pct,
            })

        pos_df = pd.DataFrame(rows)

        # Colour-coded P&L column using Streamlit styler
        def colour_pnl(val):
            color = "#00c896" if val >= 0 else "#ff4b4b"
            return f"color: {color}; font-weight: bold"

        styled = (
            pos_df.style
            .applymap(colour_pnl, subset=["Unrealized P&L", "P&L %"])
            .format({
                "Avg Entry $":    "${:,.2f}",
                "Market Value":   "${:,.2f}",
                "Unrealized P&L": "${:+,.2f}",
                "P&L %":          "{:+.2f}%",
                "Qty":            "{:,.4f}",
            })
        )
        st.dataframe(styled, use_container_width=True, hide_index=True)

        # Mini bar chart of P&L by symbol
        fig_pos = go.Figure(go.Bar(
            x=[r["Symbol"] for r in rows],
            y=[r["Unrealized P&L"] for r in rows],
            marker_color=[GREEN if r["Unrealized P&L"] >= 0 else RED for r in rows],
            text=[f"${r['Unrealized P&L']:+,.2f}" for r in rows],
            textposition="outside",
        ))
        fig_pos.update_layout(
            title="Unrealized P&L per Position",
            yaxis_title="USD",
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            height=280,
            margin=dict(t=40, b=20),
        )
        st.plotly_chart(fig_pos, use_container_width=True)

except Exception as e:
    st.error(f"Could not load positions: {e}")

st.divider()

# ── Symbol chart ──────────────────────────────────────────────────────────────

st.subheader(f"📉 {selected_symbol} — Price, Indicators & Volume")

st.markdown(
    """
    **How to read this chart:**
    - **Candlesticks** — each bar shows open/high/low/close for one hour. Green = price went up; red = price went down.
    - **Fast SMA (blue)** — short-term average price. Reacts quickly to moves.
    - **Slow SMA (orange)** — longer-term average. Moves slowly.
    - **Golden cross** ✅ — fast SMA crosses *above* slow SMA → potential BUY signal.
    - **Death cross** ❌ — fast SMA crosses *below* slow SMA → potential SELL signal.
    - **RSI** — momentum meter (0–100). Above 70 = overbought (avoid buying). Below 30 = oversold (avoid selling).
    - **Volume bars** — how many shares/coins traded. Blue bar = above average (strong move). Grey = below average (weak, unconfirmed).
    - **ATR** — volatility. High ATR = bigger price swings → bot uses a smaller position size automatically.
    """
)

df_raw = fetch_bars(selected_symbol, bar_limit, bar_minutes)

if df_raw.empty:
    st.warning(f"No bar data returned for {selected_symbol}.")
else:
    df = compute_indicators(
        df_raw, selected_symbol,
        sma_fast, sma_slow, rsi_period, vol_sma_period, atr_period,
        ema_periods, macd_fast, macd_slow, macd_signal, bar_minutes,
    )

    # Determine current signal
    # A long EMA (200) stays NaN far longer than the rest; requiring it here
    # would blank the whole panel, so the signal check uses the SMA set only.
    clean = df.dropna(subset=["sma_fast", "sma_slow", "rsi", "vol_sma", "atr"])
    signal_label = "⬜ HOLD — waiting for a confirmed signal"
    signal_color = GREY
    if len(clean) >= 2:
        prev, curr = clean.iloc[-2], clean.iloc[-1]
        golden = crossed_up(prev, curr, "sma_fast", "sma_slow")
        death  = crossed_down(prev, curr, "sma_fast", "sma_slow")
        high_vol      = curr["volume"] > curr["vol_sma"]
        not_overbought = curr["rsi"] < 70
        not_oversold   = curr["rsi"] > 30

        if golden and high_vol and not_overbought:
            signal_label = f"🟢 BUY signal  |  RSI {curr['rsi']:.1f}  |  Volume confirmed"
            signal_color = GREEN
        elif death and high_vol and not_oversold:
            signal_label = f"🔴 SELL signal  |  RSI {curr['rsi']:.1f}  |  Volume confirmed"
            signal_color = RED
        elif golden and not high_vol:
            signal_label = f"🟡 Golden cross but LOW VOLUME — signal not confirmed"
            signal_color = YELLOW
        elif golden and not not_overbought:
            signal_label = f"🟡 Golden cross but RSI OVERBOUGHT ({curr['rsi']:.1f}) — signal not confirmed"
            signal_color = YELLOW
        elif death and not high_vol:
            signal_label = f"🟡 Death cross but LOW VOLUME — signal not confirmed"
            signal_color = YELLOW
        elif death and not not_oversold:
            signal_label = f"🟡 Death cross but RSI OVERSOLD ({curr['rsi']:.1f}) — signal not confirmed"
            signal_color = YELLOW

    st.markdown(
        f"<div style='background:{signal_color}22; border-left:4px solid {signal_color};"
        f"padding:10px 16px; border-radius:6px; font-size:1.05em; font-weight:600;'>"
        f"{signal_label}</div>",
        unsafe_allow_html=True,
    )
    st.write("")

    # ── Four-panel chart ──────────────────────────────────────────────────────
    fig = make_subplots(
        rows=5, cols=1,
        shared_xaxes=True,
        row_heights=[0.42, 0.16, 0.15, 0.15, 0.12],
        vertical_spacing=0.035,
        subplot_titles=(
            "Price + SMAs, EMAs & VWAP",
            f"RSI ({rsi_period})",
            f"MACD ({macd_fast}/{macd_slow}/{macd_signal})",
            "Volume",
            f"ATR ({atr_period}) — Volatility",
        ),
    )

    ts = df["timestamp"] if "timestamp" in df.columns else df.index

    # — Candlesticks —
    fig.add_trace(go.Candlestick(
        x=ts, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name="Price",
        increasing_line_color=GREEN, decreasing_line_color=RED,
        increasing_fillcolor=GREEN, decreasing_fillcolor=RED,
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=ts, y=df["vwap"], name="VWAP",
        line=dict(color=YELLOW, width=2, dash="dot"),
    ), row=1, col=1)

    ema_palette = ["#b48ead", "#d08770", "#88c0d0", "#a3be8c", "#ebcb8b"]
    for i, period in enumerate(sorted(ema_periods)):
        col = f"ema_{period}"
        if col not in df.columns:
            continue
        fig.add_trace(go.Scatter(
            x=ts, y=df[col], name=f"EMA {period}",
            line=dict(color=ema_palette[i % len(ema_palette)],
                      width=1.6 if period >= 100 else 1.2),
        ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=ts, y=df["sma_fast"], name=f"Fast SMA ({sma_fast})",
        line=dict(color=BLUE, width=1.5),
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=ts, y=df["sma_slow"], name=f"Slow SMA ({sma_slow})",
        line=dict(color=YELLOW, width=1.5),
    ), row=1, col=1)

    # — RSI —
    fig.add_trace(go.Scatter(
        x=ts, y=df["rsi"], name="RSI",
        line=dict(color="#c084fc", width=1.5),
    ), row=2, col=1)

    # Overbought / oversold bands
    for level, label, color in [(70, "Overbought (70)", RED), (30, "Oversold (30)", GREEN)]:
        fig.add_hline(
            y=level, line_dash="dash", line_color=color,
            annotation_text=label,
            annotation_position="right",
            row=2, col=1,
        )

    # Shade RSI danger zones
    fig.add_hrect(y0=70, y1=100, fillcolor=RED,   opacity=0.07, row=2, col=1, line_width=0)
    fig.add_hrect(y0=0,  y1=30,  fillcolor=GREEN, opacity=0.07, row=2, col=1, line_width=0)

    # — MACD —
    hist_colors = [GREEN if h >= 0 else RED for h in df["macd_hist"].fillna(0)]
    fig.add_trace(go.Bar(
        x=ts, y=df["macd_hist"], name="Histogram",
        marker_color=hist_colors, showlegend=False, opacity=0.55,
    ), row=3, col=1)
    fig.add_trace(go.Scatter(
        x=ts, y=df["macd"], name="MACD",
        line=dict(color=BLUE, width=1.5),
    ), row=3, col=1)
    fig.add_trace(go.Scatter(
        x=ts, y=df["macd_signal"], name="Signal",
        line=dict(color=YELLOW, width=1.2),
    ), row=3, col=1)
    fig.add_hline(y=0, line_width=1, line_color=GREY, row=3, col=1)

    # — Volume (coloured by above/below average) —
    vol_colors = [BLUE if v > a else GREY
                  for v, a in zip(df["volume"], df["vol_sma"])]
    fig.add_trace(go.Bar(
        x=ts, y=df["volume"], name="Volume",
        marker_color=vol_colors, showlegend=False,
    ), row=4, col=1)
    fig.add_trace(go.Scatter(
        x=ts, y=df["vol_sma"], name=f"Vol SMA ({vol_sma_period})",
        line=dict(color=YELLOW, width=1.2, dash="dot"),
    ), row=4, col=1)

    # — ATR —
    fig.add_trace(go.Scatter(
        x=ts, y=df["atr"], name=f"ATR ({atr_period})",
        line=dict(color="#fb923c", width=1.5),
        fill="tozeroy", fillcolor="rgba(251,146,60,0.12)",
    ), row=5, col=1)

    fig.update_layout(
        height=920,
        xaxis_rangeslider_visible=False,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", y=1.02, x=0),
        margin=dict(t=60, b=20),
        hovermode="x unified",
    )
    fig.update_yaxes(showgrid=True, gridcolor="rgba(255,255,255,0.06)")
    fig.update_xaxes(showgrid=False)

    st.plotly_chart(fig, use_container_width=True)

    # — Latest indicator values —
    if not clean.empty:
        curr = clean.iloc[-1]
        st.subheader("📋 Current Indicator Snapshot")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Close Price",      f"${curr['close']:,.4f}")
        c2.metric(f"Fast SMA ({sma_fast})", f"${curr['sma_fast']:,.4f}")
        c3.metric(f"Slow SMA ({sma_slow})", f"${curr['sma_slow']:,.4f}")

        rsi_val = curr["rsi"]
        rsi_status = "🔴 Overbought" if rsi_val > 70 else ("🟢 Oversold" if rsi_val < 30 else "🟡 Neutral")
        c4.metric(f"RSI ({rsi_period})", f"{rsi_val:.1f}", delta=rsi_status, delta_color="off")

        atr_pct = curr["atr"] / curr["close"] * 100
        c5.metric(f"ATR ({atr_period})",
                  f"${curr['atr']:,.4f}",
                  delta=f"{atr_pct:.2f}% of price",
                  delta_color="off")

        d1, d2, d3, d4, d5 = st.columns(5)
        vwap_gap = (curr["close"] - curr["vwap"]) / curr["vwap"] * 100
        d1.metric("VWAP (today)", f"${curr['vwap']:,.4f}",
                  delta=f"{vwap_gap:+.2f}% vs price", delta_color="off")
        shown = [p for p in sorted(ema_periods) if f"ema_{p}" in clean.columns][:2]
        for col_box, period in zip((d2, d3), shown):
            col_box.metric(f"EMA {period}", f"${curr[f'ema_{period}']:,.4f}")
        d4.metric("MACD", f"{curr['macd']:,.4f}")
        hist = curr["macd_hist"]
        d5.metric("MACD Signal", f"{curr['macd_signal']:,.4f}",
                  delta=f"hist {hist:+.4f}",
                  delta_color="normal" if hist >= 0 else "inverse")

        st.caption(
            f"Periods are bar counts at the selected {bar_minutes}-minute resolution. "
            f"SMA({sma_slow}) spans {sma_slow * bar_minutes} minutes here. "
            f"VWAP resets each trading day."
        )

st.divider()

# ── Recent orders ─────────────────────────────────────────────────────────────

st.subheader("📋 Recent Orders")

try:
    orders = fetch_orders(limit=20)
    if not orders:
        st.info("No orders found yet.")
    else:
        order_rows = []
        for o in orders:
            filled_at = o.filled_at.strftime("%Y-%m-%d %H:%M") if o.filled_at else "—"
            order_rows.append({
                "Time":      filled_at,
                "Symbol":    o.symbol,
                "Side":      o.side.value.upper(),
                "Qty":       str(o.qty or o.notional),
                "Fill Price":f"${float(o.filled_avg_price):,.4f}" if o.filled_avg_price else "—",
                "Status":    o.status.value,
            })
        orders_df = pd.DataFrame(order_rows)

        def colour_side(val):
            return f"color: {GREEN}; font-weight:bold" if val == "BUY" else f"color: {RED}; font-weight:bold"

        st.dataframe(
            orders_df.style.applymap(colour_side, subset=["Side"]),
            use_container_width=True,
            hide_index=True,
        )
except Exception as e:
    st.error(f"Could not load orders: {e}")

st.divider()

# ── Bot log ───────────────────────────────────────────────────────────────────

st.subheader("🖥️ Bot Log  (most recent first)")

log_col, explain_col = st.columns([2, 1])

with log_col:
    log_lines = read_log()
    log_text  = "\n".join(log_lines)
    st.code(log_text, language=None)

with explain_col:
    st.markdown("""
**What the log messages mean:**

| Message | What it means |
|---|---|
| `── cycle ──` | Bot woke up and ran one check |
| `Portfolio: $X` | Current value and number of open trades |
| `Active signals` | Symbols where buy/sell conditions were met |
| `Executing: BUY` | Bot placed a buy order |
| `Executing: SELL` | Bot closed a position |
| `Risk block (exposure)` | Portfolio too full — buy skipped |
| `Risk block (duplicate)` | Already own this stock — buy skipped |
| `Not enough bars` | Not enough price history yet — waiting |
| `Golden cross but low volume` | Signal found but not confirmed — skipped |
| `RSI overbought` | Price ran up too fast — buy skipped |
""")

st.divider()

# ── Glossary ──────────────────────────────────────────────────────────────────

with st.expander("📚 Beginner's Glossary — click to expand"):
    st.markdown("""
| Term | Plain English |
|---|---|
| **SMA (Simple Moving Average)** | The average closing price over the last N hours. Smooths out noise. |
| **Golden Cross** | Short-term average rises above long-term average — trend may be turning up. |
| **Death Cross** | Short-term average falls below long-term average — trend may be turning down. |
| **RSI** | Measures how fast and how much a price has moved. High RSI = asset may be overpriced (overbought). Low RSI = may be underpriced (oversold). |
| **Volume** | How many shares/coins changed hands. High volume = strong conviction. Low volume = weak signal, often ignored. |
| **ATR (Average True Range)** | Measures how much an asset's price jumps around. High ATR = wild swings → bot uses a smaller position. |
| **Position** | Money currently invested in one stock or crypto. |
| **Notional** | Dollar value of a trade (e.g. "buy $500 worth of AAPL"). |
| **P&L** | Profit and Loss — how much you've made or lost. |
| **Paper trading** | Simulated trading with fake money. Safe to test strategies. |
| **Portfolio value** | Total value of everything: cash + all open positions. |
| **Exposure** | What percentage of your portfolio is currently invested. |
| **Drawdown** | How much the portfolio has fallen from its peak value. |
""")

st.caption("Dashboard auto-refreshes every 60 seconds. Click '🔄 Refresh data' in the sidebar for an instant update.")
