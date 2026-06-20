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
| `BaseStrategy` | Abstract base class — subclass to add strategies |
| `SMAcrossoverStrategy` | Default: SMA(10) / SMA(30) crossover |
| `RiskManager` | Position sizing, exposure caps |
| `OrderManager` | Signal → Alpaca market order |
| `TradingBot` | Main poll loop |

**Default watchlists**
- Stocks: AAPL, MSFT, NVDA, SPY, QQQ
- Crypto: BTC/USD, ETH/USD, SOL/USD

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
