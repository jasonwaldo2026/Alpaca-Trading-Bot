# Alpaca Trading Platform

Four apps over one shared core. **Paper trading and educational use only.**

| App | What it does | Run |
|---|---|---|
| **Bot** | Trades a watchlist on SMA + RSI + volume confirmation, with ATR-scaled sizing | `python trading_bot.py` |
| **Dashboard** | Read-only view of account, positions, orders, and indicator charts | `streamlit run dashboard.py` |
| **Scanner** | Runs saved rules across a symbol universe and reports matches | `python -m scanner.cli` |
| **Studio** | Visual rule builder — authors the rules the Scanner runs | `streamlit run studio/app.py` |

All four share `core/`, which owns credentials, bar fetching, indicator math,
and the scan-rule model. Indicator math exists in exactly one place, so the
chart, the signal, and the scan can never disagree.

---

## Quick start

### 1. Get Alpaca paper trading keys
[app.alpaca.markets](https://app.alpaca.markets) → **Settings** → **API Keys**
→ create a **Paper** key pair.

### 2. Set up
```bash
pip install -r requirements.txt
cp .env.example .env   # paste your keys in
```

### 3. Run whichever app you want
```bash
python trading_bot.py            # the bot
streamlit run dashboard.py       # the dashboard
python -m scanner.cli            # scan with rules/*.json
streamlit run studio/app.py      # build a new rule
```

---

## Layout

```
core/                 shared by everything — no app imports allowed
  client.py           Credentials + AlpacaClient (incl. extended-hours orders)
  data.py             MarketDataFetcher, bar-shape normalization
  indicators.py       SMA / RSI / ATR — single source of truth
  universe.py         symbol lists, stock-vs-crypto routing
  rules.py            scan rule model (Studio writes it, Scanner reads it)
  sessions.py         market sessions, scan cadence, horizon conversion
  persistence.py      run lengths — "true for N bars in a row"
  backtest.py         lookahead-free replay with dual-timeframe exits

bot/                  config, strategies, risk, orders, poll loop
dashboard.py          Streamlit account + chart view
scanner/              engine.py (pure logic) + cli.py
studio/               app.py — rule builder
rules/                saved scan rules (JSON)
docs/specs/           one file per planned feature
tests/                pytest, no network required
```

---

## Alerts

Each scenario is one JSON file in `rules/` carrying **both** what to look for
and what to say. Adding something you want to be told about is adding a
file — Studio writes them, with a live phone preview.

```json
{
  "name": "vwap hold",
  "conditions": [{"field": "close", "op": ">", "field2": "vwap", "for_bars": 3}],
  "alert": {
    "title": "Strong above VWAP",
    "message": "{symbol} stock is strong and trading above VWAP\n\nPrice ${price}  •  VWAP ${vwap}\nSuggested limit: ${limit_price}"
  }
}
```

Placeholders: `{symbol}`, `{price}`, `{limit_price}`, `{rule}`, `{session}`,
`{time}`, plus every indicator column the rule produces (`vwap`, `rsi`,
`roc`, `rvol`, `ema_9`, `macd_hist`, …). A typo is caught when the scenario
is saved, not when the alert should have fired.

```bash
export PUSHOVER_TOKEN=... PUSHOVER_USER=...
python -m scanner.cli --watch --extended-hours
```

Missing credentials disable alerting without crashing. A setup that stays
true is **one** alert, not one per scan — a lapsed setup that returns alerts
again, because that is new. State survives restarts.

**The notification link opens Robinhood to the stock page.** It cannot open
a pre-filled order ticket: Robinhood publishes no deep link for order entry,
and its third-party policy is explicit that outside apps cannot act in the
app. From the stock page it is Trade → Buy → Limit. The suggested limit
price is in the message text instead, and `link_template` is configurable.

### Shipped scenarios

| File | Looks for |
|---|---|
| `vwap-hold.json` | Close above VWAP for 3 consecutive 5-minute bars |
| `vwap-hold-confirmed.json` | The same, plus volume above baseline and EMA9 > EMA12 |
| `momentum-runner.json` | Up ≥10% in 10 minutes, ≥3x relative volume, above VWAP |

`momentum-runner` covers two of the four momentum criteria. **Low float and
"spiked in the last 12 months" are not checked** — float is fundamental data
Alpaca does not serve, and the prior spike needs daily bars as a universe
filter. The alert message says so. See
`docs/specs/core/missing-fundamentals.md`.

## Scanner and Studio

Studio and Scanner are two halves of one workflow:

1. **Studio** — build a rule visually: pick fields (`rsi`, `volume`,
   `sma_fast`, …), operators (`<`, `>`, `crosses_above`, …), and either a
   literal threshold or another field to compare against. Preview it live
   against the market, then save.
2. **Scanner** — reads those saved JSON files and sweeps a universe.

The saved file *is* the contract:

```json
{
  "name": "oversold bounce",
  "universe": "sp500_liquid",
  "conditions": [
    {"field": "rsi", "op": "<", "value": 35},
    {"field": "volume", "op": ">", "field2": "vol_sma"},
    {"field": "sma_fast", "op": "crosses_above", "field2": "sma_slow"}
  ],
  "params": {"sma_fast": 10, "sma_slow": 30, "rsi_period": 14,
             "volume_sma_period": 20, "atr_period": 14}
}
```

Indicator periods travel *with* the rule, so a scan reproduces exactly what
you previewed in Studio.

```bash
python -m scanner.cli                       # every rule in rules/
python -m scanner.cli rules/oversold-bounce.json
python -m scanner.cli --symbols AAPL,MSFT   # override the universe
```

---

## Market sessions and extended hours

**The default is 5-minute bars across the full extended session, 04:00–20:00
ET** — 192 scans a day, first at 04:06. (The 04:00–04:05 bar does not exist
until 04:05, so 04:06 is the earliest actionable scan.)

To narrow it:

```python
from core.sessions import SessionConfig

BotConfig(
    sessions=SessionConfig.regular_only(),  # 09:30–16:00 ET
    # or SessionConfig.after_hours(),       # 09:30–20:00 ET
    # default is SessionConfig.extended()   # 04:00–20:00 ET
)
```

### Why pre-market data looks stale

Alpaca builds bars from trades, so **a 5-minute window with no trades
produces no bar at all** — not an empty bar, a missing one. Pre-market
series are therefore sparse, and a rolling average over them reaches back
much further in clock time than its period suggests.

The usual amplifier is the data feed. `BotConfig.data_feed` is unset by
default, which uses your account's feed — on free and basic plans that is
**IEX**, one venue with a small share of consolidated volume, and very thin
before the open. If your plan includes the consolidated tape:

```python
from alpaca.data.enums import DataFeed
BotConfig(data_feed=DataFeed.SIP)      # or:  scanner.cli --feed sip
```

The scanner reports symbols whose bars came back sparse
(`ScanResult.sparse`), so thin data is visible rather than mistaken for a
stale bot.

Turning this on changes three things automatically:

- **Orders** switch to whole-share marketable limit orders with
  `extended_hours=True`. Alpaca rejects market orders and fractional
  quantities outside regular hours — a market order sent then is queued to
  the next open and fills at an unknown price.
- **Volume baselines** become session-relative, so `volume > vol_sma`
  compares an after-hours bar against after-hours volume rather than against
  a regular-hours average it could never exceed.
- **Cadence** changes: 7 hourly bars/day regular hours, 11 with after-hours,
  16 fully extended, 24 for crypto.

| Session | Hours (ET) | Hourly bars |
|---|---|---|
| Pre-market | 04:00 – 09:30 | 6 |
| Regular | 09:30 – 16:00 | 7 |
| After-hours | 16:00 – 20:00 | 4 |
| Crypto | 24/7 | 24 |

Holidays and early closes are not hardcoded; supply them from Alpaca's
calendar endpoint via `SessionCalendar`.

## Bar size

Hourly by default, but any size that divides evenly into a day works:

```python
BotConfig(bar_minutes=5)              # bot
Scanner(fetcher, bar_minutes=5)       # scanner
python -m scanner.cli --bar-minutes 5 # CLI
```

| Bar size | Regular hours | Extended | Crypto |
|---|---|---|---|
| 1 hour | 7 | 16 | 24 |
| 15 min | 26 | 64 | 96 |
| 5 min | 78 | 192 | 288 |

**Changing bar size changes what your indicator periods mean.** `sma_slow=30`
spans 30 hours of hourly bars but only 150 minutes of 5-minute bars. Both
readings are legitimate, so the choice is explicit:

```python
BotConfig(bar_minutes=5)                             # 12/26/9 five-minute
                                                     # bars — what "MACD on
                                                     # the 5-min chart" means

BotConfig(bar_minutes=5, indicator_period_basis=60,  # same wall-clock
          bar_limit=600)                             # lookback as hourly
```

The second rescales every period by `60 / 5`, so `sma_slow=30` becomes 360 —
still 30 hours. It needs far more history, which is why `bar_limit` goes up;
`BotConfig.validate()` raises with the exact number if it is too small,
rather than letting the bot run with all-NaN indicators and never trade.

## Indicators

All computed in `core/indicators.py` — SMA, EMA, RSI, ATR, MACD, VWAP, and a
volume baseline.

| Indicator | Notes |
|---|---|
| `sma_fast` / `sma_slow` | Simple moving averages |
| `ema_9` / `ema_12` / `ema_200` | One column per period in `ema_periods`; `adjust=False` recursion, matching charting platforms |
| `rsi` | Wilder's smoothing; NaN rather than 100 when there are no losses |
| `atr` | EWM true range, captures overnight gaps |
| `macd` / `macd_signal` / `macd_hist` | Signal is an EMA **of the MACD line**, so it warms up `slow + signal` bars in |
| `vwap` | **Resets each trading day.** Pass `session_day_series()` as the anchor |
| `vol_sma` | Per-session baseline when extended hours are on |

VWAP's anchor groups pre-market, regular, and after-hours bars of the same
date together — they are one trading day, and resetting at 09:30 would throw
away the pre-market volume that often sets the day's context. Crypto anchors
on the UTC day.

## Closed bars only

Alpaca includes the currently-forming bar in a `limit=N` request.
`MarketDataFetcher` drops it, so strategies only ever see completed bars.
Without this, a crossover can appear mid-bar, fire an order, and reverse
before the bar closes — a trade on a signal that never existed on the
finished series.

Scan once per *completed* bar, shortly after it closes —
`core.sessions.scan_times()` generates the schedule (10:02 → 16:02 ET hourly;
09:36 → 16:01 at 5 minutes).

Full detail: `docs/specs/core/market-sessions.md`.

---

## Strategies and backtesting

Build a rule in Studio, then backtest it there before it trades anything.
Conditions support persistence, so "price has held above VWAP" is one
condition rather than a crossing:

```json
{"field": "close", "op": ">", "field2": "vwap", "for_bars": 3}
```

Studio's backtest panel replays the rule over history with no lookahead —
entries fill at the next bar's open, and exits are managed on a finer
timeframe (1-minute) so an adverse move is caught within a minute rather
than up to five. The exit is a close below an EMA, with an optional ATR stop
underneath as a floor against gaps.

**The management timeframe changes what the exit means.** EMA(9) spans 9
minutes on 1-minute bars and 45 minutes on 5-minute bars — a materially
tighter exit, not merely a faster one. Studio states the span for whatever
you pick.

Results are signal quality only: no commission, no slippage, one position at
a time. The ATR stop assumes a fill at its trigger price, which a real gap
would beat. Treat the return as an upper bound.

See `docs/specs/studio/vwap-trend-strategy.md`.

---

## Monitor mode

**The bot does not trade by default.** `BotConfig.execution_mode` is
`"monitor"`: signals are computed, risk-checked, and logged — no order is
sent.

```
WARNING ALERT [monitor] BUY AAPL @ $184.2100  (would size $4,812.50)
        — Held above VWAP for 3 bars (+0.31% vs VWAP)
```

Risk checks still run, so the alert reports what would actually have been
traded, at the size it would have been. Switch to `execution_mode="paper"`
to place Alpaca paper orders — a deliberate change, not a default.

See `docs/specs/bot/execution-modes.md`, which also lists promotion criteria
worth deciding before flipping that switch.

## Bot strategy

**EnhancedSMAStrategy** (default) needs three confirmations to fire:

- **BUY** — SMA golden cross **and** RSI < `rsi_overbought` **and** volume > 20-bar average
- **SELL** — SMA death cross **and** RSI > `rsi_oversold` **and** volume > 20-bar average

Position sizing is ATR-scaled, so volatile assets get smaller positions:

```
dollar_risk = portfolio × risk_per_trade_pct     (default 1%)
stop_dist   = ATR × atr_risk_multiplier          (default 1.5×)
notional    = (dollar_risk / stop_dist) × price  capped at max_position_pct
```

**VwapTrendStrategy** is the strategy currently under evaluation, and the
default in `trading_bot.py`:

- **Entry** — close above VWAP for three consecutive 5-minute bars
- **Exit** — close below the 9 EMA, checked on **1-minute** bars so an
  adverse move is caught within a minute rather than up to five
- **Floor** — ATR stop beneath the entry, against gaps

It takes the same `Rule` and `ExitPolicy` objects the Studio backtest
measures, so live behavior and backtested behavior cannot drift apart.

`SMAcrossoverStrategy` (SMA only) is kept for A/B comparison:
```python
TradingBot(config, strategy=SMAcrossoverStrategy()).run()
```

### Configuration
```python
BotConfig(
    paper=True,                     # paper mode — no real money
    stock_symbols=["AAPL", ...],
    crypto_symbols=["BTC/USD", ...],
    max_position_pct=0.05,          # 5% max per position
    max_total_exposure=0.80,        # 80% max portfolio allocation
    risk_per_trade_pct=0.01,        # ATR sizing: risk 1% per trade
    atr_risk_multiplier=1.5,
    sma_fast=10, sma_slow=30,
    rsi_period=14, rsi_overbought=70.0, rsi_oversold=30.0,
    volume_sma_period=20,
    atr_period=14,
    bar_limit=60,
    poll_interval_seconds=60,
)
```

---

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

Tests need no credentials and make no network calls. Alongside the unit
tests, `tests/test_architecture.py` enforces the structural rules: `core/`
never imports an app, no app re-implements indicator math, and nothing sets
`paper=False`.

Planned features live in `docs/specs/` — one markdown file per feature, with
acceptance criteria. Write the spec first; it survives where a chat message
does not.

See `CLAUDE.md` for the conventions and invariants that apply when working
in this repo.

---

## Disclaimer

For **paper trading and educational purposes only**. These strategies are
simple baselines, not recommendations. Do not trade real money without fully
understanding the risks.
