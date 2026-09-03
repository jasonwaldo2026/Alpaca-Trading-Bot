"""
Trading strategies.

Indicator math is NOT implemented here — it comes from core.indicators so
the bot, dashboard, and scanner all agree on what "RSI 30" means. A strategy's
job is to turn indicator values into Signals, nothing more.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional

import pandas as pd

from bot.config import BotConfig
from core.backtest import ExitPolicy
from core.rules import Condition, Rule
from core import universe
from core.indicators import (
    COL_ATR,
    COL_RSI,
    COL_SMA_FAST,
    COL_SMA_SLOW,
    COL_VOL_SMA,
    COL_VWAP,
    add_indicators,
    ema_column,
    crossed_down,
    crossed_up,
)
from core.sessions import session_day_series, session_series

log = logging.getLogger(__name__)


@dataclass
class Signal:
    symbol: str
    action: str        # "BUY" | "SELL" | "HOLD"
    asset_class: str   # "stock" | "crypto"
    confidence: float  # 0.0 – 1.0
    reason: str = ""
    atr: float = 0.0           # current ATR value (used for position sizing)
    current_price: float = 0.0 # last close price (used for sizing math)


def _frame_for(bars: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Slice one symbol out of a possibly-MultiIndex bar frame."""
    if isinstance(bars.index, pd.MultiIndex):
        return bars.xs(symbol, level="symbol").copy()
    return bars.copy()


def _anchor_for(df: pd.DataFrame, symbol: str, config: BotConfig):
    """
    VWAP anchor for a bar frame, or None when it cannot be derived.

    Always computed when the frame is timestamp-indexed: unlike the volume
    baseline, VWAP needs its daily reset at every resolution, not just when
    extended hours are enabled.
    """
    if not isinstance(df.index, pd.DatetimeIndex):
        log.debug("No DatetimeIndex for %s — VWAP will not reset daily.", symbol)
        return None
    return session_day_series(df.index, symbol, config.calendar)


def _sessions_for(df: pd.DataFrame, symbol: str, config: BotConfig):
    """
    Session labels for a bar frame, or None when they are not needed.

    Only worth computing when extended hours are enabled: with regular hours
    only, every bar is in the same session and grouping changes nothing.
    Returns None if the frame is not timestamp-indexed, so a caller passing
    a reset-index frame degrades to the flat volume baseline rather than
    raising.
    """
    if not config.sessions.requires_extended_hours_orders():
        return None
    if not isinstance(df.index, pd.DatetimeIndex):
        log.debug("No DatetimeIndex for %s — using flat volume baseline.", symbol)
        return None
    return session_series(df.index, symbol, config.calendar)


class BaseStrategy(ABC):
    """
    Subclass this to add a new strategy.

    generate_signals() must return List[Signal]. Use core.indicators for any
    math you need; if the indicator you want does not exist there, add it
    there rather than inline here.
    """

    @abstractmethod
    def generate_signals(
        self,
        stock_bars: pd.DataFrame,
        crypto_bars: pd.DataFrame,
        config: BotConfig,
        positions: Optional[Dict] = None,
        manage_bars: Optional[Dict[str, pd.DataFrame]] = None,
    ) -> List[Signal]:
        """
        Turn bars into signals.

        `positions` and `manage_bars` are optional so simple strategies can
        ignore them. A strategy that manages open positions on a finer
        timeframe needs both: the entry price to size a stop against, and the
        finer bars to notice an exit sooner than the entry chart would.
        """
        ...

    def _for_both_classes(self, stock_bars, crypto_bars, config) -> List[Signal]:
        """Shared fan-out: run _signals_for_bars over stocks then crypto."""
        signals: List[Signal] = []
        if stock_bars is not None and not stock_bars.empty:
            signals += self._signals_for_bars(
                stock_bars, config.stock_symbols, "stock", config
            )
        if crypto_bars is not None and not crypto_bars.empty:
            signals += self._signals_for_bars(
                crypto_bars, config.crypto_symbols, "crypto", config
            )
        return signals


class EnhancedSMAStrategy(BaseStrategy):
    """
    Three-confirmation strategy — all three must agree before a trade fires.

    BUY:  SMA golden cross  +  RSI < rsi_overbought  +  volume > 20-bar avg
    SELL: SMA death cross   +  RSI > rsi_oversold    +  volume > 20-bar avg

    Signals carry atr and current_price so RiskManager can size positions
    proportionally to volatility instead of using a flat percentage.
    """

    def _signals_for_bars(
        self,
        bars: pd.DataFrame,
        symbols: List[str],
        asset_class: str,
        config: BotConfig,
    ) -> List[Signal]:
        params = config.indicator_params()
        min_bars = params.min_bars()
        signals: List[Signal] = []

        for sym in symbols:
            try:
                df = _frame_for(bars, sym)

                if len(df) < min_bars:
                    log.debug("Not enough bars for %s (%d < %d)", sym, len(df), min_bars)
                    continue

                df = add_indicators(
                    df, params,
                    _sessions_for(df, sym, config),
                    _anchor_for(df, sym, config),
                )
                df.dropna(inplace=True)

                if len(df) < 2:
                    continue

                prev, curr = df.iloc[-2], df.iloc[-1]
                atr_val = float(curr[COL_ATR])
                price = float(curr["close"])
                rsi_val = float(curr[COL_RSI])

                golden = crossed_up(prev, curr, COL_SMA_FAST, COL_SMA_SLOW)
                death = crossed_down(prev, curr, COL_SMA_FAST, COL_SMA_SLOW)
                high_vol = curr["volume"] > curr[COL_VOL_SMA]

                if golden:
                    if not high_vol:
                        reason = "Golden cross but low volume — skipping"
                        signals.append(Signal(sym, "HOLD", asset_class, 0.35, reason, atr_val, price))
                    elif rsi_val >= config.rsi_overbought:
                        reason = f"Golden cross but RSI overbought ({rsi_val:.1f}) — skipping"
                        signals.append(Signal(sym, "HOLD", asset_class, 0.35, reason, atr_val, price))
                    else:
                        reason = f"SMA golden cross | RSI {rsi_val:.1f} | volume confirmed"
                        signals.append(Signal(sym, "BUY", asset_class, 0.85, reason, atr_val, price))

                elif death:
                    if not high_vol:
                        reason = "Death cross but low volume — skipping"
                        signals.append(Signal(sym, "HOLD", asset_class, 0.35, reason, atr_val, price))
                    elif rsi_val <= config.rsi_oversold:
                        reason = f"Death cross but RSI oversold ({rsi_val:.1f}) — skipping"
                        signals.append(Signal(sym, "HOLD", asset_class, 0.35, reason, atr_val, price))
                    else:
                        reason = f"SMA death cross | RSI {rsi_val:.1f} | volume confirmed"
                        signals.append(Signal(sym, "SELL", asset_class, 0.85, reason, atr_val, price))

                else:
                    signals.append(Signal(sym, "HOLD", asset_class, 0.50, "No crossover", atr_val, price))

            except Exception as exc:
                log.warning("Signal error for %s: %s", sym, exc)

        return signals

    def generate_signals(self, stock_bars, crypto_bars, config,
                         positions=None, manage_bars=None) -> List[Signal]:
        return self._for_both_classes(stock_bars, crypto_bars, config)


class SMAcrossoverStrategy(BaseStrategy):
    """Original SMA-only strategy. Pass to TradingBot(strategy=SMAcrossoverStrategy())
    to compare against EnhancedSMAStrategy."""

    def _signals_for_bars(self, bars, symbols, asset_class, config) -> List[Signal]:
        params = config.indicator_params()
        signals: List[Signal] = []
        for sym in symbols:
            try:
                df = _frame_for(bars, sym)
                if len(df) < config.sma_slow + 2:
                    continue
                df = add_indicators(
                    df, params,
                    _sessions_for(df, sym, config),
                    _anchor_for(df, sym, config),
                )
                df.dropna(subset=[COL_SMA_FAST, COL_SMA_SLOW], inplace=True)
                if len(df) < 2:
                    continue
                prev, curr = df.iloc[-2], df.iloc[-1]
                if crossed_up(prev, curr, COL_SMA_FAST, COL_SMA_SLOW):
                    signals.append(Signal(sym, "BUY", asset_class, 0.75, "SMA golden cross"))
                elif crossed_down(prev, curr, COL_SMA_FAST, COL_SMA_SLOW):
                    signals.append(Signal(sym, "SELL", asset_class, 0.75, "SMA death cross"))
                else:
                    signals.append(Signal(sym, "HOLD", asset_class, 0.50, "No crossover"))
            except Exception as exc:
                log.warning("Signal error for %s: %s", sym, exc)
        return signals

    def generate_signals(self, stock_bars, crypto_bars, config,
                         positions=None, manage_bars=None) -> List[Signal]:
        return self._for_both_classes(stock_bars, crypto_bars, config)


# ── VWAP trend ────────────────────────────────────────────────────────────────

#: The shipped entry: price has closed above VWAP for three consecutive bars
#: on the entry timeframe. Held above VWAP means buyers are paying more than
#: the session's volume-weighted average; holding there distinguishes a trend
#: from a touch.
DEFAULT_VWAP_ENTRY_BARS = 3


def vwap_hold_rule(params, for_bars: int = DEFAULT_VWAP_ENTRY_BARS) -> Rule:
    """The entry rule, as a Rule so Studio and the scanner share it."""
    return Rule(
        name="vwap hold",
        params=params,
        conditions=[
            Condition("close", ">", field2=COL_VWAP, for_bars=for_bars)
        ],
    )


class VwapTrendStrategy(BaseStrategy):
    """
    Enter when price has held above VWAP; exit on a close below an EMA, with
    an ATR stop as a floor.

    The entry is a `core.rules.Rule` and the exit a `core.backtest.ExitPolicy`
    — the *same objects the backtest measures*. That is the point: a live
    strategy that reimplemented the logic could drift from the numbers that
    justified trading it, and nothing would catch the drift.

    Exits are evaluated on `manage_bars` when supplied (finer resolution, so
    an adverse move is noticed within a minute rather than up to five) and on
    the entry frame otherwise. The exit EMA is read on whichever frame is
    used, so its span changes with that frame — see
    docs/specs/studio/vwap-trend-strategy.md.
    """

    def __init__(
        self,
        rule: Optional[Rule] = None,
        exit_policy: Optional[ExitPolicy] = None,
    ):
        self.rule = rule
        self.exit_policy = exit_policy or ExitPolicy()

    def _entry_rule(self, config: BotConfig) -> Rule:
        return self.rule or vwap_hold_rule(config.indicator_params())

    # ── Entries ──────────────────────────────────────────────────────────

    def _entry_signals(self, bars, symbols, asset_class, config) -> List[Signal]:
        rule = self._entry_rule(config)
        params = rule.params
        signals: List[Signal] = []

        for sym in symbols:
            try:
                df = _frame_for(bars, sym)
                if len(df) < params.min_bars():
                    continue

                df = add_indicators(
                    df, params,
                    _sessions_for(df, sym, config),
                    _anchor_for(df, sym, config),
                )
                if df[COL_VWAP].isna().all():
                    continue

                if rule.matches(df):
                    curr = df.iloc[-1]
                    price = float(curr["close"])
                    vwap_gap = (price - float(curr[COL_VWAP])) / float(curr[COL_VWAP])
                    signals.append(Signal(
                        symbol=sym, action="BUY", asset_class=asset_class,
                        confidence=0.80,
                        reason=(
                            f"Held above VWAP for {rule.conditions[0].for_bars} bars "
                            f"(+{vwap_gap * 100:.2f}% vs VWAP)"
                        ),
                        atr=float(curr[COL_ATR]) if not pd.isna(curr[COL_ATR]) else 0.0,
                        current_price=price,
                    ))
            except Exception as exc:
                log.warning("VWAP entry check failed for %s: %s", sym, exc)

        return signals

    # ── Exits ────────────────────────────────────────────────────────────

    def _exit_signals(self, config, positions, manage_bars) -> List[Signal]:
        """
        One SELL per held symbol whose exit condition has triggered.

        RiskManager filters SELLs to symbols actually held, so emitting for a
        symbol that has since been closed is harmless.
        """
        if not positions:
            return []

        exit_ema = ema_column(self.exit_policy.ema_period)
        signals: List[Signal] = []

        for sym, position in positions.items():
            df = (manage_bars or {}).get(sym)
            if df is None or df.empty or exit_ema not in df.columns:
                log.debug("No management bars for %s — cannot evaluate its exit.", sym)
                continue

            curr = df.iloc[-1]
            price = float(curr["close"])
            ema_value = curr[exit_ema]
            atr_value = float(curr[COL_ATR]) if COL_ATR in df.columns and not pd.isna(curr[COL_ATR]) else 0.0

            # The ATR stop is measured from the actual fill, not the signal
            # price, so a bad fill tightens the stop rather than being ignored.
            entry_price = float(getattr(position, "avg_entry_price", 0) or 0)
            stop = self.exit_policy.stop_price(entry_price, atr_value) if entry_price else None

            if stop is not None and float(curr["low"]) <= stop:
                signals.append(Signal(
                    symbol=sym, action="SELL", asset_class=universe.asset_class(sym),
                    confidence=0.95,
                    reason=f"ATR stop hit — low {curr['low']:.4f} <= stop {stop:.4f}",
                    atr=atr_value, current_price=price,
                ))
                continue

            if not pd.isna(ema_value) and price < float(ema_value):
                signals.append(Signal(
                    symbol=sym, action="SELL", asset_class=universe.asset_class(sym),
                    confidence=0.90,
                    reason=(
                        f"Closed below EMA({self.exit_policy.ema_period}) — "
                        f"{price:.4f} < {float(ema_value):.4f}"
                    ),
                    atr=atr_value, current_price=price,
                ))

        return signals

    def generate_signals(self, stock_bars, crypto_bars, config,
                         positions=None, manage_bars=None) -> List[Signal]:
        held = set(positions or {})

        signals = self._exit_signals(config, positions, manage_bars)

        # Do not look for an entry in something already held; the exit above
        # owns that position until it closes.
        if stock_bars is not None and not stock_bars.empty:
            symbols = [s for s in config.stock_symbols if s not in held]
            signals += self._entry_signals(stock_bars, symbols, "stock", config)
        if crypto_bars is not None and not crypto_bars.empty:
            symbols = [s for s in config.crypto_symbols if s not in held]
            signals += self._entry_signals(crypto_bars, symbols, "crypto", config)

        return signals
