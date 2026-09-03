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


# ── Cadence (hourly bars) ────────────────────────────────────────────────────
# These document the arithmetic at HOUR_1, so they pass it explicitly; the
# shipped default is 5-minute bars (see test_default_bar_size_is_five_minutes).

HOUR = S.HOUR_1

@pytest.mark.parametrize("cfg,expected", [
    (S.SessionConfig.regular_only(), 7),   # 09:30–16:00 → hours 9..15
    (S.SessionConfig.after_hours(), 11),   # 09:30–20:00 → hours 9..19
    (S.SessionConfig.extended(), 16),      # 04:00–20:00 → hours 4..19
])
def test_bars_per_day_by_config(cfg, expected):
    assert S.bars_per_day("AAPL", cfg, day=WED, bar_minutes=HOUR) == expected


def test_crypto_bars_per_day_is_24():
    assert S.bars_per_day("BTC/USD", bar_minutes=HOUR) == 24


def test_overlapping_sessions_merge_into_one_bar():
    """09:00–09:30 (pre) and 09:30–10:00 (regular) are one clock-hour bucket,
    so enabling pre-market adds 5 bars, not 6."""
    regular = S.bars_per_day("AAPL", S.SessionConfig.regular_only(), day=WED, bar_minutes=HOUR)
    with_pre = S.bars_per_day("AAPL", S.SessionConfig(pre=True, regular=True), day=WED, bar_minutes=HOUR)
    assert with_pre - regular == 5


def test_no_bars_on_a_weekend():
    assert S.bars_per_day("AAPL", day=date(2026, 9, 5)) == 0


def test_scan_times_land_after_each_bar_closes():
    times = S.scan_times("AAPL", S.SessionConfig.regular_only(), day=WED, bar_minutes=HOUR)
    assert times[0] == time(10, 2), "first scan reads the 09:30–10:00 bar"
    assert times[-1] == time(16, 2), "last scan reads the 15:00–16:00 bar"
    assert len(times) == 7


def test_after_hours_scan_times_extend_to_2002():
    times = S.scan_times("AAPL", S.SessionConfig.after_hours(), day=WED, bar_minutes=HOUR)
    assert times[-1] == time(20, 2)
    assert len(times) == 11


def test_crypto_scans_every_hour():
    assert len(S.scan_times("BTC/USD", bar_minutes=HOUR)) == 24


def test_cycles_per_day_reports_both_classes():
    out = S.cycles_per_day(["AAPL", "BTC/USD"], S.SessionConfig.regular_only(), bar_minutes=HOUR)
    assert out == {"stock": 7, "crypto": 24}


# ── Horizons — the outcomes-table fix ────────────────────────────────────────

def test_same_horizon_is_different_bar_counts_per_asset():
    """This is the whole point: '1 day' is 7 bars for an RTH equity and 24
    for crypto. A shared '+20 bars' column compares unlike things."""
    cfg = S.SessionConfig.regular_only()
    assert S.horizon_to_bars("1d", "AAPL", cfg, bar_minutes=HOUR) == 7
    assert S.horizon_to_bars("1d", "BTC/USD", cfg, bar_minutes=HOUR) == 24


def test_multi_day_horizons_scale():
    cfg = S.SessionConfig.regular_only()
    assert S.horizon_to_bars("5d", "AAPL", cfg, bar_minutes=HOUR) == 35
    assert S.horizon_to_bars("20d", "AAPL", cfg, bar_minutes=HOUR) == 140


def test_hour_horizons_are_one_bar_each():
    assert S.horizon_to_bars("1h", "AAPL", bar_minutes=HOUR) == 1
    assert S.horizon_to_bars("4h", "BTC/USD", bar_minutes=HOUR) == 4


def test_enabling_after_hours_changes_a_day_horizon():
    assert S.horizon_to_bars("1d", "AAPL", S.SessionConfig.regular_only(), bar_minutes=HOUR) == 7
    assert S.horizon_to_bars("1d", "AAPL", S.SessionConfig.after_hours(), bar_minutes=HOUR) == 11


def test_unknown_horizon_raises_rather_than_guessing():
    with pytest.raises(KeyError, match="Unknown horizon"):
        S.horizon_to_bars("3w", "AAPL", bar_minutes=HOUR)


def test_describe_horizon_labels_a_column():
    label = S.describe_horizon("1d", "AAPL", S.SessionConfig.regular_only(), bar_minutes=HOUR)
    assert label == "1d (7 bars)"


def test_default_bar_size_is_five_minutes():
    """Every app ships at 5-minute bars. A different default in core is how a
    caller that forgets to pass bar_minutes evaluates 5-minute rules on
    hourly data without anything complaining."""
    assert S.DEFAULT_BAR_MINUTES == S.MINUTE_5
    assert S.horizon_to_bars("1d", "AAPL", S.SessionConfig.regular_only()) == 78


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


# ── Bar size (timeframe) ─────────────────────────────────────────────────────

@pytest.mark.parametrize("bar_minutes,rth,extended,crypto", [
    (60, 7, 16, 24),
    (30, 13, 32, 48),
    (15, 26, 64, 96),
    (5, 78, 192, 288),
])
def test_bars_per_day_scales_with_bar_size(bar_minutes, rth, extended, crypto):
    assert S.bars_per_day(
        "AAPL", S.SessionConfig.regular_only(), day=WED, bar_minutes=bar_minutes
    ) == rth
    assert S.bars_per_day(
        "AAPL", S.SessionConfig.extended(), day=WED, bar_minutes=bar_minutes
    ) == extended
    assert S.bars_per_day("BTC/USD", bar_minutes=bar_minutes) == crypto


def test_five_minute_regular_session_divides_evenly():
    """390 minutes of regular session / 5 = 78 bars, no stub — unlike hourly,
    where 09:30 falls mid-bar."""
    assert S.bars_per_day(
        "AAPL", S.SessionConfig.regular_only(), day=WED, bar_minutes=5
    ) == 390 // 5


def test_hourly_open_bar_is_a_stub_but_still_counts():
    """09:30–10:00 is half an hour of trading in one hourly bucket. It is one
    bar, which is why regular hours give 7 and not 6."""
    buckets = S._bar_buckets(time(9, 30), time(16, 0), 60)
    assert min(buckets) == 9 and max(buckets) == 15
    assert len(buckets) == 7


def test_bar_size_must_divide_into_a_day():
    with pytest.raises(ValueError, match="divide evenly"):
        S.bars_per_day("AAPL", day=WED, bar_minutes=7)


def test_five_minute_scan_times_start_after_the_opening_bar():
    times = S.scan_times(
        "AAPL", S.SessionConfig.regular_only(), day=WED, bar_minutes=5
    )
    assert times[0] == time(9, 36), "09:30–09:35 bar closes at 09:35, +1 min"
    assert times[-1] == time(16, 1)
    assert len(times) == 78


def test_scan_delay_shrinks_for_short_bars():
    """Two minutes is fine on an hourly bar and absurd on a 5-minute one."""
    assert S.default_delay_minutes(60) == 2
    assert S.default_delay_minutes(15) == 2
    assert S.default_delay_minutes(5) == 1


def test_hour_horizon_scales_with_bar_size():
    assert S.horizon_to_bars("1h", "AAPL", bar_minutes=60) == 1
    assert S.horizon_to_bars("1h", "AAPL", bar_minutes=15) == 4
    assert S.horizon_to_bars("1h", "AAPL", bar_minutes=5) == 12


def test_day_horizon_is_a_session_at_any_bar_size():
    """The whole point of wall-clock horizons: '1d' is one session whether
    that is 7 bars or 78."""
    cfg = S.SessionConfig.regular_only()
    assert S.horizon_to_bars("1d", "AAPL", cfg, bar_minutes=60) == 7
    assert S.horizon_to_bars("1d", "AAPL", cfg, bar_minutes=5) == 78
    assert S.horizon_to_bars("1d", "BTC/USD", cfg, bar_minutes=5) == 288


def test_describe_horizon_reflects_bar_size():
    cfg = S.SessionConfig.regular_only()
    assert S.describe_horizon("1d", "AAPL", cfg, bar_minutes=5) == "1d (78 bars)"


def test_cycles_per_day_reflects_bar_size():
    out = S.cycles_per_day(
        ["AAPL", "BTC/USD"], S.SessionConfig.regular_only(), bar_minutes=5
    )
    assert out == {"stock": 78, "crypto": 288}


# ── Config safety at fine resolutions ────────────────────────────────────────

def test_bar_limit_too_small_for_five_minute_bars_is_rejected():
    """
    The worst failure mode available: too few bars means every indicator is
    NaN, every symbol is skipped, and the bot looks healthy while never
    trading. It must fail at construction instead.
    """
    from bot.config import BotConfig

    config = BotConfig(
        api_key="k", api_secret="s",
        bar_minutes=5, indicator_period_basis=60, bar_limit=60,
    )
    with pytest.raises(ValueError, match="too small for 5-minute bars"):
        config.validate()


def test_the_error_names_the_bar_limit_that_would_work():
    from bot.config import BotConfig

    config = BotConfig(
        api_key="k", api_secret="s",
        bar_minutes=5, indicator_period_basis=60, bar_limit=60,
    )
    try:
        config.validate()
    except ValueError as exc:
        assert str(config.required_bar_limit()) in str(exc)
    else:
        pytest.fail("expected ValueError")


def test_default_hourly_config_is_valid():
    from bot.config import BotConfig
    BotConfig(api_key="k", api_secret="s").validate()


def test_conventional_five_minute_periods_need_no_extra_history():
    """Without a rescale basis the periods stay 12/26/9 bars, so the default
    bar_limit is still enough."""
    from bot.config import BotConfig
    BotConfig(api_key="k", api_secret="s", bar_minutes=5).validate()


def test_period_basis_rescales_config_periods():
    from bot.config import BotConfig
    config = BotConfig(
        api_key="k", api_secret="s",
        bar_minutes=5, indicator_period_basis=60, bar_limit=600,
    )
    params = config.indicator_params()
    assert params.bar_minutes == 5
    assert params.sma_slow == 360          # 30 hourly bars = 30 hours
    assert params.duration_minutes("sma_slow") == 1800


def test_without_a_basis_periods_are_bar_counts_at_the_data_resolution():
    from bot.config import BotConfig
    config = BotConfig(api_key="k", api_secret="s", bar_minutes=5)
    params = config.indicator_params()
    assert params.sma_slow == 30
    assert params.duration_minutes("sma_slow") == 150


def test_invalid_bar_minutes_is_rejected():
    from bot.config import BotConfig
    with pytest.raises(ValueError, match="divide evenly"):
        BotConfig(api_key="k", api_secret="s", bar_minutes=7).validate()


def test_session_baseline_is_defined_from_a_sessions_first_bar():
    """The window restarts with each session. Requiring a full period there
    would leave the first 95 minutes after the open — the time of day the
    volume gate matters most — with no baseline at all."""
    df = _extended_hours_frame()
    series = S.session_series(df.index, "AAPL")
    grouped = add_indicators(df, IndicatorParams(volume_sma_period=20), series)
    first_regular = df.index[series == S.REGULAR][0]
    assert not np.isnan(grouped.loc[first_regular, "vol_sma"])
    assert grouped.loc[first_regular, "vol_sma"] == df.loc[first_regular, "volume"]
