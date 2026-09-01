"""Main poll loop orchestrator."""

import logging
import time
from typing import Optional

from bot.config import BotConfig
from bot.orders import OrderManager
from bot.risk import RiskManager
from bot.strategies import BaseStrategy, EnhancedSMAStrategy
from core.client import AlpacaClient
from core.data import MarketDataFetcher

log = logging.getLogger(__name__)


class TradingBot:
    """
    Wires all components and runs the poll loop.

    Default strategy is EnhancedSMAStrategy (SMA + RSI + volume + ATR sizing).
    To compare against the original:
        bot = TradingBot(config, strategy=SMAcrossoverStrategy())
    """

    def __init__(self, config: BotConfig, strategy: Optional[BaseStrategy] = None):
        self.config = config
        self.client = AlpacaClient(config.credentials())
        self.data = MarketDataFetcher(self.client)
        self.strategy = strategy or EnhancedSMAStrategy()
        self.risk = RiskManager(config)
        self.orders = OrderManager(self.client)

    def run_once(self):
        log.info("── cycle ──────────────────────")
        portfolio_value = self.client.get_portfolio_value()
        positions = self.client.get_positions()
        log.info(
            "Portfolio: $%.2f  |  Open positions: %d",
            portfolio_value, len(positions),
        )

        stock_bars = self.data.get_stock_bars(
            self.config.stock_symbols, self.config.bar_limit
        )
        crypto_bars = self.data.get_crypto_bars(
            self.config.crypto_symbols, self.config.bar_limit
        )

        signals = self.strategy.generate_signals(stock_bars, crypto_bars, self.config)
        action_signals = [s for s in signals if s.action != "HOLD"]
        if action_signals:
            log.info("Active signals: %s", [(s.symbol, s.action) for s in action_signals])

        for signal in signals:
            approved, notional = self.risk.evaluate(signal, portfolio_value, positions)
            if approved:
                log.info(
                    "Executing: %s %s  $%.2f  (%s)",
                    signal.action, signal.symbol, notional, signal.reason,
                )
                self.orders.execute(signal, notional, positions)

    def run(self):
        log.info(
            "Bot starting  paper=%s  interval=%ds  stocks=%s  crypto=%s",
            self.config.paper,
            self.config.poll_interval_seconds,
            self.config.stock_symbols,
            self.config.crypto_symbols,
        )
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                log.info("Bot stopped.")
                break
            except Exception as exc:
                log.error("Cycle error: %s", exc, exc_info=True)
            time.sleep(self.config.poll_interval_seconds)
