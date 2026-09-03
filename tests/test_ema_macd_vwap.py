"""
EMA, MACD, and VWAP — correctness and resolution-independence.

VWAP is the one most easily got wrong: it is a running average *within* a
trading day, not a rolling window. An unanchored VWAP drifts further from
price every day and is not the line any charting platform draws.
"""

from datetime import datetime

import numpy as np
import pandas as pd
import pytest
from zoneinfo import ZoneInfo

from core.indicators import (
    COL_MACD,
    COL_MACD_HIST,
    COL_MACD_SIGNAL,
    COL_VWAP,
    IndicatorParams,
    add_indicators,
    ema,
    ema_column,
    indicator_columns,
    macd,
    typical_price,
    vwap,
)
from core.sessions import session_day_series

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


# ── EMA ──────────────────────────────────────────────────────────────────────

def test_ema_matches_the_recursive_definition():
    """y_t = a*x_t + (1-a)*y_(t-1) with a = 2/(period+1), seeded at x_0."""
    series = pd.Series([1.0, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    alpha = 2 / (3 + 1)

    expected = [1.0]
    for x in series[1:]:
        expected.append(alpha * x + (1 - alpha) * expected[-1])

    got = ema(series, 3)
    assert np.allclose(got[2:], expected[2:])


def test_ema_warms_up_before_emitting():
    got = ema(pd.Series(np.arange(10.0)), 4)
    assert got[:3].isna().all()
    assert got[3:].notna().all()


def test_ema_reacts_faster_than_sma():
    """The defining property: recent bars carry more weight."""
    from core.indicators import sma
    series = pd.Series([10.0] * 20 + [20.0] * 5)
    assert ema(series, 10).iloc[-1] > sma(series, 10).iloc[-1]


def test_ema_of_a_constant_series_is_that_constant():
    assert ema(pd.Series([7.0] * 30), 10).iloc[-1] == pytest.approx(7.0)


# ── MACD ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def prices() -> pd.Series:
    rng = np.random.default_rng(11)
    return pd.Series(100 + np.cumsum(rng.normal(0, 1.0, 200)))


def test_macd_line_is_the_ema_difference(prices):
    result = macd(prices, 12, 26, 9)
    expected = ema(prices, 12) - ema(prices, 26)
    pd.testing.assert_series_equal(result.macd, expected, check_names=False)


def test_signal_is_an_ema_of_the_macd_line_not_of_price(prices):
    """A signal line taken from price instead of from the MACD line is a
    common and silent error."""
    result = macd(prices, 12, 26, 9)
    pd.testing.assert_series_equal(
        result.signal, ema(result.macd, 9), check_names=False
    )
    assert not np.allclose(
        result.signal.dropna(), ema(prices, 9).reindex(result.signal.dropna().index)
    )


def test_histogram_is_line_minus_signal(prices):
    result = macd(prices, 12, 26, 9)
    pd.testing.assert_series_equal(
        result.histogram, result.macd - result.signal, check_names=False
    )


def test_signal_warms_up_after_the_line(prices):
    """The signal is an EMA of the MACD line, so it cannot exist until the
    line has produced `signal` readings."""
    result = macd(prices, 3, 6, 4)
    assert result.macd.first_valid_index() == 5           # slow - 1
    assert result.signal.first_valid_index() == 8         # + signal - 1
    assert result.signal.first_valid_index() > result.macd.first_valid_index()


def test_macd_rejects_fast_slower_than_slow():
    with pytest.raises(ValueError, match="fast < slow"):
        macd(pd.Series([1.0, 2, 3]), fast=26, slow=12)


def test_macd_crosses_zero_when_trend_flips():
    up = pd.Series(np.linspace(100, 140, 80))
    down = pd.Series(np.concatenate([np.linspace(100, 140, 80),
                                     np.linspace(140, 90, 80)]))
    assert macd(up, 12, 26, 9).macd.iloc[-1] > 0
    assert macd(down, 12, 26, 9).macd.iloc[-1] < 0


# ── VWAP ─────────────────────────────────────────────────────────────────────

def _day_bars(day: int, closes, volumes=None) -> pd.DataFrame:
    stamps = [datetime(2026, 9, day, 10 + i, tzinfo=ET) for i in range(len(closes))]
    closes = np.asarray(closes, dtype=float)
    volumes = np.full(len(closes), 100.0) if volumes is None else np.asarray(volumes, float)
    return pd.DataFrame(
        {"open": closes, "high": closes + 1, "low": closes - 1,
         "close": closes, "volume": volumes},
        index=pd.DatetimeIndex(stamps, name="timestamp"),
    )


def test_typical_price_is_the_hlc_average():
    df = _day_bars(2, [10.0])
    assert typical_price(df).iloc[0] == pytest.approx((11 + 9 + 10) / 3)


def test_vwap_known_values():
    """Two bars, typical prices 10 and 20, equal volume → VWAP 15."""
    df = _day_bars(2, [10.0, 20.0])
    result = vwap(df)
    assert result.iloc[0] == pytest.approx(10.0)
    assert result.iloc[1] == pytest.approx(15.0)


def test_vwap_is_volume_weighted_not_a_plain_mean():
    df = _day_bars(2, [10.0, 20.0], volumes=[300.0, 100.0])
    # (10*300 + 20*100) / 400 = 12.5
    assert vwap(df).iloc[-1] == pytest.approx(12.5)


def test_vwap_resets_each_trading_day():
    """The behavior that makes VWAP VWAP. Without the reset the second day's
    line is dragged toward the first day's prices forever."""
    df = pd.concat([_day_bars(2, [100.0] * 6), _day_bars(3, [200.0] * 6)])
    anchor = session_day_series(df.index, "AAPL")

    anchored = vwap(df, anchor)
    assert anchored.iloc[5] == pytest.approx(100.0)
    assert anchored.iloc[-1] == pytest.approx(200.0), "day 2 must start fresh"

    unanchored = vwap(df)
    assert unanchored.iloc[-1] == pytest.approx(150.0), "no reset blends the days"


def test_vwap_anchor_groups_extended_hours_with_their_own_day():
    """Pre-market and after-hours bars belong to the same trading day as the
    regular session — resetting at 09:30 would discard pre-market volume."""
    stamps = [datetime(2026, 9, 2, h, tzinfo=ET) for h in (5, 11, 18)]
    anchor = session_day_series(pd.DatetimeIndex(stamps), "AAPL")
    assert anchor.nunique() == 1


def test_crypto_anchors_on_the_utc_day():
    """Crypto has no session; the conventional daily anchor is UTC midnight."""
    stamps = pd.DatetimeIndex([
        datetime(2026, 9, 2, 23, tzinfo=UTC),
        datetime(2026, 9, 3, 1, tzinfo=UTC),
    ])
    anchor = session_day_series(stamps, "BTC/USD")
    assert anchor.nunique() == 2


def test_zero_volume_bars_do_not_divide_by_zero():
    df = _day_bars(2, [10.0, 20.0], volumes=[0.0, 0.0])
    assert vwap(df).isna().all()


def test_vwap_rejects_a_misaligned_anchor():
    df = _day_bars(2, [10.0, 20.0])
    with pytest.raises(ValueError, match="share an index"):
        vwap(df, pd.Series(["a", "b", "c"]))


# ── Wiring through add_indicators ────────────────────────────────────────────

def test_add_indicators_emits_every_new_column():
    rng = np.random.default_rng(3)
    closes = 100 + np.cumsum(rng.normal(0, 1, 120))
    df = pd.DataFrame(
        {"open": closes, "high": closes + 1, "low": closes - 1,
         "close": closes, "volume": np.full(120, 1000.0)},
        index=pd.date_range("2026-09-02", periods=120, freq="5min", tz="UTC"),
    )
    out = add_indicators(df, IndicatorParams(bar_minutes=5))
    for col in (ema_column(9), COL_VWAP, COL_MACD, COL_MACD_SIGNAL, COL_MACD_HIST):
        assert col in out.columns
        assert out[col].notna().any(), f"{col} produced no values"


def test_add_indicators_passes_the_vwap_anchor_through():
    df = pd.concat([_day_bars(2, [100.0] * 6), _day_bars(3, [200.0] * 6)])
    params = IndicatorParams(
        sma_fast=2, sma_slow=3, ema_periods=(2, 3),
        rsi_period=2, volume_sma_period=2, atr_period=2,
        macd_fast=2, macd_slow=3, macd_signal=2,
    )
    anchor = session_day_series(df.index, "AAPL")
    out = add_indicators(df, params, anchor=anchor)
    assert out[COL_VWAP].iloc[-1] == pytest.approx(200.0)


# ── Resolution independence ──────────────────────────────────────────────────

def test_rescaling_preserves_wall_clock_lookback():
    hourly = IndicatorParams(bar_minutes=60)
    five = hourly.rescaled_to(5)
    for field in IndicatorParams.PERIOD_FIELDS:
        assert five.duration_minutes(field) == hourly.duration_minutes(field), field


def test_rescaling_multiplies_periods_by_the_resolution_ratio():
    five = IndicatorParams(sma_slow=30, macd_slow=26, bar_minutes=60).rescaled_to(5)
    assert five.sma_slow == 360
    assert five.macd_slow == 312
    assert five.bar_minutes == 5


def test_rescaling_is_a_no_op_at_the_same_resolution():
    params = IndicatorParams(bar_minutes=60)
    assert params.rescaled_to(60) is params


def test_rescaling_round_trips():
    original = IndicatorParams(bar_minutes=60)
    assert original.rescaled_to(5).rescaled_to(60) == original


def test_rescaling_never_produces_a_degenerate_period():
    """Coarsening can round a short period toward zero; a 1-bar EMA is
    meaningless and a 0-bar one raises."""
    coarse = IndicatorParams(rsi_period=2, bar_minutes=5).rescaled_to(60)
    for field in IndicatorParams.PERIOD_FIELDS:
        assert getattr(coarse, field) >= 2, field


def test_min_bars_accounts_for_the_macd_signal_chain():
    """MACD needs slow + signal bars — more than any single period."""
    params = IndicatorParams(
        sma_slow=5, ema_periods=(5,), rsi_period=5, volume_sma_period=5,
        atr_period=5, macd_fast=12, macd_slow=26, macd_signal=9,
    )
    assert params.min_bars() == 26 + 9 + 2


def test_min_bars_accounts_for_the_longest_ema():
    """EMA(200) outlasts the MACD chain and becomes the binding constraint."""
    params = IndicatorParams(ema_periods=(9, 12, 200))
    assert params.min_bars() == 202


def test_indicators_are_complete_after_min_bars_at_five_minutes():
    params = IndicatorParams(bar_minutes=5)
    n = params.min_bars()
    rng = np.random.default_rng(5)
    closes = 100 + np.cumsum(rng.normal(0, 1, n))
    df = pd.DataFrame(
        {"open": closes, "high": closes + 1, "low": closes - 1,
         "close": closes, "volume": np.full(n, 1000.0)},
        index=pd.date_range("2026-09-02 13:30", periods=n, freq="5min", tz="UTC"),
    )
    last = add_indicators(df, params).iloc[-1]
    for col in (ema_column(9), COL_VWAP, COL_MACD, COL_MACD_SIGNAL, COL_MACD_HIST):
        assert not pd.isna(last[col]), f"{col} still NaN after min_bars()"


def test_describe_shows_both_units():
    text = IndicatorParams(bar_minutes=5).describe()
    assert "[5m bars]" in text and "sma_slow=30 (150m)" in text
