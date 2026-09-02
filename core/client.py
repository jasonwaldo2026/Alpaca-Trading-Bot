"""
Authenticated Alpaca client, shared by every app.

Credentials are resolved once, here, so the bot, dashboard, scanner, and
studio can't drift into four different ways of reading keys.
"""

import logging
import os
from dataclasses import dataclass
from typing import Dict, Optional

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.data.historical import (
        StockHistoricalDataClient,
        CryptoHistoricalDataClient,
    )
except ImportError:  # pragma: no cover - environment guard
    raise SystemExit("Run:  pip install -r requirements.txt")

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Credentials:
    """Alpaca API credentials plus the paper/live switch."""

    api_key: str
    api_secret: str
    paper: bool = True

    @classmethod
    def from_env(cls, paper: bool = True) -> "Credentials":
        """Read from ALPACA_API_KEY / ALPACA_API_SECRET."""
        return cls(
            api_key=os.getenv("ALPACA_API_KEY", ""),
            api_secret=os.getenv("ALPACA_API_SECRET", ""),
            paper=paper,
        )

    @classmethod
    def from_streamlit(cls, secrets, paper: bool = True) -> "Credentials":
        """
        Read from st.secrets, falling back to env vars.

        Streamlit Cloud injects secrets rather than a .env file, so the
        dashboard and studio need this path; `secrets` is passed in rather
        than imported so core stays free of a streamlit dependency.
        """
        return cls(
            api_key=secrets.get("ALPACA_API_KEY", os.getenv("ALPACA_API_KEY", "")),
            api_secret=secrets.get(
                "ALPACA_API_SECRET", os.getenv("ALPACA_API_SECRET", "")
            ),
            paper=paper,
        )

    def is_complete(self) -> bool:
        return bool(self.api_key and self.api_secret)


class AlpacaClient:
    """Wrapper around the three alpaca-py REST clients."""

    def __init__(self, credentials: Credentials):
        if not credentials.is_complete():
            raise ValueError(
                "Missing credentials — set ALPACA_API_KEY and "
                "ALPACA_API_SECRET in your .env file (or Streamlit secrets)."
            )
        self.credentials = credentials
        self.trading = TradingClient(
            credentials.api_key, credentials.api_secret, paper=credentials.paper
        )
        self.stock_data = StockHistoricalDataClient(
            credentials.api_key, credentials.api_secret
        )
        self.crypto_data = CryptoHistoricalDataClient(
            credentials.api_key, credentials.api_secret
        )
        log.info("Alpaca client ready  (paper=%s)", credentials.paper)

    # ── Account ──────────────────────────────────────────────────────────────

    def get_account(self):
        return self.trading.get_account()

    def get_portfolio_value(self) -> float:
        return float(self.get_account().portfolio_value)

    def get_positions(self) -> Dict[str, object]:
        return {p.symbol: p for p in self.trading.get_all_positions()}

    # ── Orders ───────────────────────────────────────────────────────────────

    def place_market_order(
        self,
        symbol: str,
        side: OrderSide,
        notional: Optional[float] = None,
        qty: Optional[float] = None,
    ):
        """Buy/sell by notional USD or by quantity."""
        if notional:
            req = MarketOrderRequest(
                symbol=symbol,
                notional=round(notional, 2),
                side=side,
                time_in_force=TimeInForce.DAY,
            )
        else:
            req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.GTC,
            )
        order = self.trading.submit_order(req)
        log.info(
            "Order submitted ▶ %s %s  notional=%s  qty=%s",
            side, symbol, notional, qty,
        )
        return order

    def place_extended_hours_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: float,
        reference_price: float,
        limit_offset_pct: float = 0.002,
    ):
        """
        Place an order valid in pre-market or after-hours.

        Alpaca will not accept a market order outside regular hours — it is
        queued to the next open and fills at an unknown price, which silently
        invalidates any sizing computed from the signal-time price. Extended
        hours requires all three of: a **limit** order, `TimeInForce.DAY`,
        and `extended_hours=True`.

        The limit is placed marketably — slightly through the reference price
        in the direction of the trade — so it fills against the thin extended
        book while still capping slippage. Extended-hours liquidity is a small
        fraction of regular-hours liquidity, so a plain mid-price limit
        frequently will not fill at all.

        Fractional and notional orders are regular-hours only, so `qty` must
        be a whole number of shares here.
        """
        whole_qty = int(qty)
        if whole_qty < 1:
            raise ValueError(
                f"Extended-hours orders must be whole shares; computed qty "
                f"{qty:.4f} for {symbol} rounds to zero. Increase position "
                f"size or trade this symbol during regular hours."
            )

        direction = 1 if side == OrderSide.BUY else -1
        limit_price = round(reference_price * (1 + direction * limit_offset_pct), 2)

        req = LimitOrderRequest(
            symbol=symbol,
            qty=whole_qty,
            side=side,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price,
            extended_hours=True,
        )
        order = self.trading.submit_order(req)
        log.info(
            "Extended-hours order ▶ %s %s  qty=%d  limit=$%.2f  (ref $%.2f)",
            side, symbol, whole_qty, limit_price, reference_price,
        )
        return order

    def close_position(self, symbol: str):
        """
        Close a position at market. Regular hours only — Alpaca rejects
        market orders outside them. Use place_extended_hours_order() with a
        SELL side to exit during pre-market or after-hours.
        """
        self.trading.close_position(symbol)
        log.info("Position closed: %s", symbol)

    def get_position_qty(self, symbol: str) -> float:
        """Shares currently held, or 0.0 when flat."""
        position = self.get_positions().get(symbol)
        return float(position.qty) if position else 0.0
