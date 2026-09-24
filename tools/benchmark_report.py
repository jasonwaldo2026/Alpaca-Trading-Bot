"""
SPCX against the market, on one printable page.

A stock's own percentage says very little on its own. Down 4% on a day
the market fell 4% is a stock that did nothing; down 4% against a market
that fell 0.8% is most of a story. This draws the difference over a run
of sessions and marks the share-unlock dates on it, so the question
"was that the market or was that this stock?" can be answered by
looking rather than by remembering.

It predicts nothing and recommends nothing. Where the lines diverge is a
fact; why they diverged is not in this data, and a date lining up with a
divergence is a coincidence until there are enough of them to be
anything else.

READ-ONLY. Market data only. No trading client, no order object.

    python benchmark_report.py                    # 70 sessions vs SPY
    python benchmark_report.py --days 40 --benchmark QQQ
    python benchmark_report.py --self-test

Setup
-----
    pip install alpaca-py pandas matplotlib python-dotenv
    ALPACA_API_KEY / ALPACA_SECRET_KEY in .env, as the other tools use.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402

import lockups  # noqa: E402
from daily_report import (  # noqa: E402
    ACCENT, AXIS, DOWN, INK, INK_2, MUTED, SECOND, SURFACE, UP, reveal,
)
from feed_check import ET, load_credentials, load_env, trading_days  # noqa: E402

SYMBOL = "SPCX"
BENCHMARK = "SPY"
DEFAULT_DAYS = 70


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def fetch_daily(symbol: str, start: date, end: date) -> pd.DataFrame:
    """One bar per session. Daily bars, not aggregated minutes.

    Seventy days of one-minute bars is twenty-seven thousand rows to
    answer a question about seventy numbers.
    """
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    client = StockHistoricalDataClient(*load_credentials())
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=datetime.combine(start, time(0, 0), tzinfo=ET),
        end=datetime.combine(end, time(23, 59), tzinfo=ET),
        feed=DataFeed.SIP,
    )
    frame = client.get_stock_bars(request).df
    if frame is None or frame.empty:
        return pd.DataFrame()
    if isinstance(frame.index, pd.MultiIndex):
        frame = frame.xs(symbol, level="symbol")
    frame = frame.tz_convert(ET).sort_index()
    frame.index = pd.DatetimeIndex([d.date() for d in frame.index])
    return frame


@dataclass
class Comparison:
    symbol: str
    benchmark: str
    days: List[date]
    stock: pd.DataFrame
    market: pd.DataFrame

    @property
    def stock_index(self) -> List[float]:
        base = float(self.stock["close"].iloc[0])
        return [100.0 * float(c) / base for c in self.stock["close"]]

    @property
    def market_index(self) -> List[float]:
        base = float(self.market["close"].iloc[0])
        return [100.0 * float(c) / base for c in self.market["close"]]

    @property
    def relative(self) -> List[float]:
        """The stock's index divided by the market's. Flat means it kept up."""
        return [100.0 * s / m for s, m in zip(self.stock_index, self.market_index)]

    def daily_moves(self) -> Tuple[List[float], List[float]]:
        s = self.stock["close"].pct_change().fillna(0.0) * 100.0
        m = self.market["close"].pct_change().fillna(0.0) * 100.0
        return list(s), list(m)


def build(symbol: str, benchmark: str, days: int) -> Optional[Comparison]:
    wanted = trading_days(date.today(), days)
    start, end = wanted[0], wanted[-1]
    stock = fetch_daily(symbol, start, end)
    market = fetch_daily(benchmark, start, end)
    if stock.empty or market.empty:
        return None
    shared = sorted(set(stock.index) & set(market.index))
    if len(shared) < 5:
        return None
    return Comparison(symbol=symbol.upper(), benchmark=benchmark.upper(),
                      days=shared, stock=stock.loc[shared],
                      market=market.loc[shared])


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def ticks(days: List[date], count: int = 9):
    if len(days) <= count:
        idx = list(range(len(days)))
    else:
        step = max(1, len(days) // count)
        idx = list(range(0, len(days), step))
    return idx, [f"{days[i]:%d %b}" for i in idx]


def mark_unlocks(ax, comparison: Comparison, label: bool = False) -> List[int]:
    """Vertical rules at every unlock inside the window."""
    positions = []
    for unlock in lockups.for_symbol(comparison.symbol):
        if unlock.day is None or unlock.day not in comparison.days:
            continue
        i = comparison.days.index(unlock.day)
        positions.append(i)
        ax.axvline(i, color=SECOND, linewidth=1.1, alpha=0.55,
                   linestyle=(0, (4, 2)), zorder=1)
        if label:
            lo, hi = ax.get_ylim()
            ax.text(i, hi - (hi - lo) * 0.035,
                    f" {unlock.size().replace(' shares', '')}",
                    va="top", ha="left", size=7.6, color=SECOND, zorder=6,
                    bbox=dict(facecolor=SURFACE, edgecolor="none", pad=1.4,
                              alpha=0.9))
    return positions


def page(pdf: PdfPages, comparison: Comparison) -> None:
    days = comparison.days
    x = list(range(len(days)))
    stock_pct = comparison.stock_index[-1] - 100.0
    market_pct = comparison.market_index[-1] - 100.0
    gap = comparison.relative[-1] - 100.0

    fig = plt.figure(figsize=(11.7, 8.3))
    fig.text(0.045, 0.963, f"{comparison.symbol} vs {comparison.benchmark}",
             size=15, weight="bold", color=INK)
    fig.text(0.228, 0.9625,
             f"{days[0]:%d %b} to {days[-1]:%d %b %Y} · {len(days)} sessions",
             size=9.5, color=INK_2)

    stats = [
        (comparison.symbol, f"{stock_pct:+.1f}%", UP if stock_pct >= 0 else DOWN),
        (comparison.benchmark, f"{market_pct:+.1f}%",
         UP if market_pct >= 0 else DOWN),
        ("Difference", f"{gap:+.1f}%", UP if gap >= 0 else DOWN),
    ]
    for i, (label, value, tone) in enumerate(stats):
        px = 0.045 + i * 0.17
        fig.text(px, 0.9355, label.upper(), size=7, color=MUTED)
        fig.text(px + 0.055, 0.934, value, size=10, color=tone)
    fig.add_artist(plt.Line2D([0.045, 0.965], [0.920, 0.920],
                              color=AXIS, linewidth=0.8, transform=fig.transFigure))

    grid = fig.add_gridspec(3, 1, height_ratios=[2.6, 1.5, 1.2], hspace=0.30,
                            left=0.068, right=0.965, top=0.885, bottom=0.075)
    both = fig.add_subplot(grid[0])
    rel = fig.add_subplot(grid[1], sharex=both)
    bars = fig.add_subplot(grid[2], sharex=both)

    # --- both, indexed so they start together ------------------------------
    both.plot(x, comparison.stock_index, color=ACCENT, linewidth=1.9,
              label=comparison.symbol, zorder=3)
    both.plot(x, comparison.market_index, color=INK_2, linewidth=1.5,
              label=comparison.benchmark, zorder=3)
    both.axhline(100, color=AXIS, linewidth=1)
    mark_unlocks(both, comparison, label=True)
    lo, hi = both.get_ylim()
    both.set_ylim(lo, hi + (hi - lo) * 0.10)
    both.set_ylabel(f"indexed, {days[0]:%d %b} = 100")
    both.legend(loc="upper right", frameon=True, facecolor=SURFACE,
                edgecolor="none", framealpha=0.92, fontsize=9, ncol=2)
    both.set_title("Both start at 100, so the gap between them is the "
                   "difference in performance",
                   loc="left", size=9.5, color=INK_2, pad=4)
    both.spines[["top", "right"]].set_visible(False)

    # --- the gap on its own ------------------------------------------------
    rel.axhline(100, color=AXIS, linewidth=1)
    rel.fill_between(x, comparison.relative, 100,
                     where=[r >= 100 for r in comparison.relative],
                     color=UP, alpha=0.16, interpolate=True)
    rel.fill_between(x, comparison.relative, 100,
                     where=[r < 100 for r in comparison.relative],
                     color=DOWN, alpha=0.16, interpolate=True)
    rel.plot(x, comparison.relative, color=INK, linewidth=1.6, zorder=3)
    mark_unlocks(rel, comparison)
    rel.set_ylabel("stock vs market")
    rel.set_title(f"The same thing as one line. Falling means {comparison.symbol} "
                  f"is losing ground to the market, whichever way both went",
                  loc="left", size=9.5, color=INK_2, pad=4)
    rel.spines[["top", "right"]].set_visible(False)

    # --- day by day, stock minus market ------------------------------------
    stock_moves, market_moves = comparison.daily_moves()
    diff = [s - m for s, m in zip(stock_moves, market_moves)]
    unlock_positions = set(mark_unlocks(bars, comparison))
    bars.axhline(0, color=AXIS, linewidth=1)
    bars.bar(x, diff, width=0.74, zorder=2,
             color=[SECOND if i in unlock_positions
                    else (UP if d >= 0 else DOWN) for i, d in enumerate(diff)])
    bars.set_ylabel("% vs market")
    bars.set_title("Each session on its own: the stock's move minus the "
                   "market's. Unlock days in orange",
                   loc="left", size=9.5, color=INK_2, pad=4)
    bars.spines[["top", "right"]].set_visible(False)

    idx, labels = ticks(days)
    for ax in (both, rel):
        ax.tick_params(labelbottom=False)
    bars.set_xlim(-0.8, len(days) - 0.2)
    bars.set_xticks(idx)
    bars.set_xticklabels(labels, size=8)

    fig.text(0.045, 0.030,
             "Unlock dates are UNCONFIRMED — transcribed from secondary "
             "sources, not the prospectus. A date lining up with a fall is a "
             "coincidence until there are enough of them to be anything else.",
             size=7.6, color=MUTED)

    pdf.savefig(fig)
    plt.close(fig)


def table_page(pdf: PdfPages, comparison: Comparison) -> None:
    """Every unlock in the window, and how that day actually went."""
    stock_moves, market_moves = comparison.daily_moves()
    rows = []
    for unlock in lockups.for_symbol(comparison.symbol):
        if unlock.day is None or unlock.day not in comparison.days:
            continue
        i = comparison.days.index(unlock.day)
        rows.append((unlock, stock_moves[i], market_moves[i]))
    if not rows:
        return

    fig = plt.figure(figsize=(11.7, 8.3))
    fig.text(0.045, 0.945, "Unlock days", size=15, weight="bold", color=INK)
    fig.text(0.045, 0.918,
             f"{comparison.symbol} against {comparison.benchmark} on each "
             f"dated tranche inside this window", size=9.5, color=INK_2)
    fig.add_artist(plt.Line2D([0.045, 0.965], [0.900, 0.900],
                              color=AXIS, linewidth=0.8, transform=fig.transFigure))

    heads = ("Date", "Shares", comparison.symbol, comparison.benchmark,
             "Difference", "Tranche")
    xs = (0.045, 0.165, 0.275, 0.385, 0.495, 0.625)
    for x, head in zip(xs, heads):
        fig.text(x, 0.862, head.upper(), size=7.5, color=MUTED)

    y = 0.822
    for unlock, stock_move, market_move in rows:
        gap = stock_move - market_move
        cells = (f"{unlock.day:%d %b %Y}", unlock.size().replace(" shares", ""),
                 f"{stock_move:+.2f}%", f"{market_move:+.2f}%", f"{gap:+.2f}%",
                 unlock.label)
        tones = (INK, INK_2, UP if stock_move >= 0 else DOWN,
                 UP if market_move >= 0 else DOWN, UP if gap >= 0 else DOWN, INK_2)
        for x, cell, tone in zip(xs, cells, tones):
            fig.text(x, y, cell, size=9.5, color=tone)
        y -= 0.042

    diffs = [s - m for s, m in zip(stock_moves, market_moves)]
    ordinary = sorted(diffs)
    mid = ordinary[len(ordinary) // 2]
    worse = sum(1 for d in diffs if d < 0)
    fig.text(0.045, y - 0.03,
             f"For comparison, across all {len(diffs)} sessions here: the median "
             f"day was {mid:+.2f}% against the market, and {comparison.symbol} "
             f"lagged on {worse} of them ({100.0 * worse / len(diffs):.0f}%).",
             size=9.5, color=INK)
    fig.text(0.045, y - 0.075,
             f"{len(rows)} events is an anecdote. Read this as what those days "
             f"did, never as what the next one will do.",
             size=9.5, color=INK_2)

    pdf.savefig(fig)
    plt.close(fig)


def render(comparison: Comparison, path: str) -> str:
    with PdfPages(path) as pdf:
        page(pdf, comparison)
        table_page(pdf, comparison)
        info = pdf.infodict()
        info["Title"] = f"{comparison.symbol} vs {comparison.benchmark}"
        info["Subject"] = "Relative performance — read-only market data"
    return path


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def self_test() -> int:
    print("Self-test: checking the comparison maths...\n")
    failures = []

    days = [date(2026, 6, 1) + timedelta(days=i) for i in range(10)]
    # The stock doubles the market's move, so the relative line must rise.
    stock = pd.DataFrame({"close": [100 * (1.02 ** i) for i in range(10)],
                          "volume": [1_000] * 10}, index=days)
    market = pd.DataFrame({"close": [100 * (1.01 ** i) for i in range(10)],
                           "volume": [1_000] * 10}, index=days)
    c = Comparison("TEST", "BENCH", days, stock, market)

    if abs(c.stock_index[0] - 100) > 1e-9 or abs(c.market_index[0] - 100) > 1e-9:
        failures.append("both series must start at exactly 100, or the gap "
                        "between them is not a comparison")
    if c.relative[-1] <= 100:
        failures.append("a stock outrunning the market must give a rising "
                        f"relative line, got {c.relative[-1]:.2f}")
    if any(b < a for a, b in zip(c.relative, c.relative[1:])):
        failures.append("steady outperformance should not produce a falling leg")

    # A stock and market that move identically must read as flat, not as a
    # win -- this is the whole point of the page.
    same = Comparison("TEST", "BENCH", days, stock, stock.copy())
    if max(abs(r - 100) for r in same.relative) > 1e-9:
        failures.append("identical series must give a flat relative line")

    # A falling stock in a worse market is OUTperforming. If the page cannot
    # say that, it is just another price chart.
    falling = pd.DataFrame({"close": [100 * (0.99 ** i) for i in range(10)],
                            "volume": [1_000] * 10}, index=days)
    worse = pd.DataFrame({"close": [100 * (0.97 ** i) for i in range(10)],
                          "volume": [1_000] * 10}, index=days)
    down = Comparison("TEST", "BENCH", days, falling, worse)
    if down.stock_index[-1] >= 100:
        failures.append("the fixture's stock should have fallen")
    if down.relative[-1] <= 100:
        failures.append("falling less than the market is outperformance and "
                        "the relative line must rise to say so")

    stock_moves, market_moves = c.daily_moves()
    if len(stock_moves) != len(days) or stock_moves[0] != 0.0:
        failures.append("daily moves should align with the days, first at zero")
    if abs(stock_moves[1] - 2.0) > 1e-6:
        failures.append(f"a 2% day should read 2.00, got {stock_moves[1]:.4f}")

    source = open(__file__, encoding="utf-8").read()
    for forbidden in ("TradingClient", "submit_order", "MarketOrderRequest"):
        if forbidden in source.replace(f'"{forbidden}"', ""):
            failures.append(f"this file must not mention {forbidden}")

    print("  Indexed start                  : both at 100.00")
    print(f"  Outperforming                  : relative {c.relative[-1]:.2f}")
    print("  Identical series               : flat")
    print(f"  Falling less than the market   : relative {down.relative[-1]:.2f} "
          f"(stock {down.stock_index[-1]:.1f})")
    print("  Trading client in this file    : none")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nAll checks passed.")
    return 0


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(
        description="One page: the stock against the market, with unlocks marked.")
    parser.add_argument("--symbol", default=SYMBOL)
    parser.add_argument("--benchmark", default=BENCHMARK,
                        help=f"What to compare against (default {BENCHMARK})")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"Trading days back (default {DEFAULT_DAYS})")
    parser.add_argument("--out", default=None)
    parser.add_argument("--no-open", action="store_true",
                        help="Write the file without opening it")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    symbol = args.symbol.upper()
    print(f"Fetching {args.days} daily bars for {symbol} and "
          f"{args.benchmark.upper()}...")
    comparison = build(symbol, args.benchmark, args.days)
    if comparison is None:
        print(f"Not enough overlapping data for {symbol} and "
              f"{args.benchmark.upper()}.")
        return 1

    path = args.out or f"{symbol}_vs_{comparison.benchmark}.pdf"
    render(comparison, path)

    stock_pct = comparison.stock_index[-1] - 100.0
    market_pct = comparison.market_index[-1] - 100.0
    print(f"\n  {len(comparison.days)} sessions, "
          f"{comparison.days[0]} to {comparison.days[-1]}")
    print(f"  {symbol:<8}{stock_pct:+.1f}%")
    print(f"  {comparison.benchmark:<8}{market_pct:+.1f}%")
    print(f"  {'gap':<8}{stock_pct - market_pct:+.1f}%")
    print(f"\nwritten to {path}")
    if not args.no_open:
        reveal(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
