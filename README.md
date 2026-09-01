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
  client.py           Credentials + AlpacaClient
  data.py             MarketDataFetcher, bar-shape normalization
  indicators.py       SMA / RSI / ATR — single source of truth
  universe.py         symbol lists, stock-vs-crypto routing
  rules.py            scan rule model (Studio writes it, Scanner reads it)

bot/                  config, strategies, risk, orders, poll loop
dashboard.py          Streamlit account + chart view
scanner/              engine.py (pure logic) + cli.py
studio/               app.py — rule builder
rules/                saved scan rules (JSON)
docs/specs/           one file per planned feature
tests/                pytest, no network required
```

---

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
