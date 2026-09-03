# Daily drawdown circuit breaker

**Status:** proposed
**Touches:** `bot/risk.py`, `bot/config.py`

## Why
`RiskManager` caps per-position size and total exposure, but nothing stops
the bot from trading through a bad day. A sequence of losing entries can
compound without any rule interrupting it.

## What
- Record portfolio value at the start of each trading day.
- Block all BUY signals once daily PnL falls below a configurable threshold
  (`max_daily_drawdown_pct`, default 3%). SELLs still pass — the breaker
  must never trap an open position.
- Log a WARNING once when it trips, not once per blocked signal.
- Reset at midnight UTC.

## Done when
- [ ] BUY blocked, SELL allowed, once the threshold is crossed
- [ ] Warning logged exactly once per trip
- [ ] Resets across a UTC day boundary
- [ ] Threshold configurable on `BotConfig`
- [ ] Tests drive it with an injected clock and synthetic portfolio values

## Explicitly out of scope
Liquidating positions when the breaker trips. Stopping new risk is the
feature; forced exits are a different and much riskier decision.
