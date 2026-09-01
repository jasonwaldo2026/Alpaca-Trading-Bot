"""Position sizing and exposure guards."""

import logging
from typing import Dict, Tuple

from bot.config import BotConfig
from bot.strategies import Signal

log = logging.getLogger(__name__)


class RiskManager:
    """
    Validates signals and sizes positions before they reach the order manager.

    Position sizing (ATR-based):
        dollar_risk = portfolio × risk_per_trade_pct
        stop_dist   = ATR × atr_risk_multiplier
        notional    = (dollar_risk / stop_dist) × price
        — capped at portfolio × max_position_pct

    High-ATR (volatile) assets automatically receive smaller positions;
    low-ATR assets can grow toward the cap. Falls back to flat max_position_pct
    when ATR data is unavailable.
    """

    def __init__(self, config: BotConfig):
        self.config = config

    def _size_notional(self, signal: Signal, portfolio_value: float) -> float:
        max_notional = portfolio_value * self.config.max_position_pct

        if signal.atr > 0 and signal.current_price > 0:
            dollar_risk = portfolio_value * self.config.risk_per_trade_pct
            stop_dist = signal.atr * self.config.atr_risk_multiplier
            shares = dollar_risk / stop_dist
            notional = shares * signal.current_price
            notional = min(notional, max_notional)
            log.debug(
                "ATR sizing %s: atr=%.4f stop=%.4f shares=%.3f notional=$%.2f",
                signal.symbol, signal.atr, stop_dist, shares, notional,
            )
        else:
            notional = max_notional
            log.debug("ATR unavailable for %s — using flat sizing $%.2f",
                      signal.symbol, notional)

        return round(notional, 2)

    def evaluate(
        self,
        signal: Signal,
        portfolio_value: float,
        positions: Dict,
    ) -> Tuple[bool, float]:
        """Returns (approved, notional_usd). SELL notional is always 0."""
        if signal.action == "HOLD":
            return False, 0.0

        if signal.action == "SELL":
            return signal.symbol in positions, 0.0

        # BUY checks
        total_invested = sum(float(p.market_value) for p in positions.values())
        exposure = total_invested / portfolio_value if portfolio_value > 0 else 0

        if exposure >= self.config.max_total_exposure:
            log.info(
                "Risk block (exposure): %s — portfolio %.0f%% invested",
                signal.symbol, exposure * 100,
            )
            return False, 0.0

        if signal.symbol in positions:
            log.info("Risk block (duplicate): already holding %s", signal.symbol)
            return False, 0.0

        notional = self._size_notional(signal, portfolio_value)
        return True, notional
