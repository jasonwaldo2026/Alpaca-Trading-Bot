# Alpaca AI Trading Bot

Paper-trades US equities and crypto using Alpaca's API. Designed to be extended by **Jules** (Google's AI coding agent).

---

## Quick start

### 1. Get Alpaca paper trading keys
Log into [app.alpaca.markets](https://app.alpaca.markets) → **Settings** → **API Keys** → create a **Paper** key pair.

### 2. Set up the project
```bash
pip install -r requirements.txt
cp .env.example .env   # then paste your keys into .env
```

### 3. Run
```bash
python trading_bot.py
```

The bot polls every 60 seconds. It fetches hourly bars, runs the SMA crossover strategy, checks risk limits, and executes market orders — all in paper mode.

---

## Architecture

| Component | Role |
|---|---|
| `BotConfig` | All settings and credentials |
| `AlpacaClient` | Authenticated alpaca-py wrapper |
| `MarketDataFetcher` | Hourly OHLCV bars — stocks + crypto |
| `AlpacaMarketScanner` | Ranks a broad universe → shortlist (`scanner.py`) |
| `BaseStrategy` | Abstract base class — subclass to add strategies |
| `EnhancedSMAStrategy` | Default: SMA crossover + RSI + volume + ATR sizing |
| `RiskManager` | Position sizing, exposure caps |
| `OrderManager` | Signal → Alpaca market order |
| `TradingBot` | Main poll loop |

---

## Market scanner

The bot no longer trades a fixed watchlist. Each refresh the scanner ranks a
broad universe (~70 liquid stocks + 8 crypto pairs by default) and hands the
top candidates to the strategy.

**The scanner narrows; the strategy decides.** A high scanner score is not a
buy signal — it only means the symbol earned a place on the shortlist that the
SMA + RSI + volume rules then evaluate. Every entry is still explainable from
the indicator rules alone, which keeps the system deterministic and
backtestable.

```
universe → hard filters → percentile ranking → shortlist
                                                   ↓
                              + currently-held positions
                                                   ↓
                                    strategy → risk → order
```

Held positions are always re-appended to the shortlist. If a symbol rotates
out of the scan while you still own it, dropping it would mean its SELL signal
never fires and the position is stranded with no exit.

### Scoring

Each metric is percentile-ranked across the surviving universe, then combined
with configurable weights — so the score is relative to today's market rather
than to fixed thresholds, and adapts to quiet and volatile regimes alike.

| Metric | Default weight | Meaning |
|---|---|---|
| Volume surge | 0.30 | Latest volume ÷ 20-bar average |
| Momentum | 0.40 | % price change over the lookback window |
| Volatility | 0.15 | ATR as % of price |
| Trend | 0.15 | Price above the slow SMA |

Hard filters run **before** scoring, and a symbol failing any of them is
dropped outright rather than merely ranked low — junk can never place into the
shortlist just because the rest of the universe looks worse:

- `scan_min_price` (default $5) — skip penny stocks
- `scan_min_dollar_volume` (default $1M) — skip illiquid names
- `scan_max_atr_pct` (default 15%) — skip anything wildly volatile
- `scan_require_uptrend` (default off) — set `True` for long-only

### Connecting your own scanner app

`external_scanner.py` adapts a scanner living outside this repo. Pick the
adapter matching how yours exposes results — no rewrite needed:

```python
from external_scanner import FileScanner, CallableScanner, HttpScanner

# It writes a JSON or CSV file (works across machines via a synced folder)
scanner = FileScanner("~/scanner/out/latest.json")

# It's importable Python on the same machine
from my_scanner import run_scan
scanner = CallableScanner(run_scan)

# It serves JSON over HTTP (LAN, tunnel, anywhere reachable)
scanner = HttpScanner("http://localhost:8000/scan")

bot = TradingBot(config, strategy=EnhancedSMAStrategy(), scanner=scanner)
```

**Field mapping** is automatic for common names — `symbol`/`ticker`,
`score`/`rank`/`rating`, `price`/`last`/`close`, `reason`/`why`. Only a symbol
is required. Anything unusual can be named explicitly:

```python
FileScanner("out.json", fields={"symbol": ("instrument",), "score": ("edge",)})
```

Accepted shapes: a list of records, a wrapper object with the list under
`results`/`symbols`/`candidates`/`data`, a plain list of ticker strings, or a
pandas DataFrame.

**Where things run.** The bot calls the scanner every cycle, so a scanner that
only exists on your desktop means the bot must run there too. `FileScanner`
pointed at a synced folder is the way to decouple them; `HttpScanner` works if
the machine is reachable.

**Safety.** An external scanner is an input the bot trades on, so it is
treated as untrusted:

- **Staleness** — results older than `max_age_minutes` (default 120) are
  refused. A scanner that silently stopped updating would otherwise have the
  bot trading yesterday's shortlist indefinitely.
- **Size cap** — `max_symbols` (default 200) bounds a malformed or runaway file.
- **Per-record isolation** — one bad row is skipped, not fatal.
- **Fails closed** — an unreachable scanner yields an empty shortlist and the
  bot simply trades nothing new. No data must never imply "buy anything". Set
  `fail_open=True` to reuse the last good shortlist instead.

None of this lets the external scanner place a trade. It proposes what to look
at; entries still require the strategy to fire and the risk manager to approve.

### Plugging in something custom

Subclass `BaseScanner` directly:

```python
from scanner import BaseScanner, ScanResult

class MyScanner(BaseScanner):
    needs_bars = False   # True if you use the bars passed in

    def scan(self, stock_bars, crypto_bars, config) -> list[ScanResult]:
        ...
```

To disable scanning entirely and go back to the fixed watchlists, set
`use_scanner=False` in `BotConfig`.

### Tests

```bash
python test_scanner.py           # ranking, filters, held-position logic
python test_external_scanner.py  # adapters, field mapping, staleness guard
```

Both use synthetic data and temp files — no API calls, no network.

---

## Integrating Jules

[Jules](https://jules.google.com) is Google's AI coding agent. You give it a GitHub repo and GitHub issues — it writes the code and opens PRs.

### Setup
1. Push this repo to GitHub
2. Go to [jules.google.com](https://jules.google.com) → connect your repo
3. Create issues using the prompts below — Jules handles the rest

### Ready-made Jules issues

**Add an RSI strategy**
```
Add an RSIStrategy class to trading_bot.py that subclasses BaseStrategy.
- Use RSI(14) computed from hourly close prices
- BUY signal when RSI crosses below 30 (oversold)
- SELL signal when RSI crosses above 70 (overbought)
- Add rsi_period: int = 14 to BotConfig
- Make it selectable via a strategy_type field in BotConfig
```

**Add a drawdown circuit breaker**
```
Add a daily drawdown guard to RiskManager.evaluate() in trading_bot.py.
- Record portfolio value at the start of each trading day
- Block all BUY signals if daily PnL < -3% (make threshold configurable)
- Log a WARNING when the breaker trips
- Reset at midnight UTC
```

**Add Slack trade alerts**
```
Add Slack notifications to TradingBot.run_once() in trading_bot.py.
- Send a message when a BUY or SELL order executes
- Include: symbol, action, notional value, reason, timestamp
- Read SLACK_WEBHOOK_URL from environment variables
- Fail silently (log error, don't crash the bot) if webhook fails
```

**Add a portfolio status endpoint**
```
Add a FastAPI REST API to trading_bot.py that runs alongside the bot.
- GET /positions → current open positions as JSON
- GET /account  → portfolio value and buying power
- Run the API server in a background thread (uvicorn)
- Add fastapi and uvicorn to requirements.txt
```

**Add backtesting mode**
```
Add a backtest() method to TradingBot in trading_bot.py.
- Accept start_date and end_date parameters
- Fetch historical hourly bars for that range
- Run the strategy on each bar in sequence (no lookahead)
- Track simulated PnL, win rate, and max drawdown
- Print a summary report at the end
```

---

## Configuration reference

```python
BotConfig(
    paper=True,                    # paper mode — no real money
    # Scanner
    use_scanner=True,              # False → use the fixed watchlists below
    scan_top_n=8,                  # shortlist size handed to the strategy
    scan_refresh_cycles=15,        # rescan every N cycles (bars are hourly)
    scan_min_price=5.0,            # hard filter: skip penny stocks
    scan_min_dollar_volume=1e6,    # hard filter: skip illiquid names
    scan_max_atr_pct=15.0,         # hard filter: skip wild volatility
    scan_require_uptrend=False,    # True → long-only, above slow SMA
    scan_momentum_lookback=12,     # bars for the momentum metric
    # Fallback watchlists (used when use_scanner=False)
    stock_symbols=["AAPL", ...],   # US equities to watch
    crypto_symbols=["BTC/USD",...],# crypto pairs (format: "XXX/USD")
    max_position_pct=0.05,         # 5% max per position
    max_total_exposure=0.80,       # 80% max portfolio allocation
    stop_loss_pct=0.03,            # 3% stop loss
    sma_fast=10,                   # fast SMA period (bars)
    sma_slow=30,                   # slow SMA period (bars)
    bar_limit=50,                  # bars to fetch per symbol
    poll_interval_seconds=60,      # polling frequency
)
```

---

## Disclaimer

This bot is for **paper trading and educational purposes only**. SMA crossover is a simple baseline strategy — not a recommendation. Do not trade real money without fully understanding the risks.
