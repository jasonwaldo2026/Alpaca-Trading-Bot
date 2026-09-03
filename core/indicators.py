"""
Indicator math — the single source of truth.

Every app (bot, dashboard, scanner, studio) computes indicators from here.
Do not re-implement any of these inside an app: two copies drift, and a
scanner that disagrees with the bot about RSI will fire trades you can't
reproduce on the chart.

All functions are pure: DataFrame/Series in, Series out, no I/O.
"""

from dataclasses import dataclass, replace
from typing import NamedTuple, Optional, Tuple

import pandas as pd

# Column names written by add_indicators(). Apps should reference these
# constants rather than hardcoding strings, so a rename stays mechanical.
COL_SMA_FAST = "sma_fast"
COL_SMA_SLOW = "sma_slow"
COL_RSI = "rsi"
COL_VOL_SMA = "vol_sma"
COL_ATR = "atr"
COL_VWAP = "vwap"
COL_MACD = "macd"
COL_MACD_SIGNAL = "macd_signal"
COL_MACD_HIST = "macd_hist"
COL_ROC = "roc"
COL_RVOL = "rvol"

#: Columns that do not depend on configuration. EMA columns are named after
#: their period (ema_9, ema_200, …) so several can coexist, so the full list
#: is per-params — use indicator_columns().
FIXED_INDICATOR_COLUMNS = (
    COL_SMA_FAST, COL_SMA_SLOW,
    COL_RSI, COL_VOL_SMA, COL_ATR, COL_VWAP,
    COL_MACD, COL_MACD_SIGNAL, COL_MACD_HIST,
    COL_ROC, COL_RVOL,
)


def ema_column(period: int) -> str:
    """Column name for an EMA of a given period: 9 -> 'ema_9'."""
    return f"ema_{period}"


def indicator_columns(params: "IndicatorParams") -> Tuple[str, ...]:
    """
    Every column add_indicators() writes for these params.

    Studio's field picker and the scanner's reported values both derive from
    this, so adding an EMA period makes it selectable everywhere with no app
    edit.
    """
    return FIXED_INDICATOR_COLUMNS + tuple(
        ema_column(p) for p in params.ema_periods
    )

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

    #: EMA periods, one column each (ema_9, ema_12, ema_200). 9 and 12 are
    #: fast intraday averages; 200 is the long-trend reference every charting
    #: platform draws. Order does not matter.
    ema_periods: Tuple[int, ...] = (9, 12, 200)

    rsi_period: int = 14
    volume_sma_period: int = 20

    #: Bars for the rate-of-change window. At 5-minute bars, 2 covers the
    #: last 10 minutes — "up 10% in 10 minutes" is `roc > 10` with roc_period=2.
    roc_period: int = 2
    atr_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    #: The bar size these periods are expressed in. Periods are *bar counts*,
    #: so the same numbers mean different durations at different resolutions:
    #: sma_slow=30 is 30 hours of hourly bars but 150 minutes of 5-minute
    #: bars. Recording the intended resolution is what makes rescaled_to()
    #: possible and stops the mismatch from being silent.
    bar_minutes: int = 60

    #: Period fields, for rescaling and validation. Not every field on this
    #: dataclass is a period, so they are named rather than inferred.
    PERIOD_FIELDS = (
        "sma_fast", "sma_slow", "rsi_period",
        "volume_sma_period", "atr_period", "macd_fast", "macd_slow",
        "macd_signal", "roc_period",
    )

    def __post_init__(self):
        # Rules deserialize from JSON, where a tuple becomes a list. A list
        # would make this dataclass unhashable, and the scanner uses it as a
        # cache key, so coerce and sort for a stable identity.
        object.__setattr__(
            self, "ema_periods", tuple(sorted(int(p) for p in self.ema_periods))
        )
        if any(p < 2 for p in self.ema_periods):
            raise ValueError(
                f"EMA periods must be >= 2; got {self.ema_periods}."
            )

    def min_bars(self) -> int:
        """
        Bars needed before every indicator has a non-NaN value, plus one
        extra so crossover logic can compare the last two rows.

        MACD is the binding constraint: its signal line is an EMA *of* the
        MACD line, so it needs macd_slow bars before the line exists and
        macd_signal more before the signal does.
        """
        return max(
            self.sma_slow,
            max(self.ema_periods) if self.ema_periods else 0,
            self.rsi_period,
            self.volume_sma_period,
            self.atr_period,
            self.macd_slow + self.macd_signal,
        ) + 2

    def rescaled_to(self, bar_minutes: int) -> "IndicatorParams":
        """
        Same wall-clock lookback, expressed at a different bar size.

        `IndicatorParams(sma_slow=30, bar_minutes=60).rescaled_to(5)` gives
        sma_slow=360 — still 30 hours, now counted in 5-minute bars.

        Use this when the periods were tuned at one resolution and you want
        the *same strategy* at another. Do NOT use it if you mean the
        conventional reading of "MACD 12/26/9 on the 5-minute chart", which
        is 12/26/9 five-minute bars and a genuinely faster indicator. Both
        are legitimate; they are different strategies, so the choice is
        explicit rather than automatic.
        """
        if bar_minutes <= 0:
            raise ValueError(f"bar_minutes must be positive; got {bar_minutes}.")
        if bar_minutes == self.bar_minutes:
            return self

        factor = self.bar_minutes / bar_minutes
        scaled = {
            name: max(2, round(getattr(self, name) * factor))
            for name in self.PERIOD_FIELDS
        }
        scaled["ema_periods"] = tuple(
            max(2, round(p * factor)) for p in self.ema_periods
        )
        return replace(self, bar_minutes=bar_minutes, **scaled)

    def duration_minutes(self, field: str) -> int:
        """Wall-clock span of one period, for labelling."""
        if field not in self.PERIOD_FIELDS:
            raise KeyError(
                f"{field!r} is not a period. Known: {', '.join(self.PERIOD_FIELDS)}"
            )
        return getattr(self, field) * self.bar_minutes

    def ema_duration_minutes(self, period: int) -> int:
        """Wall-clock span of one EMA period."""
        return period * self.bar_minutes

    def describe(self) -> str:
        """Human-readable summary — periods with their wall-clock meaning."""
        parts = [
            f"{name}={getattr(self, name)} ({self.duration_minutes(name)}m)"
            for name in ("sma_fast", "sma_slow", "macd_fast", "macd_slow")
        ]
        return f"[{self.bar_minutes}m bars] " + "  ".join(parts)


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


def ema(series: pd.Series, period: int) -> pd.Series:
    """
    Exponential moving average, span-based with alpha = 2 / (period + 1).

    `adjust=False` gives the standard recursive form traders expect —
    y_t = alpha*x_t + (1-alpha)*y_(t-1) — rather than pandas' default
    bias-corrected weighting, which does not match charting platforms.

    `min_periods=period` suppresses output until `period` observations exist.
    The recursion still seeds from the first value, so the first emitted
    reading carries some seed influence; that matches TradingView and the
    common convention. It differs slightly from an SMA-seeded EMA, which
    matters only in the first few readings.
    """
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


class MACDResult(NamedTuple):
    """MACD line, its signal line, and the histogram between them."""

    macd: pd.Series
    signal: pd.Series
    histogram: pd.Series


def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> MACDResult:
    """
    Moving Average Convergence Divergence.

        macd      = EMA(fast) - EMA(slow)
        signal    = EMA(macd, signal)
        histogram = macd - signal

    The signal line is an EMA *of the MACD line*, not of price, so it only
    becomes valid `signal` bars after the MACD line itself does — roughly
    `slow + signal` bars from the start of the series. Feeding it a short
    frame yields NaN rather than a wrong number.

    Periods are bar counts. On 5-minute bars, 12/26/9 spans 60/130/45
    minutes; on hourly bars the same numbers span 12/26/9 hours. Use
    `IndicatorParams.rescaled_to()` if you want the same wall-clock span at a
    different resolution.
    """
    if fast >= slow:
        raise ValueError(
            f"MACD needs fast < slow; got fast={fast}, slow={slow}."
        )
    line = ema(series, fast) - ema(series, slow)
    signal_line = ema(line, signal)
    return MACDResult(line, signal_line, line - signal_line)


def typical_price(df: pd.DataFrame) -> pd.Series:
    """(high + low + close) / 3 — the price VWAP weights by volume."""
    return (df["high"] + df["low"] + df["close"]) / 3


def vwap(df: pd.DataFrame, anchor: Optional[pd.Series] = None) -> pd.Series:
    """
    Volume-Weighted Average Price, anchored to a session.

        vwap = cumsum(typical_price * volume) / cumsum(volume)

    **VWAP resets.** It is a running average *within* a trading day, not a
    rolling window — a VWAP accumulated continuously across weeks is not the
    line any trading platform draws, and drifts further from price every day.
    That reset is what `anchor` provides: pass
    `core.sessions.session_day_series(...)`, which groups bars by ET trading
    day for equities and by UTC day for crypto.

    Omitting `anchor` accumulates over the whole frame. That is only correct
    for a frame that already covers exactly one session.

    Bars with zero volume contribute nothing and leave VWAP flat rather than
    dividing by zero.
    """
    price_volume = typical_price(df) * df["volume"]

    if anchor is None:
        cumulative_pv = price_volume.cumsum()
        cumulative_volume = df["volume"].cumsum()
    else:
        if not df.index.equals(anchor.index):
            raise ValueError(
                "bars and anchor must share an index; got "
                f"{len(df)} and {len(anchor)} rows."
            )
        grouped = anchor
        cumulative_pv = price_volume.groupby(grouped, sort=False).cumsum()
        cumulative_volume = df["volume"].groupby(grouped, sort=False).cumsum()

    return cumulative_pv / cumulative_volume.replace(0, float("nan"))


def roc(series: pd.Series, period: int) -> pd.Series:
    """
    Rate of change: percent move over the last `period` bars.

    This is the "up 10% in 10 minutes" measure. `period` is bar counts, so
    the window it covers depends on the resolution — 2 bars is 10 minutes at
    5-minute bars and 2 minutes at 1-minute bars. Use
    `IndicatorParams.duration_minutes("roc_period")` to state which is meant.

    Returns percent, so 10.0 means +10%.
    """
    if period < 1:
        raise ValueError(f"roc period must be >= 1; got {period}.")
    previous = series.shift(period)
    # A zero or missing prior price has no meaningful percentage change.
    return (series / previous.replace(0, float("nan")) - 1) * 100


def relative_volume(volume: pd.Series, baseline: pd.Series) -> pd.Series:
    """
    Volume as a multiple of its baseline: 3.0 means three times normal.

    A ratio rather than a boolean, so a rule can ask for `rvol > 3` instead
    of only "above average". When the baseline is session-aware (see
    `volume_sma_by_session`) this compares a pre-market bar against
    pre-market volume rather than against the regular session.

    Note this is volume *per bar* against recent bars — not the same as the
    "RVOL" on a scanner screen, which compares the day's cumulative volume
    against the same time of day over previous days. See
    docs/specs/core/relative-volume.md.
    """
    return volume / baseline.replace(0, float("nan"))


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
    anchor: Optional[pd.Series] = None,
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

    Pass `anchor` (from `core.sessions.session_day_series`) so VWAP resets
    each trading day. Without it VWAP accumulates across the whole frame,
    which is only correct for a single-session frame.

    `params.bar_minutes` records the resolution the periods were written for.
    It is not enforced here — the caller decides whether to reinterpret the
    periods at a new resolution (`params.rescaled_to(...)`) or keep them as
    bar counts. See `docs/specs/core/market-sessions.md`.
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
    for period in params.ema_periods:
        out[ema_column(period)] = ema(out["close"], period)
    out[COL_RSI] = rsi(out["close"], params.rsi_period)
    if sessions is None:
        out[COL_VOL_SMA] = sma(out["volume"], params.volume_sma_period)
    else:
        out[COL_VOL_SMA] = volume_sma_by_session(
            out["volume"], sessions, params.volume_sma_period
        )
    out[COL_ATR] = atr(out, params.atr_period)
    out[COL_VWAP] = vwap(out, anchor)
    out[COL_ROC] = roc(out["close"], params.roc_period)
    out[COL_RVOL] = relative_volume(out["volume"], out[COL_VOL_SMA])

    macd_result = macd(
        out["close"], params.macd_fast, params.macd_slow, params.macd_signal
    )
    out[COL_MACD] = macd_result.macd
    out[COL_MACD_SIGNAL] = macd_result.signal
    out[COL_MACD_HIST] = macd_result.histogram
    return out


def crossed_up(prev: pd.Series, curr: pd.Series, fast: str, slow: str) -> bool:
    """Golden cross: fast was at or below slow, and is now strictly above."""
    return bool(prev[fast] <= prev[slow] and curr[fast] > curr[slow])


def crossed_down(prev: pd.Series, curr: pd.Series, fast: str, slow: str) -> bool:
    """Death cross: fast was at or above slow, and is now strictly below."""
    return bool(prev[fast] >= prev[slow] and curr[fast] < curr[slow])
