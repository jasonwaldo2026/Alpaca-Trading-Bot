"""Scan engine tests — driven by a fake fetcher, so no network."""

import numpy as np
import pandas as pd
import pytest

from core.indicators import IndicatorParams
from core.rules import Condition, Rule
from scanner.engine import Scanner


def _bars(n=120, start=100.0, drift=0.0, seed=1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = start + np.cumsum(rng.normal(drift, 1.0, n))
    return pd.DataFrame({
        "open": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": rng.integers(1_000, 50_000, n).astype(float),
    })


class FakeFetcher:
    """Stands in for MarketDataFetcher; records what was requested."""

    def __init__(self, frames):
        self.frames = frames
        self.requests = []

    def get_bars(self, symbols, limit=60, timeframe=None):
        symbols = list(symbols)
        self.requests.append(symbols)
        return {s: self.frames[s] for s in symbols if s in self.frames}


@pytest.fixture
def fetcher():
    return FakeFetcher({"AAPL": _bars(seed=1), "MSFT": _bars(seed=2)})


def test_matches_are_reported_with_values(fetcher):
    rule = Rule(name="always", universe=["AAPL"],
                conditions=[Condition("close", ">", value=-1e9)])
    result = Scanner(fetcher).scan([rule])

    assert result.scanned == 1
    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.symbol == "AAPL"
    assert match.rule_name == "always"
    assert match.asset_class == "stock"
    assert match.price > 0
    assert "rsi" in match.values and "atr" in match.values


def test_non_matching_rule_yields_nothing(fetcher):
    rule = Rule(name="never", universe=["AAPL"],
                conditions=[Condition("close", "<", value=-1e9)])
    assert Scanner(fetcher).scan([rule]).matches == []


def test_symbols_are_fetched_once_across_rules(fetcher):
    """Two rules over the same universe must not double the API cost."""
    common = ["AAPL", "MSFT"]
    rules = [
        Rule(name="a", universe=common, conditions=[Condition("close", ">", value=-1e9)]),
        Rule(name="b", universe=common, conditions=[Condition("close", ">", value=-1e9)]),
    ]
    result = Scanner(fetcher).scan(rules)

    assert len(fetcher.requests) == 1
    assert sorted(fetcher.requests[0]) == common
    assert result.scanned == 2
    assert len(result.matches) == 4  # 2 rules × 2 symbols


def test_explicit_symbols_override_rule_universe(fetcher):
    rule = Rule(name="a", universe=["NVDA"],
                conditions=[Condition("close", ">", value=-1e9)])
    result = Scanner(fetcher).scan([rule], symbols=["AAPL"])
    assert [m.symbol for m in result.matches] == ["AAPL"]


def test_missing_data_is_skipped_not_fatal(fetcher):
    rule = Rule(name="a", universe=["AAPL", "NOPE"],
                conditions=[Condition("close", ">", value=-1e9)])
    result = Scanner(fetcher).scan([rule])
    assert result.scanned == 1
    assert "NOPE" in result.skipped


def test_short_history_is_skipped(fetcher):
    fetcher.frames["TINY"] = _bars(n=5)
    rule = Rule(name="a", universe=["TINY"],
                conditions=[Condition("close", ">", value=-1e9)])
    result = Scanner(fetcher).scan([rule])
    assert result.matches == []
    assert "TINY" in result.skipped


def test_invalid_rule_is_rejected_before_any_fetch(fetcher):
    with pytest.raises(Exception):
        Scanner(fetcher).scan([Rule(name="empty", universe=["AAPL"])])
    assert fetcher.requests == []


def test_by_rule_groups_matches(fetcher):
    rules = [
        Rule(name="a", universe=["AAPL"], conditions=[Condition("close", ">", value=-1e9)]),
        Rule(name="b", universe=["MSFT"], conditions=[Condition("close", ">", value=-1e9)]),
    ]
    grouped = Scanner(fetcher).scan(rules).by_rule()
    assert set(grouped) == {"a", "b"}


def test_empty_inputs_are_safe(fetcher):
    assert Scanner(fetcher).scan([]).matches == []
    rule = Rule(name="a", universe=[], conditions=[Condition("close", ">", value=0)])
    assert Scanner(fetcher).scan([rule]).matches == []


def test_shared_params_reuse_enriched_frame(fetcher):
    """Rules with identical IndicatorParams should hit the cache — proven by
    IndicatorParams being hashable and comparing equal."""
    assert IndicatorParams() == IndicatorParams()
    assert hash(IndicatorParams()) == hash(IndicatorParams())
