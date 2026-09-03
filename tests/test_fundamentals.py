"""
Float lookup.

Float is not derivable from price and volume and Alpaca does not serve it,
so it comes from outside. The rule that governs every provider here: an
unknown float must **fail closed**. A low-float condition that silently
matched every symbol because the lookup failed would be the dangerous
outcome, so unknown becomes NaN and NaN comparisons are False.
"""

import json


import pytest

from core.fundamentals import (
    FMPFloatProvider,
    NullFloatProvider,
    StaticFloatProvider,
    load_provider,
)


# ── Static file ──────────────────────────────────────────────────────────────

def test_static_provider_reads_shares(tmp_path):
    path = tmp_path / "floats.json"
    path.write_text(json.dumps({"WXYZ": 45000000}))
    assert StaticFloatProvider(path).float_shares("WXYZ") == 45_000_000


def test_millions_shorthand_is_understood(tmp_path):
    """8.2 means 8.2M — no real float is 8 shares."""
    path = tmp_path / "floats.json"
    path.write_text(json.dumps({"ABCD": 8.2}))
    provider = StaticFloatProvider(path)
    assert provider.float_millions("ABCD") == pytest.approx(8.2)


def test_lookup_is_case_insensitive(tmp_path):
    path = tmp_path / "floats.json"
    path.write_text(json.dumps({"abcd": 8.2}))
    assert StaticFloatProvider(path).float_shares("ABCD") is not None


def test_unknown_symbol_is_unknown_not_zero(tmp_path):
    path = tmp_path / "floats.json"
    path.write_text(json.dumps({"ABCD": 8.2}))
    assert StaticFloatProvider(path).float_shares("NOPE") is None


def test_a_missing_file_is_not_fatal(tmp_path):
    provider = StaticFloatProvider(tmp_path / "absent.json")
    assert provider.float_shares("ABCD") is None


def test_a_corrupt_file_is_not_fatal(tmp_path):
    path = tmp_path / "floats.json"
    path.write_text("{not json")
    assert StaticFloatProvider(path).float_shares("ABCD") is None


def test_null_provider_knows_nothing():
    assert NullFloatProvider().float_shares("AAPL") is None
    assert NullFloatProvider().float_millions("AAPL") is None


# ── FMP ──────────────────────────────────────────────────────────────────────

class FakeFMP(FMPFloatProvider):
    """FMP with the HTTP call replaced."""

    def __init__(self, responses, **kw):
        self.responses = responses
        self.requests = []
        super().__init__(api_key="test-key", **kw)

    def _get(self, url, params):
        self.requests.append((url, params))
        return self.responses.pop(0) if self.responses else None


def test_bulk_load_fills_the_cache(tmp_path):
    rows = [{"symbol": "ABCD", "floatShares": 8_200_000},
            {"symbol": "WXYZ", "floatShares": 45_000_000}]
    provider = FakeFMP([rows], cache_path=tmp_path / "c.json")

    assert provider.load_all() == 2
    assert provider.float_shares("ABCD") == 8_200_000
    assert provider.float_millions("WXYZ") == pytest.approx(45.0)


def test_one_bulk_call_covers_the_whole_market(tmp_path):
    """The reason FMP is preferred: 11k symbols must not be 11k requests."""
    rows = [{"symbol": f"S{i}", "floatShares": 1_000_000} for i in range(50)]
    provider = FakeFMP([rows], cache_path=tmp_path / "c.json")
    provider.warm([f"S{i}" for i in range(50)])

    for i in range(50):
        provider.float_shares(f"S{i}")
    assert len(provider.requests) == 1, "cache served every symbol after one call"


def test_paging_stops_on_a_short_page(tmp_path):
    full = [{"symbol": f"S{i}", "floatShares": 1} for i in range(FMPFloatProvider.PAGE_LIMIT)]
    short = [{"symbol": "LAST", "floatShares": 1}]
    provider = FakeFMP([full, short], cache_path=tmp_path / "c.json")
    provider.load_all()
    assert len(provider.requests) == 2


def test_float_shares_is_preferred_over_outstanding():
    provider = FMPFloatProvider(api_key="k")
    assert provider._extract(
        {"floatShares": 8_000_000, "outstandingShares": 20_000_000}
    ) == 8_000_000


def test_outstanding_is_a_last_resort():
    provider = FMPFloatProvider(api_key="k")
    assert provider._extract({"outstandingShares": 20_000_000}) == 20_000_000
    assert provider._extract({}) is None


def test_a_failed_request_yields_unknown_not_a_crash(tmp_path):
    provider = FakeFMP([None], cache_path=tmp_path / "c.json")
    assert provider.float_shares("ABCD") is None


def test_no_api_key_means_unknown(tmp_path, monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    provider = FMPFloatProvider(api_key="", cache_path=tmp_path / "c.json")
    assert provider.load_all() == 0
    assert provider.float_shares("ABCD") is None


def test_the_api_key_never_reaches_a_log_message():
    """The key travels in the query string, so any error derived from the
    request could carry it into a log file."""
    provider = FMPFloatProvider(api_key="SECRET123")
    scrubbed = provider._scrub("HTTP Error 403 for ...apikey=SECRET123&page=0")
    assert "SECRET123" not in scrubbed
    assert "***" in scrubbed


def test_the_cache_is_reused_within_the_day(tmp_path):
    cache = tmp_path / "c.json"
    rows = [{"symbol": "ABCD", "floatShares": 8_200_000}]
    FakeFMP([rows], cache_path=cache).load_all()

    reopened = FakeFMP([], cache_path=cache)
    assert reopened.float_shares("ABCD") == 8_200_000
    assert reopened.requests == [], "served from cache, no request"


# ── Choosing a provider ──────────────────────────────────────────────────────

def test_a_static_file_wins_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "k")
    path = tmp_path / "floats.json"
    path.write_text(json.dumps({"ABCD": 8.2}))
    assert isinstance(load_provider(path), StaticFloatProvider)


def test_fmp_is_used_when_a_key_is_present(tmp_path, monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "k")
    assert isinstance(load_provider(tmp_path / "absent.json"), FMPFloatProvider)


def test_nothing_configured_means_float_stays_unknown(tmp_path, monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    provider = load_provider(tmp_path / "absent.json")
    assert isinstance(provider, NullFloatProvider)


# ── Fail-closed through the scanner ──────────────────────────────────────────

def test_a_low_float_rule_does_not_match_when_float_is_unknown():
    """The whole point: no data must mean no match, never every match."""
    import numpy as np
    import pandas as pd

    from core.indicators import IndicatorParams
    from core.rules import Condition, Rule
    from scanner.engine import Scanner

    n = 300
    closes = 100 + np.cumsum(np.random.default_rng(1).normal(0, 0.5, n))
    df = pd.DataFrame(
        {"open": closes, "high": closes + 1, "low": closes - 1,
         "close": closes, "volume": np.full(n, 1000.0)},
        index=pd.date_range("2026-09-02 13:30", periods=n, freq="5min", tz="UTC"),
    )

    class FakeFetcher:
        def get_bars(self, symbols, limit=60, timeframe=None):
            return {s: df for s in symbols}

    rule = Rule(
        name="low float", universe=["ABCD"],
        params=IndicatorParams(bar_minutes=5),
        conditions=[Condition("float_millions", "<=", value=50)],
    )
    result = Scanner(FakeFetcher(), bar_minutes=5, skip_closed=False).scan([rule])

    assert result.matches == [], "unknown float must not match"
    assert "ABCD" in result.missing_float


def test_a_known_low_float_does_match(tmp_path):
    import numpy as np
    import pandas as pd

    from core.indicators import IndicatorParams
    from core.rules import Condition, Rule
    from scanner.engine import Scanner

    n = 300
    closes = 100 + np.cumsum(np.random.default_rng(1).normal(0, 0.5, n))
    df = pd.DataFrame(
        {"open": closes, "high": closes + 1, "low": closes - 1,
         "close": closes, "volume": np.full(n, 1000.0)},
        index=pd.date_range("2026-09-02 13:30", periods=n, freq="5min", tz="UTC"),
    )

    class FakeFetcher:
        def get_bars(self, symbols, limit=60, timeframe=None):
            return {s: df for s in symbols}

    path = tmp_path / "floats.json"
    path.write_text(json.dumps({"ABCD": 8.2}))

    rule = Rule(
        name="low float", universe=["ABCD"],
        params=IndicatorParams(bar_minutes=5),
        conditions=[Condition("float_millions", "<=", value=50)],
    )
    result = Scanner(
        FakeFetcher(), bar_minutes=5, skip_closed=False,
        fundamentals=StaticFloatProvider(path),
    ).scan([rule])

    assert len(result.matches) == 1
    assert result.matches[0].values["float_millions"] == pytest.approx(8.2)
