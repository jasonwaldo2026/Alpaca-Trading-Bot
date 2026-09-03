"""
Symbol universes and asset-class routing.

Alpaca splits stocks and crypto across two different data clients with two
different request types, so every app needs to answer "is this symbol
crypto?" the same way. That answer lives here.
"""

import json
import logging
from datetime import date
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

STOCK = "stock"
CRYPTO = "crypto"

DEFAULT_STOCKS: Tuple[str, ...] = ("AAPL", "MSFT", "NVDA", "SPY", "QQQ")
DEFAULT_CRYPTO: Tuple[str, ...] = ("BTC/USD", "ETH/USD", "SOL/USD")

# Broader lists for the scanner, which sweeps rather than trades a watchlist.
SP500_LIQUID: Tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO",
    "JPM", "V", "UNH", "XOM", "MA", "COST", "HD", "PG", "NFLX", "AMD",
)
MAJOR_CRYPTO: Tuple[str, ...] = (
    "BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD", "DOT/USD",
)


#: Cached asset list, refreshed once a day. The full list is ~11k symbols
#: and changes slowly, so re-fetching it every scan would be waste.
ASSET_CACHE_PATH = Path(".alpaca-assets.json")

#: A scan this wide takes real time and many requests. Above this, callers
#: are warned rather than silently left wondering why a 5-minute cadence is
#: not being met.
LARGE_UNIVERSE_WARNING = 500


def all_tradable(client, cache_path: Path = ASSET_CACHE_PATH,
                 refresh: bool = False) -> List[str]:
    """
    Every US equity Alpaca will let you trade.

    Any stock can fit any scenario, so the scanner should not be restricted
    to a curated list. This is the unrestricted universe: active, tradable US
    equities, straight from Alpaca's asset list.

    **It is large — roughly 11,000 symbols.** At 100 symbols per request that
    is ~110 requests and a lot of data per pass, which will not complete
    inside a 5-minute cadence on a REST API. See
    docs/specs/scanner/universe-size.md for what that costs and the ways
    round it.

    Cached to disk for the day, because the list changes slowly.
    """
    today = date.today().isoformat()

    if not refresh and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            if cached.get("date") == today and cached.get("symbols"):
                return list(cached["symbols"])
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            log.warning("Ignoring unreadable asset cache %s: %s", cache_path, exc)

    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest

    assets = client.trading.get_all_assets(GetAssetsRequest(
        status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY,
    ))
    symbols = sorted(a.symbol for a in assets if getattr(a, "tradable", False))

    try:
        cache_path.write_text(json.dumps({"date": today, "symbols": symbols}))
    except OSError as exc:
        log.warning("Could not cache asset list to %s: %s", cache_path, exc)

    log.info("Loaded %d tradable US equities from Alpaca.", len(symbols))
    return symbols


def warn_if_large(symbols: Sequence[str], bar_minutes: int = 5) -> Optional[str]:
    """
    A sentence describing the cost of scanning this many symbols, or None.

    Returned rather than logged so a CLI can print it and Studio can show it.
    """
    count = len(symbols)
    if count <= LARGE_UNIVERSE_WARNING:
        return None
    requests = -(-count // 100)      # ceiling division; 100 symbols per request
    return (
        f"{count:,} symbols is roughly {requests} batched requests per scan. "
        f"A pass this wide is unlikely to finish inside a {bar_minutes}-minute "
        f"cadence over REST — expect scans to overlap or lag. Narrow the "
        f"universe, lengthen the interval, or see "
        f"docs/specs/scanner/universe-size.md."
    )


def is_crypto(symbol: str) -> bool:
    """
    Crypto pairs use a slash ("BTC/USD"); equities never do.

    This is the single rule the whole codebase routes on — if Alpaca ever
    changes pair formatting, this is the one place to fix.
    """
    return "/" in symbol


def asset_class(symbol: str) -> str:
    return CRYPTO if is_crypto(symbol) else STOCK


#: Quote currencies Alpaca crypto pairs settle in. Longest first so that
#: "USDT" and "USDC" are matched before "USD".
CRYPTO_QUOTES: Tuple[str, ...] = ("USDT", "USDC", "USD", "BTC")


def canonical_symbol(symbol: str, crypto: bool) -> str:
    """
    The watchlist form of a symbol Alpaca reported.

    Alpaca quotes, trades and lists crypto as "BTC/USD" but reports held
    positions as "BTCUSD". Every lookup in the bot is keyed by the watchlist
    form, so a position left in Alpaca's form never matches: its exit is
    never evaluated, its SELL is never approved, and a second entry is not
    blocked. Equities are returned unchanged.
    """
    if not crypto or "/" in symbol:
        return symbol
    for quote in CRYPTO_QUOTES:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return f"{symbol[:-len(quote)]}/{quote}"
    return symbol


def split_by_asset_class(symbols: Iterable[str]) -> Tuple[List[str], List[str]]:
    """Partition a mixed symbol list into (stocks, crypto), order preserved."""
    stocks: List[str] = []
    crypto: List[str] = []
    for sym in symbols:
        (crypto if is_crypto(sym) else stocks).append(sym)
    return stocks, crypto


def resolve(name_or_symbols: Sequence[str] | str) -> List[str]:
    """
    Accept either a named universe ("sp500_liquid") or an explicit symbol
    list, and return a concrete symbol list. Lets a saved scan rule say
    `universe: "major_crypto"` without pinning the membership into the rule.
    """
    if isinstance(name_or_symbols, str):
        named = {
            "default_stocks": DEFAULT_STOCKS,
            "default_crypto": DEFAULT_CRYPTO,
            "sp500_liquid": SP500_LIQUID,
            "major_crypto": MAJOR_CRYPTO,
        }
        if name_or_symbols == "all_tradable":
            raise KeyError(
                "The 'all_tradable' universe is resolved from Alpaca's asset "
                "list, so it needs a client: call universe.all_tradable(client) "
                "and pass the result, or use --universe all_tradable on the "
                "scanner CLI."
            )
        if name_or_symbols not in named:
            raise KeyError(
                f"Unknown universe {name_or_symbols!r}. "
                f"Known: {', '.join(sorted(named))}, all_tradable"
            )
        return list(named[name_or_symbols])
    return list(name_or_symbols)
