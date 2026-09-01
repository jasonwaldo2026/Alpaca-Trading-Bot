"""
Bar fetching for stocks and crypto.

Alpaca returns a MultiIndex DataFrame (symbol, timestamp) for multi-symbol
requests and a flat one for single-symbol requests. Normalizing that is the
kind of thing that gets re-solved slightly differently in every app, so it
is solved once here: get_bars() always returns {symbol: flat DataFrame}.
"""

import logging
from typing import Dict, Iterable, List

import pandas as pd

try:
    from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame
except ImportError:  # pragma: no cover - environment guard
    raise SystemExit("Run:  pip install -r requirements.txt")

from core import universe
from core.client import AlpacaClient

log = logging.getLogger(__name__)

# Alpaca rejects very large symbol batches; sweep in chunks this size.
MAX_SYMBOLS_PER_REQUEST = 100


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
    """Fetches OHLCV bars, routing each symbol to the right Alpaca client."""

    def __init__(self, client: AlpacaClient):
        self.client = client

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
        timeframe = timeframe or TimeFrame.Hour
        stocks, crypto = universe.split_by_asset_class(symbols)
        out: Dict[str, pd.DataFrame] = {}

        for batch in _chunked(stocks, MAX_SYMBOLS_PER_REQUEST):
            try:
                out.update(
                    split_frame_by_symbol(
                        self._fetch_stocks(batch, timeframe, limit), batch
                    )
                )
            except Exception as exc:
                log.warning("Stock bar fetch failed for %s: %s", batch, exc)

        for batch in _chunked(crypto, MAX_SYMBOLS_PER_REQUEST):
            try:
                out.update(
                    split_frame_by_symbol(
                        self._fetch_crypto(batch, timeframe, limit), batch
                    )
                )
            except Exception as exc:
                log.warning("Crypto bar fetch failed for %s: %s", batch, exc)

        return out

    def get_stock_bars(self, symbols: List[str], limit: int = 60) -> pd.DataFrame:
        """Raw MultiIndex frame — used by strategies that slice it themselves."""
        return self._fetch_stocks(symbols, TimeFrame.Hour, limit)

    def get_crypto_bars(self, symbols: List[str], limit: int = 60) -> pd.DataFrame:
        """Raw MultiIndex frame — used by strategies that slice it themselves."""
        return self._fetch_crypto(symbols, TimeFrame.Hour, limit)
