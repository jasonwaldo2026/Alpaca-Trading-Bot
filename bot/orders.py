"""Signal → Alpaca order translation."""

import logging
from typing import Dict

from alpaca.trading.enums import OrderSide

from bot.strategies import Signal
from core.client import AlpacaClient

log = logging.getLogger(__name__)


class OrderManager:
    """Translates approved signals into Alpaca API calls."""

    def __init__(self, client: AlpacaClient):
        self.client = client

    def execute(self, signal: Signal, notional: float, positions: Dict):
        try:
            if signal.action == "BUY":
                self.client.place_market_order(
                    signal.symbol, OrderSide.BUY, notional=notional
                )
            elif signal.action == "SELL" and signal.symbol in positions:
                self.client.close_position(signal.symbol)
        except Exception as exc:
            log.error("Order failed for %s: %s", signal.symbol, exc)
