"""
Lookahead-free backtesting with dual-timeframe exits.

The shape this supports: **find entries on a coarse timeframe, manage the
position on a fine one.** A 5-minute chart is a reasonable place to notice
that price has held above VWAP for half an hour; it is a poor place to
discover you are down 1.5%, because you learn it up to five minutes late.
Passing `manage_bars` (1-minute) evaluates exits on every finer bar while
the position is open.

Two rules govern everything here:

1. **No lookahead.** At bar *i* the strategy sees `bars[:i+1]` and nothing
   more. A backtest that peeks is worse than no backtest, because it
   produces a confident number that cannot be repeated live.
2. **Signals and fills are separated.** A condition true at a bar's close
   fills at the *next* bar's open, because that close is the earliest moment
   the information existed. Filling at the signal bar's own close credits
   the strategy with a price it could not have traded.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

from core.indicators import COL_ATR, ema_column
from core.rules import Rule

log = logging.getLogger(__name__)

# Exit reasons, in the order they are checked.
EXIT_STOP = "atr_stop"
EXIT_EMA = "ema_cross_under"
EXIT_END_OF_DATA = "end_of_data"


@dataclass(frozen=True)
class ExitPolicy:
    """
    When to close a position.

    The intent is a trend-following exit — hold while the move works — with
    a hard floor so a gap cannot turn one trade into an unbounded loss.

    `ema_period` is read on the **management** timeframe, which changes what
    it means: a 9-period EMA is 45 minutes of 5-minute bars but 9 minutes of
    1-minute bars. Managing on 1-minute bars therefore exits considerably
    sooner than the same number on the entry chart. That is the intended
    trade-off — faster reaction, more trades cut short — but it is a
    different exit, not merely a faster one.
    """

    ema_period: int = 9
    #: Stop distance as a multiple of ATR at entry. None disables the floor,
    #: leaving the EMA as the only exit.
    atr_stop_multiple: Optional[float] = 1.5

    def stop_price(self, entry_price: float, atr_at_entry: float) -> Optional[float]:
        if self.atr_stop_multiple is None or not atr_at_entry or atr_at_entry <= 0:
            return None
        return entry_price - atr_at_entry * self.atr_stop_multiple


@dataclass
class Trade:
    symbol: str
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    exit_reason: str
    stop_price: Optional[float] = None

    @property
    def pnl_pct(self) -> float:
        return (self.exit_price - self.entry_price) / self.entry_price * 100

    @property
    def holding_minutes(self) -> float:
        return (self.exit_time - self.entry_time).total_seconds() / 60

    @property
    def is_win(self) -> bool:
        return self.exit_price > self.entry_price


@dataclass
class BacktestResult:
    symbol: str = ""
    trades: List[Trade] = field(default_factory=list)
    entry_bars_scanned: int = 0
    #: Signals that could not be filled because the data ran out.
    unfilled_signals: int = 0

    # ── Summary statistics ───────────────────────────────────────────────

    @property
    def count(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.is_win for t in self.trades) / len(self.trades) * 100

    @property
    def avg_pnl_pct(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.pnl_pct for t in self.trades) / len(self.trades)

    @property
    def median_pnl_pct(self) -> float:
        if not self.trades:
            return 0.0
        return float(pd.Series([t.pnl_pct for t in self.trades]).median())

    @property
    def total_return_pct(self) -> float:
        """
        Compounded return of trading one position at a time, fully invested.

        Ignores commission and slippage, so treat it as an upper bound.
        """
        equity = 1.0
        for trade in self.trades:
            equity *= 1 + trade.pnl_pct / 100
        return (equity - 1) * 100

    @property
    def max_drawdown_pct(self) -> float:
        """Deepest peak-to-trough fall of the trade-by-trade equity curve."""
        if not self.trades:
            return 0.0
        equity, peak, worst = 1.0, 1.0, 0.0
        for trade in self.trades:
            equity *= 1 + trade.pnl_pct / 100
            peak = max(peak, equity)
            worst = min(worst, equity / peak - 1)
        return worst * 100

    @property
    def avg_holding_minutes(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.holding_minutes for t in self.trades) / len(self.trades)

    def exit_reasons(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for trade in self.trades:
            counts[trade.exit_reason] = counts.get(trade.exit_reason, 0) + 1
        return counts

    def summary(self) -> Dict[str, float]:
        return {
            "trades": self.count,
            "win_rate_pct": round(self.win_rate, 1),
            "avg_pnl_pct": round(self.avg_pnl_pct, 3),
            "median_pnl_pct": round(self.median_pnl_pct, 3),
            "total_return_pct": round(self.total_return_pct, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "avg_holding_minutes": round(self.avg_holding_minutes, 1),
        }

    def trades_frame(self) -> pd.DataFrame:
        return pd.DataFrame([
            {
                "entry_time": t.entry_time,
                "entry_price": round(t.entry_price, 4),
                "exit_time": t.exit_time,
                "exit_price": round(t.exit_price, 4),
                "pnl_pct": round(t.pnl_pct, 3),
                "minutes": round(t.holding_minutes, 1),
                "exit": t.exit_reason,
            }
            for t in self.trades
        ])


def _require_columns(df: pd.DataFrame, columns, what: str) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ValueError(
            f"{what} is missing column(s) {missing}. Enrich the frame with "
            f"core.indicators.add_indicators() before backtesting."
        )


def backtest(
    entry_bars: pd.DataFrame,
    rule: Rule,
    exit_policy: ExitPolicy = ExitPolicy(),
    manage_bars: Optional[pd.DataFrame] = None,
    symbol: str = "",
) -> BacktestResult:
    """
    Replay `rule` over `entry_bars`, managing exits on `manage_bars`.

    Both frames must be timestamp-indexed and already enriched by
    `add_indicators()` — the backtest does not compute indicators, so that
    the numbers it measures are provably the same ones the scanner matches
    on.

    Entries fill at the first bar opening strictly after the signal bar's
    timestamp. With `manage_bars` supplied that is the next 1-minute bar,
    which is what "open a position as soon as possible" means in practice.

    Exits are checked on each closed management bar in this order:

    1. **ATR stop** — if the bar's low reaches the stop, the trade is closed
       at the stop price. This assumes a resting stop order filled at its
       trigger; a real gap through the level would fill worse.
    2. **EMA cross-under** — if the bar closes below the EMA, the trade is
       closed at the *next* bar's open.

    A position still open when the data ends is closed at the final bar's
    close and marked `end_of_data`, so it is visible rather than silently
    dropped from the statistics.
    """
    result = BacktestResult(symbol=symbol)

    if entry_bars is None or entry_bars.empty:
        return result
    if not isinstance(entry_bars.index, pd.DatetimeIndex):
        raise TypeError("entry_bars must be timestamp-indexed.")

    rule.validate()
    _require_columns(entry_bars, ["open", "close", COL_ATR], "entry_bars")

    manage = manage_bars if manage_bars is not None and not manage_bars.empty else entry_bars
    if not isinstance(manage.index, pd.DatetimeIndex):
        raise TypeError("manage_bars must be timestamp-indexed.")

    exit_ema = ema_column(exit_policy.ema_period)
    _require_columns(manage, ["open", "low", "close", exit_ema], "manage_bars")

    manage = manage.sort_index()
    entry_index = entry_bars.sort_index().index
    min_bars = rule.params.min_bars()

    position_open_until = None   # timestamp the current trade exited at
    i = 0

    while i < len(entry_index):
        signal_time = entry_index[i]

        # Skip entry bars that fall inside a trade we are already in.
        if position_open_until is not None and signal_time <= position_open_until:
            i += 1
            continue

        # Lookahead guard: the rule sees this bar and everything before it.
        window = entry_bars.iloc[: i + 1]
        result.entry_bars_scanned += 1

        if len(window) < min_bars or not rule.matches(window):
            i += 1
            continue

        # ── Fill the entry ────────────────────────────────────────────────
        fill_positions = manage.index[manage.index > signal_time]
        if len(fill_positions) == 0:
            result.unfilled_signals += 1
            break

        entry_time = fill_positions[0]
        entry_price = float(manage.loc[entry_time, "open"])
        atr_at_entry = float(entry_bars.iloc[i][COL_ATR])
        stop = exit_policy.stop_price(entry_price, atr_at_entry)

        # ── Walk the management bars looking for an exit ──────────────────
        after_entry = manage.loc[manage.index >= entry_time]
        trade = None

        for pos in range(len(after_entry)):
            bar = after_entry.iloc[pos]
            bar_time = after_entry.index[pos]

            if stop is not None and float(bar["low"]) <= stop:
                trade = Trade(
                    symbol=symbol, entry_time=entry_time, entry_price=entry_price,
                    exit_time=bar_time, exit_price=stop,
                    exit_reason=EXIT_STOP, stop_price=stop,
                )
                break

            ema_value = bar[exit_ema]
            if not pd.isna(ema_value) and float(bar["close"]) < float(ema_value):
                # Act on the close; fill at the next bar's open.
                if pos + 1 < len(after_entry):
                    exit_time = after_entry.index[pos + 1]
                    exit_price = float(after_entry.iloc[pos + 1]["open"])
                else:
                    exit_time, exit_price = bar_time, float(bar["close"])
                trade = Trade(
                    symbol=symbol, entry_time=entry_time, entry_price=entry_price,
                    exit_time=exit_time, exit_price=exit_price,
                    exit_reason=EXIT_EMA, stop_price=stop,
                )
                break

        if trade is None:
            last_time = after_entry.index[-1]
            trade = Trade(
                symbol=symbol, entry_time=entry_time, entry_price=entry_price,
                exit_time=last_time, exit_price=float(after_entry.iloc[-1]["close"]),
                exit_reason=EXIT_END_OF_DATA, stop_price=stop,
            )

        result.trades.append(trade)
        position_open_until = trade.exit_time
        i += 1

    return result
