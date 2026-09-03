"""
Scan engine.

Takes rules (authored in Studio) and a symbol universe, fetches bars once
per symbol, and reports which symbols satisfy which rules. Deliberately has
no UI and no I/O beyond the data fetch, so it can be driven by a CLI, a
Streamlit page, or a cron job without change.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

import pandas as pd

from core import universe
from core.data import MarketDataFetcher, bar_coverage
from core.fundamentals import (
    COL_FLOAT_MILLIONS,
    COL_FLOAT_SHARES,
    FloatProvider,
    NullFloatProvider,
)
from core.indicators import add_indicators
from core.rules import Rule
from core.sessions import (
    DEFAULT_BAR_MINUTES,
    DEFAULT_CALENDAR,
    SessionCalendar,
    SessionConfig,
    is_tradable,
    session_at,
    session_day_series,
    session_series,
)

log = logging.getLogger(__name__)


@dataclass
class Match:
    """One symbol satisfying one rule, with the values that made it match."""

    symbol: str
    rule_name: str
    asset_class: str
    price: float
    values: Dict[str, float] = field(default_factory=dict)

    def __str__(self) -> str:
        detail = "  ".join(f"{k}={v:,.2f}" for k, v in self.values.items())
        return f"{self.symbol:<10} {self.rule_name:<24} ${self.price:,.2f}  {detail}"


@dataclass
class ScanResult:
    matches: List[Match] = field(default_factory=list)
    scanned: int = 0
    skipped: Dict[str, str] = field(default_factory=dict)  # symbol → why
    #: Symbols skipped because a rule needed float and none was available.
    missing_float: List[str] = field(default_factory=list)
    #: symbol → coverage description, for series with visible gaps. Thin
    #: pre-market tape (or a narrow feed like IEX) produces far fewer bars
    #: than the clock suggests, which stretches every rolling indicator.
    sparse: Dict[str, str] = field(default_factory=dict)

    def by_rule(self) -> Dict[str, List[Match]]:
        out: Dict[str, List[Match]] = {}
        for m in self.matches:
            out.setdefault(m.rule_name, []).append(m)
        return out


# Indicator values worth reporting alongside a match.
#: Values reported alongside a match. Derived per-rule so a rule's EMA
#: periods show up automatically.
_REPORTED_FIXED = (
    "rsi", "atr", "vwap", "macd", "macd_signal", "sma_fast", "sma_slow",
    "vol_sma", "roc", "rvol", "efficiency", "swings", "swing_total_pct",
    COL_FLOAT_MILLIONS,
)


def _reported_columns(rule: Rule):
    from core.indicators import ema_column
    return _REPORTED_FIXED + tuple(
        ema_column(p) for p in rule.params.ema_periods
    )


class Scanner:
    """Evaluates rules across a universe of symbols."""

    def __init__(
        self,
        fetcher: MarketDataFetcher,
        bar_limit: int = 120,
        sessions: Optional[SessionConfig] = None,
        calendar: SessionCalendar = DEFAULT_CALENDAR,
        skip_closed: bool = True,
        bar_minutes: int = DEFAULT_BAR_MINUTES,
        fundamentals: Optional[FloatProvider] = None,
    ):
        self.fetcher = fetcher
        self.bar_limit = bar_limit
        self.bar_minutes = bar_minutes
        self.sessions = sessions or SessionConfig()
        self.calendar = calendar
        # Backtests and Studio previews scan regardless of the clock; a
        # scheduled sweep should not waste calls on a closed market.
        self.skip_closed = skip_closed
        # Float is a per-symbol constant, not a per-bar indicator, so it is
        # attached as a column here rather than computed in core.indicators.
        # Unknown float becomes NaN, and a NaN comparison is False — a rule
        # requiring low float skips the symbol rather than matching it.
        self.fundamentals = fundamentals or NullFloatProvider()

    def scan(
        self,
        rules: Iterable[Rule],
        symbols: Optional[Iterable[str]] = None,
    ) -> ScanResult:
        """
        Run every rule against every symbol.

        When `symbols` is None each rule's own universe is used, and the
        union is fetched once so a symbol shared by two rules costs one
        request, not two.
        """
        rules = list(rules)
        for rule in rules:
            rule.validate()

        if symbols is None:
            wanted: List[str] = []
            for rule in rules:
                for sym in universe.resolve(rule.universe):
                    if sym not in wanted:
                        wanted.append(sym)
        else:
            wanted = list(symbols)

        result = ScanResult()
        if not wanted or not rules:
            return result

        if self.skip_closed:
            now = datetime.now(timezone.utc)
            still_open = []
            for sym in wanted:
                if is_tradable(now, sym, self.sessions, self.calendar):
                    still_open.append(sym)
                else:
                    result.skipped[sym] = (
                        f"market closed ({session_at(now, sym, self.calendar)} session)"
                    )
            wanted = still_open
            if not wanted:
                return result

        # One bulk float load per scan rather than a lookup per symbol.
        warm = getattr(self.fundamentals, "warm", None)
        if callable(warm):
            try:
                warm(wanted)
            except Exception as exc:                 # noqa: BLE001
                log.warning("Float pre-load failed, continuing without: %s", exc)

        bars = self.fetcher.get_bars(wanted, limit=self.bar_limit)

        # Cache enriched frames per (symbol, params) so two rules sharing
        # indicator settings don't recompute the same columns.
        enriched_cache: Dict[tuple, pd.DataFrame] = {}

        for sym in wanted:
            df = bars.get(sym)
            if df is None or df.empty:
                result.skipped[sym] = "no data returned"
                continue
            result.scanned += 1

            coverage = bar_coverage(df, self.bar_minutes)
            if coverage and coverage.is_sparse():
                result.sparse[sym] = coverage.describe()
                log.debug("Sparse bars for %s: %s", sym, coverage.describe())

            for rule in rules:
                key = (sym, rule.params)
                if key not in enriched_cache:
                    if len(df) < rule.params.min_bars():
                        result.skipped[sym] = (
                            f"only {len(df)} bars, need {rule.params.min_bars()}"
                        )
                        continue
                    sessions = anchor = None
                    if isinstance(df.index, pd.DatetimeIndex):
                        # VWAP needs its daily reset at every resolution.
                        anchor = session_day_series(df.index, sym, self.calendar)
                        if self.sessions.requires_extended_hours_orders():
                            sessions = session_series(df.index, sym, self.calendar)
                    enriched = add_indicators(df, rule.params, sessions, anchor)
                    shares = self.fundamentals.float_shares(sym)
                    enriched[COL_FLOAT_SHARES] = (
                        float(shares) if shares else float("nan")
                    )
                    enriched[COL_FLOAT_MILLIONS] = (
                        float(shares) / 1_000_000 if shares else float("nan")
                    )
                    if shares is None and sym not in result.missing_float:
                        result.missing_float.append(sym)
                    enriched_cache[key] = enriched

                enriched = enriched_cache.get(key)
                if enriched is None:
                    continue

                try:
                    if rule.matches(enriched):
                        curr = enriched.iloc[-1]
                        result.matches.append(
                            Match(
                                symbol=sym,
                                rule_name=rule.name,
                                asset_class=universe.asset_class(sym),
                                price=float(curr["close"]),
                                values={
                                    k: float(curr[k])
                                    for k in _reported_columns(rule)
                                    if k in enriched.columns and not pd.isna(curr[k])
                                },
                            )
                        )
                except Exception as exc:
                    log.warning("Rule %r failed on %s: %s", rule.name, sym, exc)

        return result
