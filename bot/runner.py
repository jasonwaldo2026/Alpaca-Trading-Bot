"""Main poll loop orchestrator."""

import logging
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import List, Optional

from bot.config import BotConfig
from bot.orders import OrderManager
from bot.risk import RiskManager
from bot.strategies import BaseStrategy, EnhancedSMAStrategy
from core.client import AlpacaClient
from core.data import MarketDataFetcher
from core.enrich import enrich
from core.sessions import is_tradable, session_at

log = logging.getLogger(__name__)


class TradingBot:
    """
    Wires all components and runs the poll loop.

    Default strategy is EnhancedSMAStrategy (SMA + RSI + volume + ATR sizing).
    To compare against the original:
        bot = TradingBot(config, strategy=SMAcrossoverStrategy())
    """

    def __init__(self, config: BotConfig, strategy: Optional[BaseStrategy] = None):
        # Fail at construction rather than running a bot that silently never
        # signals because it fetches fewer bars than the indicators need.
        config.validate()
        self.config = config
        self.client = AlpacaClient(config.credentials())
        self.data = MarketDataFetcher(
            self.client, config.bar_minutes, feed=config.data_feed
        )
        # A second, finer fetcher used only for symbols currently held.
        self.manage_data = (
            MarketDataFetcher(
                self.client, config.manage_bar_minutes, feed=config.data_feed
            )
            if config.manage_bar_minutes
            and config.manage_bar_minutes != config.bar_minutes
            else self.data
        )
        self.strategy = strategy or EnhancedSMAStrategy()
        self.risk = RiskManager(config)
        self.orders = OrderManager(self.client, config)

    def _tradable_now(self, symbols: List[str]) -> List[str]:
        """
        Symbols whose market is open under the configured sessions.

        Scanning a closed market burns API calls to re-read yesterday's last
        bar, and any signal it produces cannot be acted on.
        """
        now = datetime.now(timezone.utc)
        return [
            s for s in symbols
            if is_tradable(now, s, self.config.sessions, self.config.calendar)
        ]

    def _management_bars(self, positions):
        """
        Finer bars for held symbols only.

        Scoped to open positions because that is the whole cost argument:
        one or two symbols at 1-minute resolution is cheap, the entire
        watchlist would not be.
        """
        if not positions:
            return {}

        exit_ema = getattr(
            getattr(self.strategy, "exit_policy", None), "ema_period", None
        )
        if exit_ema is None:
            return {}

        params = self.config.manage_params(exit_ema)
        limit = max(params.min_bars() * 2, 60)
        raw = self.manage_data.get_bars(list(positions), limit=limit)

        enriched = {}
        for sym, df in raw.items():
            if df.empty or len(df) < params.min_bars():
                log.debug(
                    "Only %d management bars for %s; need %d to evaluate its exit.",
                    len(df), sym, params.min_bars(),
                )
                continue
            enriched[sym] = enrich(df, params, sym, self.config.calendar)
        return enriched

    def run_once(self):
        log.info("── cycle ──────────────────────")
        portfolio_value = self.client.get_portfolio_value()
        positions = self.client.get_positions()
        log.info(
            "Portfolio: $%.2f  |  Open positions: %d",
            portfolio_value, len(positions),
        )

        now = datetime.now(timezone.utc)
        open_stocks = self._tradable_now(self.config.stock_symbols)
        if not open_stocks and self.config.stock_symbols:
            log.info(
                "Equity market closed (%s session) — crypto only this cycle.",
                session_at(now, self.config.stock_symbols[0], self.config.calendar),
            )

        stock_bars = self.data.get_stock_bars(open_stocks, self.config.bar_limit)
        crypto_bars = self.data.get_crypto_bars(
            self.config.crypto_symbols, self.config.bar_limit
        )

        # Strategies iterate config.stock_symbols, so narrow the config to the
        # symbols actually open rather than letting them look for missing bars.
        cycle_config = replace(self.config, stock_symbols=open_stocks)
        manage_bars = self._management_bars(positions)
        signals = self.strategy.generate_signals(
            stock_bars, crypto_bars, cycle_config, positions, manage_bars
        )
        action_signals = [s for s in signals if s.action != "HOLD"]
        if action_signals:
            log.info("Active signals: %s", [(s.symbol, s.action) for s in action_signals])

        for signal in signals:
            approved, notional = self.risk.evaluate(signal, portfolio_value, positions)
            if not approved:
                continue

            if self.config.is_monitor_only():
                # The whole point of monitor mode: everything up to the order
                # happens, and the order does not. Logged at WARNING so it
                # stands out in a quiet log and is easy to grep for.
                log.warning(
                    "ALERT [monitor] %s %s @ $%.4f  (would size $%.2f)  — %s",
                    signal.action, signal.symbol, signal.current_price,
                    notional, signal.reason,
                )
                continue

            log.info(
                "Executing: %s %s  $%.2f  (%s)",
                signal.action, signal.symbol, notional, signal.reason,
            )
            self.orders.execute(signal, notional, positions)

    def run(self):
        if self.config.is_monitor_only():
            log.warning(
                "MONITOR MODE — signals will be reported but NO orders will be "
                "placed. Set execution_mode='paper' on BotConfig to trade."
            )
        else:
            log.warning(
                "PAPER TRADING MODE — this will place orders on your Alpaca "
                "paper account."
            )
        log.info(
            "Bot starting  mode=%s  paper=%s  bars=%dm  manage=%sm  interval=%ds",
            self.config.execution_mode,
            self.config.paper,
            self.config.bar_minutes,
            self.config.manage_bar_minutes,
            self.config.poll_interval_seconds,
        )
        log.info(
            "Watching  stocks=%s  crypto=%s",
            self.config.stock_symbols, self.config.crypto_symbols,
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
