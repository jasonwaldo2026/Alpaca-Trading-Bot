"""
Per-symbol fundamentals — currently just float.

Float is why a given amount of buying moves a small-cap far more than a
mega-cap, which is the premise of the momentum setup. It is also not
derivable from price and volume, and **Alpaca's market data API does not
serve it**, so it has to come from somewhere else.

Three sources, in order of preference:

- `FMPFloatProvider` — Financial Modeling Prep. A documented API with a
  **bulk endpoint**, so the whole market's float arrives in a handful of
  requests rather than one per symbol. That matters when the universe is
  11,000 names. Free tier available; needs `FMP_API_KEY`.
- `StaticFloatProvider` — a JSON file you maintain. No network, no
  dependency, no rate limit, and it never breaks. For a watchlist of a few
  dozen names this is entirely reasonable.
- `YahooFloatProvider` — via `yfinance`. Free and automatic, but it scrapes
  an undocumented endpoint one symbol at a time, so it is slow, rate-limited
  and can break without notice. A fallback, not a foundation.

Everything **fails closed**: an unknown float yields None, which becomes NaN
in the scan frame, and a NaN comparison is False. A rule requiring low float
will therefore skip a symbol whose float is unknown rather than matching it.
Silently matching everything would be the dangerous failure.
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, Optional

log = logging.getLogger(__name__)

FLOAT_CACHE_PATH = Path(".float-cache.json")

#: Column names the scanner adds when a provider is configured.
COL_FLOAT_SHARES = "float_shares"
COL_FLOAT_MILLIONS = "float_millions"


class FloatProvider:
    """Interface: shares available to trade, or None when unknown."""

    def float_shares(self, symbol: str) -> Optional[float]:
        raise NotImplementedError

    def float_millions(self, symbol: str) -> Optional[float]:
        shares = self.float_shares(symbol)
        return None if shares is None else shares / 1_000_000


class NullFloatProvider(FloatProvider):
    """Knows nothing. The default, so float conditions never silently pass."""

    def float_shares(self, symbol: str) -> Optional[float]:
        return None


class StaticFloatProvider(FloatProvider):
    """
    Float from a JSON file you maintain: {"ABCD": 8200000, ...}.

    Values may be written in shares (8200000) or in millions (8.2); anything
    below 100_000 is read as millions, since no real float is that small in
    share terms.
    """

    def __init__(self, path: Path, values: Optional[Dict[str, float]] = None):
        self.path = Path(path)
        self.values: Dict[str, float] = {}
        if values is not None:
            self.values = {k.upper(): float(v) for k, v in values.items()}
        else:
            self.load()

    def load(self) -> None:
        if not self.path.exists():
            log.info("No float file at %s — float conditions will not match.", self.path)
            return
        try:
            raw = json.loads(self.path.read_text())
            self.values = {str(k).upper(): float(v) for k, v in raw.items()}
            log.info("Loaded float for %d symbols from %s", len(self.values), self.path)
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            log.warning("Could not read float file %s: %s", self.path, exc)

    def float_shares(self, symbol: str) -> Optional[float]:
        value = self.values.get(symbol.upper())
        if value is None:
            return None
        return value * 1_000_000 if value < 100_000 else value


class YahooFloatProvider(FloatProvider):
    """
    Float via `yfinance`, cached to disk for the day.

    yfinance scrapes an undocumented Yahoo endpoint. It is not an API
    contract: it rate-limits, can be blocked by a network, and can break on a
    Yahoo change. Every failure degrades to None rather than raising, so a
    scan continues with float simply unknown for that symbol.

    Install with `pip install yfinance` — it is not a required dependency.
    """

    def __init__(self, cache_path: Path = FLOAT_CACHE_PATH):
        self.cache_path = Path(cache_path)
        self.cache: Dict[str, Optional[float]] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            raw = json.loads(self.cache_path.read_text())
            if raw.get("date") == date.today().isoformat():
                self.cache = raw.get("values", {})
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            log.warning("Ignoring unreadable float cache: %s", exc)

    def save_cache(self) -> None:
        try:
            self.cache_path.write_text(json.dumps({
                "date": date.today().isoformat(), "values": self.cache,
            }))
        except OSError as exc:
            log.warning("Could not write float cache: %s", exc)

    def float_shares(self, symbol: str) -> Optional[float]:
        key = symbol.upper()
        if key in self.cache:
            return self.cache[key]

        value: Optional[float] = None
        try:
            import yfinance

            info = yfinance.Ticker(key).get_info() or {}
            raw = info.get("floatShares") or info.get("sharesOutstanding")
            value = float(raw) if raw else None
        except ImportError:
            log.warning(
                "yfinance is not installed — float is unknown, so float "
                "conditions will not match. pip install yfinance, or use a "
                "static float file."
            )
        except Exception as exc:                     # noqa: BLE001
            # Scraped source: any failure means "unknown", never a crash.
            log.debug("Float lookup failed for %s: %s", key, exc)

        self.cache[key] = value
        return value


class FMPFloatProvider(FloatProvider):
    """
    Float from Financial Modeling Prep.

    Prefers the bulk endpoint: one paged request stream fills the whole cache,
    which is the only practical shape when the scan universe is the entire
    market. Falls back to a single-symbol lookup for anything the bulk load
    missed.

    Needs `FMP_API_KEY`. Every failure degrades to unknown rather than
    raising — a float provider must never take the scanner down.
    """

    BULK_URL = "https://financialmodelingprep.com/stable/all-shares-float"
    SINGLE_URL = "https://financialmodelingprep.com/stable/shares-float"
    PAGE_LIMIT = 5000

    def __init__(
        self,
        api_key: Optional[str] = None,
        cache_path: Path = FLOAT_CACHE_PATH,
        timeout: float = 30.0,
    ):
        self.api_key = api_key or os.getenv("FMP_API_KEY", "")
        self.cache_path = Path(cache_path)
        self.timeout = timeout
        self.cache: Dict[str, Optional[float]] = {}
        self._bulk_loaded = False
        self._load_cache()

    # ── Cache ────────────────────────────────────────────────────────────

    def _load_cache(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            raw = json.loads(self.cache_path.read_text())
            if raw.get("date") == date.today().isoformat():
                self.cache = raw.get("values", {})
                self._bulk_loaded = bool(raw.get("bulk"))
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            log.warning("Ignoring unreadable float cache: %s", exc)

    def save_cache(self) -> None:
        try:
            self.cache_path.write_text(json.dumps({
                "date": date.today().isoformat(),
                "bulk": self._bulk_loaded,
                "values": self.cache,
            }))
        except OSError as exc:
            log.warning("Could not write float cache: %s", exc)

    # ── HTTP ─────────────────────────────────────────────────────────────

    def _scrub(self, text: str) -> str:
        """Keep the API key out of logs, whatever an error message contains."""
        return text.replace(self.api_key, "***") if self.api_key else text

    def _get(self, url: str, params: Dict[str, str]):
        query = urllib.parse.urlencode({**params, "apikey": self.api_key})
        try:
            with urllib.request.urlopen(f"{url}?{query}", timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, OSError, TimeoutError,
                json.JSONDecodeError, ValueError) as exc:
            # The key is in the query string, so anything derived from the
            # request could carry it into a log file.
            log.warning("FMP request failed (%s): %s", url, self._scrub(str(exc)))
            return None

    @staticmethod
    def _extract(row: Dict) -> Optional[float]:
        """floatShares is the number actually available to trade."""
        for key in ("floatShares", "freeFloat", "outstandingShares"):
            value = row.get(key)
            if value:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return None

    # ── Loading ──────────────────────────────────────────────────────────

    def load_all(self, max_pages: int = 10) -> int:
        """
        Fill the cache from the bulk endpoint. Returns symbols loaded.

        Bounded by `max_pages` so a paging bug cannot loop forever.
        """
        if not self.api_key:
            log.warning(
                "FMP_API_KEY is not set — float is unknown, so low-float "
                "conditions will not match."
            )
            return 0

        loaded = 0
        for page in range(max_pages):
            rows = self._get(self.BULK_URL, {
                "page": str(page), "limit": str(self.PAGE_LIMIT),
            })
            if not rows or not isinstance(rows, list):
                break
            for row in rows:
                symbol = str(row.get("symbol", "")).upper()
                if symbol:
                    self.cache[symbol] = self._extract(row)
                    loaded += 1
            if len(rows) < self.PAGE_LIMIT:
                break

        if loaded:
            self._bulk_loaded = True
            self.save_cache()
            log.info("Loaded float for %d symbols from FMP.", loaded)
        return loaded

    def float_shares(self, symbol: str) -> Optional[float]:
        key = symbol.upper()
        if key in self.cache:
            return self.cache[key]
        if not self._bulk_loaded:
            self.load_all()
            if key in self.cache:
                return self.cache[key]
        if not self.api_key:
            return None

        rows = self._get(self.SINGLE_URL, {"symbol": key})
        value = self._extract(rows[0]) if isinstance(rows, list) and rows else None
        self.cache[key] = value
        return value

    def warm(self, symbols: Iterable[str]) -> None:
        """Populate the cache before a scan, so it is one bulk call not N."""
        if not self._bulk_loaded:
            self.load_all()


def load_provider(
    static_path: Optional[Path] = None,
    use_yahoo: bool = False,
    use_fmp: Optional[bool] = None,
) -> FloatProvider:
    """
    Pick a float source.

    A static file wins when present — it is explicit and cannot fail. Then
    FMP if a key is available, since it is the only source that scales to the
    whole market. Yahoo last. Nothing configured means float stays unknown,
    and low-float conditions therefore never match.
    """
    if static_path and Path(static_path).exists():
        return StaticFloatProvider(static_path)
    if use_fmp or (use_fmp is None and os.getenv("FMP_API_KEY")):
        return FMPFloatProvider()
    if use_yahoo:
        return YahooFloatProvider()
    return NullFloatProvider()
