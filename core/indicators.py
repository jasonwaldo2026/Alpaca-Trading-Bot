"""
Indicator math — the single source of truth.

Every app (bot, dashboard, scanner, studio) computes indicators from here.
Do not re-implement any of these inside an app: two copies drift, and a
scanner that disagrees with the bot about RSI will fire trades you can't
reproduce on the chart.

All functions are pure: DataFrame/Series in, Series out, no I/O.
"""

from dataclasses import dataclass
from typing import Optional

import pandas as pd

# Column names written by add_indicators(). Apps should reference these
# constants rather than hardcoding strings, so a rename stays mechanical.
COL_SMA_FAST = "sma_fast"
COL_SMA_SLOW = "sma_slow"
COL_RSI = "rsi"
COL_VOL_SMA = "vol_sma"
COL_ATR = "atr"

INDICATOR_COLUMNS = (COL_SMA_FAST, COL_SMA_SLOW, COL_RSI, COL_VOL_SMA, COL_ATR)

# OHLCV columns every fetcher must supply before indicators can be computed.
REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class IndicatorParams:
    """
    Periods shared by every app.

    BotConfig holds a superset of these (it also carries risk and polling
    settings); use BotConfig.indicator_params() to get one of these from it
    so the bot and the scanner are guaranteed to agree.
    """

    sma_fast: int = 10
    sma_slow: int = 30
    rsi_period: int = 14
    volume_sma_period: int = 20
    atr_period: int = 14

    def min_bars(self) -> int:
        """Bars needed before every indicator has a non-NaN value, plus one
        extra so crossover logic can compare the last two rows."""
        return max(
            self.sma_slow,
            self.rsi_period,
            self.volume_sma_period,
            self.atr_period,
        ) + 2


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(period).mean()


def rsi(series: pd.Series, period: int) -> pd.Series:
    """
    Wilder's RSI, smoothed with an EWM of com=period-1 (equivalent to
    Wilder's 1/period smoothing factor).

    A flat or monotonically rising series gives avg_loss == 0, which would
    divide by zero; that case is mapped to NaN rather than 100 so callers
    can tell "no data" from "maximally overbought".
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    """
    Average True Range — EWM-smoothed true range.

    True range is the widest of: today's high-low, and each of high/low
    measured against yesterday's close (which captures overnight gaps).
    """
    prev_close = df["close"].shift()
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(com=period - 1, min_periods=period).mean()


def volume_sma_by_session(
    volume: pd.Series, sessions: pd.Series, period: int
) -> pd.Series:
    """
    Rolling average volume computed *within* each session.

    Pre-market and after-hours volume is a small fraction of regular-hours
    volume. A single rolling average across all sessions makes
    `volume > vol_sma` fire on essentially every regular-hours bar and
    essentially no extended-hours bar — it stops measuring "unusual volume"
    and starts measuring "is it 09:30 yet".

    Grouping by session compares each bar against its own session's
    baseline, so an unusually busy after-hours bar is detectable as such.
    """
    if not volume.index.equals(sessions.index):
        raise ValueError(
            "volume and sessions must share an index; got "
            f"{len(volume)} and {len(sessions)} rows."
        )
    return volume.groupby(sessions, sort=False).transform(
        lambda g: g.rolling(period).mean()
    )


def add_indicators(
    df: pd.DataFrame,
    params: IndicatorParams,
    sessions: Optional[pd.Series] = None,
) -> pd.DataFrame:
    """
    Return a copy of `df` with every indicator column added.

    Leading rows carry NaNs until each indicator has enough history — callers
    that need complete rows should dropna() afterwards, which is why this
    does not drop them itself (the dashboard wants the NaN prefix so charts
    keep their full x-axis).

    Pass `sessions` (from `core.sessions.session_series`) when the frame spans
    extended hours, so the volume baseline is computed per session rather
    than across sessions of wildly different typical volume.
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Bars are missing required column(s): {', '.join(missing)}. "
            f"Got: {', '.join(map(str, df.columns))}"
        )

    out = df.copy()
    out[COL_SMA_FAST] = sma(out["close"], params.sma_fast)
    out[COL_SMA_SLOW] = sma(out["close"], params.sma_slow)
    out[COL_RSI] = rsi(out["close"], params.rsi_period)
    if sessions is None:
        out[COL_VOL_SMA] = sma(out["volume"], params.volume_sma_period)
    else:
        out[COL_VOL_SMA] = volume_sma_by_session(
            out["volume"], sessions, params.volume_sma_period
        )
    out[COL_ATR] = atr(out, params.atr_period)
    return out


def crossed_up(prev: pd.Series, curr: pd.Series, fast: str, slow: str) -> bool:
    """Golden cross: fast was at or below slow, and is now strictly above."""
    return bool(prev[fast] <= prev[slow] and curr[fast] > curr[slow])


def crossed_down(prev: pd.Series, curr: pd.Series, fast: str, slow: str) -> bool:
    """Death cross: fast was at or above slow, and is now strictly below."""
    return bool(prev[fast] >= prev[slow] and curr[fast] < curr[slow])
