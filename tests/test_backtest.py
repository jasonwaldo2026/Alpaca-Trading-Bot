"""
Backtest engine: lookahead-freedom, fills, and dual-timeframe exits.

Frames here are hand-built with indicator columns pre-set, so every expected
trade can be reasoned about by hand rather than depending on indicator math
that is tested elsewhere.
"""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from core.backtest import (
    EXIT_EMA,
    EXIT_END_OF_DATA,
    EXIT_STOP,
    ExitPolicy,
    backtest,
)
from core.indicators import IndicatorParams
from core.rules import Condition, Rule

UTC = "UTC"

# Small periods so min_bars() does not swamp a short fixture. The rule's own
# EMA is unrelated to the exit EMA, which lives on the management frame.
SMALL = IndicatorParams(
    sma_fast=2, sma_slow=3, ema_periods=(2,), rsi_period=2,
    volume_sma_period=2, atr_period=2,
    macd_fast=2, macd_slow=3, macd_signal=2, bar_minutes=5,
)
MIN_BARS = SMALL.min_bars()          # 7


def _frame(start: str, count: int, minutes: int, **columns) -> pd.DataFrame:
    idx = pd.date_range(start, periods=count, freq=f"{minutes}min", tz=UTC)
    data = {name: list(values) for name, values in columns.items()}
    return pd.DataFrame(data, index=idx)


def _entry_frame(closes, vwaps, atr=1.0):
    """5-minute entry bars. Carries ema_9 too, so it can stand in as its own
    management frame when a test does not supply a finer one."""
    n = len(closes)
    return _frame(
        "2026-09-02 14:00", n, 5,
        open=closes, high=[c + 0.5 for c in closes], low=[c - 0.5 for c in closes],
        close=closes, volume=[1000.0] * n, vwap=vwaps, atr=[atr] * n,
        rsi=[50.0] * n, ema_9=[0.0] * n,
    )


def _below_then_above(above: int = 3, below: int = 6):
    """Closes that sit under VWAP, then hold above it for `above` bars.

    `below` is long enough that the frame clears min_bars() before the run
    completes, so the entry is driven by the rule rather than by warmup.
    """
    return [99.0] * below + [101.0 + i for i in range(above)]


def _manage_frame(start, closes, ema9, lows=None, opens=None):
    n = len(closes)
    return _frame(
        start, n, 1,
        open=opens or closes,
        high=[c + 0.2 for c in closes],
        low=lows or [c - 0.2 for c in closes],
        close=closes, volume=[100.0] * n, ema_9=ema9,
    )


ABOVE_VWAP = Rule(
    name="above vwap",
    params=SMALL,
    conditions=[Condition("close", ">", field2="vwap", for_bars=3)],
)


# ── Entry timing ─────────────────────────────────────────────────────────────

def test_entry_fills_at_the_next_bar_open_not_the_signal_close():
    """The signal exists at a bar's close, so the earliest tradable price is
    the next bar's open. Filling at the signal close credits a price that
    could not have been traded."""
    closes = _below_then_above()
    entry = _entry_frame(closes, [100.0] * len(closes))
    manage = _manage_frame("2026-09-02 14:00", [102.0] * 90, [50.0] * 90,
                           opens=[101.5] * 90)

    result = backtest(entry, ABOVE_VWAP, ExitPolicy(atr_stop_multiple=None), manage)

    assert result.count == 1
    trade = result.trades[0]
    assert trade.entry_price == pytest.approx(101.5), "filled at a manage-bar open"
    assert trade.entry_time > entry.index[-1], "fill is after the signal bar"


def test_no_entry_while_the_condition_has_not_persisted():
    """for_bars=3 must not fire on the first bar above VWAP."""
    alternating = [99.0, 101.0] * 6
    entry = _entry_frame(alternating, [100.0] * len(alternating))
    manage = _manage_frame("2026-09-02 14:00", [101.0] * 90, [50.0] * 90)
    assert backtest(entry, ABOVE_VWAP, ExitPolicy(atr_stop_multiple=None), manage).count == 0


def test_signal_with_no_bars_left_to_fill_is_recorded_not_silently_dropped():
    closes = _below_then_above()
    entry = _entry_frame(closes, [100.0] * len(closes))
    manage = _manage_frame("2026-09-02 12:00", [101.0] * 5, [50.0] * 5)  # all before
    result = backtest(entry, ABOVE_VWAP, ExitPolicy(atr_stop_multiple=None), manage)
    assert result.count == 0
    assert result.unfilled_signals == 1


# ── Lookahead ────────────────────────────────────────────────────────────────

def test_engine_is_lookahead_free():
    """
    Price is below VWAP until the very last entry bar. A lookahead bug would
    let the rule see that final bar early and enter sooner; correct behavior
    is at most one entry, signalled on the last bar.
    """
    closes = [95.0, 96, 97, 98, 99, 99, 99, 101, 102, 103]
    entry = _entry_frame(closes, [100.0] * len(closes))
    manage = _manage_frame("2026-09-02 14:00", [102.0] * 120, [50.0] * 120)

    result = backtest(entry, ABOVE_VWAP, ExitPolicy(atr_stop_multiple=None), manage)

    assert result.count <= 1
    if result.trades:
        # The 3-bar run completes only on the final entry bar, so the fill
        # must land after it.
        assert result.trades[0].entry_time > entry.index[-1]


def test_rule_never_sees_bars_after_the_signal():
    """Directly asserts the window handed to the rule ends at the current
    bar — the property every other guarantee rests on."""
    seen = []

    class SpyRule(Rule):
        def matches(self, bars):
            seen.append(bars.index[-1])
            return False

    entry = _entry_frame([101.0] * 12, [100.0] * 12)
    spy = SpyRule(name="spy", params=SMALL,
                  conditions=[Condition("close", ">", value=0)])
    backtest(entry, spy, ExitPolicy(atr_stop_multiple=None), None)

    # The rule is consulted once per bar from min_bars() onward, and each
    # window ends at exactly that bar — never later.
    expected = list(entry.index[MIN_BARS - 1:])
    assert seen == expected


# ── Exits ────────────────────────────────────────────────────────────────────

def test_ema_cross_under_exits_at_the_next_open():
    closes = _below_then_above()
    entry = _entry_frame(closes, [100.0] * len(closes))
    # 1-minute bars: hold above ema9, then close below it, then the fill bar.
    n = 60
    m_closes = [102.0] * (n - 3) + [95.0, 94.0, 94.0]
    m_opens = [102.0] * (n - 3) + [95.0, 93.5, 94.0]
    manage = _manage_frame("2026-09-02 14:00", m_closes, [100.0] * n, opens=m_opens)

    result = backtest(entry, ABOVE_VWAP, ExitPolicy(atr_stop_multiple=None), manage)

    trade = result.trades[0]
    assert trade.exit_reason == EXIT_EMA
    assert trade.exit_price == pytest.approx(93.5), "filled at the next open"


def test_atr_stop_fires_before_the_ema_and_fills_at_the_stop():
    closes = _below_then_above()
    entry = _entry_frame(closes, [100.0] * len(closes), atr=2.0)
    # Entry fills at 102; stop = 102 - 2.0 * 1.5 = 99.0. A late bar dips to 98.
    n = 60
    m_closes = [102.0] * n
    m_lows = [101.8] * (n - 2) + [98.0, 100.0]
    manage = _manage_frame("2026-09-02 14:00", m_closes, [50.0] * n,
                           lows=m_lows, opens=[102.0] * n)

    result = backtest(entry, ABOVE_VWAP, ExitPolicy(atr_stop_multiple=1.5), manage)

    trade = result.trades[0]
    assert trade.exit_reason == EXIT_STOP
    assert trade.stop_price == pytest.approx(99.0)
    assert trade.exit_price == pytest.approx(99.0)
    assert trade.pnl_pct < 0


def test_disabling_the_stop_leaves_the_ema_as_the_only_exit():
    closes = _below_then_above()
    entry = _entry_frame(closes, [100.0] * len(closes), atr=2.0)
    n = 60
    m_closes = [102.0] * (n - 2) + [90.0, 90.0]
    m_lows = [101.0] * (n - 2) + [80.0, 89.0]
    m_opens = [102.0] * (n - 2) + [91.0, 90.0]
    manage = _manage_frame("2026-09-02 14:00", m_closes, [100.0] * n,
                           lows=m_lows, opens=m_opens)
    result = backtest(entry, ABOVE_VWAP, ExitPolicy(atr_stop_multiple=None), manage)
    assert result.trades[0].exit_reason == EXIT_EMA


def test_open_position_at_end_of_data_is_flagged():
    closes = _below_then_above()
    entry = _entry_frame(closes, [100.0] * len(closes))
    manage = _manage_frame("2026-09-02 14:00", [102.0] * 60, [50.0] * 60)
    result = backtest(entry, ABOVE_VWAP, ExitPolicy(atr_stop_multiple=None), manage)
    assert result.trades[0].exit_reason == EXIT_END_OF_DATA


def test_positions_never_overlap():
    """A new entry signal while already in a trade must be ignored."""
    entry = _entry_frame([99.0] * 6 + [101.0] * 20, [100.0] * 26)
    manage = _manage_frame("2026-09-02 14:00", [102.0] * 300, [50.0] * 300)
    result = backtest(entry, ABOVE_VWAP, ExitPolicy(atr_stop_multiple=None), manage)

    for earlier, later in zip(result.trades, result.trades[1:]):
        assert later.entry_time > earlier.exit_time


# ── Dual timeframe ───────────────────────────────────────────────────────────

def test_managing_on_one_minute_bars_exits_sooner_than_on_five():
    """
    The reason for the finer timeframe: the same adverse move is caught at
    the next 1-minute bar instead of waiting up to five minutes.
    """
    closes = _below_then_above()
    entry = _entry_frame(closes, [100.0] * len(closes), atr=2.0)

    # 1-minute bars break down immediately after the entry fill.
    n = 60
    m_closes = [102.0] * 46 + [95.0] * (n - 46)
    minute = _manage_frame("2026-09-02 14:00", m_closes, [100.0] * n,
                           opens=[102.0] * 46 + [94.0] * (n - 46))

    fast = backtest(entry, ABOVE_VWAP, ExitPolicy(atr_stop_multiple=None), minute)

    assert fast.count >= 1
    trade = fast.trades[0]
    breakdown_bar = minute.index[46]        # first close below the exit EMA

    # What the finer timeframe buys is reaction time: the exit lands on the
    # very next bar after the breakdown, not up to five minutes later.
    reaction = (trade.exit_time - breakdown_bar).total_seconds() / 60
    assert reaction == pytest.approx(1.0), (
        "a 1-minute management frame must exit on the next minute bar"
    )
    assert trade.exit_reason == EXIT_EMA


def test_management_frame_must_carry_the_exit_ema():
    closes = _below_then_above()
    entry = _entry_frame(closes, [100.0] * len(closes))
    manage = _manage_frame("2026-09-02 14:00", [102.0] * 60, [50.0] * 60)
    manage = manage.drop(columns=["ema_9"])
    with pytest.raises(ValueError, match="missing column"):
        backtest(entry, ABOVE_VWAP, ExitPolicy(ema_period=9), manage)


def test_entry_frame_must_carry_atr():
    closes = _below_then_above()
    entry = _entry_frame(closes, [100.0] * len(closes)).drop(columns=["atr"])
    with pytest.raises(ValueError, match="missing column"):
        backtest(entry, ABOVE_VWAP, ExitPolicy(), None)


# ── Statistics ───────────────────────────────────────────────────────────────

def _result_with(pnls):
    from core.backtest import BacktestResult, Trade
    base = datetime(2026, 9, 2, 14, 0)
    trades = [
        Trade("T", base, 100.0, base + timedelta(minutes=10), 100 * (1 + p / 100),
              EXIT_EMA)
        for p in pnls
    ]
    return BacktestResult(symbol="T", trades=trades)


def test_win_rate_and_averages():
    result = _result_with([2.0, -1.0, 3.0, -1.0])
    assert result.count == 4
    assert result.win_rate == pytest.approx(50.0)
    assert result.avg_pnl_pct == pytest.approx(0.75)


def test_total_return_compounds():
    result = _result_with([10.0, 10.0])
    assert result.total_return_pct == pytest.approx(21.0)


def test_max_drawdown_tracks_peak_to_trough():
    result = _result_with([20.0, -50.0, 10.0])
    assert result.max_drawdown_pct == pytest.approx(-50.0, abs=0.01)


def test_empty_result_has_safe_statistics():
    from core.backtest import BacktestResult
    empty = BacktestResult()
    assert empty.count == 0
    assert empty.win_rate == 0.0
    assert empty.total_return_pct == 0.0
    assert empty.max_drawdown_pct == 0.0
    assert empty.trades_frame().empty


def test_exit_reasons_are_counted():
    closes = _below_then_above()
    entry = _entry_frame(closes, [100.0] * len(closes))
    manage = _manage_frame("2026-09-02 14:00", [102.0] * 60, [50.0] * 60)
    result = backtest(entry, ABOVE_VWAP, ExitPolicy(atr_stop_multiple=None), manage)
    assert result.exit_reasons() == {EXIT_END_OF_DATA: 1}


def test_empty_input_is_safe():
    assert backtest(pd.DataFrame(), ABOVE_VWAP).count == 0
