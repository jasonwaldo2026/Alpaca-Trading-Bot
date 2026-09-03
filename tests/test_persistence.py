"""Run-length helpers and the rule-level `for_bars` operator."""

import numpy as np
import pandas as pd
import pytest

from core.persistence import bars_since, consecutive_true, held_for
from core.rules import Condition, Rule, RuleError


# ── Run lengths ──────────────────────────────────────────────────────────────

def test_consecutive_true_counts_the_current_run():
    flags = pd.Series([False, True, True, False, True, True, True])
    assert consecutive_true(flags).tolist() == [0, 1, 2, 0, 1, 2, 3]


def test_a_false_resets_the_run():
    flags = pd.Series([True] * 5 + [False] + [True] * 2)
    assert consecutive_true(flags).tolist() == [1, 2, 3, 4, 5, 0, 1, 2]


def test_nan_breaks_a_run():
    """A warming-up indicator must not count as satisfying a condition."""
    flags = pd.Series([True, np.nan, True, True])
    assert consecutive_true(flags).tolist() == [1, 0, 1, 2]


def test_all_false_and_all_true():
    assert consecutive_true(pd.Series([False] * 4)).tolist() == [0, 0, 0, 0]
    assert consecutive_true(pd.Series([True] * 4)).tolist() == [1, 2, 3, 4]


def test_empty_series_is_safe():
    assert consecutive_true(pd.Series([], dtype=bool)).empty


def test_held_for_needs_the_full_run():
    flags = pd.Series([False, True, True, True])
    assert held_for(flags, 3).tolist() == [False, False, False, True]
    assert held_for(flags, 4).tolist() == [False] * 4


def test_held_for_rejects_a_nonsense_length():
    with pytest.raises(ValueError, match="bars must be >= 1"):
        held_for(pd.Series([True]), 0)


def test_bars_since_counts_back_to_the_last_true():
    flags = pd.Series([False, True, False, False, True])
    result = bars_since(flags)
    assert pd.isna(result.iloc[0]), "no True has happened yet"
    assert result.iloc[1] == 0
    assert result.iloc[3] == 2
    assert result.iloc[4] == 0


# ── The rule operator ────────────────────────────────────────────────────────

def _frame(closes, vwaps):
    return pd.DataFrame({"close": closes, "vwap": vwaps})


ABOVE_3 = Condition("close", ">", field2="vwap", for_bars=3)


def test_three_bars_above_vwap_matches():
    """The shipped alert threshold: three consecutive 5-minute closes above
    VWAP."""
    assert ABOVE_3.evaluate(_frame([99, 101, 102, 103], [100.0] * 4))


def test_two_bars_above_vwap_does_not_match():
    assert not ABOVE_3.evaluate(_frame([99, 99, 101, 102], [100.0] * 4))


def test_a_dip_below_resets_the_count():
    assert not ABOVE_3.evaluate(_frame([101, 102, 99, 101, 102], [100.0] * 5))


def test_run_must_end_on_the_latest_bar():
    """Three bars above VWAP earlier in the frame is not a current signal."""
    assert not ABOVE_3.evaluate(_frame([101, 102, 103, 99], [100.0] * 4))


def test_a_frame_shorter_than_the_run_cannot_match():
    assert not ABOVE_3.evaluate(_frame([101, 102], [100.0, 100.0]))


def test_nan_in_the_indicator_breaks_the_run():
    """VWAP is NaN on a zero-volume bar; that must not extend a run."""
    frame = _frame([101, 102, 103], [100.0, float("nan"), 100.0])
    assert not ABOVE_3.evaluate(frame)


def test_for_bars_one_is_the_plain_comparison():
    plain = Condition("close", ">", field2="vwap")
    once = Condition("close", ">", field2="vwap", for_bars=1)
    frame = _frame([99, 99, 101], [100.0] * 3)
    assert plain.evaluate(frame) == once.evaluate(frame) is True


def test_for_bars_works_with_a_literal_threshold():
    cond = Condition("rsi", ">", value=50, for_bars=2)
    assert cond.evaluate(pd.DataFrame({"rsi": [40.0, 60.0, 70.0]}))
    assert not cond.evaluate(pd.DataFrame({"rsi": [60.0, 40.0, 70.0]}))


def test_for_bars_must_be_positive():
    with pytest.raises(RuleError, match="for_bars must be >= 1"):
        Condition("close", ">", field2="vwap", for_bars=0).validate()


def test_for_bars_is_rejected_on_a_crossover():
    """A crossover is a single-bar event; requiring it to persist is a
    contradiction."""
    with pytest.raises(RuleError, match="single-bar event"):
        Condition("sma_fast", "crosses_above", field2="sma_slow",
                  for_bars=3).validate()


def test_describe_mentions_the_persistence():
    assert ABOVE_3.describe() == "close > vwap for 3 bars"


def test_persistence_survives_a_json_round_trip():
    rule = Rule(name="vwap hold", conditions=[ABOVE_3])
    restored = Rule.from_json(rule.to_json())
    assert restored.conditions[0].for_bars == 3
    assert restored.describe() == "close > vwap for 3 bars"
