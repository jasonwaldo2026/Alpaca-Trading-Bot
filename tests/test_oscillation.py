"""
Detecting cyclical stocks — ones that travel a long way and end up nowhere.

Three measures, because no single one is enough:

- `efficiency`  — net move / distance travelled. Near 0 is choppy, 1 is a
  straight line. Alone it cannot tell a 5% oscillation from a 0.1% wobble.
- `range_pct`   — high-to-low amplitude. Supplies the "is it worth trading".
- `swings`      — completed 5% legs so far today. The most literal reading of
  "up and down over and over".
"""

import numpy as np
import pandas as pd
import pytest

from core.indicators import (
    IndicatorParams,
    add_indicators,
    efficiency_ratio,
    range_pct,
    swing_count,
)
from core.rules import load_rules
from core.sessions import session_day_series


def _alternating(legs: int, pct: float = 8.0, start: float = 100.0):
    """Prices that rise and fall by `pct` alternately — a clean oscillator."""
    prices = [start]
    for i in range(legs):
        prices.append(prices[-1] * (1 + pct / 100 if i % 2 == 0 else 1 / (1 + pct / 100)))
    return pd.Series(prices)


# ── Efficiency ratio ─────────────────────────────────────────────────────────

def test_a_straight_line_is_perfectly_efficient():
    trend = pd.Series(np.linspace(100, 140, 9))
    assert efficiency_ratio(trend, 8).iloc[-1] == pytest.approx(1.0)


def test_an_oscillator_that_returns_home_is_maximally_inefficient():
    osc = pd.Series([100.0, 105, 100, 105, 100, 105, 100, 105, 100])
    assert efficiency_ratio(osc, 8).iloc[-1] == pytest.approx(0.0)


def test_a_choppy_uptrend_sits_between():
    prices = pd.Series([100.0, 103, 101, 104, 102, 105, 103, 106, 108])
    ratio = efficiency_ratio(prices, 8).iloc[-1]
    assert 0.1 < ratio < 0.9


def test_a_flat_line_has_no_defined_efficiency():
    """Zero distance travelled — the ratio is undefined, not zero."""
    assert pd.isna(efficiency_ratio(pd.Series([100.0] * 9), 8).iloc[-1])


def test_efficiency_period_must_be_positive():
    with pytest.raises(ValueError, match="efficiency period"):
        efficiency_ratio(pd.Series([1.0, 2.0]), 0)


# ── Amplitude ────────────────────────────────────────────────────────────────

def test_range_pct_measures_high_to_low():
    df = pd.DataFrame({"high": [110.0] * 5, "low": [100.0] * 5})
    assert range_pct(df, 5).iloc[-1] == pytest.approx(10.0)


def test_range_pct_separates_a_real_swing_from_a_wobble():
    """Both are inefficient; only one is worth trading."""
    big = pd.DataFrame({"high": [105.0] * 9, "low": [95.0] * 9})
    tiny = pd.DataFrame({"high": [100.2] * 9, "low": [100.0] * 9})
    assert range_pct(big, 8).iloc[-1] > 10
    assert range_pct(tiny, 8).iloc[-1] < 1


# ── Swing counting ───────────────────────────────────────────────────────────

def test_each_completed_leg_counts_once():
    assert swing_count(_alternating(8), 5).iloc[-1] == 8


def test_a_trend_never_completes_a_second_leg():
    """One leg is established and never reverses."""
    assert swing_count(pd.Series(np.linspace(100, 140, 9)), 5).iloc[-1] == 1


def test_moves_below_the_threshold_are_not_swings():
    assert swing_count(_alternating(8, pct=2.0), 5).iloc[-1] == 0


def test_a_flat_series_has_no_swings():
    assert swing_count(pd.Series([100.0] * 20), 5).iloc[-1] == 0


def test_the_threshold_is_measured_from_the_extreme():
    """A trader means 'it pulled back 5% from the high', not 'from where it
    started'."""
    # Up to 110, then down to 104.5 — exactly 5% off the 110 high.
    prices = pd.Series([100.0, 110.0, 104.5])
    assert swing_count(prices, 5).iloc[-1] == 2
    # A shallower pullback does not complete the leg.
    assert swing_count(pd.Series([100.0, 110.0, 106.0]), 5).iloc[-1] == 1


def test_the_count_is_monotonic():
    counts = swing_count(_alternating(10), 5).tolist()
    assert counts == sorted(counts)


def test_swings_reset_each_trading_day():
    """Yesterday's swings are not today's opportunity."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")

    prices, stamps = [], []
    for day in (2, 3):
        legs = _alternating(6).tolist()
        prices += legs
        stamps += [datetime(2026, 9, day, 10, i, tzinfo=et) for i in range(len(legs))]

    series = pd.Series(prices, index=pd.DatetimeIndex(stamps))
    anchor = session_day_series(series.index, "AAPL")

    counted = swing_count(series, 5, anchor)
    first_day_final = counted.iloc[len(prices) // 2 - 1]
    second_day_first = counted.iloc[len(prices) // 2]
    assert first_day_final == 6
    assert second_day_first == 0, "day two starts fresh"


def test_swing_threshold_must_be_positive():
    with pytest.raises(ValueError, match="swing threshold"):
        swing_count(pd.Series([1.0]), 0)


def test_swing_anchor_must_align():
    with pytest.raises(ValueError, match="share an index"):
        swing_count(pd.Series([1.0, 2.0]), 5, pd.Series(["a"]))


# ── Wiring ───────────────────────────────────────────────────────────────────

def test_add_indicators_emits_the_oscillation_columns():
    n = 250
    prices = _alternating(n - 1, pct=6.0).values
    df = pd.DataFrame(
        {"open": prices, "high": prices * 1.01, "low": prices * 0.99,
         "close": prices, "volume": np.full(n, 1000.0)},
        index=pd.date_range("2026-09-02 13:30", periods=n, freq="5min", tz="UTC"),
    )
    out = add_indicators(df, IndicatorParams(bar_minutes=5))
    last = out.iloc[-1]
    assert last["swings"] > 10
    assert last["range_pct"] > 5
    assert last["efficiency"] < 0.5


def test_a_trending_series_scores_the_opposite_way():
    n = 250
    prices = np.linspace(100, 160, n)
    df = pd.DataFrame(
        {"open": prices, "high": prices * 1.001, "low": prices * 0.999,
         "close": prices, "volume": np.full(n, 1000.0)},
        index=pd.date_range("2026-09-02 13:30", periods=n, freq="5min", tz="UTC"),
    )
    last = add_indicators(df, IndicatorParams(bar_minutes=5)).iloc[-1]
    assert last["efficiency"] > 0.9
    assert last["swings"] <= 1


# ── The scenario ─────────────────────────────────────────────────────────────

def test_the_oscillator_scenario_loads_and_validates():
    rule = load_rules(["rules/oscillator.json"])[0]
    rule.validate()
    assert rule.params.swing_threshold_pct == 5.0
    assert "swings >= 4" in rule.describe()
    assert "efficiency <= 0.3" in rule.describe()


def test_the_oscillator_alert_names_the_risk():
    """A range trade fails when the range breaks; the message should say so."""
    from core.alerts import build_context
    rule = load_rules(["rules/oscillator.json"])[0]
    payload = rule.alert.render(build_context(
        "ABCD", 50.0, rule.name, rule.alert,
        {"swings": 6, "range_pct": 11.2, "efficiency": 0.12, "vwap": 49.8},
    ))
    assert "ABCD is cycling" in payload["message"]
    assert "range breaking" in payload["message"]


def test_the_oscillator_is_the_opposite_signal_to_vwap_hold():
    """Documented deliberately: one is momentum, the other mean reversion."""
    rule = load_rules(["rules/oscillator.json"])[0]
    assert "opposite of 'vwap hold'" in rule.description
