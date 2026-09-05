"""
Bar fetching for stocks and crypto.

Alpaca returns a MultiIndex DataFrame (symbol, timestamp) for multi-symbol
requests and a flat one for single-symbol requests. Normalizing that is the
kind of thing that gets re-solved slightly differently in every app, so it
is solved once here: get_bars() always returns {symbol: flat DataFrame}.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

import pandas as pd

try:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
except ImportError:  # pragma: no cover - environment guard
    raise SystemExit("Run:  pip install -r requirements.txt")

from core import universe
from core.client import AlpacaClient
from core.sessions import DEFAULT_BAR_MINUTES

log = logging.getLogger(__name__)

# Alpaca rejects very large symbol batches; sweep in chunks this size.
MAX_SYMBOLS_PER_REQUEST = 100

#: A gap at least this long is a session break (overnight, weekend), not
#: missing data. The longest stretch pre-market can go without a trade is
#: the whole pre-market session, 04:00-09:30, which is under this; the
#: shortest break between sessions, 20:00-04:00, is over it.
SESSION_BREAK_MINUTES = 6 * 60


@dataclass(frozen=True)
class BarCoverage:
    """
    How complete a bar series actually is.

    Alpaca builds bars from trades: a window with no trades produces **no
    bar**, not a zero-volume bar. In pre-market that is common, so a series
    of N bars can span far more wall-clock time than N * bar_minutes. Every
    rolling indicator is affected — an SMA(30) over sparse 5-minute
    pre-market bars may reach back hours rather than 150 minutes.

    Session breaks are not gaps. A 300-bar fetch at 5 minutes necessarily
    crosses an overnight close; counting those hours as missing bars would
    flag every symbol as sparse and bury the real signal.
    """

    bars: int
    span_minutes: float
    expected_bars: int
    largest_gap_minutes: float

    @property
    def density(self) -> float:
        """Fraction of the expected bars that actually exist (0.0 - 1.0)."""
        return self.bars / self.expected_bars if self.expected_bars else 1.0

    def is_sparse(self, threshold: float = 0.8) -> bool:
        return self.density < threshold

    def describe(self) -> str:
        return (
            f"{self.bars} bars over {self.span_minutes:.0f} min "
            f"({self.density:.0%} of expected); largest gap "
            f"{self.largest_gap_minutes:.0f} min"
        )


def bar_coverage(df: pd.DataFrame, bar_minutes: int) -> Optional[BarCoverage]:
    """
    Measure gaps in a bar series. Returns None if it cannot be measured.

    Use it to explain why an indicator looks stale: thin pre-market tape (or
    a narrow data feed such as IEX) yields far fewer bars than the clock
    suggests.
    """
    if df is None or df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return None
    if len(df) < 2:
        return None

    stamps = df.index.sort_values()
    span = (stamps[-1] - stamps[0]).total_seconds() / 60
    deltas = stamps.to_series().diff().dropna().dt.total_seconds() / 60
    within_session = deltas[deltas < SESSION_BREAK_MINUTES]

    # A bar after a session break accounts for itself only; a bar after an
    # intra-session gap of d minutes stands where d / bar_minutes bars
    # should have been.
    expected = 1 + int((within_session // bar_minutes).sum()) + int(
        (deltas >= SESSION_BREAK_MINUTES).sum()
    )

    return BarCoverage(
        bars=len(df),
        span_minutes=span,
        expected_bars=max(expected, len(df)),
        largest_gap_minutes=(
            float(within_session.max()) if not within_session.empty else 0.0
        ),
    )


def feed_from_env(value: Optional[str] = None) -> Optional[DataFeed]:
    """
    The equity data feed named in ALPACA_DATA_FEED, or None for the account
    default.

    Unset or blank means "whatever the plan gives" — IEX on the free plan.
    `sip` is the consolidated tape and needs Algo Trader Plus; setting it
    without that plan makes every bar request fail with a 403, so the value
    is validated here and an unknown name is reported rather than passed on.
    """
    import os
    name = (value if value is not None else os.getenv("ALPACA_DATA_FEED", "")).strip().lower()
    if not name:
        return None
    try:
        return DataFeed(name)
    except ValueError:
        raise ValueError(
            f"ALPACA_DATA_FEED={name!r} is not a feed Alpaca knows. "
            f"Use 'iex' (free plan), 'sip' (Algo Trader Plus), or leave it blank."
        )


def alpaca_timeframe(bar_minutes: int) -> TimeFrame:
    """Map a bar size in minutes onto an Alpaca TimeFrame."""
    if bar_minutes <= 0 or 1440 % bar_minutes:
        raise ValueError(
            f"bar_minutes must divide evenly into 1440; got {bar_minutes}."
        )
    if bar_minutes % 60 == 0:
        return TimeFrame(bar_minutes // 60, TimeFrameUnit.Hour)
    return TimeFrame(bar_minutes, TimeFrameUnit.Minute)


def drop_forming_bars(
    df: pd.DataFrame,
    bar_minutes: int = DEFAULT_BAR_MINUTES,
    now: Optional[datetime] = None,
) -> pd.DataFrame:
    """
    Drop the bar that is still forming.

    Alpaca stamps each bar with its **start** time and, for a `limit=N`
    request with no end timestamp, includes the period currently in progress.
    Evaluating that partial bar is how a crossover appears mid-period, fires
    an order, and then reverses before the bar closes — the signal never
    existed on the completed series, but the trade is already placed.

    A bar starting at T covers [T, T + bar_minutes) and is closed once
    `now >= T + bar_minutes`.
    """
    if df is None or df.empty:
        return df

    now = now or datetime.now(timezone.utc)
    interval = timedelta(minutes=bar_minutes)

    if isinstance(df.index, pd.MultiIndex):
        if "timestamp" not in df.index.names:
            log.debug("No timestamp level on MultiIndex — cannot drop forming bars.")
            return df
        stamps = df.index.get_level_values("timestamp")
    elif isinstance(df.index, pd.DatetimeIndex):
        stamps = df.index
    else:
        log.debug("Bar frame is not timestamp-indexed — cannot drop forming bars.")
        return df

    # Compare in UTC; a tz-naive Alpaca frame is already UTC.
    stamps = pd.DatetimeIndex(stamps)
    if stamps.tz is None:
        cutoff = pd.Timestamp(now).tz_convert("UTC").tz_localize(None) - interval
    else:
        cutoff = pd.Timestamp(now).tz_convert("UTC") - interval

    return df[stamps <= cutoff]


def split_frame_by_symbol(df: pd.DataFrame, symbols: Iterable[str]) -> Dict[str, pd.DataFrame]:
    """
    Turn Alpaca's response frame into {symbol: flat DataFrame}.

    Handles both shapes: a MultiIndex frame from a multi-symbol request, and
    a flat frame from a single-symbol request.
    """
    out: Dict[str, pd.DataFrame] = {}
    if df is None or df.empty:
        return out

    if isinstance(df.index, pd.MultiIndex):
        available = set(df.index.get_level_values("symbol"))
        for sym in symbols:
            if sym in available:
                out[sym] = df.xs(sym, level="symbol").copy()
    else:
        # Single-symbol request — the frame is already just that symbol.
        symbols = list(symbols)
        if len(symbols) == 1:
            out[symbols[0]] = df.copy()
    return out


def _chunked(items: List[str], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


class MarketDataFetcher:
    """
    Fetches OHLCV bars, routing each symbol to the right Alpaca client.

    By default the still-forming bar is dropped, so callers always see closed
    bars only. Backtests that supply an explicit end timestamp have no
    forming bar and can turn this off.
    """

    def __init__(
        self,
        client: AlpacaClient,
        bar_minutes: int = DEFAULT_BAR_MINUTES,
        drop_forming: bool = True,
        feed: Optional[DataFeed] = None,
    ):
        self.client = client
        self.bar_minutes = bar_minutes
        self.drop_forming = drop_forming
        # Which equity data feed to request. None uses the account default,
        # which on free and basic plans is IEX — a single venue carrying a
        # small share of consolidated volume. In pre-market that often means
        # long stretches with no trades and therefore no bars. DataFeed.SIP
        # is the consolidated tape and needs a paid Alpaca data plan.
        self.feed = feed

    @property
    def timeframe(self) -> TimeFrame:
        return alpaca_timeframe(self.bar_minutes)

    def _finalize(self, df: pd.DataFrame) -> pd.DataFrame:
        if not self.drop_forming:
            return df
        return drop_forming_bars(df, self.bar_minutes)

    def _fetch_stocks(self, symbols: List[str], timeframe, limit: int) -> pd.DataFrame:
        if not symbols:
            return pd.DataFrame()
        req = StockBarsRequest(
            symbol_or_symbols=symbols, timeframe=timeframe, limit=limit,
            **({"feed": self.feed} if self.feed else {}),
        )
        bars = self.client.stock_data.get_stock_bars(req)
        return bars.df if hasattr(bars, "df") else pd.DataFrame()

    def _fetch_crypto(self, symbols: List[str], timeframe, limit: int) -> pd.DataFrame:
        if not symbols:
            return pd.DataFrame()
        req = CryptoBarsRequest(
            symbol_or_symbols=symbols, timeframe=timeframe, limit=limit
        )
        bars = self.client.crypto_data.get_crypto_bars(req)
        return bars.df if hasattr(bars, "df") else pd.DataFrame()

    def get_bars(
        self,
        symbols: Iterable[str],
        limit: int = 60,
        timeframe=None,
    ) -> Dict[str, pd.DataFrame]:
        """
        Fetch bars for any mix of stock and crypto symbols.

        Returns {symbol: DataFrame}; symbols with no data are simply absent
        from the result rather than mapped to an empty frame, so callers can
        use a plain `for sym, df in bars.items()`.
        """
        timeframe = timeframe or self.timeframe
        stocks, crypto = universe.split_by_asset_class(symbols)
        out: Dict[str, pd.DataFrame] = {}

        for batch in _chunked(stocks, MAX_SYMBOLS_PER_REQUEST):
            try:
                out.update(
                    split_frame_by_symbol(
                        self._finalize(self._fetch_stocks(batch, timeframe, limit)),
                        batch,
                    )
                )
            except Exception as exc:
                log.warning("Stock bar fetch failed for %s: %s", batch, exc)

        for batch in _chunked(crypto, MAX_SYMBOLS_PER_REQUEST):
            try:
                out.update(
                    split_frame_by_symbol(
                        self._finalize(self._fetch_crypto(batch, timeframe, limit)),
                        batch,
                    )
                )
            except Exception as exc:
                log.warning("Crypto bar fetch failed for %s: %s", batch, exc)

        return out

    def get_bars_between(
        self,
        symbols: Iterable[str],
        start: datetime,
        end: Optional[datetime] = None,
    ) -> Dict[str, pd.DataFrame]:
        """
        Fetch a historical date range rather than the most recent N bars.

        This is what a backtest needs: `limit=N` walks back from now, which
        makes the window depend on when you run it. A range is reproducible.

        Alpaca paginates internally, so a long range is a single call here
        but several over the wire — expect it to be slow for months of
        1-minute data.
        """
        stocks, crypto = universe.split_by_asset_class(symbols)
        out: Dict[str, pd.DataFrame] = {}
        timeframe = self.timeframe

        for batch in _chunked(stocks, MAX_SYMBOLS_PER_REQUEST):
            try:
                req = StockBarsRequest(
                    symbol_or_symbols=batch, timeframe=timeframe,
                    start=start, end=end,
                    **({"feed": self.feed} if self.feed else {}),
                )
                bars = self.client.stock_data.get_stock_bars(req)
                frame = bars.df if hasattr(bars, "df") else pd.DataFrame()
                out.update(split_frame_by_symbol(self._finalize(frame), batch))
            except Exception as exc:
                log.warning("Historical stock fetch failed for %s: %s", batch, exc)

        for batch in _chunked(crypto, MAX_SYMBOLS_PER_REQUEST):
            try:
                req = CryptoBarsRequest(
                    symbol_or_symbols=batch, timeframe=timeframe,
                    start=start, end=end,
                )
                bars = self.client.crypto_data.get_crypto_bars(req)
                frame = bars.df if hasattr(bars, "df") else pd.DataFrame()
                out.update(split_frame_by_symbol(self._finalize(frame), batch))
            except Exception as exc:
                log.warning("Historical crypto fetch failed for %s: %s", batch, exc)

        return out

    def get_stock_bars(self, symbols: List[str], limit: int = 60) -> pd.DataFrame:
        """Raw MultiIndex frame — used by strategies that slice it themselves."""
        return self._finalize(self._fetch_stocks(symbols, self.timeframe, limit))

    def get_crypto_bars(self, symbols: List[str], limit: int = 60) -> pd.DataFrame:
        """Raw MultiIndex frame — used by strategies that slice it themselves."""
        return self._finalize(self._fetch_crypto(symbols, self.timeframe, limit))
