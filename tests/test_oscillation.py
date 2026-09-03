"""
Detecting cyclical stocks — ones that travel a long way and end up nowhere.

Three measures, because no single one is enough:

- `efficiency`  — net move / distance travelled. Near 0 is choppy, 1 is a
  straight line. Alone it cannot tell a 5% oscillation from a 0.1% wobble.
- `range_pct`   — high-to-low amplitude. Supplies the "is it worth trading".
- `swings`      — completed 5% legs so far today. The most literal reading of
  "up and down over and over".
"""

import numpy as np
import pandas as pd
import pytest

from core.indicators import (
    IndicatorParams,
    add_indicators,
    efficiency_ratio,
    range_pct,
    swing_count,
)
from core.rules import load_rules
from core.sessions import session_day_series

#: The two scenarios that alert. Kept as one list so adding a third is a
#: single edit rather than a hunt through the tests.
SCENARIOS = ("strong-above-vwap", "bouncing-around-mean")


def _alternating(legs: int, pct: float = 8.0, start: float = 100.0):
    """Prices that rise and fall by `pct` alternately — a clean oscillator."""
    prices = [start]
    for i in range(legs):
        prices.append(prices[-1] * (1 + pct / 100 if i % 2 == 0 else 1 / (1 + pct / 100)))
    return pd.Series(prices)


# ── Efficiency ratio ─────────────────────────────────────────────────────────

def test_a_straight_line_is_perfectly_efficient():
    trend = pd.Series(np.linspace(100, 140, 9))
    assert efficiency_ratio(trend, 8).iloc[-1] == pytest.approx(1.0)


def test_an_oscillator_that_returns_home_is_maximally_inefficient():
    osc = pd.Series([100.0, 105, 100, 105, 100, 105, 100, 105, 100])
    assert efficiency_ratio(osc, 8).iloc[-1] == pytest.approx(0.0)


def test_a_choppy_uptrend_sits_between():
    prices = pd.Series([100.0, 103, 101, 104, 102, 105, 103, 106, 108])
    ratio = efficiency_ratio(prices, 8).iloc[-1]
    assert 0.1 < ratio < 0.9


def test_a_flat_line_has_no_defined_efficiency():
    """Zero distance travelled — the ratio is undefined, not zero."""
    assert pd.isna(efficiency_ratio(pd.Series([100.0] * 9), 8).iloc[-1])


def test_efficiency_period_must_be_positive():
    with pytest.raises(ValueError, match="efficiency period"):
        efficiency_ratio(pd.Series([1.0, 2.0]), 0)


# ── Amplitude ────────────────────────────────────────────────────────────────

def test_range_pct_measures_high_to_low():
    df = pd.DataFrame({"high": [110.0] * 5, "low": [100.0] * 5})
    assert range_pct(df, 5).iloc[-1] == pytest.approx(10.0)


def test_range_pct_separates_a_real_swing_from_a_wobble():
    """Both are inefficient; only one is worth trading."""
    big = pd.DataFrame({"high": [105.0] * 9, "low": [95.0] * 9})
    tiny = pd.DataFrame({"high": [100.2] * 9, "low": [100.0] * 9})
    assert range_pct(big, 8).iloc[-1] > 10
    assert range_pct(tiny, 8).iloc[-1] < 1


# ── Swing counting ───────────────────────────────────────────────────────────

def test_each_completed_leg_counts_once():
    """A leg counts when it *completes* — the final one is still open, since
    its size is not known until price turns."""
    assert swing_count(_alternating(8), 5).iloc[-1] == 7


def test_a_trend_completes_no_legs():
    """A direction is established but never reverses, so nothing closes."""
    assert swing_count(pd.Series(np.linspace(100, 140, 9)), 5).iloc[-1] == 0


def test_moves_below_the_threshold_are_not_swings():
    assert swing_count(_alternating(8, pct=2.0), 5).iloc[-1] == 0


def test_a_flat_series_has_no_swings():
    assert swing_count(pd.Series([100.0] * 20), 5).iloc[-1] == 0


def test_the_threshold_is_measured_from_the_extreme():
    """A trader means 'it pulled back 5% from the high', not 'from where it
    started'."""
    # Up to 110, then down to 104.5 — exactly 5% off the 110 high, so the
    # up-leg closes.
    assert swing_count(pd.Series([100.0, 110.0, 104.5]), 5).iloc[-1] == 1
    # A shallower pullback leaves the leg open.
    assert swing_count(pd.Series([100.0, 110.0, 106.0]), 5).iloc[-1] == 0


def test_a_completed_leg_is_measured_pivot_to_extreme():
    """The leg that closes is worth its full run, not the retracement."""
    from core.indicators import swing_total_pct
    # 100 -> 110 is a 10% leg; the pullback to 104.5 closes it.
    total = swing_total_pct(pd.Series([100.0, 110.0, 104.5]), 5).iloc[-1]
    assert total == pytest.approx(10.0)


def test_the_count_is_monotonic():
    counts = swing_count(_alternating(10), 5).tolist()
    assert counts == sorted(counts)


def test_swings_reset_each_trading_day():
    """Yesterday's swings are not today's opportunity."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")

    prices, stamps = [], []
    for day in (2, 3):
        legs = _alternating(6).tolist()
        prices += legs
        stamps += [datetime(2026, 9, day, 10, i, tzinfo=et) for i in range(len(legs))]

    series = pd.Series(prices, index=pd.DatetimeIndex(stamps))
    anchor = session_day_series(series.index, "AAPL")

    counted = swing_count(series, 5, anchor)
    first_day_final = counted.iloc[len(prices) // 2 - 1]
    second_day_first = counted.iloc[len(prices) // 2]
    assert first_day_final == 5, "six legs run, five have closed"
    assert second_day_first == 0, "day two starts fresh"


def test_swing_threshold_must_be_positive():
    with pytest.raises(ValueError, match="swing threshold"):
        swing_count(pd.Series([1.0]), 0)


def test_swing_anchor_must_align():
    with pytest.raises(ValueError, match="share an index"):
        swing_count(pd.Series([1.0, 2.0]), 5, pd.Series(["a"]))


# ── Wiring ───────────────────────────────────────────────────────────────────

def test_add_indicators_emits_the_oscillation_columns():
    n = 250
    prices = _alternating(n - 1, pct=6.0).values
    df = pd.DataFrame(
        {"open": prices, "high": prices * 1.01, "low": prices * 0.99,
         "close": prices, "volume": np.full(n, 1000.0)},
        index=pd.date_range("2026-09-02 13:30", periods=n, freq="5min", tz="UTC"),
    )
    out = add_indicators(df, IndicatorParams(bar_minutes=5))
    last = out.iloc[-1]
    assert last["swings"] > 10
    assert last["swing_total_pct"] > 10
    assert last["range_pct"] > 5
    assert last["efficiency"] < 0.5


def test_a_trending_series_scores_the_opposite_way():
    n = 250
    prices = np.linspace(100, 160, n)
    df = pd.DataFrame(
        {"open": prices, "high": prices * 1.001, "low": prices * 0.999,
         "close": prices, "volume": np.full(n, 1000.0)},
        index=pd.date_range("2026-09-02 13:30", periods=n, freq="5min", tz="UTC"),
    )
    last = add_indicators(df, IndicatorParams(bar_minutes=5)).iloc[-1]
    assert last["efficiency"] > 0.9
    assert last["swings"] <= 1


# ── The scenario ─────────────────────────────────────────────────────────────

def test_the_bouncing_scenario_loads_and_validates():
    rule = load_rules(["rules/bouncing-around-mean.json"])[0]
    rule.validate()
    assert rule.params.swing_threshold_pct == 1.0
    assert "swing_total_pct >= 10" in rule.describe()
    assert "efficiency <= 0.35" in rule.describe()


def test_the_bouncing_alert_reports_the_travel():
    """A range trade fails when the range breaks; the message should say so."""
    from core.alerts import build_context
    rule = load_rules(["rules/bouncing-around-mean.json"])[0]
    payload = rule.alert.render(build_context(
        "ABCD", 50.0, rule.name, rule.alert,
        {"swings": 6, "swing_total_pct": 13.4, "efficiency": 0.12, "vwap": 49.8},
    ))
    assert "repeatedly bouncing" in payload["message"]
    assert "13.4% travelled" in payload["message"]


def test_the_two_scenarios_are_documented_as_opposites():
    """One is momentum, the other mean reversion. If both ever fire on the
    same symbol, neither should be trusted."""
    rule = load_rules(["rules/bouncing-around-mean.json"])[0]
    assert "opposite of 'strong above VWAP'" in rule.description


# ── Cumulative travel, not fixed leg sizes ───────────────────────────────────

def test_legs_of_any_size_add_up():
    """Eight 2% moves and two 8% moves are both bouncing; pinning the pattern
    to a specific leg size would miss whichever shape the stock has."""
    from core.indicators import swing_total_pct

    many_small = _alternating(8, pct=2.0)
    few_big = _alternating(2, pct=8.0)

    assert swing_total_pct(many_small, 1.0).iloc[-1] > 10
    assert swing_total_pct(few_big, 1.0).iloc[-1] > 5


def test_a_trend_travels_nothing_in_completed_legs():
    """A leg only closes when price reverses, so a straight run has no
    completed travel however far it goes."""
    from core.indicators import swing_total_pct
    trend = pd.Series(np.linspace(100, 110, 12))
    assert swing_total_pct(trend, 1.0).iloc[-1] == 0.0


def test_the_threshold_is_a_noise_floor_not_the_pattern():
    """Jitter below the floor contributes nothing; the pattern is defined by
    the total, not by any single leg."""
    from core.indicators import swing_total_pct
    jitter = _alternating(20, pct=0.1)
    assert swing_total_pct(jitter, 1.0).iloc[-1] == 0.0


def test_travel_is_monotonic():
    from core.indicators import swing_total_pct
    totals = swing_total_pct(_alternating(10, pct=3.0), 1.0).tolist()
    assert totals == sorted(totals)


def test_travel_resets_each_trading_day():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from core.indicators import swing_total_pct
    et = ZoneInfo("America/New_York")

    prices, stamps = [], []
    for day in (2, 3):
        legs = _alternating(6, pct=3.0).tolist()
        prices += legs
        stamps += [datetime(2026, 9, day, 10, i, tzinfo=et) for i in range(len(legs))]

    series = pd.Series(prices, index=pd.DatetimeIndex(stamps))
    anchor = session_day_series(series.index, "AAPL")
    totals = swing_total_pct(series, 1.0, anchor)

    assert totals.iloc[len(prices) // 2 - 1] > 0
    assert totals.iloc[len(prices) // 2] == 0.0, "day two starts fresh"


def test_the_scenario_triggers_on_cumulative_travel():
    rule = load_rules(["rules/bouncing-around-mean.json"])[0]
    rule.validate()
    assert "swing_total_pct >= 10" in rule.describe()
    assert rule.params.swing_threshold_pct == 1.0, "threshold is a noise floor"


def test_the_scenario_scans_every_stock():
    """Any stock can fit any scenario, so nothing is restricted by default."""
    for name in SCENARIOS:
        assert load_rules([f"rules/{name}.json"])[0].universe == "all_tradable"


def test_the_bouncing_alert_uses_the_requested_wording():
    from core.alerts import build_context
    rule = load_rules(["rules/bouncing-around-mean.json"])[0]
    payload = rule.alert.render(build_context(
        "ABCD", 12.40, rule.name, rule.alert,
        {"swings": 6, "swing_total_pct": 14.2, "efficiency": 0.11, "vwap": 12.35},
    ))
    assert "ABCD stock is repeatedly bouncing at least 10% around the mean price" \
        in payload["message"]


def test_every_scenario_links_to_the_robinhood_chart():
    """Each alert's symbol opens the app's instrument screen — the chart, with
    the Trade button on it."""
    for name in SCENARIOS:
        rule = load_rules([f"rules/{name}.json"])[0]
        assert rule.alert.link_template == "robinhood://instrument/{symbol}"


# ── Universe sizing ──────────────────────────────────────────────────────────

def test_a_wide_universe_warns_about_the_cost():
    from core import universe
    warning = universe.warn_if_large(["X"] * 11000, 5)
    assert "11,000 symbols" in warning
    assert "110 batched requests" in warning


def test_a_small_universe_does_not_warn():
    from core import universe
    assert universe.warn_if_large(["X"] * 50) is None


def test_all_tradable_needs_a_client_and_says_so():
    from core import universe
    with pytest.raises(KeyError, match="needs a client"):
        universe.resolve("all_tradable")


def test_the_asset_list_is_cached_for_the_day(tmp_path):
    import json
    from datetime import date
    from types import SimpleNamespace

    from core import universe

    cache = tmp_path / "assets.json"
    cache.write_text(json.dumps({"date": date.today().isoformat(),
                                 "symbols": ["AAA", "BBB"]}))

    def explode(*a, **kw):
        raise AssertionError("should have used the cache")

    client = SimpleNamespace(trading=SimpleNamespace(get_all_assets=explode))
    assert universe.all_tradable(client, cache) == ["AAA", "BBB"]


def test_a_stale_cache_is_refetched(tmp_path):
    import json
    from types import SimpleNamespace

    from core import universe

    cache = tmp_path / "assets.json"
    cache.write_text(json.dumps({"date": "2020-01-01", "symbols": ["OLD"]}))

    assets = [SimpleNamespace(symbol="NEW", tradable=True),
              SimpleNamespace(symbol="SKIP", tradable=False)]
    client = SimpleNamespace(
        trading=SimpleNamespace(get_all_assets=lambda req: assets)
    )
    assert universe.all_tradable(client, cache) == ["NEW"], "untradable filtered"
