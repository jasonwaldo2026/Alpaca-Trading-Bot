"""
enrich() is the one path from raw bars to indicator columns.

It exists because the anchor and the session labels were being derived by
each app separately, and some forgot: the volume baseline was per-session
only when the *config* enabled extended hours, while Alpaca returns extended
bars regardless.
"""

import numpy as np
import pandas as pd

from core import sessions as S
from core.enrich import enrich
from core.indicators import COL_VOL_SMA, COL_VWAP, IndicatorParams


def _two_session_frame():
    """Regular-hours bars followed by after-hours bars, at very different
    volume, across two days."""
    stamps = []
    for day in (1, 2):
        stamps += list(pd.date_range(f"2026-09-0{day} 09:30", periods=12, freq="30min", tz=S.ET))
        stamps += list(pd.date_range(f"2026-09-0{day} 16:00", periods=6, freq="30min", tz=S.ET))
    idx = pd.DatetimeIndex(stamps)
    sess = S.session_series(idx, "AAPL")
    volume = np.where(sess == S.REGULAR, 50_000.0, 1_000.0)
    close = np.linspace(100, 104, len(idx))
    return pd.DataFrame(
        {"open": close, "high": close + 0.5, "low": close - 0.5,
         "close": close, "volume": volume},
        index=idx,
    ), sess


def test_volume_baseline_is_per_session_without_being_asked():
    df, sess = _two_session_frame()
    out = enrich(df, IndicatorParams(volume_sma_period=3, bar_minutes=30), "AAPL")
    after = out[sess == S.AFTER]
    assert np.allclose(after[COL_VOL_SMA], 1_000.0), (
        "after-hours bars must average against after-hours volume even "
        "though nobody passed a session series"
    )


def test_vwap_restarts_each_day():
    df, _ = _two_session_frame()
    out = enrich(df, IndicatorParams(bar_minutes=30), "AAPL")
    first_bar_day2 = out.index[out.index.date == pd.Timestamp("2026-09-02").date()][0]
    typical = (df.loc[first_bar_day2, ["high", "low", "close"]].mean())
    assert abs(out.loc[first_bar_day2, COL_VWAP] - typical) < 1e-9


def test_a_frame_without_timestamps_is_enriched_unanchored():
    df, _ = _two_session_frame()
    plain = df.reset_index(drop=True)
    out = enrich(plain, IndicatorParams(bar_minutes=30), "AAPL")
    assert COL_VOL_SMA in out.columns
