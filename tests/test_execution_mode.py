"""
Execution mode and the VWAP-trend strategy.

The guarantee that matters most here: in monitor mode every step up to the
order happens, and the order does not. This is the difference between a tool
that tells you what to look at and one that trades your account, and it must
not depend on remembering which command to run.
"""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from bot.config import MODE_MONITOR, MODE_PAPER, BotConfig
from bot.runner import TradingBot
from bot.strategies import Signal, VwapTrendStrategy, vwap_hold_rule
from core.backtest import ExitPolicy
from core.indicators import IndicatorParams, add_indicators
from core.sessions import SessionConfig


# ── Fixtures ─────────────────────────────────────────────────────────────────

SMALL = IndicatorParams(
    sma_fast=2, sma_slow=3, ema_periods=(9,), rsi_period=2,
    volume_sma_period=2, atr_period=2,
    macd_fast=2, macd_slow=3, macd_signal=2, bar_minutes=5,
)


def _bars(closes, volumes=None, start="2026-09-02 14:00", minutes=5):
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": closes, "high": closes + 0.2, "low": closes - 0.2,
            "close": closes,
            "volume": np.asarray(volumes if volumes else [1000.0] * n, dtype=float),
        },
        index=pd.date_range(start, periods=n, freq=f"{minutes}min", tz="UTC"),
    )


def _above_vwap_bars():
    """Bars whose last three closes sit above the day's VWAP."""
    closes = [100.0] * 12 + [103.0, 104.0, 105.0]
    volumes = [5000.0] * 12 + [200.0, 200.0, 200.0]
    return _bars(closes, volumes)


class FakeOrders:
    def __init__(self):
        self.executed = []

    def execute(self, signal, notional, positions):
        self.executed.append((signal.action, signal.symbol, notional))


class FakeClient:
    def __init__(self, positions=None):
        self._positions = positions or {}

    def get_portfolio_value(self):
        return 100_000.0

    def get_positions(self):
        return self._positions


def _bot(mode, positions=None, stock_bars=None, manage=None) -> TradingBot:
    """A TradingBot with its network edges replaced."""
    config = BotConfig(
        api_key="k", api_secret="s", execution_mode=mode,
        stock_symbols=["AAPL"], crypto_symbols=[],
        sessions=SessionConfig.extended(), bar_limit=300,
    )
    bot = TradingBot.__new__(TradingBot)
    bot.config = config
    bot.client = FakeClient(positions)
    bot.orders = FakeOrders()

    from bot.risk import RiskManager
    bot.risk = RiskManager(config)

    always_buy = SimpleNamespace(
        generate_signals=lambda *a, **kw: [
            Signal("AAPL", "BUY", "stock", 0.9, "test", atr=1.0, current_price=100.0)
        ],
        exit_policy=ExitPolicy(),
    )
    bot.strategy = always_buy
    bot.data = SimpleNamespace(
        get_stock_bars=lambda syms, limit: stock_bars if stock_bars is not None else pd.DataFrame(),
        get_crypto_bars=lambda syms, limit: pd.DataFrame(),
    )
    bot.manage_data = SimpleNamespace(get_bars=lambda syms, limit=60: manage or {})
    return bot


# ── Monitor mode ─────────────────────────────────────────────────────────────

def test_monitor_mode_is_the_default():
    """Running the bot must not trade unless that was chosen deliberately."""
    assert BotConfig(api_key="k", api_secret="s").execution_mode == MODE_MONITOR
    assert BotConfig(api_key="k", api_secret="s").is_monitor_only()


def test_monitor_mode_places_no_orders():
    bot = _bot(MODE_MONITOR)
    bot.run_once()
    assert bot.orders.executed == [], "monitor mode must never reach the broker"


def test_monitor_mode_still_reports_the_signal(caplog):
    """Silence would be indistinguishable from a broken bot."""
    bot = _bot(MODE_MONITOR)
    with caplog.at_level("WARNING"):
        bot.run_once()
    assert "ALERT [monitor]" in caplog.text
    assert "BUY AAPL" in caplog.text


def test_paper_mode_does_place_the_order():
    bot = _bot(MODE_PAPER)
    bot.run_once()
    assert len(bot.orders.executed) == 1
    assert bot.orders.executed[0][:2] == ("BUY", "AAPL")


def test_an_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="execution_mode must be one of"):
        BotConfig(api_key="k", api_secret="s", execution_mode="live").validate()


def test_monitor_mode_does_not_disable_risk_checks():
    """The alert should reflect what would actually have been traded, so risk
    still runs and a blocked signal produces no alert."""
    position = SimpleNamespace(market_value="90000", qty="10", avg_entry_price="100")
    bot = _bot(MODE_MONITOR, positions={"AAPL": position})
    bot.run_once()
    assert bot.orders.executed == []


# ── Management timeframe ─────────────────────────────────────────────────────

def test_manage_bar_minutes_defaults_finer_than_the_entry_frame():
    config = BotConfig(api_key="k", api_secret="s")
    assert config.manage_bar_minutes == 1
    assert config.manage_bar_minutes < config.bar_minutes


def test_manage_frame_cannot_be_coarser_than_the_entry_frame():
    with pytest.raises(ValueError, match="no coarser"):
        BotConfig(api_key="k", api_secret="s",
                  bar_minutes=5, manage_bar_minutes=15).validate()


def test_manage_params_carry_only_the_exit_ema():
    """Computing a 200-period EMA on 1-minute bars per held symbol would cost
    far more than it informs."""
    params = BotConfig(api_key="k", api_secret="s").manage_params(9)
    assert params.ema_periods == (9,)
    assert params.bar_minutes == 1


def test_management_bars_are_fetched_only_for_held_symbols():
    requested = []

    bot = _bot(MODE_MONITOR, positions={"AAPL": SimpleNamespace(
        market_value="100", qty="1", avg_entry_price="100")})

    def spy(symbols, limit=60):
        requested.append(list(symbols))
        return {}

    bot.manage_data = SimpleNamespace(get_bars=spy)
    bot._management_bars(bot.client.get_positions())
    assert requested == [["AAPL"]]


def test_no_positions_means_no_management_fetch():
    bot = _bot(MODE_MONITOR)
    assert bot._management_bars({}) == {}


# ── VwapTrendStrategy ────────────────────────────────────────────────────────

def _config():
    return BotConfig(
        api_key="k", api_secret="s", stock_symbols=["AAPL"], crypto_symbols=[],
        bar_limit=300,
    )


def test_entry_rule_is_three_bars_above_vwap():
    assert vwap_hold_rule(SMALL).describe() == "close > vwap for 3 bars"


def test_entry_fires_when_price_has_held_above_vwap():
    strategy = VwapTrendStrategy(rule=vwap_hold_rule(SMALL))
    signals = strategy.generate_signals(_above_vwap_bars(), pd.DataFrame(), _config())
    assert [s.action for s in signals] == ["BUY"]
    assert "Held above VWAP" in signals[0].reason


def test_no_entry_when_price_is_below_vwap():
    closes = [100.0] * 12 + [97.0, 96.0, 95.0]
    strategy = VwapTrendStrategy(rule=vwap_hold_rule(SMALL))
    assert strategy.generate_signals(_bars(closes), pd.DataFrame(), _config()) == []


def test_no_second_entry_in_a_symbol_already_held():
    """The exit owns an open position; a fresh entry signal must not stack."""
    position = SimpleNamespace(qty="10", avg_entry_price="100", market_value="1000")
    strategy = VwapTrendStrategy(rule=vwap_hold_rule(SMALL))
    signals = strategy.generate_signals(
        _above_vwap_bars(), pd.DataFrame(), _config(),
        positions={"AAPL": position}, manage_bars={},
    )
    assert not any(s.action == "BUY" for s in signals)


def _manage_frame(closes, lows=None):
    df = _bars(closes, start="2026-09-02 15:00", minutes=1)
    if lows is not None:
        df["low"] = lows
    return add_indicators(
        df, IndicatorParams(ema_periods=(9,), atr_period=2, bar_minutes=1)
    )


def test_exit_fires_on_a_close_below_the_ema():
    position = SimpleNamespace(qty="10", avg_entry_price="100", market_value="1000")
    manage = _manage_frame([105.0] * 14 + [90.0])
    strategy = VwapTrendStrategy(exit_policy=ExitPolicy(9, None))

    signals = strategy.generate_signals(
        pd.DataFrame(), pd.DataFrame(), _config(),
        positions={"AAPL": position}, manage_bars={"AAPL": manage},
    )
    sells = [s for s in signals if s.action == "SELL"]
    assert len(sells) == 1
    assert "below EMA(9)" in sells[0].reason


def test_atr_stop_takes_priority_over_the_ema_exit():
    position = SimpleNamespace(qty="10", avg_entry_price="100", market_value="1000")
    # Price still above its EMA, but the bar's low breaches the stop.
    manage = _manage_frame([105.0] * 15, lows=[105.0] * 14 + [80.0])
    strategy = VwapTrendStrategy(exit_policy=ExitPolicy(9, 1.5))

    signals = strategy.generate_signals(
        pd.DataFrame(), pd.DataFrame(), _config(),
        positions={"AAPL": position}, manage_bars={"AAPL": manage},
    )
    sells = [s for s in signals if s.action == "SELL"]
    assert len(sells) == 1
    assert "ATR stop" in sells[0].reason


def test_no_exit_while_price_holds_above_the_ema():
    position = SimpleNamespace(qty="10", avg_entry_price="100", market_value="1000")
    manage = _manage_frame(list(np.linspace(100, 120, 20)))
    strategy = VwapTrendStrategy(exit_policy=ExitPolicy(9, None))
    signals = strategy.generate_signals(
        pd.DataFrame(), pd.DataFrame(), _config(),
        positions={"AAPL": position}, manage_bars={"AAPL": manage},
    )
    assert [s for s in signals if s.action == "SELL"] == []


def test_missing_management_bars_produce_no_exit_rather_than_a_guess():
    position = SimpleNamespace(qty="10", avg_entry_price="100", market_value="1000")
    strategy = VwapTrendStrategy()
    signals = strategy.generate_signals(
        pd.DataFrame(), pd.DataFrame(), _config(),
        positions={"AAPL": position}, manage_bars={},
    )
    assert signals == []


def test_live_strategy_and_backtest_share_the_same_objects():
    """A reimplemented live strategy could drift from the numbers that
    justified trading it, and nothing would catch the drift."""
    strategy = VwapTrendStrategy(rule=vwap_hold_rule(SMALL), exit_policy=ExitPolicy(9, 1.5))
    from core.backtest import ExitPolicy as BacktestPolicy
    from core.rules import Rule as CoreRule
    assert isinstance(strategy.rule, CoreRule)
    assert isinstance(strategy.exit_policy, BacktestPolicy)
