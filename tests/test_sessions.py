"""Market session, cadence, and horizon tests."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from core import sessions as S
from core.indicators import IndicatorParams, add_indicators, volume_sma_by_session

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def et(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


# A Wednesday, an ordinary full trading day.
WED = date(2026, 9, 2)


# ── Session classification ───────────────────────────────────────────────────

@pytest.mark.parametrize("hh,mm,expected", [
    (3, 59, S.CLOSED),
    (4, 0, S.PRE),
    (9, 29, S.PRE),
    (9, 30, S.REGULAR),
    (15, 59, S.REGULAR),
    (16, 0, S.AFTER),
    (19, 59, S.AFTER),
    (20, 0, S.CLOSED),
])
def test_session_boundaries(hh, mm, expected):
    assert S.session_at(et(2026, 9, 2, hh, mm), "AAPL") == expected


def test_crypto_has_no_sessions():
    for hh in (0, 4, 10, 17, 23):
        assert S.session_at(et(2026, 9, 2, hh), "BTC/USD") == S.CRYPTO


def test_weekend_is_closed_for_stocks_open_for_crypto():
    saturday = et(2026, 9, 5, 12)
    assert S.session_at(saturday, "AAPL") == S.CLOSED
    assert S.session_at(saturday, "BTC/USD") == S.CRYPTO


def test_holiday_is_closed_when_supplied():
    cal = S.SessionCalendar(holidays=frozenset({WED}))
    assert S.session_at(et(2026, 9, 2, 12), "AAPL", cal) == S.CLOSED


def test_early_close_shortens_regular_session():
    cal = S.SessionCalendar(early_closes=frozenset({WED}))
    assert S.session_at(et(2026, 9, 2, 12, 59), "AAPL", cal) == S.REGULAR
    assert S.session_at(et(2026, 9, 2, 13, 30), "AAPL", cal) == S.AFTER
    assert S.session_at(et(2026, 9, 2, 17, 30), "AAPL", cal) == S.CLOSED


def test_naive_timestamps_are_treated_as_utc():
    """Alpaca returns UTC. Guessing local time would shift every boundary."""
    naive = datetime(2026, 9, 2, 14, 0)          # 14:00 UTC == 10:00 ET
    aware = datetime(2026, 9, 2, 14, 0, tzinfo=UTC)
    assert S.session_at(naive, "AAPL") == S.session_at(aware, "AAPL") == S.REGULAR


def test_dst_boundary_uses_eastern_not_fixed_offset():
    """In March ET is UTC-4; in January it is UTC-5. A fixed offset would
    misclassify one of them."""
    # 14:00 UTC → 09:00 ET in winter (pre-market), 10:00 ET in summer (regular)
    assert S.session_at(datetime(2026, 1, 7, 14, 0, tzinfo=UTC), "AAPL") == S.PRE
    assert S.session_at(datetime(2026, 7, 8, 14, 0, tzinfo=UTC), "AAPL") == S.REGULAR


# ── Tradability ──────────────────────────────────────────────────────────────

def test_regular_only_config_rejects_extended_hours():
    cfg = S.SessionConfig.regular_only()
    assert S.is_tradable(et(2026, 9, 2, 11), "AAPL", cfg)
    assert not S.is_tradable(et(2026, 9, 2, 18), "AAPL", cfg)
    assert not S.is_tradable(et(2026, 9, 2, 6), "AAPL", cfg)


def test_after_hours_config_accepts_after_but_not_pre():
    cfg = S.SessionConfig.after_hours()
    assert S.is_tradable(et(2026, 9, 2, 18), "AAPL", cfg)
    assert not S.is_tradable(et(2026, 9, 2, 6), "AAPL", cfg)


def test_crypto_is_always_tradable():
    cfg = S.SessionConfig.regular_only()
    assert S.is_tradable(et(2026, 9, 5, 3), "BTC/USD", cfg)


# ── Cadence ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cfg,expected", [
    (S.SessionConfig.regular_only(), 7),   # 09:30–16:00 → hours 9..15
    (S.SessionConfig.after_hours(), 11),   # 09:30–20:00 → hours 9..19
    (S.SessionConfig.extended(), 16),      # 04:00–20:00 → hours 4..19
])
def test_bars_per_day_by_config(cfg, expected):
    assert S.bars_per_day("AAPL", cfg, day=WED) == expected


def test_crypto_bars_per_day_is_24():
    assert S.bars_per_day("BTC/USD") == 24


def test_overlapping_sessions_merge_into_one_bar():
    """09:00–09:30 (pre) and 09:30–10:00 (regular) are one clock-hour bucket,
    so enabling pre-market adds 5 bars, not 6."""
    regular = S.bars_per_day("AAPL", S.SessionConfig.regular_only(), day=WED)
    with_pre = S.bars_per_day("AAPL", S.SessionConfig(pre=True, regular=True), day=WED)
    assert with_pre - regular == 5


def test_no_bars_on_a_weekend():
    assert S.bars_per_day("AAPL", day=date(2026, 9, 5)) == 0


def test_scan_times_land_after_each_bar_closes():
    times = S.scan_times("AAPL", S.SessionConfig.regular_only(), day=WED)
    assert times[0] == time(10, 2), "first scan reads the 09:30–10:00 bar"
    assert times[-1] == time(16, 2), "last scan reads the 15:00–16:00 bar"
    assert len(times) == 7


def test_after_hours_scan_times_extend_to_2002():
    times = S.scan_times("AAPL", S.SessionConfig.after_hours(), day=WED)
    assert times[-1] == time(20, 2)
    assert len(times) == 11


def test_crypto_scans_every_hour():
    assert len(S.scan_times("BTC/USD")) == 24


def test_cycles_per_day_reports_both_classes():
    out = S.cycles_per_day(["AAPL", "BTC/USD"], S.SessionConfig.regular_only())
    assert out == {"stock": 7, "crypto": 24}


# ── Horizons — the outcomes-table fix ────────────────────────────────────────

def test_same_horizon_is_different_bar_counts_per_asset():
    """This is the whole point: '1 day' is 7 bars for an RTH equity and 24
    for crypto. A shared '+20 bars' column compares unlike things."""
    cfg = S.SessionConfig.regular_only()
    assert S.horizon_to_bars("1d", "AAPL", cfg) == 7
    assert S.horizon_to_bars("1d", "BTC/USD", cfg) == 24


def test_multi_day_horizons_scale():
    cfg = S.SessionConfig.regular_only()
    assert S.horizon_to_bars("5d", "AAPL", cfg) == 35
    assert S.horizon_to_bars("20d", "AAPL", cfg) == 140


def test_hour_horizons_are_one_bar_each():
    assert S.horizon_to_bars("1h", "AAPL") == 1
    assert S.horizon_to_bars("4h", "BTC/USD") == 4


def test_enabling_after_hours_changes_a_day_horizon():
    assert S.horizon_to_bars("1d", "AAPL", S.SessionConfig.regular_only()) == 7
    assert S.horizon_to_bars("1d", "AAPL", S.SessionConfig.after_hours()) == 11


def test_unknown_horizon_raises_rather_than_guessing():
    with pytest.raises(KeyError, match="Unknown horizon"):
        S.horizon_to_bars("3w", "AAPL")


def test_describe_horizon_labels_a_column():
    label = S.describe_horizon("1d", "AAPL", S.SessionConfig.regular_only())
    assert label == "1d (7 bars)"


# ── Session-aware volume baseline ────────────────────────────────────────────

def _extended_hours_frame() -> pd.DataFrame:
    """Three trading days of hourly bars over 04:00–20:00 ET, where
    regular-hours volume is ~50x extended-hours volume."""
    stamps = []
    for day in (2, 3, 4):
        for hour in range(4, 20):
            stamps.append(datetime(2026, 9, day, hour, 0, tzinfo=ET))
    idx = pd.DatetimeIndex(stamps)
    sess = [S.session_at(ts, "AAPL") for ts in stamps]
    volume = np.array([50_000.0 if s == S.REGULAR else 1_000.0 for s in sess])
    close = np.linspace(100, 105, len(idx))
    return pd.DataFrame(
        {"open": close, "high": close + 0.5, "low": close - 0.5,
         "close": close, "volume": volume},
        index=idx,
    )


def test_session_series_labels_every_bar():
    df = _extended_hours_frame()
    series = S.session_series(df.index, "AAPL")
    assert set(series.unique()) == {S.PRE, S.REGULAR, S.AFTER}
    assert len(series) == len(df)


def test_session_series_rejects_non_datetime_index():
    with pytest.raises(TypeError, match="DatetimeIndex"):
        S.session_series(pd.RangeIndex(5), "AAPL")


def test_flat_volume_baseline_makes_the_condition_meaningless():
    """Without session grouping, an after-hours bar can never beat an average
    dominated by regular-hours volume — the condition degenerates into
    'is it regular hours'."""
    df = _extended_hours_frame()
    flat = add_indicators(df, IndicatorParams(volume_sma_period=5))
    after = flat[S.session_series(df.index, "AAPL") == S.AFTER].dropna()
    assert (after["volume"] > after["vol_sma"]).sum() == 0


def test_session_baseline_compares_like_with_like():
    """Grouped by session, a quiet after-hours bar sits at its own session's
    average rather than being buried by regular-hours volume."""
    df = _extended_hours_frame()
    series = S.session_series(df.index, "AAPL")
    grouped = add_indicators(df, IndicatorParams(volume_sma_period=3), series)
    after = grouped[series == S.AFTER].dropna()
    assert np.allclose(after["vol_sma"], 1_000.0), (
        "after-hours bars should average against after-hours volume"
    )
    regular = grouped[series == S.REGULAR].dropna()
    assert np.allclose(regular["vol_sma"], 50_000.0)


def test_session_baseline_detects_an_unusual_after_hours_bar():
    """The behavior that makes after-hours usable: a genuine volume spike
    after the close is visible, where the flat baseline hid it."""
    df = _extended_hours_frame()
    series = S.session_series(df.index, "AAPL")
    spike = df.index[-2]                     # an after-hours bar
    assert series.loc[spike] == S.AFTER
    df.loc[spike, "volume"] = 20_000.0       # 20x its session norm, still < RTH

    # The period must be long enough that the flat window reaches back into
    # regular hours — that overlap is exactly what buries the spike.
    flat = add_indicators(df, IndicatorParams(volume_sma_period=8))
    grouped = add_indicators(df, IndicatorParams(volume_sma_period=8), series)

    assert not flat.loc[spike, "volume"] > flat.loc[spike, "vol_sma"], (
        "flat baseline misses the spike"
    )
    assert grouped.loc[spike, "volume"] > grouped.loc[spike, "vol_sma"], (
        "session baseline catches it"
    )


def test_volume_sma_by_session_requires_aligned_index():
    df = _extended_hours_frame()
    with pytest.raises(ValueError, match="share an index"):
        volume_sma_by_session(df["volume"], pd.Series(["a", "b"]), 3)
