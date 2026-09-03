"""
Trading strategies.

Indicator math is NOT implemented here — it comes from core.indicators so
the bot, dashboard, and scanner all agree on what "RSI 30" means. A strategy's
job is to turn indicator values into Signals, nothing more.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List

import pandas as pd

from bot.config import BotConfig
from core.indicators import (
    COL_ATR,
    COL_RSI,
    COL_SMA_FAST,
    COL_SMA_SLOW,
    COL_VOL_SMA,
    add_indicators,
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
    ) -> List[Signal]:
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

    def generate_signals(self, stock_bars, crypto_bars, config) -> List[Signal]:
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

    def generate_signals(self, stock_bars, crypto_bars, config) -> List[Signal]:
        return self._for_both_classes(stock_bars, crypto_bars, config)
