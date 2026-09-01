"""Scan rule tests — the Studio↔Scanner contract."""

import numpy as np
import pandas as pd
import pytest

from core.indicators import IndicatorParams, add_indicators
from core.rules import Condition, Rule, RuleError


@pytest.fixture
def bars() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 1.0, 120))
    return pd.DataFrame({
        "open": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": rng.integers(1_000, 50_000, 120).astype(float),
    })


# ── Conditions ───────────────────────────────────────────────────────────────

def test_literal_comparison():
    df = pd.DataFrame({"rsi": [50.0, 25.0]})
    assert Condition("rsi", "<", value=30).evaluate(df)
    assert not Condition("rsi", ">", value=30).evaluate(df)


def test_field_to_field_comparison():
    df = pd.DataFrame({"volume": [10.0, 90.0], "vol_sma": [50.0, 50.0]})
    assert Condition("volume", ">", field2="vol_sma").evaluate(df)


def test_crossover_needs_two_bars():
    df = pd.DataFrame({"sma_fast": [1.0, 3.0], "sma_slow": [2.0, 2.0]})
    assert Condition("sma_fast", "crosses_above", field2="sma_slow").evaluate(df)
    assert not Condition("sma_fast", "crosses_above", field2="sma_slow").evaluate(df.iloc[:1])


def test_nan_never_matches():
    """A symbol still in indicator warmup must not satisfy a condition."""
    df = pd.DataFrame({"rsi": [50.0, float("nan")]})
    assert not Condition("rsi", "<", value=30).evaluate(df)
    assert not Condition("rsi", ">", value=30).evaluate(df)


def test_unknown_field_is_an_error():
    df = pd.DataFrame({"rsi": [50.0]})
    with pytest.raises(RuleError, match="Unknown field"):
        Condition("macd", "<", value=0).evaluate(df)


def test_unknown_operator_is_an_error():
    with pytest.raises(RuleError, match="Unknown operator"):
        Condition("rsi", "≈", value=30).validate()


def test_condition_needs_exactly_one_right_hand_side():
    with pytest.raises(RuleError, match="exactly one"):
        Condition("rsi", "<").validate()
    with pytest.raises(RuleError, match="exactly one"):
        Condition("rsi", "<", value=30, field2="vol_sma").validate()


def test_crossover_rejects_literal_value():
    with pytest.raises(RuleError, match="needs 'field2'"):
        Condition("sma_fast", "crosses_above", value=1.0).validate()


# ── Rules ────────────────────────────────────────────────────────────────────

def test_rule_requires_conditions():
    with pytest.raises(RuleError, match="no conditions"):
        Rule(name="empty").validate()


def test_rule_requires_name():
    with pytest.raises(RuleError, match="non-empty name"):
        Rule(name="  ", conditions=[Condition("rsi", "<", value=30)]).validate()


def test_rule_all_conditions_must_hold(bars):
    enriched = add_indicators(bars, IndicatorParams())
    always = Condition("close", ">", value=-1e9)
    never = Condition("close", "<", value=-1e9)
    assert Rule(name="a", conditions=[always, always]).matches(enriched)
    assert not Rule(name="b", conditions=[always, never]).matches(enriched)


def test_rule_computes_indicators_when_given_raw_bars(bars):
    """Scanner may pass raw OHLCV; the rule adds indicators itself."""
    rule = Rule(name="rsi", conditions=[Condition("rsi", "<", value=200)])
    assert rule.matches(bars)


def test_rule_rejects_too_short_history(bars):
    rule = Rule(name="rsi", conditions=[Condition("rsi", "<", value=200)])
    assert not rule.matches(bars.iloc[:5])
    assert not rule.matches(pd.DataFrame())


# ── Serialization — Studio saves, Scanner loads ──────────────────────────────

def test_round_trips_through_json():
    rule = Rule(
        name="oversold reversal",
        description="RSI washout with volume",
        universe="major_crypto",
        conditions=[
            Condition("rsi", "<", value=30),
            Condition("volume", ">", field2="vol_sma"),
            Condition("sma_fast", "crosses_above", field2="sma_slow"),
        ],
        params=IndicatorParams(rsi_period=21),
    )
    restored = Rule.from_json(rule.to_json())
    assert restored.name == rule.name
    assert restored.universe == "major_crypto"
    assert restored.params.rsi_period == 21
    assert restored.describe() == rule.describe()


def test_malformed_json_is_a_rule_error():
    with pytest.raises(RuleError, match="not valid JSON"):
        Rule.from_json("{not json")


def test_malformed_condition_is_a_rule_error():
    with pytest.raises(RuleError, match="Malformed condition"):
        Rule.from_dict({"name": "x", "conditions": [{"bogus": 1}]})


def test_describe_is_human_readable():
    rule = Rule(name="x", conditions=[
        Condition("rsi", "<", value=30),
        Condition("volume", ">", field2="vol_sma"),
    ])
    assert rule.describe() == "rsi < 30 AND volume > vol_sma"
