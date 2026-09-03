"""Asset-class routing tests."""

import pytest

from core import universe


@pytest.mark.parametrize("symbol,expected", [
    ("AAPL", False), ("SPY", False), ("BRK.B", False),
    ("BTC/USD", True), ("ETH/USD", True),
])
def test_is_crypto(symbol, expected):
    assert universe.is_crypto(symbol) is expected


def test_asset_class_labels():
    assert universe.asset_class("AAPL") == universe.STOCK
    assert universe.asset_class("BTC/USD") == universe.CRYPTO


def test_split_preserves_order():
    stocks, crypto = universe.split_by_asset_class(
        ["AAPL", "BTC/USD", "MSFT", "ETH/USD"]
    )
    assert stocks == ["AAPL", "MSFT"]
    assert crypto == ["BTC/USD", "ETH/USD"]


def test_split_handles_empty():
    assert universe.split_by_asset_class([]) == ([], [])


def test_resolve_named_universe():
    assert universe.resolve("major_crypto") == list(universe.MAJOR_CRYPTO)


def test_resolve_explicit_list_passes_through():
    assert universe.resolve(["AAPL", "TSLA"]) == ["AAPL", "TSLA"]


def test_resolve_unknown_name_lists_valid_options():
    with pytest.raises(KeyError, match="sp500_liquid"):
        universe.resolve("nope")


# ── Position symbols ─────────────────────────────────────────────────────────

def test_crypto_positions_are_keyed_by_the_watchlist_form():
    """Alpaca reports a held BTC/USD position as "BTCUSD". Left that way, the
    bot never matches it against its own watchlist: the exit is never
    evaluated and a second entry is not blocked."""
    from core.universe import canonical_symbol
    assert canonical_symbol("BTCUSD", crypto=True) == "BTC/USD"
    assert canonical_symbol("ETHUSDT", crypto=True) == "ETH/USDT"
    assert canonical_symbol("SOLUSDC", crypto=True) == "SOL/USDC"
    assert canonical_symbol("ETHBTC", crypto=True) == "ETH/BTC"


def test_already_canonical_and_equity_symbols_pass_through():
    from core.universe import canonical_symbol
    assert canonical_symbol("BTC/USD", crypto=True) == "BTC/USD"
    assert canonical_symbol("AAPL", crypto=False) == "AAPL"
    # An equity that happens to end in USD is not a pair.
    assert canonical_symbol("FUSD", crypto=False) == "FUSD"


def test_an_unrecognised_crypto_symbol_is_left_alone():
    from core.universe import canonical_symbol
    assert canonical_symbol("USD", crypto=True) == "USD"
    assert canonical_symbol("WEIRD", crypto=True) == "WEIRD"
