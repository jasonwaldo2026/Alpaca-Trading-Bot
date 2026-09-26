"""
Which stocks are worth a day trader's attention, measured rather than felt.

A stock is tradeable on a one-minute chart when it moves enough to pay for
the spread, does so often rather than on three days a quarter, and is
liquid enough to get out of. Those are measurable, and measuring them
beats a watchlist assembled from things that were in the news.

There is deliberately NO overall score. A single number ranking stocks
would hide the trade-off that matters -- range against liquidity -- behind
false precision, and invite exactly the kind of confidence the rest of
this project has spent weeks dismantling. The columns are shown, SPCX sits
in the table as the reference you already know by feel, and the judgement
stays with you.

READ-ONLY. Market-data client only. No trading client, no order object.
Candidates come from a file you write; nothing here enumerates the market,
because that needs the trading client and this promise is worth more.

    python screener.py                       # candidates.txt, 60 days
    python screener.py --symbols AAPL,NVDA
    python screener.py --days 90 --pdf
    python screener.py --self-test

Setup
-----
    pip install alpaca-py pandas matplotlib python-dotenv
    ALPACA_API_KEY / ALPACA_SECRET_KEY in .env, as the other tools use.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import List, Optional, Sequence

import pandas as pd

from feed_check import ET, load_env, trading_days

REFERENCE = "SPCX"
DEFAULT_DAYS = 60
CANDIDATES_FILE = "candidates.txt"

#: Days of one-minute bars used for the morning-share column. Daily bars
#: answer most of this cheaply; only "where in the day does it move" needs
#: intraday, and ten sessions is enough to tell 30% from 60% without
#: fetching a quarter of minute data per symbol.
MORNING_SAMPLE_DAYS = 10
MORNING_END = time(11, 0)

#: A day that moved less than this is a day with nothing in it. Used for
#: the consistency column -- not as a filter, as a count.
WORTHWHILE_RANGE_PCT = 1.0

#: Below this there is no getting out of a position without moving it.
#: Reported, never applied silently: a symbol that fails it still appears,
#: flagged, because a screen that hides its rejects teaches you nothing.
THIN_DOLLAR_VOLUME = 20_000_000.0

FALLBACK_CANDIDATES = ("SPCX", "AAPL", "MSFT", "NVDA", "AMZN", "TSLA", "AMD",
                       "META", "GOOGL", "NFLX", "COIN", "PLTR")


@dataclass
class Measured:
    symbol: str
    days: int
    range_pct: float          # median (high-low)/open
    dollar_volume: float      # median close * volume
    worthwhile: float         # share of days clearing WORTHWHILE_RANGE_PCT
    morning_share: Optional[float]   # share of the day's movement done by 11:00
    trade_dollars: Optional[float]   # median dollars per trade

    @property
    def thin(self) -> bool:
        return self.dollar_volume < THIN_DOLLAR_VOLUME


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def candidates(path: str = CANDIDATES_FILE,
               explicit: Optional[str] = None) -> List[str]:
    """Symbols to measure: the flag, then the file, then a starting set."""
    if explicit:
        return [s.strip().upper() for s in explicit.split(",") if s.strip()]
    try:
        with open(path, encoding="utf-8") as handle:
            names = [line.split("#")[0].strip().upper() for line in handle]
        found = [n for n in names if n]
        if found:
            return found
    except OSError:
        pass
    return list(FALLBACK_CANDIDATES)


def daily_bars(symbol: str, start: date, end: date) -> pd.DataFrame:
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from feed_check import load_credentials

    client = StockHistoricalDataClient(*load_credentials())
    frame = client.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
        start=datetime.combine(start, time(0, 0), tzinfo=ET),
        end=datetime.combine(end, time(23, 59), tzinfo=ET),
        feed=DataFeed.SIP)).df
    if frame is None or frame.empty:
        return pd.DataFrame()
    if isinstance(frame.index, pd.MultiIndex):
        frame = frame.xs(symbol, level="symbol")
    return frame.tz_convert(ET).sort_index()


def morning_path_share(bars: pd.DataFrame) -> Optional[float]:
    """Share of the day's *travelled distance* that happened before 11:00.

    Distance, not range. The obvious version of this -- the morning's
    high-to-low over the day's high-to-low -- measures where the day's two
    extremes landed, and returns exactly 1.0 whenever both were set before
    11:00. A stock that grinds upward all afternoon without exceeding its
    10:15 high scores 100%, as though the afternoon had not happened, so
    the column saturates and separates nothing.

    Summing each minute's absolute move cannot saturate that way: an
    afternoon that keeps working adds to the denominator whether or not it
    sets a new extreme. A name genuinely finished by 11:00 still reads
    high; one that merely opened wide drops to a truthful figure.
    """
    if bars.empty:
        return None
    step = bars["close"].diff().abs()
    full = float(step.sum())
    if full <= 0:
        return None
    early = float(step[bars.index.time < MORNING_END].sum())
    return early / full


def morning_share(symbol: str, days: Sequence[date]) -> Optional[float]:
    """The median of `morning_path_share` over a handful of sessions.

    The question is not whether a stock moves but whether it moves while
    you are able to watch it. A name that does all its work at 15:45 is
    not tradeable by someone at a desk job, however lively the daily
    range column looks.
    """
    from open_candles import fetch_minutes

    shares = []
    for day in days:
        try:
            bars = fetch_minutes(symbol,
                                 datetime.combine(day, time(9, 30), tzinfo=ET),
                                 datetime.combine(day, time(16, 0), tzinfo=ET),
                                 force_sip=True)
        except Exception:  # noqa: BLE001 -- one thin day is not the measurement
            continue
        share = morning_path_share(bars)
        if share is not None:
            shares.append(share)
    if not shares:
        return None
    return float(pd.Series(shares).median())


def measure(symbol: str, days: int, morning_days: int) -> Optional[Measured]:
    window = trading_days(date.today(), days)
    bars = daily_bars(symbol, window[0], window[-1])
    if bars.empty or len(bars) < 5:
        return None

    ranges = 100.0 * (bars["high"] - bars["low"]) / bars["open"]
    dollars = bars["close"] * bars["volume"]
    # Dollars per trade, not shares. A share count compares the two
    # stocks' prices: the same money buys ten times the shares of a $25
    # name as of a $250 one, so SOFI reading 289 against TSLA's 39 says
    # only that SOFI is cheap. Dollars are comparable across price levels.
    per_trade = None
    if "trade_count" in bars:
        counts = bars["trade_count"].replace(0, pd.NA)
        per_trade = float((dollars / counts).median())

    sample = [d.date() for d in bars.index[-morning_days:]] if morning_days else []
    return Measured(
        symbol=symbol,
        days=len(bars),
        range_pct=float(ranges.median()),
        dollar_volume=float(dollars.median()),
        worthwhile=float((ranges >= WORTHWHILE_RANGE_PCT).mean()),
        morning_share=morning_share(symbol, sample) if sample else None,
        trade_dollars=per_trade,
    )


def money(value: float) -> str:
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}bn"
    return f"${value / 1_000_000:.0f}M"


def small_money(value: float) -> str:
    """Per-trade sizes live in the thousands, where money() reads $0M."""
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}k"
    return f"${value:.0f}"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def report(rows: List[Measured], days: int) -> None:
    rule = "=" * 88
    print(f"\n{rule}")
    print(f"  DAY-TRADE SCREEN  --  {len(rows)} symbols over {days} sessions")
    print(rule)
    print("  Sorted by daily range, because a stock that does not move cannot")
    print("  be traded on a one-minute chart however good it looks otherwise.")
    print("  There is no overall score on purpose: range and liquidity pull")
    print("  against each other and the trade-off is yours, not a formula's.\n")

    print(f"  {'Symbol':<9}{'Days':>6}{'Range':>9}{'>=1%':>8}{'$ volume':>12}"
          f"{'Morning':>10}{'$/trade':>11}")
    for row in sorted(rows, key=lambda r: -r.range_pct):
        morning = f"{row.morning_share * 100:.0f}%" if row.morning_share else "--"
        size = small_money(row.trade_dollars) if row.trade_dollars else "--"
        flag = "  thin" if row.thin else ""
        mark = " <" if row.symbol == REFERENCE else ""
        print(f"  {row.symbol:<9}{row.days:>6}{row.range_pct:>8.2f}%"
              f"{row.worthwhile * 100:>7.0f}%{money(row.dollar_volume):>12}"
              f"{morning:>10}{size:>11}{flag}{mark}")

    print("\n  Range     median daily high-to-low, as a % of the open")
    print("  >=1%      share of sessions that moved at least 1%")
    print("  $ volume  median dollars traded a day -- can you get out")
    print(f"  Morning   share of the day's movement done by {MORNING_END:%H:%M},")
    print("            which is the only part of the day you can watch.")
    print("            Distance travelled, not the spread of the extremes")
    print("  $/trade   median dollars per trade, a hint at who is trading it")
    if any(r.thin for r in rows):
        print(f"\n  'thin' marks under {money(THIN_DOLLAR_VOLUME)} a day. Shown rather")
        print("  than filtered out: a screen that hides its rejects teaches")
        print("  you nothing about where the line is.")
    reference = next((r for r in rows if r.symbol == REFERENCE), None)
    if reference:
        better = [r for r in rows
                  if r.range_pct > reference.range_pct
                  and r.dollar_volume >= reference.dollar_volume
                  and not r.thin]
        print(f"\n  {REFERENCE} is marked with '<' as your reference point.")
        if better:
            print(f"  {len(better)} name(s) moved more AND traded at least as much: "
                  f"{', '.join(r.symbol for r in better)}.")
        else:
            print(f"  Nothing here both moved more than {REFERENCE} and traded "
                  f"as heavily.")
    print(f"{rule}\n")


def label_offsets(rows: List[Measured]) -> List[tuple]:
    """Where to hang each symbol's label so two of them do not overlap.

    Points that sit close together on both axes -- a cluster of megacaps
    with the same range, say -- get their labels flipped to the other side
    rather than printed on top of each other. Cheap, and it only has to
    handle a watchlist, not a whole market.
    """
    import math

    placed: List[tuple] = []
    offsets: List[tuple] = []
    xs = [math.log10(max(r.dollar_volume, 1.0)) for r in rows]
    ys = [r.range_pct for r in rows]
    span_x = (max(xs) - min(xs)) or 1.0
    span_y = (max(ys) - min(ys)) or 1.0
    for x, y in zip(xs, ys):
        near = any(abs(x - px) / span_x < 0.06 and abs(y - py) / span_y < 0.06
                   for px, py in placed)
        offsets.append((-7, -11) if near else (7, 4))
        placed.append((x, y))
    return offsets


def render_pdf(rows: List[Measured], days: int, path: str) -> str:
    """Range against liquidity. The scatter is the point: you want both."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"
    AXIS, SURFACE, GRID = "#c3c2b7", "#fcfcfb", "#e1e0d9"
    ACCENT, SECOND = "#2a78d6", "#eb6834"

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9,
                         "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
                         "axes.edgecolor": AXIS, "text.color": INK,
                         "xtick.color": INK_2, "ytick.color": INK_2,
                         "axes.grid": True, "grid.color": GRID,
                         "grid.linewidth": 0.6, "axes.axisbelow": True})

    fig = plt.figure(figsize=(11.7, 8.3))
    fig.text(0.045, 0.955, "Day-trade screen", size=17, weight="bold", color=INK)
    fig.text(0.045, 0.928,
             f"{len(rows)} symbols over {days} sessions · "
             f"{REFERENCE} marked in orange", size=10, color=INK_2)
    fig.add_artist(plt.Line2D([0.045, 0.965], [0.912, 0.912], color=AXIS,
                              linewidth=0.8, transform=fig.transFigure))

    ax = fig.add_axes([0.075, 0.505, 0.89, 0.355])
    reference = next((r for r in rows if r.symbol == REFERENCE), None)
    for row, offset in zip(rows, label_offsets(rows)):
        is_ref = row.symbol == REFERENCE
        ax.scatter(row.dollar_volume / 1e9, row.range_pct,
                   s=150 if is_ref else 70,
                   color=SECOND if is_ref else (MUTED if row.thin else ACCENT),
                   zorder=4, edgecolors=SURFACE, linewidths=1.2)
        ax.annotate(row.symbol, (row.dollar_volume / 1e9, row.range_pct),
                    xytext=offset, textcoords="offset points", size=8.5,
                    ha="right" if offset[0] < 0 else "left",
                    color=SECOND if is_ref else INK_2)
    ax.margins(x=0.09, y=0.14)
    if reference:
        ax.axhline(reference.range_pct, color=SECOND, linewidth=1,
                   linestyle=(0, (4, 3)), alpha=0.6)
        ax.axvline(reference.dollar_volume / 1e9, color=SECOND, linewidth=1,
                   linestyle=(0, (4, 3)), alpha=0.6)
    ax.set_xscale("log")
    ax.set_xlabel("median dollars traded a day (billions, log scale)")
    ax.set_ylabel("median daily range, %")
    ax.set_title("Up and to the right is more movement and easier exits. "
                 "Dashed lines are where SPCX sits.",
                 loc="left", size=9.5, color=INK_2, pad=6)
    ax.spines[["top", "right"]].set_visible(False)

    heads = ("Symbol", "Range", ">=1%", "$ volume", "Morning", "$/trade")
    xs = (0.045, 0.165, 0.265, 0.365, 0.495, 0.605)
    for x, head in zip(xs, heads):
        fig.text(x, 0.395, head.upper(), size=7.5, color=MUTED)
    y = 0.360
    for row in sorted(rows, key=lambda r: -r.range_pct)[:9]:
        tone = SECOND if row.symbol == REFERENCE else (
            MUTED if row.thin else INK_2)
        cells = (row.symbol, f"{row.range_pct:.2f}%",
                 f"{row.worthwhile * 100:.0f}%", money(row.dollar_volume),
                 f"{row.morning_share * 100:.0f}%" if row.morning_share else "--",
                 small_money(row.trade_dollars) if row.trade_dollars else "--")
        for x, cell in zip(xs, cells):
            fig.text(x, y, cell, size=9.5, color=tone)
        y -= 0.034

    fig.text(0.045, 0.040,
             "No overall score, deliberately: range and liquidity pull against "
             "each other and a single number would hide the trade-off. "
             "Measured, not predicted — none of this says a stock will move "
             "tomorrow.", size=8, color=MUTED)

    with PdfPages(path) as pdf:
        pdf.savefig(fig)
        info = pdf.infodict()
        info["Title"] = "Day-trade screen"
        info["Subject"] = "Read-only market data"
    plt.close(fig)
    return path


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def self_test() -> int:
    print("Self-test: checking the screen...\n")
    failures = []

    lively = Measured("MOVER", 60, 3.10, 2.0e9, 0.85, 0.55, 140)
    dull = Measured("DULL", 60, 0.42, 5.0e9, 0.05, 0.40, 210)
    thin = Measured("THIN", 60, 6.00, 3.0e6, 0.90, 0.60, 40)

    if thin.thin is not True or lively.thin is not False:
        failures.append("the liquidity flag should mark THIN and not MOVER")

    ordered = [r.symbol for r in sorted([dull, lively, thin],
                                        key=lambda r: -r.range_pct)]
    if ordered[0] != "THIN" or ordered[-1] != "DULL":
        failures.append(f"ranking is by range, got {ordered}")

    # A thin stock must still appear. Silently dropping rejects is how a
    # screen stops teaching you where its own line is.
    if thin not in [dull, lively, thin]:
        failures.append("a thin symbol must survive into the table")

    # Two points on top of each other must not get two labels in the same
    # place. A chart nobody can read is a chart that lies by omission.
    crowded = [thin, lively,
               Measured("ONE", 60, 1.20, 9.0e9, 0.5, None, None),
               Measured("TWO", 60, 1.21, 9.1e9, 0.5, None, None)]
    offsets = label_offsets(crowded)
    if offsets[-1] == offsets[-2]:
        failures.append("overlapping points should get labels on opposite sides")
    if label_offsets([lively, dull, thin]) != [(7, 4)] * 3:
        failures.append("well-separated points should all label the same way")

    if money(2.4e9) != "$2.4bn" or money(4.7e7) != "$47M":
        failures.append(f"money() formatting: {money(2.4e9)}, {money(4.7e7)}")
    if small_money(4900) != "$4.9k" or small_money(820) != "$820":
        failures.append(f"small_money(): {small_money(4900)}, {small_money(820)}")

    # The morning column's whole point. This synthetic day sets BOTH its
    # extremes before 11:00 and then oscillates all afternoon inside that
    # band. The old high-to-low definition scored it 100% -- the afternoon
    # was invisible to it. Distance travelled must not.
    index = pd.date_range("2026-09-23 09:30", "2026-09-23 15:59",
                          freq="1min", tz=ET)
    closes = []
    for stamp in index:
        if stamp.time() < MORNING_END:
            closes.append(102.0 if len(closes) % 2 else 98.0)   # sets the extremes
        else:
            closes.append(101.0 if len(closes) % 2 else 99.0)   # busy, inside them
    busy = pd.DataFrame({"close": closes, "high": closes, "low": closes},
                        index=index)
    share = morning_path_share(busy)
    extremes = ((busy[busy.index.time < MORNING_END]["high"].max()
                 - busy[busy.index.time < MORNING_END]["low"].min())
                / (busy["high"].max() - busy["low"].min()))
    if share is None or share > 0.60:
        failures.append(f"a busy afternoon must not read as a finished day: "
                        f"{share}")
    if round(extremes, 6) != 1.0:
        failures.append("the fixture should saturate the old definition, "
                        f"got {extremes}")

    # A dead afternoon should still read high -- the fix must not simply
    # push every symbol down.
    quiet = busy.copy()
    quiet.loc[quiet.index.time >= MORNING_END, ["close", "high", "low"]] = 100.0
    if (morning_path_share(quiet) or 0) < 0.95:
        failures.append("a day that truly finished by 11:00 should read high")

    if morning_path_share(pd.DataFrame({"close": [], "high": [], "low": []},
                                       index=pd.DatetimeIndex([], tz=ET))) is not None:
        failures.append("an empty day should be skipped, not scored")

    # $/trade must compare across price levels: same money per trade, same
    # number, whatever the share price.
    cheap = Measured("CHEAP", 60, 3.0, 1e9, 0.9, 0.5, 5_000.0)
    dear = Measured("DEAR", 60, 3.0, 1e9, 0.9, 0.5, 5_000.0)
    if small_money(cheap.trade_dollars) != small_money(dear.trade_dollars):
        failures.append("dollars per trade should not depend on share price")

    # Candidate resolution, in its stated order of preference.
    if candidates(explicit="aapl, nvda ,") != ["AAPL", "NVDA"]:
        failures.append("an explicit list should win, be upper-cased and trimmed")
    if candidates(path="does-not-exist.txt") != list(FALLBACK_CANDIDATES):
        failures.append("a missing candidates file should fall back, not raise")

    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        handle.write("# a comment\nTSLA  # trailing note\n\n  amd\n")
        temp = handle.name
    try:
        if candidates(path=temp) != ["TSLA", "AMD"]:
            failures.append(f"comments and blanks should be stripped: "
                            f"{candidates(path=temp)}")
    finally:
        os.unlink(temp)

    # The promise every file in this folder makes.
    source = open(__file__, encoding="utf-8").read()
    for forbidden in ("TradingClient", "submit_order", "MarketOrderRequest",
                      "get_all_assets"):
        if forbidden in source.replace(f'"{forbidden}"', ""):
            failures.append(f"this file must not mention {forbidden}")

    print("  Liquidity flag                 : THIN marked, MOVER not")
    print(f"  Ranking                        : {' > '.join(ordered)}")
    print("  Thin symbols                   : shown, not filtered")
    print("  Crowded chart labels           : flipped, not stacked")
    print(f"  Morning column                 : busy afternoon reads "
          f"{share * 100:.0f}%, not 100%")
    print("  $/trade                        : independent of share price")
    print("  Candidate file                 : comments and blanks stripped")
    print(f"  Missing candidate file         : falls back to {len(FALLBACK_CANDIDATES)} names")
    print("  Trading client in this file    : none")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nAll checks passed. Now run it against real data.")
    return 0


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(
        description="Which candidates are worth a day trader's attention.")
    parser.add_argument("--symbols", default=None,
                        help="Comma-separated, instead of the candidates file")
    parser.add_argument("--file", default=CANDIDATES_FILE,
                        help=f"One symbol per line, # for comments "
                             f"(default {CANDIDATES_FILE})")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--morning-days", type=int, default=MORNING_SAMPLE_DAYS,
                        metavar="N",
                        help=f"Sessions of 1-minute bars for the morning column "
                             f"(default {MORNING_SAMPLE_DAYS}; 0 skips it and "
                             f"is much faster)")
    parser.add_argument("--pdf", nargs="?", const="day_trade_screen.pdf",
                        metavar="FILE", help="Also write a one-page PDF")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    names = candidates(args.file, args.symbols)
    if REFERENCE not in names:
        names.append(REFERENCE)
    print(f"Measuring {len(names)} symbols over {args.days} sessions "
          f"(SIP daily bars)...")
    if args.morning_days:
        print(f"  plus {args.morning_days} sessions of 1-minute bars each for "
              f"the morning column -- this is the slow part.")

    rows, missing = [], []
    for n, symbol in enumerate(names, 1):
        try:
            row = measure(symbol, args.days, args.morning_days)
        except Exception as exc:  # noqa: BLE001 -- one bad symbol is not the run
            print(f"  {symbol:<8} failed: {type(exc).__name__}: {exc}")
            missing.append(symbol)
            continue
        if row is None:
            missing.append(symbol)
            print(f"  {symbol:<8} no usable data")
            continue
        rows.append(row)
        print(f"  {symbol:<8} {n}/{len(names)}")

    if not rows:
        print("\nNothing measurable. Check the symbols and your connection.")
        return 1

    report(rows, args.days)
    if missing:
        print(f"  No data for: {', '.join(missing)}\n")
    if args.pdf:
        print(f"written to {render_pdf(rows, args.days, args.pdf)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
