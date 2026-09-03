"""
Indicator tests.

The most important test here is test_matches_legacy_implementations: it pins
core.indicators against the original copy-pasted math that lived in
trading_bot.py and dashboard.py before they were unified, so the extraction
is provably behavior-preserving.
"""

import numpy as np
import pandas as pd
import pytest

from core.indicators import (
    IndicatorParams,
    add_indicators,
    atr,
    crossed_down,
    crossed_up,
    rsi,
    sma,
)


@pytest.fixture
def bars() -> pd.DataFrame:
    """Deterministic pseudo-random OHLCV, long enough for every warmup."""
    rng = np.random.default_rng(42)
    n = 300  # must exceed IndicatorParams().min_bars(), which EMA(200) drives
    close = 100 + np.cumsum(rng.normal(0, 1.5, n))
    high = close + rng.uniform(0.1, 2.0, n)
    low = close - rng.uniform(0.1, 2.0, n)
    return pd.DataFrame(
        {
            "open": close + rng.normal(0, 0.5, n),
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.integers(1_000, 100_000, n).astype(float),
        }
    )


# ── Known values ─────────────────────────────────────────────────────────────

def test_sma_known_values():
    s = pd.Series([1.0, 2, 3, 4, 5])
    result = sma(s, 3)
    assert result.isna().tolist() == [True, True, False, False, False]
    assert result.iloc[2] == pytest.approx(2.0)
    assert result.iloc[4] == pytest.approx(4.0)


def test_rsi_all_gains_is_nan_not_hundred():
    """A monotonically rising series has zero average loss. That divides by
    zero, and we map it to NaN so callers can distinguish 'no losses yet'
    from a real reading of 100."""
    s = pd.Series(np.arange(1.0, 30.0))
    assert pd.isna(rsi(s, 14).iloc[-1])


def test_rsi_bounded_and_warms_up(bars):
    result = rsi(bars["close"], 14)
    assert result.iloc[:13].isna().all(), "RSI must not emit before its period"
    valid = result.dropna()
    assert not valid.empty
    assert ((valid >= 0) & (valid <= 100)).all()


def test_rsi_falls_on_downtrend_rises_on_uptrend():
    """Both series oscillate (so avg_loss is non-zero and RSI is defined),
    but each has a clear net direction."""
    up = pd.Series(np.linspace(60, 100, 60) + np.tile([0.0, -1.5], 30))
    down = pd.Series(np.linspace(100, 60, 60) + np.tile([0.0, 1.5], 30))
    assert rsi(up, 14).iloc[-1] > 60
    assert rsi(down, 14).iloc[-1] < 40


def test_atr_equals_high_low_when_no_gaps():
    """With every close inside the prior bar's range, true range collapses to
    high - low, so a constant-width bar gives an ATR of that width."""
    df = pd.DataFrame({
        "high": [11.0] * 40,
        "low": [9.0] * 40,
        "close": [10.0] * 40,
        "open": [10.0] * 40,
        "volume": [1.0] * 40,
    })
    assert atr(df, 14).iloc[-1] == pytest.approx(2.0)


def test_atr_captures_overnight_gap():
    """A gap up means high-low understates the move; ATR must use the
    prior close instead."""
    df = pd.DataFrame({
        "high": [11.0] * 20 + [30.0],
        "low": [9.0] * 20 + [29.0],
        "close": [10.0] * 20 + [29.5],
        "open": [10.0] * 21,
        "volume": [1.0] * 21,
    })
    plain_range = 1.0
    assert atr(df, 14).iloc[-1] > plain_range


# ── add_indicators ───────────────────────────────────────────────────────────

def test_add_indicators_adds_all_columns_without_mutating_input(bars):
    before = bars.copy()
    out = add_indicators(bars, IndicatorParams())
    for col in ("sma_fast", "sma_slow", "rsi", "vol_sma", "atr"):
        assert col in out.columns
    pd.testing.assert_frame_equal(bars, before), "input frame must not be mutated"


def test_add_indicators_rejects_missing_columns():
    df = pd.DataFrame({"close": [1.0, 2.0], "volume": [1.0, 2.0]})
    with pytest.raises(ValueError, match="missing required column"):
        add_indicators(df, IndicatorParams())


def test_min_bars_covers_longest_period():
    """Isolates the SMA/volume path — EMA and MACD are shortened so they do
    not become the binding constraint."""
    params = IndicatorParams(
        sma_slow=30, rsi_period=14, volume_sma_period=50, atr_period=14,
        ema_periods=(9,), macd_fast=3, macd_slow=6, macd_signal=3,
    )
    assert params.min_bars() == 52


def test_indicators_are_complete_after_min_bars(bars):
    params = IndicatorParams()
    window = bars.iloc[: params.min_bars()]
    out = add_indicators(window, params)
    assert not out.iloc[-1][list(("sma_fast", "sma_slow", "rsi", "vol_sma", "atr"))].isna().any()


# ── Crossovers ───────────────────────────────────────────────────────────────

def test_crossed_up_and_down():
    prev = pd.Series({"f": 1.0, "s": 2.0})
    curr = pd.Series({"f": 3.0, "s": 2.0})
    assert crossed_up(prev, curr, "f", "s")
    assert not crossed_down(prev, curr, "f", "s")
    assert crossed_down(curr, prev, "f", "s")


def test_no_cross_when_merely_touching():
    """Equal-then-equal is not a cross; the fast line must end strictly
    past the slow one."""
    prev = pd.Series({"f": 2.0, "s": 2.0})
    curr = pd.Series({"f": 2.0, "s": 2.0})
    assert not crossed_up(prev, curr, "f", "s")
    assert not crossed_down(prev, curr, "f", "s")


# ── Regression: the extraction must not change any number ────────────────────

def _legacy_rsi(series, period):
    """Verbatim from the pre-refactor trading_bot.py / dashboard.py."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def _legacy_atr(df, period):
    """Verbatim from the pre-refactor trading_bot.py / dashboard.py."""
    prev_close = df["close"].shift()
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(com=period - 1, min_periods=period).mean()


def test_matches_legacy_implementations(bars):
    """core.indicators must reproduce the old duplicated math exactly."""
    params = IndicatorParams()
    out = add_indicators(bars, params)

    pd.testing.assert_series_equal(
        out["rsi"], _legacy_rsi(bars["close"], params.rsi_period), check_names=False
    )
    pd.testing.assert_series_equal(
        out["atr"], _legacy_atr(bars, params.atr_period), check_names=False
    )
    pd.testing.assert_series_equal(
        out["sma_fast"], bars["close"].rolling(params.sma_fast).mean(), check_names=False
    )
    pd.testing.assert_series_equal(
        out["sma_slow"], bars["close"].rolling(params.sma_slow).mean(), check_names=False
    )
    pd.testing.assert_series_equal(
        out["vol_sma"],
        bars["volume"].rolling(params.volume_sma_period).mean(),
        check_names=False,
    )
