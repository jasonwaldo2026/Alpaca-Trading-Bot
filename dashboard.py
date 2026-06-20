"""
Trading Bot Dashboard
=====================
Run with:  streamlit run dashboard.py

Shows live account data, open positions, indicator charts, and the bot log.
All data comes directly from Alpaca — no bot process needs to be running.
"""

import os
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
    from alpaca.trading.client import TradingClient
    from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame
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
def get_clients():
    key    = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_API_SECRET", "")
    if not key or not secret:
        return None, None, None
    trading     = TradingClient(key, secret, paper=True)
    stock_data  = StockHistoricalDataClient(key, secret)
    crypto_data = CryptoHistoricalDataClient(key, secret)
    return trading, stock_data, crypto_data

trading_client, stock_data_client, crypto_data_client = get_clients()

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
def fetch_bars(symbol: str, is_crypto: bool, bar_limit: int = 100):
    try:
        if is_crypto:
            req  = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Hour, limit=bar_limit)
            bars = crypto_data_client.get_crypto_bars(req)
        else:
            req  = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Hour, limit=bar_limit)
            bars = stock_data_client.get_stock_bars(req)
        df = bars.df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        return df.reset_index()
    except Exception as e:
        st.warning(f"Could not fetch bars for {symbol}: {e}")
        return pd.DataFrame()

def compute_indicators(df: pd.DataFrame, sma_fast=10, sma_slow=30,
                        rsi_period=14, vol_sma_period=20, atr_period=14) -> pd.DataFrame:
    df = df.copy()
    df["sma_fast"] = df["close"].rolling(sma_fast).mean()
    df["sma_slow"] = df["close"].rolling(sma_slow).mean()

    # RSI
    delta    = df["close"].diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=rsi_period - 1, min_periods=rsi_period).mean()
    avg_loss = loss.ewm(com=rsi_period - 1, min_periods=rsi_period).mean()
    rs       = avg_gain / avg_loss.replace(0, float("nan"))
    df["rsi"] = 100 - (100 / (1 + rs))

    # Volume SMA
    df["vol_sma"] = df["volume"].rolling(vol_sma_period).mean()

    # ATR
    prev_close  = df["close"].shift()
    tr          = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"]   = tr.ewm(com=atr_period - 1, min_periods=atr_period).mean()

    return df

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

    all_symbols = {
        "AAPL":    False,
        "MSFT":    False,
        "NVDA":    False,
        "SPY":     False,
        "QQQ":     False,
        "BTC/USD": True,
        "ETH/USD": True,
        "SOL/USD": True,
    }

    selected_symbol = st.selectbox(
        "Inspect symbol",
        list(all_symbols.keys()),
        index=0,
    )
    is_crypto = all_symbols[selected_symbol]

    st.divider()
    st.subheader("Indicator settings")
    sma_fast       = st.slider("Fast SMA period",   5,  50, 10)
    sma_slow       = st.slider("Slow SMA period",  10, 100, 30)
    rsi_period     = st.slider("RSI period",        5,  30, 14)
    vol_sma_period = st.slider("Volume SMA period", 5,  50, 20)
    atr_period     = st.slider("ATR period",        5,  30, 14)
    bar_limit      = st.slider("Bars to load",     60, 500, 120)

    st.divider()
    if st.button("🔄 Refresh data"):
        st.cache_data.clear()
        st.rerun()

    st.caption("Data refreshes automatically every 60 s.")

# ── Guard: credentials ────────────────────────────────────────────────────────

if trading_client is None:
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

df_raw = fetch_bars(selected_symbol, is_crypto, bar_limit)

if df_raw.empty:
    st.warning(f"No bar data returned for {selected_symbol}.")
else:
    df = compute_indicators(df_raw, sma_fast, sma_slow, rsi_period, vol_sma_period, atr_period)

    # Determine current signal
    clean = df.dropna(subset=["sma_fast", "sma_slow", "rsi", "vol_sma", "atr"])
    signal_label = "⬜ HOLD — waiting for a confirmed signal"
    signal_color = GREY
    if len(clean) >= 2:
        prev, curr = clean.iloc[-2], clean.iloc[-1]
        golden = prev["sma_fast"] <= prev["sma_slow"] and curr["sma_fast"] > curr["sma_slow"]
        death  = prev["sma_fast"] >= prev["sma_slow"] and curr["sma_fast"] < curr["sma_slow"]
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
        rows=4, cols=1,
        shared_xaxes=True,
        row_heights=[0.50, 0.18, 0.18, 0.14],
        vertical_spacing=0.04,
        subplot_titles=("Price + SMAs", "RSI (14)", "Volume", "ATR — Volatility"),
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

    # — Volume (coloured by above/below average) —
    vol_colors = [BLUE if v > a else GREY
                  for v, a in zip(df["volume"], df["vol_sma"])]
    fig.add_trace(go.Bar(
        x=ts, y=df["volume"], name="Volume",
        marker_color=vol_colors, showlegend=False,
    ), row=3, col=1)
    fig.add_trace(go.Scatter(
        x=ts, y=df["vol_sma"], name=f"Vol SMA ({vol_sma_period})",
        line=dict(color=YELLOW, width=1.2, dash="dot"),
    ), row=3, col=1)

    # — ATR —
    fig.add_trace(go.Scatter(
        x=ts, y=df["atr"], name=f"ATR ({atr_period})",
        line=dict(color="#fb923c", width=1.5),
        fill="tozeroy", fillcolor="rgba(251,146,60,0.12)",
    ), row=4, col=1)

    fig.update_layout(
        height=780,
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
