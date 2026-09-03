"""
The one way an app turns raw bars into indicator columns.

`add_indicators()` takes the VWAP anchor and the session labels as
parameters so it can be tested in isolation, but every app wants the same
two things derived the same way from the frame's timestamps. When each
caller derives them itself, some forget: the bot passed session labels only
when extended hours were on, the scanner only when the *config* said so —
yet Alpaca returns pre- and after-market bars for intraday timeframes
regardless, so a flat volume baseline was silently mixing sessions
whenever the config said "regular only".

Use this everywhere. Pass `add_indicators()` directly only from a test.
"""

from typing import Optional

import pandas as pd

from core.indicators import IndicatorParams, add_indicators
from core.sessions import (
    DEFAULT_CALENDAR,
    SessionCalendar,
    session_day_series,
    session_series,
)


def enrich(
    df: pd.DataFrame,
    params: IndicatorParams,
    symbol: str,
    calendar: SessionCalendar = DEFAULT_CALENDAR,
) -> pd.DataFrame:
    """
    Bars plus every indicator column for `params`, anchored correctly.

    - VWAP restarts at each trading day (`session_day_series`), as on every
      charting platform.
    - The volume baseline is computed within each session
      (`session_series`), so `volume > vol_sma` measures unusual volume
      rather than "is it regular hours". With only regular-hours bars in
      the frame every bar shares one label and this is the flat baseline.

    A frame without a DatetimeIndex cannot be anchored; it is enriched
    unanchored rather than rejected, since a backtest fixture may be built
    that way deliberately.
    """
    anchor: Optional[pd.Series] = None
    sessions: Optional[pd.Series] = None
    if isinstance(df.index, pd.DatetimeIndex):
        anchor = session_day_series(df.index, symbol, calendar)
        sessions = session_series(df.index, symbol, calendar)
    return add_indicators(df, params, sessions, anchor)
