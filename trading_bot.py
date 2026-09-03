"""
Alpaca AI Trading Bot — entry point.

The implementation now lives in the `bot/` package, on top of the shared
`core/` package:

  core.client      → Credentials + AlpacaClient      (shared with dashboard/scanner/studio)
  core.data        → MarketDataFetcher               (shared)
  core.indicators  → SMA / RSI / ATR                 (shared — single source of truth)
  bot.config       → BotConfig
  bot.strategies   → BaseStrategy, EnhancedSMAStrategy, SMAcrossoverStrategy, Signal
  bot.risk         → RiskManager
  bot.orders       → OrderManager
  bot.runner       → TradingBot poll loop

Run with:  python trading_bot.py

Signal confirmation logic (EnhancedSMAStrategy):
  BUY  = SMA golden cross  AND  RSI < rsi_overbought  AND  volume > 20-bar avg
  SELL = SMA death cross   AND  RSI > rsi_oversold    AND  volume > 20-bar avg

ATR position sizing:
  dollar_risk  = portfolio × risk_per_trade_pct   (default 1%)
  stop_dist    = ATR × atr_risk_multiplier         (default 1.5×)
  notional     = (dollar_risk / stop_dist) × price, capped at max_position_pct
"""

import logging

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log"),
    ],
)

# Re-exported so existing imports (`from trading_bot import BotConfig`) keep working.
from bot.config import BotConfig                                       # noqa: E402
from bot.orders import OrderManager                                    # noqa: E402
from bot.risk import RiskManager                                       # noqa: E402
from bot.runner import TradingBot                                      # noqa: E402
from bot.strategies import (                                           # noqa: E402
    BaseStrategy,
    EnhancedSMAStrategy,
    Signal,
    SMAcrossoverStrategy,
    VwapTrendStrategy,
    vwap_hold_rule,
)
from core.client import AlpacaClient, Credentials                      # noqa: E402
from core.data import MarketDataFetcher                                # noqa: E402
from core.sessions import SessionConfig                                # noqa: E402

__all__ = [
    "AlpacaClient", "BaseStrategy", "BotConfig", "Credentials",
    "EnhancedSMAStrategy", "MarketDataFetcher", "OrderManager",
    "RiskManager", "SessionConfig", "Signal", "SMAcrossoverStrategy",
    "TradingBot", "VwapTrendStrategy", "vwap_hold_rule",
]


if __name__ == "__main__":
    config = BotConfig(
        paper=True,

        # MONITOR reports signals and places no orders — the mode for
        # learning which alerts are worth acting on. Change to "paper" only
        # deliberately; see docs/specs/bot/execution-modes.md.
        execution_mode="monitor",
        stock_symbols=["AAPL", "MSFT", "NVDA", "SPY", "QQQ"],
        crypto_symbols=["BTC/USD", "ETH/USD", "SOL/USD"],

        # 5-minute bars across the full extended session (04:00-20:00 ET).
        # Periods below are bar counts at this resolution, which is what
        # "EMA 9 on the 5-minute chart" means to a trader.
        bar_minutes=5,             # entry chart
        manage_bar_minutes=1,      # exits checked on 1-minute bars
        sessions=SessionConfig.extended(),
        bar_limit=300,             # EMA(200) needs 202; 300 leaves headroom

        # Set to DataFeed.SIP if your Alpaca plan includes the consolidated
        # tape. The default (IEX) is thin in pre-market — many 5-minute
        # windows contain no trades and so produce no bar at all.
        data_feed=None,

        # SMA
        sma_fast=10,
        sma_slow=30,
        # EMA — 4 and 12 are drawn, 9 drives the exit rule, 200 the trend
        ema_periods=(4, 9, 12, 200),
        # MACD
        macd_fast=12,
        macd_slow=26,
        macd_signal=9,
        # RSI confirmation
        rsi_period=14,
        rsi_overbought=70.0,
        rsi_oversold=30.0,
        # Volume confirmation (per session when extended hours are on)
        volume_sma_period=20,
        # ATR position sizing
        atr_period=14,
        risk_per_trade_pct=0.01,   # risk 1% of portfolio per trade
        atr_risk_multiplier=1.5,
        max_position_pct=0.05,     # hard cap regardless of ATR sizing
        max_total_exposure=0.80,
        poll_interval_seconds=60,
    )
    # VwapTrendStrategy is the one under evaluation: enter when price has
    # held above VWAP for three bars, exit on a close below the 9 EMA
    # (checked on 1-minute bars) with an ATR stop beneath it.
    TradingBot(config, strategy=VwapTrendStrategy()).run()
