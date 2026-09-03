"""
Bar fetching for stocks and crypto.

Alpaca returns a MultiIndex DataFrame (symbol, timestamp) for multi-symbol
requests and a flat one for single-symbol requests. Normalizing that is the
kind of thing that gets re-solved slightly differently in every app, so it
is solved once here: get_bars() always returns {symbol: flat DataFrame}.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

import pandas as pd

try:
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
    ):
        self.client = client
        self.bar_minutes = bar_minutes
        self.drop_forming = drop_forming

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
            symbol_or_symbols=symbols, timeframe=timeframe, limit=limit
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

    def get_stock_bars(self, symbols: List[str], limit: int = 60) -> pd.DataFrame:
        """Raw MultiIndex frame — used by strategies that slice it themselves."""
        return self._finalize(self._fetch_stocks(symbols, self.timeframe, limit))

    def get_crypto_bars(self, symbols: List[str], limit: int = 60) -> pd.DataFrame:
        """Raw MultiIndex frame — used by strategies that slice it themselves."""
        return self._finalize(self._fetch_crypto(symbols, self.timeframe, limit))
