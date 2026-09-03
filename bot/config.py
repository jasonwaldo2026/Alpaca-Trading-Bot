"""Bot configuration."""

import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

from core.indicators import IndicatorParams
from core.client import Credentials
from core.sessions import SessionCalendar, SessionConfig
from core import universe


#: Execution modes.
#:
#: MONITOR is the default and the safe one: the bot computes signals, logs
#: them, and alerts — but never sends an order. It is the mode for learning
#: which alerts are worth acting on before any money moves.
#:
#: PAPER places real Alpaca paper orders. Switching to it is a deliberate
#: decision, which is why it is not the default.
MODE_MONITOR = "monitor"
MODE_PAPER = "paper"
EXECUTION_MODES = (MODE_MONITOR, MODE_PAPER)


@dataclass
class BotConfig:
    """All tunable parameters in one place. Edit here or pull from env vars."""

    # Alpaca credentials — use paper keys until you're ready for live
    api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("ALPACA_API_SECRET", ""))
    paper: bool = True  # ← switch to False only when you're fully ready

    # How signals are acted on. "monitor" computes and reports signals but
    # never places an order; "paper" sends Alpaca paper orders. Monitor is
    # the default so running the bot cannot trade by accident.
    execution_mode: str = MODE_MONITOR

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

    # EMA periods (bars) — one column each: ema_9, ema_12, ema_200.
    # 9 and 12 track intraday momentum; 200 is the long-trend reference.
    # At 5-minute bars, EMA(200) spans 1000 minutes of *trading* time —
    # roughly a day and a half of the 04:00-20:00 extended session.
    ema_periods: Tuple[int, ...] = (9, 12, 200)

    # MACD periods (bars) — conventional 12/26/9
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

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
    sessions: SessionConfig = field(default_factory=SessionConfig.extended)
    calendar: SessionCalendar = field(default_factory=SessionCalendar)

    # Equity data feed. None uses the account default, which on free and
    # basic plans is IEX — one venue with a small share of consolidated
    # volume. Pre-market IEX activity is thin enough that many 5-minute
    # windows contain no trades and therefore produce no bar at all, which
    # is the usual reason pre-market data "does not update often".
    # DataFeed.SIP is the consolidated tape and requires a paid data plan.
    data_feed: Optional[object] = None

    # How far through the reference price to place an extended-hours limit,
    # so it fills against the thin book without unbounded slippage.
    extended_hours_limit_offset_pct: float = 0.002

    # Bar size, in minutes. Must divide evenly into 1440.
    # 60 (hourly) gives 7 bars per regular-hours day; 5 gives 78.
    bar_minutes: int = 5

    # Resolution used to manage an open position. Finer than bar_minutes so
    # an adverse move is noticed within a minute rather than up to five.
    # None disables the second fetch and manages exits on the entry frame.
    #
    # Note this changes what the exit EMA means: EMA(9) spans 9 minutes on
    # 1-minute bars and 45 on 5-minute ones — a tighter exit, not merely a
    # faster one.
    manage_bar_minutes: Optional[int] = 1

    # How to interpret the indicator periods above when bar_minutes changes.
    #
    #   None (default) — periods are bar counts at bar_minutes. "MACD 12/26/9
    #     on the 5-minute chart" means 12/26/9 five-minute bars, which is what
    #     a trader means and what a charting platform draws. Switching
    #     resolution therefore changes the strategy.
    #
    #   60 (or any basis) — periods were tuned at that resolution and are
    #     rescaled to preserve wall-clock lookback. sma_slow=30 authored at 60
    #     becomes 360 at 5-minute: still 30 hours.
    #
    # Neither is more correct; they are different strategies. The setting
    # exists so the choice is made deliberately rather than by accident.
    indicator_period_basis: Optional[int] = None

    # Polling
    poll_interval_seconds: int = 60   # how often the bot cycles
    bar_limit: int = 300              # must cover min_bars(); EMA(200) binds

    def is_monitor_only(self) -> bool:
        return self.execution_mode == MODE_MONITOR

    def manage_params(self, ema_period: int) -> IndicatorParams:
        """
        Indicator periods for the management timeframe.

        Only the exit EMA and ATR are needed there, so this is deliberately
        small — computing a 200-period EMA on 1-minute bars for every held
        symbol would cost far more than it informs.
        """
        return IndicatorParams(
            ema_periods=(ema_period,),
            atr_period=self.atr_period,
            bar_minutes=self.manage_bar_minutes or self.bar_minutes,
        )

    def indicator_params(self) -> IndicatorParams:
        """
        The periods the shared indicator module needs, resolved to the
        configured bar size.

        Going through this method is what guarantees the bot and the scanner
        compute RSI over the same window — never build an IndicatorParams by
        hand in app code.

        When `indicator_period_basis` is set, the periods are treated as
        having been authored at that resolution and are rescaled to preserve
        wall-clock lookback. Otherwise they are taken as bar counts at
        `bar_minutes`.
        """
        basis = self.indicator_period_basis or self.bar_minutes
        params = IndicatorParams(
            sma_fast=self.sma_fast,
            sma_slow=self.sma_slow,
            ema_periods=self.ema_periods,
            rsi_period=self.rsi_period,
            volume_sma_period=self.volume_sma_period,
            atr_period=self.atr_period,
            macd_fast=self.macd_fast,
            macd_slow=self.macd_slow,
            macd_signal=self.macd_signal,
            bar_minutes=basis,
        )
        return params.rescaled_to(self.bar_minutes)

    def required_bar_limit(self) -> int:
        """
        Bars that must be fetched for every indicator to produce a value.

        MACD's signal line is the binding constraint. A 20% margin absorbs
        bars dropped as still-forming and any gaps in the returned series.
        """
        return int(self.indicator_params().min_bars() * 1.2) + 1

    def validate(self) -> None:
        """
        Fail loudly on a configuration that cannot produce signals.

        Fetching fewer bars than the indicators need does not raise anywhere
        downstream — every indicator is simply NaN, every symbol is skipped,
        and the bot looks like it is running fine while never trading. That
        is the worst failure mode available, so it is checked here.
        """
        if self.execution_mode not in EXECUTION_MODES:
            raise ValueError(
                f"execution_mode must be one of {EXECUTION_MODES}; "
                f"got {self.execution_mode!r}."
            )

        if self.bar_minutes <= 0 or 1440 % self.bar_minutes:
            raise ValueError(
                f"bar_minutes must divide evenly into 1440; got {self.bar_minutes}."
            )

        if self.manage_bar_minutes is not None:
            if 1440 % self.manage_bar_minutes:
                raise ValueError(
                    f"manage_bar_minutes must divide evenly into 1440; "
                    f"got {self.manage_bar_minutes}."
                )
            if self.manage_bar_minutes > self.bar_minutes:
                raise ValueError(
                    f"manage_bar_minutes ({self.manage_bar_minutes}) must be no "
                    f"coarser than bar_minutes ({self.bar_minutes}), or exits "
                    f"would be noticed later than entries."
                )

        needed = self.required_bar_limit()
        if self.bar_limit < needed:
            params = self.indicator_params()
            raise ValueError(
                f"bar_limit={self.bar_limit} is too small for {self.bar_minutes}-minute "
                f"bars: the indicators need {params.min_bars()} bars "
                f"({params.min_bars() * self.bar_minutes / 60:.1f} hours of data), so "
                f"bar_limit must be at least {needed}. "
                f"Set bar_limit={needed}, or reduce the indicator periods."
            )

        if self.indicator_period_basis and self.indicator_period_basis != self.bar_minutes:
            log.info(
                "Indicator periods authored for %d-minute bars, rescaled to %d-minute: %s",
                self.indicator_period_basis, self.bar_minutes,
                self.indicator_params().describe(),
            )

    def credentials(self) -> Credentials:
        return Credentials(
            api_key=self.api_key, api_secret=self.api_secret, paper=self.paper
        )
