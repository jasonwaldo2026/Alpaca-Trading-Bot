"""
Pre-market at 5-minute resolution.

Two things are easy to get wrong here and both are covered:

1. **Cadence.** The pre-market session opens at 04:00, but the first
   *actionable* scan is 04:06 — the 04:00-04:05 bar does not exist until
   04:05, and acting before it closes is the phantom-signal bug.
2. **Sparsity.** Alpaca builds bars from trades, so a 5-minute window with
   no trades produces no bar at all. A pre-market series of N bars can span
   far more wall-clock time than N * 5 minutes, which stretches every
   rolling indicator.
"""

from datetime import date, datetime, time

import numpy as np
import pandas as pd
import pytest
from zoneinfo import ZoneInfo

from bot.config import BotConfig
from core import sessions as S
from core.data import bar_coverage
from core.indicators import IndicatorParams, add_indicators, ema_column, indicator_columns

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
WED = date(2026, 9, 2)


# ── Cadence from 04:00 ───────────────────────────────────────────────────────

def test_premarket_five_minute_scans_begin_just_after_four_am():
    times = S.scan_times("AAPL", S.SessionConfig.extended(), day=WED, bar_minutes=5)
    assert times[0] == time(4, 6), (
        "the 04:00-04:05 bar closes at 04:05; +1 min delay makes 04:06"
    )
    assert times[-1] == time(20, 1)


def test_extended_five_minute_day_is_192_scans():
    """04:00-20:00 is 960 minutes = 192 five-minute bars."""
    assert S.bars_per_day(
        "AAPL", S.SessionConfig.extended(), day=WED, bar_minutes=5
    ) == 192
    assert len(
        S.scan_times("AAPL", S.SessionConfig.extended(), day=WED, bar_minutes=5)
    ) == 192


def test_premarket_adds_66_scans_over_regular_plus_after():
    """04:00-09:30 is 330 minutes = 66 five-minute bars, and none of them
    overlap the regular session at this resolution."""
    with_pre = S.bars_per_day("AAPL", S.SessionConfig.extended(), day=WED, bar_minutes=5)
    without = S.bars_per_day("AAPL", S.SessionConfig.after_hours(), day=WED, bar_minutes=5)
    assert with_pre - without == 66


def test_four_am_is_tradable_under_the_extended_config():
    cfg = S.SessionConfig.extended()
    assert S.is_tradable(datetime(2026, 9, 2, 4, 0, tzinfo=ET), "AAPL", cfg)
    assert not S.is_tradable(datetime(2026, 9, 2, 3, 59, tzinfo=ET), "AAPL", cfg)


def test_a_bar_opening_at_0400_is_premarket():
    assert S.session_at(datetime(2026, 9, 2, 4, 0, tzinfo=ET), "AAPL") == S.PRE


# ── Default configuration ────────────────────────────────────────────────────

def test_default_config_runs_premarket_five_minute():
    config = BotConfig(api_key="k", api_secret="s")
    config.validate()
    assert config.bar_minutes == 5
    assert config.sessions.pre and config.sessions.regular and config.sessions.after


def test_default_config_carries_the_9_12_200_emas():
    params = BotConfig(api_key="k", api_secret="s").indicator_params()
    assert params.ema_periods == (9, 12, 200)


def test_default_bar_limit_covers_the_200_ema():
    config = BotConfig(api_key="k", api_secret="s")
    assert config.bar_limit >= config.required_bar_limit()
    assert config.indicator_params().min_bars() == 202


def test_ema_200_at_five_minutes_spans_a_day_and_a_half_of_trading():
    """1000 minutes of trading time, against a 960-minute extended session."""
    params = BotConfig(api_key="k", api_secret="s").indicator_params()
    assert params.ema_duration_minutes(200) == 1000


# ── Multiple EMAs ────────────────────────────────────────────────────────────

def test_each_ema_period_gets_its_own_column():
    params = IndicatorParams(ema_periods=(9, 12, 200), bar_minutes=5)
    for period in (9, 12, 200):
        assert ema_column(period) in indicator_columns(params)


def test_ema_columns_are_computed():
    n = 250
    rng = np.random.default_rng(4)
    closes = 100 + np.cumsum(rng.normal(0, 0.5, n))
    df = pd.DataFrame(
        {"open": closes, "high": closes + 0.3, "low": closes - 0.3,
         "close": closes, "volume": np.full(n, 1000.0)},
        index=pd.date_range("2026-09-02 09:00", periods=n, freq="5min", tz="UTC"),
    )
    out = add_indicators(df, IndicatorParams(ema_periods=(9, 12, 200), bar_minutes=5))
    assert not pd.isna(out[ema_column(9)].iloc[-1])
    assert not pd.isna(out[ema_column(200)].iloc[-1])
    # A shorter EMA tracks price more closely than a longer one.
    assert abs(out[ema_column(9)].iloc[-1] - closes[-1]) < abs(
        out[ema_column(200)].iloc[-1] - closes[-1]
    )


def test_ema_periods_survive_a_json_round_trip_as_a_tuple():
    """Rules serialize to JSON, where a tuple becomes a list. A list would
    make IndicatorParams unhashable and break the scanner's cache key."""
    from core.rules import Condition, Rule
    rule = Rule(
        name="ema stack", conditions=[Condition("ema_9", ">", field2="ema_200")],
        params=IndicatorParams(ema_periods=(9, 12, 200)),
    )
    restored = Rule.from_json(rule.to_json())
    assert restored.params.ema_periods == (9, 12, 200)
    assert isinstance(restored.params.ema_periods, tuple)
    assert hash(restored.params) == hash(rule.params)


def test_ema_periods_are_sorted_for_a_stable_identity():
    assert IndicatorParams(ema_periods=(200, 9, 12)).ema_periods == (9, 12, 200)
    assert IndicatorParams(ema_periods=(9, 12, 200)) == IndicatorParams(
        ema_periods=(200, 12, 9)
    )


def test_degenerate_ema_period_is_rejected():
    with pytest.raises(ValueError, match="must be >= 2"):
        IndicatorParams(ema_periods=(1,))


def test_ema_periods_rescale_with_resolution():
    five = IndicatorParams(ema_periods=(9, 12, 200), bar_minutes=60).rescaled_to(5)
    assert five.ema_periods == (108, 144, 2400)


# ── Sparse pre-market bars ───────────────────────────────────────────────────

def _sparse_premarket_frame() -> pd.DataFrame:
    """Pre-market bars with realistic gaps — trades, and therefore bars,
    arrive only intermittently."""
    stamps = pd.DatetimeIndex([
        datetime(2026, 9, 2, h, m, tzinfo=ET)
        for h, m in [(4, 0), (4, 35), (5, 10), (5, 15), (6, 40), (7, 0),
                     (8, 25), (9, 5), (9, 25)]
    ])
    closes = np.linspace(100, 101, len(stamps))
    return pd.DataFrame(
        {"open": closes, "high": closes + 0.1, "low": closes - 0.1,
         "close": closes, "volume": np.full(len(stamps), 500.0)},
        index=stamps,
    )


def test_sparse_premarket_is_detected():
    coverage = bar_coverage(_sparse_premarket_frame(), 5)
    assert coverage.is_sparse()
    assert coverage.bars == 9
    assert coverage.largest_gap_minutes >= 30


def test_dense_regular_hours_is_not_flagged():
    df = pd.DataFrame(
        {"open": [1.0] * 40, "high": [1.0] * 40, "low": [1.0] * 40,
         "close": [1.0] * 40, "volume": [1.0] * 40},
        index=pd.date_range("2026-09-02 14:00", periods=40, freq="5min", tz="UTC"),
    )
    coverage = bar_coverage(df, 5)
    assert not coverage.is_sparse()
    assert coverage.largest_gap_minutes == 5


def test_sparse_bars_stretch_a_rolling_window():
    """The consequence worth knowing: 9 sparse bars span 325 minutes, so a
    9-period average there reaches back five hours, not 45 minutes."""
    coverage = bar_coverage(_sparse_premarket_frame(), 5)
    naive_span = 9 * 5
    assert coverage.span_minutes > naive_span * 3


def test_coverage_is_none_when_it_cannot_be_measured():
    assert bar_coverage(pd.DataFrame(), 5) is None
    assert bar_coverage(pd.DataFrame({"close": [1.0]}), 5) is None


def test_scanner_reports_sparse_symbols():
    from core.rules import Condition, Rule
    from scanner.engine import Scanner

    sparse = _sparse_premarket_frame()

    class FakeFetcher:
        def get_bars(self, symbols, limit=60, timeframe=None):
            return {s: sparse for s in symbols}

    rule = Rule(
        name="any", universe=["AAPL"],
        conditions=[Condition("close", ">", value=0)],
        params=IndicatorParams(
            sma_fast=2, sma_slow=3, ema_periods=(2, 3), rsi_period=2,
            volume_sma_period=2, atr_period=2,
            macd_fast=2, macd_slow=3, macd_signal=2, bar_minutes=5,
        ),
    )
    result = Scanner(FakeFetcher(), bar_minutes=5, skip_closed=False).scan([rule])
    assert "AAPL" in result.sparse
