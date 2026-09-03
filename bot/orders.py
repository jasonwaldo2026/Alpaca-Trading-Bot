"""Signal → Alpaca order translation, routed by market session."""

import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from alpaca.trading.enums import OrderSide

from bot.config import BotConfig
from bot.strategies import Signal
from core.client import AlpacaClient
from core.sessions import AFTER, PRE, session_at

log = logging.getLogger(__name__)


class OrderManager:
    """
    Translates approved signals into Alpaca API calls.

    Order type depends on the session. Regular hours take market orders,
    which may be notional (fractional). Pre-market and after-hours take
    marketable limit orders with `extended_hours=True` and whole-share
    quantities — Alpaca rejects anything else outside regular hours, and a
    market order sent then is silently queued to the next open.
    """

    def __init__(self, client: AlpacaClient, config: Optional[BotConfig] = None):
        self.client = client
        self.config = config or BotConfig()

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def execute(self, signal: Signal, notional: float, positions: Dict):
        session = session_at(self._now(), signal.symbol, self.config.calendar)
        extended = session in (PRE, AFTER)

        try:
            if extended:
                self._execute_extended(signal, notional, positions, session)
            elif signal.action == "BUY":
                self.client.place_market_order(
                    signal.symbol, OrderSide.BUY, notional=notional
                )
            elif signal.action == "SELL" and signal.symbol in positions:
                self.client.close_position(signal.symbol)
        except Exception as exc:
            log.error("Order failed for %s: %s", signal.symbol, exc)

    def _execute_extended(
        self, signal: Signal, notional: float, positions: Dict, session: str
    ):
        """Extended-hours path: marketable limit, whole shares."""
        price = signal.current_price
        if price <= 0:
            log.warning(
                "No reference price for %s in %s session — skipping. Extended-hours "
                "orders need a limit price.", signal.symbol, session,
            )
            return

        offset = self.config.extended_hours_limit_offset_pct

        if signal.action == "BUY":
            qty = notional / price
            if int(qty) < 1:
                log.info(
                    "Skipping %s BUY in %s session: $%.2f at $%.2f is %.3f shares, "
                    "and extended hours cannot trade fractions.",
                    signal.symbol, session, notional, price, qty,
                )
                return
            self.client.place_extended_hours_order(
                signal.symbol, OrderSide.BUY, qty, price, offset
            )

        elif signal.action == "SELL" and signal.symbol in positions:
            held = float(positions[signal.symbol].qty)
            if int(held) < 1:
                log.info(
                    "Skipping %s SELL in %s session: holding %.4f shares, which is "
                    "a fraction — close it during regular hours.",
                    signal.symbol, session, held,
                )
                return
            self.client.place_extended_hours_order(
                signal.symbol, OrderSide.SELL, held, price, offset
            )
