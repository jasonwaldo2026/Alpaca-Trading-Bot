"""
Symbol universes and asset-class routing.

Alpaca splits stocks and crypto across two different data clients with two
different request types, so every app needs to answer "is this symbol
crypto?" the same way. That answer lives here.
"""

from typing import Iterable, List, Sequence, Tuple

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


def is_crypto(symbol: str) -> bool:
    """
    Crypto pairs use a slash ("BTC/USD"); equities never do.

    This is the single rule the whole codebase routes on — if Alpaca ever
    changes pair formatting, this is the one place to fix.
    """
    return "/" in symbol


def asset_class(symbol: str) -> str:
    return CRYPTO if is_crypto(symbol) else STOCK


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
        if name_or_symbols not in named:
            raise KeyError(
                f"Unknown universe {name_or_symbols!r}. "
                f"Known: {', '.join(sorted(named))}"
            )
        return list(named[name_or_symbols])
    return list(name_or_symbols)
