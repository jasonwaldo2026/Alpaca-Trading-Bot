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
