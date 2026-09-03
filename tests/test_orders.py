"""
Order routing tests.

The key behavior: outside regular hours Alpaca rejects market orders and
fractional quantities, so the bot must switch to whole-share marketable
limit orders with extended_hours=True. Getting this wrong does not raise —
the order is silently queued to the next open and fills at an unknown price,
which invalidates the ATR sizing computed at signal time.
"""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from alpaca.trading.enums import OrderSide

from bot.config import BotConfig
from bot.orders import OrderManager
from bot.strategies import Signal
from core.sessions import SessionConfig

ET = ZoneInfo("America/New_York")


class FakeClient:
    """Records calls instead of reaching Alpaca."""

    def __init__(self):
        self.market_orders = []
        self.extended_orders = []
        self.closed = []

    def place_market_order(self, symbol, side, notional=None, qty=None):
        self.market_orders.append(
            {"symbol": symbol, "side": side, "notional": notional, "qty": qty}
        )

    def place_extended_hours_order(
        self, symbol, side, qty, reference_price, limit_offset_pct=0.002
    ):
        self.extended_orders.append({
            "symbol": symbol, "side": side, "qty": qty,
            "reference_price": reference_price, "offset": limit_offset_pct,
        })

    def close_position(self, symbol):
        self.closed.append(symbol)


class ClockedOrderManager(OrderManager):
    """OrderManager with a pinned clock, so tests do not depend on when they run."""

    def __init__(self, client, config, now):
        super().__init__(client, config)
        self._pinned = now

    def _now(self):
        return self._pinned


def _mgr(now, sessions=None):
    client = FakeClient()
    config = BotConfig(
        api_key="k", api_secret="s",
        sessions=sessions or SessionConfig.after_hours(),
    )
    return client, ClockedOrderManager(client, config, now)


def _position(qty):
    return SimpleNamespace(qty=str(qty), market_value="1000")


BUY = Signal("AAPL", "BUY", "stock", 0.9, "test", atr=1.0, current_price=100.0)
SELL = Signal("AAPL", "SELL", "stock", 0.9, "test", atr=1.0, current_price=100.0)

REGULAR_TIME = datetime(2026, 9, 2, 11, 0, tzinfo=ET)
AFTER_TIME = datetime(2026, 9, 2, 18, 0, tzinfo=ET)
PRE_TIME = datetime(2026, 9, 2, 6, 0, tzinfo=ET)


# ── Regular hours keeps the existing path ────────────────────────────────────

def test_regular_hours_buy_uses_notional_market_order():
    client, mgr = _mgr(REGULAR_TIME)
    mgr.execute(BUY, 500.0, {})
    assert client.market_orders == [
        {"symbol": "AAPL", "side": OrderSide.BUY, "notional": 500.0, "qty": None}
    ]
    assert client.extended_orders == []


def test_regular_hours_sell_closes_position():
    client, mgr = _mgr(REGULAR_TIME)
    mgr.execute(SELL, 0.0, {"AAPL": _position(5)})
    assert client.closed == ["AAPL"]
    assert client.extended_orders == []


# ── Extended hours switches order type ───────────────────────────────────────

@pytest.mark.parametrize("now,label", [(AFTER_TIME, "after"), (PRE_TIME, "pre")])
def test_extended_hours_buy_uses_whole_share_limit(now, label):
    client, mgr = _mgr(now, SessionConfig.extended())
    mgr.execute(BUY, 500.0, {})

    assert client.market_orders == [], f"{label}: must not send a market order"
    assert len(client.extended_orders) == 1
    order = client.extended_orders[0]
    assert order["side"] == OrderSide.BUY
    assert order["qty"] == pytest.approx(5.0)      # $500 / $100
    assert order["reference_price"] == 100.0


def test_extended_hours_sell_uses_limit_not_close_position():
    """close_position() is a market order — rejected outside regular hours."""
    client, mgr = _mgr(AFTER_TIME)
    mgr.execute(SELL, 0.0, {"AAPL": _position(7)})

    assert client.closed == [], "close_position would be rejected after hours"
    assert len(client.extended_orders) == 1
    assert client.extended_orders[0]["side"] == OrderSide.SELL
    assert client.extended_orders[0]["qty"] == pytest.approx(7.0)


def test_sub_one_share_buy_is_skipped_after_hours():
    """Fractional trading is regular-hours only, so a $50 order on a $100
    stock has nothing valid to send."""
    client, mgr = _mgr(AFTER_TIME)
    mgr.execute(BUY, 50.0, {})
    assert client.extended_orders == []
    assert client.market_orders == []


def test_fractional_holding_is_not_sold_after_hours():
    client, mgr = _mgr(AFTER_TIME)
    mgr.execute(SELL, 0.0, {"AAPL": _position(0.4)})
    assert client.extended_orders == []
    assert client.closed == []


def test_missing_reference_price_skips_rather_than_guessing():
    """An extended-hours order needs a limit price; there is no safe default."""
    client, mgr = _mgr(AFTER_TIME)
    no_price = Signal("AAPL", "BUY", "stock", 0.9, "test", atr=1.0, current_price=0.0)
    mgr.execute(no_price, 500.0, {})
    assert client.extended_orders == []
    assert client.market_orders == []


def test_limit_offset_comes_from_config():
    client = FakeClient()
    config = BotConfig(
        api_key="k", api_secret="s",
        sessions=SessionConfig.after_hours(),
        extended_hours_limit_offset_pct=0.01,
    )
    ClockedOrderManager(client, config, AFTER_TIME).execute(BUY, 500.0, {})
    assert client.extended_orders[0]["offset"] == 0.01


def test_sell_without_a_position_does_nothing():
    client, mgr = _mgr(AFTER_TIME)
    mgr.execute(SELL, 0.0, {})
    assert client.extended_orders == []
    assert client.closed == []


def test_crypto_always_takes_the_regular_path():
    """Crypto has no sessions and no extended-hours restrictions."""
    client, mgr = _mgr(AFTER_TIME)
    btc = Signal("BTC/USD", "BUY", "crypto", 0.9, "test", atr=1.0, current_price=50_000.0)
    mgr.execute(btc, 500.0, {})
    assert len(client.market_orders) == 1
    assert client.extended_orders == []


def test_order_failure_is_logged_not_raised():
    class Exploding(FakeClient):
        def place_market_order(self, *a, **kw):
            raise RuntimeError("alpaca down")

    client = Exploding()
    config = BotConfig(api_key="k", api_secret="s")
    ClockedOrderManager(client, config, REGULAR_TIME).execute(BUY, 500.0, {})
    # One bad symbol must not kill the poll loop.


# ── Limit price construction (core.client) ───────────────────────────────────

class _StubTrading:
    def __init__(self):
        self.submitted = []

    def submit_order(self, req):
        self.submitted.append(req)
        return req


def _client_with_stub():
    from core.client import AlpacaClient, Credentials
    client = AlpacaClient.__new__(AlpacaClient)   # skip network-touching __init__
    client.credentials = Credentials("k", "s", paper=True)
    client.trading = _StubTrading()
    return client


def test_buy_limit_is_placed_above_reference_to_fill():
    client = _client_with_stub()
    client.place_extended_hours_order("AAPL", OrderSide.BUY, 10, 100.0, 0.002)
    req = client.trading.submitted[0]
    assert req.limit_price == 100.2, "buy must reach up to fill against thin book"
    assert req.extended_hours is True
    assert req.qty == 10


def test_sell_limit_is_placed_below_reference_to_fill():
    client = _client_with_stub()
    client.place_extended_hours_order("AAPL", OrderSide.SELL, 10, 100.0, 0.002)
    assert client.trading.submitted[0].limit_price == 99.8


def test_quantity_is_truncated_to_whole_shares():
    client = _client_with_stub()
    client.place_extended_hours_order("AAPL", OrderSide.BUY, 7.9, 100.0)
    assert client.trading.submitted[0].qty == 7


def test_sub_one_share_raises_with_an_actionable_message():
    client = _client_with_stub()
    with pytest.raises(ValueError, match="whole shares"):
        client.place_extended_hours_order("AAPL", OrderSide.BUY, 0.4, 100.0)
    assert client.trading.submitted == []


def test_time_in_force_is_day():
    """Extended-hours orders must be DAY; GTC is rejected."""
    from alpaca.trading.enums import TimeInForce
    client = _client_with_stub()
    client.place_extended_hours_order("AAPL", OrderSide.BUY, 5, 100.0)
    assert client.trading.submitted[0].time_in_force == TimeInForce.DAY
