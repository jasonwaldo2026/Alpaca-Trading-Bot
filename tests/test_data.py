"""
Bar fetching, timeframes, and the closed-bar guarantee.

The headline behavior: a bar that is still forming must never reach a
strategy. Alpaca stamps bars with their start time and includes the
in-progress period in a `limit=N` request, so without this the bot evaluates
a partial bar — a crossover can appear mid-period, fire an order, and
reverse before the bar closes.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from core.data import MarketDataFetcher, alpaca_timeframe, drop_forming_bars

UTC = timezone.utc


def _frame(start: datetime, count: int, bar_minutes: int = 60) -> pd.DataFrame:
    idx = pd.DatetimeIndex(
        [start + timedelta(minutes=bar_minutes * i) for i in range(count)],
        name="timestamp",
    )
    close = np.linspace(100, 100 + count, count)
    return pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1,
         "close": close, "volume": np.full(count, 1000.0)},
        index=idx,
    )


# ── Timeframe mapping ────────────────────────────────────────────────────────

@pytest.mark.parametrize("minutes,expected", [
    (5, "5Min"), (15, "15Min"), (30, "30Min"), (60, "1Hour"),
])
def test_alpaca_timeframe_mapping(minutes, expected):
    assert str(alpaca_timeframe(minutes)) == expected


@pytest.mark.parametrize("bad", [0, -5, 7, 50])
def test_timeframe_must_divide_into_a_day(bad):
    with pytest.raises(ValueError, match="divide evenly"):
        alpaca_timeframe(bad)


# ── Dropping the forming bar ─────────────────────────────────────────────────

def test_forming_bar_is_dropped():
    """Bars at 10:00, 11:00, 12:00; at 12:30 the 12:00 bar is still forming."""
    df = _frame(datetime(2026, 9, 2, 10, tzinfo=UTC), 3)
    out = drop_forming_bars(df, 60, now=datetime(2026, 9, 2, 12, 30, tzinfo=UTC))
    assert len(out) == 2
    assert out.index[-1] == pd.Timestamp("2026-09-02 11:00", tz="UTC")


def test_bar_is_kept_the_instant_it_closes():
    df = _frame(datetime(2026, 9, 2, 10, tzinfo=UTC), 3)
    exactly_closed = drop_forming_bars(
        df, 60, now=datetime(2026, 9, 2, 13, 0, tzinfo=UTC)
    )
    assert len(exactly_closed) == 3, "a bar is closed at exactly T + interval"


def test_nothing_dropped_when_all_bars_are_historical():
    df = _frame(datetime(2026, 9, 2, 10, tzinfo=UTC), 5)
    out = drop_forming_bars(df, 60, now=datetime(2026, 9, 3, tzinfo=UTC))
    assert len(out) == 5


def test_five_minute_bars_use_a_five_minute_window():
    df = _frame(datetime(2026, 9, 2, 10, tzinfo=UTC), 4, bar_minutes=5)
    # Bars at 10:00, 10:05, 10:10, 10:15. At 10:17 the 10:15 bar is forming.
    out = drop_forming_bars(df, 5, now=datetime(2026, 9, 2, 10, 17, tzinfo=UTC))
    assert len(out) == 3
    assert out.index[-1] == pd.Timestamp("2026-09-02 10:10", tz="UTC")


def test_naive_index_is_treated_as_utc():
    """Alpaca frames can arrive tz-naive; they are UTC, not local."""
    df = _frame(datetime(2026, 9, 2, 10), 3)
    df.index = df.index.tz_localize(None)
    out = drop_forming_bars(df, 60, now=datetime(2026, 9, 2, 12, 30, tzinfo=UTC))
    assert len(out) == 2


def test_multiindex_frame_is_handled():
    """Multi-symbol requests come back keyed (symbol, timestamp)."""
    a = _frame(datetime(2026, 9, 2, 10, tzinfo=UTC), 3)
    b = _frame(datetime(2026, 9, 2, 10, tzinfo=UTC), 3)
    combined = pd.concat({"AAPL": a, "MSFT": b}, names=["symbol", "timestamp"])
    out = drop_forming_bars(combined, 60, now=datetime(2026, 9, 2, 12, 30, tzinfo=UTC))
    assert len(out) == 4, "one forming bar removed per symbol"
    stamps = out.index.get_level_values("timestamp")
    assert pd.Timestamp("2026-09-02 12:00", tz="UTC") not in stamps


def test_empty_frame_is_safe():
    assert drop_forming_bars(pd.DataFrame(), 60).empty


def test_unindexed_frame_passes_through_rather_than_raising():
    """A reset-index frame cannot be filtered; degrade instead of crashing."""
    df = _frame(datetime(2026, 9, 2, 10, tzinfo=UTC), 3).reset_index()
    assert len(drop_forming_bars(df, 60)) == 3


# ── Fetcher wiring ───────────────────────────────────────────────────────────

class _StubData:
    def __init__(self, df):
        self.df = df

    def get_stock_bars(self, req):
        return self

    def get_crypto_bars(self, req):
        return self


class _StubClient:
    def __init__(self, df):
        self.stock_data = _StubData(df)
        self.crypto_data = _StubData(df)


def test_fetcher_drops_the_forming_bar_by_default():
    recent = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    df = _frame(recent - timedelta(hours=2), 3)
    fetcher = MarketDataFetcher(_StubClient(df), bar_minutes=60)
    out = fetcher.get_bars(["AAPL"], limit=10)
    assert len(out["AAPL"]) == 2, "the in-progress bar must not reach a strategy"


def test_fetcher_can_keep_forming_bars_for_backtests():
    recent = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    df = _frame(recent - timedelta(hours=2), 3)
    fetcher = MarketDataFetcher(_StubClient(df), bar_minutes=60, drop_forming=False)
    assert len(fetcher.get_bars(["AAPL"], limit=10)["AAPL"]) == 3


def test_fetcher_timeframe_follows_bar_minutes():
    df = _frame(datetime(2026, 9, 2, 10, tzinfo=UTC), 3)
    assert str(MarketDataFetcher(_StubClient(df), 5).timeframe) == "5Min"
    assert str(MarketDataFetcher(_StubClient(df), 60).timeframe) == "1Hour"
