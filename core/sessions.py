"""
US market sessions, bar cadence, and horizon conversion.

Three things in this repo depend on knowing which session a timestamp falls
in, and all three were previously guesswork:

1. **Cadence** — how often it is worth scanning. A signal on hourly bars can
   only change once per hour, so anything faster is redundant work; and a
   scan during a closed session is pure waste.
2. **Volume comparisons** — pre-market and after-hours volume is a small
   fraction of regular-hours volume. A rolling average that mixes sessions
   makes `volume > vol_sma` a proxy for "is it regular hours" rather than
   "is volume unusual". See `core.indicators.volume_sma_by_session`.
3. **Horizons** — "20 bars ahead" is not a duration. It is ~20 hours for
   crypto and ~3 trading days for a regular-hours-only equity. Outcome
   tables that mix the two compare unlike things; `horizon_to_bars()` is
   how a wall-clock horizon becomes a per-symbol bar count.

All times are US Eastern, which is what US equity sessions are defined in.
This module is network-free: market holidays must be supplied by the caller
(Alpaca's calendar endpoint is authoritative — see `SessionCalendar`).
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from core import universe

ET = ZoneInfo("America/New_York")

# Session names.
PRE = "pre"
REGULAR = "regular"
AFTER = "after"
CLOSED = "closed"
CRYPTO = "crypto"  # 24/7 — no session structure

#: Session boundaries in Eastern Time. Alpaca accepts orders from 04:00 with
#: `extended_hours=True`; the regular session is 09:30–16:00.
SESSION_BOUNDS: Dict[str, Tuple[time, time]] = {
    PRE: (time(4, 0), time(9, 30)),
    REGULAR: (time(9, 30), time(16, 0)),
    AFTER: (time(16, 0), time(20, 0)),
}

#: Early-close days end the regular session at 13:00 and have no after-hours
#: session beyond 17:00.
EARLY_CLOSE_REGULAR_END = time(13, 0)
EARLY_CLOSE_AFTER_END = time(17, 0)


@dataclass(frozen=True)
class SessionConfig:
    """
    Which sessions this app trades or scans.

    Defaults to regular hours only. Extended hours are opt-in because they
    change how orders must be placed (limit-only, whole shares) and how
    volume must be compared — see `docs/specs/core/market-sessions.md`.
    """

    pre: bool = False
    regular: bool = True
    after: bool = False

    @classmethod
    def regular_only(cls) -> "SessionConfig":
        return cls(pre=False, regular=True, after=False)

    @classmethod
    def extended(cls) -> "SessionConfig":
        """Pre-market through after-hours — 04:00 to 20:00 ET."""
        return cls(pre=True, regular=True, after=True)

    @classmethod
    def after_hours(cls) -> "SessionConfig":
        """Regular session plus after-hours, no pre-market."""
        return cls(pre=False, regular=True, after=True)

    def enabled(self) -> FrozenSet[str]:
        names = set()
        if self.pre:
            names.add(PRE)
        if self.regular:
            names.add(REGULAR)
        if self.after:
            names.add(AFTER)
        return frozenset(names)

    def allows(self, session: str) -> bool:
        """Crypto is always allowed; equities depend on the enabled set."""
        if session == CRYPTO:
            return True
        return session in self.enabled()

    def requires_extended_hours_orders(self) -> bool:
        return self.pre or self.after


@dataclass(frozen=True)
class SessionCalendar:
    """
    Trading-day calendar.

    Weekends are handled here. Holidays and early closes are *not* hardcoded
    — they move year to year, and Alpaca's `/v2/calendar` endpoint is the
    authoritative source. Fetch them once at startup and pass them in;
    an empty calendar simply treats every weekday as a full session.
    """

    holidays: FrozenSet[date] = field(default_factory=frozenset)
    early_closes: FrozenSet[date] = field(default_factory=frozenset)

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def is_early_close(self, day: date) -> bool:
        return day in self.early_closes

    def bounds_for(self, day: date, session: str) -> Optional[Tuple[time, time]]:
        """Session bounds on a given day, shortened on early-close days."""
        if not self.is_trading_day(day) or session not in SESSION_BOUNDS:
            return None
        start, end = SESSION_BOUNDS[session]
        if self.is_early_close(day):
            if session == REGULAR:
                end = EARLY_CLOSE_REGULAR_END
            elif session == AFTER:
                start, end = EARLY_CLOSE_REGULAR_END, EARLY_CLOSE_AFTER_END
            if start >= end:
                return None
        return start, end


DEFAULT_CALENDAR = SessionCalendar()


def to_eastern(ts: datetime) -> datetime:
    """
    Convert to Eastern Time.

    A naive timestamp is assumed to be UTC — that is what Alpaca returns, and
    guessing local time instead would silently shift every session boundary.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ZoneInfo("UTC"))
    return ts.astimezone(ET)


def session_at(
    ts: datetime,
    symbol: str,
    calendar: SessionCalendar = DEFAULT_CALENDAR,
) -> str:
    """Which session `ts` falls in for `symbol`. Crypto is always CRYPTO."""
    if universe.is_crypto(symbol):
        return CRYPTO

    et = to_eastern(ts)
    day = et.date()
    if not calendar.is_trading_day(day):
        return CLOSED

    now = et.time()
    for name in (PRE, REGULAR, AFTER):
        bounds = calendar.bounds_for(day, name)
        if bounds and bounds[0] <= now < bounds[1]:
            return name
    return CLOSED


def is_tradable(
    ts: datetime,
    symbol: str,
    config: SessionConfig,
    calendar: SessionCalendar = DEFAULT_CALENDAR,
) -> bool:
    """True when `symbol` may be traded or scanned at `ts` under `config`."""
    return config.allows(session_at(ts, symbol, calendar))


# ── Bar cadence ──────────────────────────────────────────────────────────────

def _clock_hours_spanned(start: time, end: time) -> FrozenSet[int]:
    """
    Clock-hour buckets a session touches.

    Alpaca aligns hourly bars to the clock hour, so a session running
    09:30–16:00 produces bars in hours 9…15 — seven of them, the first a
    half-length stub. Returning the hour set (rather than a count) is what
    makes overlapping sessions merge correctly: with pre-market enabled,
    09:00–09:30 and 09:30–10:00 are one bar, not two.
    """
    return frozenset(range(start.hour, end.hour + (1 if end.minute else 0)))


def bars_per_day(
    symbol: str,
    config: SessionConfig = SessionConfig(),
    calendar: SessionCalendar = DEFAULT_CALENDAR,
    day: Optional[date] = None,
) -> int:
    """
    Hourly bars a symbol produces on one trading day.

    Crypto is 24. Equities depend on which sessions are enabled — which is
    exactly why a bar count is not a duration.
    """
    if universe.is_crypto(symbol):
        return 24

    day = day or date(2026, 1, 5)  # an ordinary Monday, for the default case
    if not calendar.is_trading_day(day):
        return 0

    hours: set = set()
    for name in config.enabled():
        bounds = calendar.bounds_for(day, name)
        if bounds:
            hours |= _clock_hours_spanned(*bounds)
    return len(hours)


def scan_times(
    symbol: str,
    config: SessionConfig = SessionConfig(),
    calendar: SessionCalendar = DEFAULT_CALENDAR,
    day: Optional[date] = None,
    delay_minutes: int = 2,
) -> List[time]:
    """
    When to scan, in Eastern Time — once per *completed* hourly bar.

    `delay_minutes` pushes each scan past the bar boundary so the bar being
    evaluated is closed. Acting on the in-progress bar is what produces
    signals that appear mid-hour and vanish before the hour ends.
    """
    if universe.is_crypto(symbol):
        return [time((h + 1) % 24, delay_minutes) for h in range(24)]

    day = day or date(2026, 1, 5)
    if not calendar.is_trading_day(day):
        return []

    hours: set = set()
    for name in config.enabled():
        bounds = calendar.bounds_for(day, name)
        if bounds:
            hours |= _clock_hours_spanned(*bounds)

    # A bar starting at hour H is complete at H+1.
    return sorted(time((h + 1) % 24, delay_minutes) for h in hours)


def cycles_per_day(
    symbols: Iterable[str],
    config: SessionConfig = SessionConfig(),
    calendar: SessionCalendar = DEFAULT_CALENDAR,
) -> Dict[str, int]:
    """Scans per day per asset class, for capacity planning."""
    stocks, crypto = universe.split_by_asset_class(symbols)
    out: Dict[str, int] = {}
    if stocks:
        out[universe.STOCK] = bars_per_day(stocks[0], config, calendar)
    if crypto:
        out[universe.CRYPTO] = 24
    return out


# ── Horizons ─────────────────────────────────────────────────────────────────

#: Wall-clock horizons, in hours of *market time*. "1d" means one trading
#: session for equities and 24 hours for crypto — see horizon_to_bars.
HORIZONS: Dict[str, float] = {
    "1h": 1.0,
    "2h": 2.0,
    "4h": 4.0,
    "1d": None,   # resolved per symbol: one session-day
    "5d": None,   # five session-days
    "20d": None,
}

_SESSION_DAY_MULTIPLIER = {"1d": 1, "5d": 5, "20d": 20}


def horizon_to_bars(
    horizon: str,
    symbol: str,
    config: SessionConfig = SessionConfig(),
    calendar: SessionCalendar = DEFAULT_CALENDAR,
) -> int:
    """
    Convert a wall-clock horizon to a bar count for one symbol.

    This is the fix for comparing outcomes across asset classes. "+20 bars"
    means ~20 hours for crypto and ~3 trading days for a regular-hours
    equity; "+1d" means one session for both, which is comparable.

    Raises KeyError on an unknown horizon rather than guessing.
    """
    if horizon not in HORIZONS:
        raise KeyError(
            f"Unknown horizon {horizon!r}. Known: {', '.join(sorted(HORIZONS))}"
        )

    per_day = bars_per_day(symbol, config, calendar)
    if per_day == 0:
        return 0

    if horizon in _SESSION_DAY_MULTIPLIER:
        return per_day * _SESSION_DAY_MULTIPLIER[horizon]

    # Hour-denominated horizons are one bar per hour on hourly data.
    return int(HORIZONS[horizon])


def describe_horizon(
    horizon: str,
    symbol: str,
    config: SessionConfig = SessionConfig(),
) -> str:
    """Human-readable label for an outcomes table column."""
    bars = horizon_to_bars(horizon, symbol, config)
    return f"{horizon} ({bars} bars)"


def session_series(index, symbol: str, calendar: SessionCalendar = DEFAULT_CALENDAR):
    """
    Label every bar in a DatetimeIndex with its session.

    Used to group volume baselines by session — see
    `core.indicators.add_indicators(..., sessions=...)`. A bar is labelled by
    its *start* timestamp, which is how Alpaca stamps them.
    """
    import pandas as pd

    if not isinstance(index, pd.DatetimeIndex):
        raise TypeError(
            f"session_series needs a DatetimeIndex, got {type(index).__name__}. "
            f"Alpaca bar frames are timestamp-indexed; reset_index() drops that."
        )
    return pd.Series(
        [session_at(ts.to_pydatetime(), symbol, calendar) for ts in index],
        index=index,
        name="session",
    )
