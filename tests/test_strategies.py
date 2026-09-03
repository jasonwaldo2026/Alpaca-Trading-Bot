"""
Strategy tests, focused on the closed-bar guarantee.

The bug this pins: Alpaca includes the currently-forming bar in a `limit=N`
request. Evaluating it lets a crossover appear mid-period, fire a BUY, and
then reverse before the bar closes — a trade taken on a signal that never
existed on the completed series.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from bot.config import BotConfig
from bot.strategies import EnhancedSMAStrategy, SMAcrossoverStrategy
from core.data import drop_forming_bars

UTC = timezone.utc

# Sideways drift, then a final bar that pushes the fast SMA above the slow one
# on high volume. On the closed series there is no crossover at all.
CLOSED_CLOSES = [100, 101, 100.5, 101.5, 100.8, 101.2, 100.4,
                 100.9, 100.2, 100.6, 100.0, 100.3, 99.8]
FORMING_CLOSE = 101.5

CONFIG = BotConfig(
    api_key="k", api_secret="s",
    stock_symbols=["AAPL"], crypto_symbols=[],
    sma_fast=3, sma_slow=5, rsi_period=5,
    volume_sma_period=3, atr_period=3,
)


def _frame(include_forming: bool) -> pd.DataFrame:
    closes = list(CLOSED_CLOSES)
    volumes = [5_000.0] * len(closes)
    if include_forming:
        closes.append(FORMING_CLOSE)
        volumes.append(20_000.0)

    start = datetime(2026, 9, 2, 10, tzinfo=UTC)
    idx = pd.DatetimeIndex(
        [start + timedelta(hours=i) for i in range(len(closes))], name="timestamp"
    )
    return pd.DataFrame(
        {"open": closes, "high": [c + 0.2 for c in closes],
         "low": [c - 0.2 for c in closes], "close": closes, "volume": volumes},
        index=idx,
    )


def _actions(bars: pd.DataFrame) -> list:
    signals = EnhancedSMAStrategy().generate_signals(bars, pd.DataFrame(), CONFIG)
    return [s.action for s in signals]


def test_forming_bar_would_fire_a_buy():
    """Establishes the scenario is real — without the fix, this trades."""
    assert _actions(_frame(include_forming=True)) == ["BUY"]


def test_closed_series_alone_produces_no_buy():
    """The same data, minus the in-progress bar, has no signal."""
    assert _actions(_frame(include_forming=False)) == ["HOLD"]


def test_dropping_the_forming_bar_prevents_the_phantom_trade():
    """End to end: the fetcher's filter turns the phantom BUY into a HOLD."""
    bars = _frame(include_forming=True)
    # 'now' sits inside the final bar's period, so that bar is still forming.
    now = bars.index[-1] + timedelta(minutes=30)
    closed_only = drop_forming_bars(bars, 60, now=now.to_pydatetime())

    assert len(closed_only) == len(bars) - 1
    assert _actions(closed_only) == ["HOLD"], (
        "a crossover that only exists on a partial bar must not trade"
    )


def test_signal_price_and_atr_come_from_the_closed_bar():
    """RiskManager sizes from signal.current_price; if that comes off the
    partial bar the position size is computed from a price that may not
    survive the bar."""
    bars = _frame(include_forming=True)
    now = bars.index[-1] + timedelta(minutes=30)
    closed_only = drop_forming_bars(bars, 60, now=now.to_pydatetime())

    signals = EnhancedSMAStrategy().generate_signals(closed_only, pd.DataFrame(), CONFIG)
    assert signals[0].current_price == pytest.approx(CLOSED_CLOSES[-1])
    assert signals[0].current_price != pytest.approx(FORMING_CLOSE)


def test_sma_only_strategy_is_equally_protected():
    """Both strategies read iloc[-1]; the guarantee is at the fetcher, so it
    covers them both."""
    bars = _frame(include_forming=True)
    now = bars.index[-1] + timedelta(minutes=30)
    closed_only = drop_forming_bars(bars, 60, now=now.to_pydatetime())

    with_forming = SMAcrossoverStrategy().generate_signals(bars, pd.DataFrame(), CONFIG)
    without = SMAcrossoverStrategy().generate_signals(closed_only, pd.DataFrame(), CONFIG)
    assert [s.action for s in with_forming] == ["BUY"]
    assert [s.action for s in without] == ["HOLD"]


def test_crypto_always_has_a_forming_bar():
    """Crypto runs 24/7, so there is never a moment without one — the filter
    must apply there too."""
    bars = _frame(include_forming=True)
    now = bars.index[-1] + timedelta(minutes=30)
    assert len(drop_forming_bars(bars, 60, now=now.to_pydatetime())) == len(bars) - 1
