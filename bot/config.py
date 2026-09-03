"""Bot configuration."""

import os
from dataclasses import dataclass, field
from typing import List

from core.indicators import IndicatorParams
from core.client import Credentials
from core.sessions import DEFAULT_BAR_MINUTES, SessionCalendar, SessionConfig
from core import universe


@dataclass
class BotConfig:
    """All tunable parameters in one place. Edit here or pull from env vars."""

    # Alpaca credentials — use paper keys until you're ready for live
    api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("ALPACA_API_SECRET", ""))
    paper: bool = True  # ← switch to False only when you're fully ready

    # Watchlists
    stock_symbols: List[str] = field(
        default_factory=lambda: list(universe.DEFAULT_STOCKS)
    )
    crypto_symbols: List[str] = field(
        default_factory=lambda: list(universe.DEFAULT_CRYPTO)
    )

    # Risk controls
    max_position_pct: float = 0.05    # hard cap: max % of portfolio per position
    max_total_exposure: float = 0.80  # max 80% of portfolio allocated at once
    risk_per_trade_pct: float = 0.01  # ATR sizing: risk this % of portfolio per trade
    atr_risk_multiplier: float = 1.5  # stop distance = ATR × this multiplier

    # SMA crossover parameters
    sma_fast: int = 10   # fast moving average period (bars)
    sma_slow: int = 30   # slow moving average period (bars)

    # RSI parameters
    rsi_period: int = 14
    rsi_overbought: float = 70.0   # block BUY if RSI is above this
    rsi_oversold: float = 30.0     # block SELL if RSI is below this

    # Volume confirmation
    volume_sma_period: int = 20    # bars for rolling average volume

    # ATR (volatility-based sizing)
    atr_period: int = 14

    # Market sessions
    #
    # Defaults to regular hours only (09:30–16:00 ET). Enable extended hours
    # with SessionConfig.after_hours() or .extended() — read
    # docs/specs/core/market-sessions.md first: extended hours changes how
    # orders must be placed (limit-only, whole shares) and makes the volume
    # baseline session-relative.
    sessions: SessionConfig = field(default_factory=SessionConfig)
    calendar: SessionCalendar = field(default_factory=SessionCalendar)

    # How far through the reference price to place an extended-hours limit,
    # so it fills against the thin book without unbounded slippage.
    extended_hours_limit_offset_pct: float = 0.002

    # Bar size, in minutes. Must divide evenly into 1440.
    #
    # 60 (hourly) gives 7 bars per regular-hours day; 5 gives 78. A shorter
    # bar means more signals and a much shorter effective lookback — SMA(30)
    # spans 30 hours of hourly bars but only 150 minutes of 5-minute bars, so
    # revisit the indicator periods when changing this.
    bar_minutes: int = DEFAULT_BAR_MINUTES

    # Polling
    poll_interval_seconds: int = 60   # how often the bot cycles
    bar_limit: int = 60               # must cover sma_slow + atr/rsi periods

    def indicator_params(self) -> IndicatorParams:
        """
        The periods the shared indicator module needs.

        Going through this method is what guarantees the bot and the scanner
        compute RSI over the same window — never build an IndicatorParams by
        hand in app code.
        """
        return IndicatorParams(
            sma_fast=self.sma_fast,
            sma_slow=self.sma_slow,
            rsi_period=self.rsi_period,
            volume_sma_period=self.volume_sma_period,
            atr_period=self.atr_period,
        )

    def credentials(self) -> Credentials:
        return Credentials(
            api_key=self.api_key, api_secret=self.api_secret, paper=self.paper
        )
