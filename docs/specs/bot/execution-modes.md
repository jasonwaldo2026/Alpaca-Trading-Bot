# Execution modes and the path to trading

**Status:** done — `BotConfig.execution_mode`
**Touches:** `bot/config.py`, `bot/runner.py`, `trading_bot.py`

## Why

The intended sequence is: watch the alerts, trade by hand, learn which
setups are worth acting on, and only then let the bot execute. That requires
a system where "compute signals" and "place orders" are separate switches,
not separate commands.

Before this, the boundary was *which file you ran*: Studio and Scanner never
order, `trading_bot.py` always did. That is thin protection for a system
whose author does not yet want it trading.

## Modes

| Mode | Signals | Risk checks | Orders |
|---|---|---|---|
| `monitor` (**default**) | computed | run | **never sent** |
| `paper` | computed | run | Alpaca paper orders |

Monitor is the default, so running the bot cannot trade by accident. Every
step up to the order still happens — including risk sizing — so the alert
reports what *would* have been traded, at the size it would have been. A
signal blocked by exposure limits produces no alert, because it would not
have produced a trade.

Alerts log at WARNING with an `ALERT [monitor]` prefix, so they stand out in
a quiet log and are trivial to grep.

There is deliberately no `live` mode. `paper=True` is a repo invariant and a
test asserts `paper=False` appears nowhere.

## Promotion criteria

"Switch it on when it is perfected" is not a criterion — nothing reaches
perfect, and the temptation is to promote after a good week. Decide the bar
in advance, while nothing is at stake. A reasonable set:

- [ ] 50+ alerts reviewed, with the outcome recorded for each
- [ ] You can predict the outcome before it resolves more often than not
- [ ] Backtest results and hand-traded results broadly agree — if they
      disagree, the backtest is modelling something you are not doing
- [ ] A maximum daily loss is defined *and enforced in code*
      (`docs/specs/bot/drawdown-circuit-breaker.md`)
- [ ] You have seen the strategy lose and know why it lost

The circuit breaker is deliberately on this list: switching to `paper`
without a loss limit means the first bad day is unbounded.

## What monitor mode does not protect against

- `MarketDataFetcher` still calls Alpaca for data. Monitor mode is not
  offline mode.
- Nothing stops someone editing the config. It is a safe default, not a
  lock.
- Risk checks run against *live* account state, so an account with existing
  positions changes which alerts appear.
