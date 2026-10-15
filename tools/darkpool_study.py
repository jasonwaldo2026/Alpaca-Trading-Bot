"""
Dark pool study: do big off-exchange prints mark prices that matter?

FlowAlgo's own guide concedes the point that decides everything here: a
block of shares has a buyer and a seller, so a print cannot say which way
anyone leaned. What it offers instead is a LEVEL -- "the order happened at
$150.00; if price then breaks resistance it is bullish, if it breaks
support it is bearish". That is testable, and this measures it:

  1. LEVELS    After a large dark pool print, when price comes back to
               that print's price, does it turn there (held) or carry
               through (broke)? Scored against random prices nearby,
               judged by exactly the same rule. At a random price the two
               outcomes are a coin flip by construction; a print level is
               worth marking only if it holds clearly more often than one.

  2. AFTERMATH What did price do 15/30/60 minutes after the print, against
               every other minute of the same hours on the same days? A
               print with no direction should show no signed drift; if
               anything is there, it is in the size of the move.

The data is Alpaca's full SIP tape, free historically as long as the
request ends 15+ minutes ago. Nothing comes from FlowAlgo: its terms
forbid passing its data on, and this needs none of it. Off-exchange
trades are the ones the tape marks as reported to FINRA (exchange "D").

READ-ONLY. Market-data client only, inherited from feed_check. There is
no trading client anywhere in these files.

What it cannot see
------------------
* Who was on either side, or why. Neither can anyone selling the data.
* Late prints. Average-price and prior-reference trades are reported
  long after they were agreed, at a price the market has left. They are
  counted, labelled "away from market", and kept out of the level test:
  a level the market was not at when it printed says nothing about where
  the market is.
* More than one level per price per day. Twenty-six prints at $153.88
  are one level, not twenty-six -- counting each would multiply one
  observation into a sample.

Usage
-----
    python darkpool_study.py                       # SPCX, last 20 days
    python darkpool_study.py --days 40
    python darkpool_study.py --min-notional 20000000
    python darkpool_study.py --flowalgo export.csv # FlowAlgo's prints instead
    python darkpool_study.py --self-test           # no network

--flowalgo reads a CSV from FlowAlgo's Equity Exporter and tests ITS
prints the same way, against Alpaca's bars. The file is FlowAlgo's data,
licensed to you alone: keep it on your own machine, never in this repo.

The first run downloads every trade of every day, which for a busy symbol
takes minutes. The off-exchange part is kept in darkpool_cache/ beside
this file, so later runs with different thresholds are quick. That
folder is market data and stays out of git.
"""

from __future__ import annotations

import argparse
import math
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

from feed_check import ET, _data_client, load_env, trading_days

SYMBOL = "SPCX"
DAYS = 20

#: A "block" is an off-exchange print worth at least this much. FlowAlgo's
#: SPCX orders averaged far more (26 orders, $2.18B); this is lower so
#: there is a sample at all, and --min-notional raises it.
MIN_NOTIONAL = 5_000_000

#: The tape's code for trades reported to FINRA: dark pools and
#: internalised orders. Every venue that is not an exchange lands here.
OFF_EXCHANGE = "D"

#: What the cache keeps. Small enough to stay tiny, low enough that any
#: --min-notional at a sane price is still answerable from it.
CACHE_MIN_SHARES = 1_000
CACHE_DIR = Path(__file__).with_name("darkpool_cache")

#: Extended hours, the whole of what Alpaca has.
SESSION_START, SESSION_END = time(4, 0), time(20, 0)

#: The level test. Price must first be AWAY percent from the level for a
#: return to count as an arrival; it then HELD if it goes REACT percent
#: back the way it came before going REACT percent through. Equal by
#: default, which is what makes a random level a coin flip.
AWAY, REACT = 0.30, 0.30
#: Minutes after an arrival to wait for either outcome.
WINDOW = 30
#: Random levels per print level, and how far from it they may fall.
CONTROLS, SPAN = 5, 2.0
SEED = 20261015

HORIZONS = (15, 30, 60)
#: Fewer than this prints "too few" instead of a percentage.
MIN_SAMPLE = 20


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def _window(day: date) -> Tuple[datetime, datetime]:
    return (datetime.combine(day, SESSION_START, tzinfo=ET),
            datetime.combine(day, SESSION_END, tzinfo=ET))


def fetch_minutes(symbol: str, day: date) -> pd.DataFrame:
    """04:00-20:00 ET of 1-minute SIP bars."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    start, end = _window(day)
    frame = _data_client().get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
        start=start, end=end, feed=DataFeed.SIP)).df
    if frame is None or frame.empty:
        return pd.DataFrame()
    if isinstance(frame.index, pd.MultiIndex):
        frame = frame.xs(symbol, level="symbol")
    return frame.tz_convert(ET).sort_index()


def fetch_offexchange(symbol: str, day: date) -> pd.DataFrame:
    """The day's off-exchange trades of CACHE_MIN_SHARES or more.

    Read from the cache when present. An empty frame is cached too, so a
    holiday is asked about once rather than on every run.
    """
    path = CACHE_DIR / f"{symbol}_{day:%Y%m%d}.csv.gz"
    if path.exists():
        cached = pd.read_csv(path)
        if cached.empty:
            return pd.DataFrame(columns=["price", "size", "conditions"])
        cached["timestamp"] = pd.to_datetime(cached["timestamp"], utc=True)
        return cached.set_index("timestamp").tz_convert(ET)

    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockTradesRequest

    start, end = _window(day)
    trades = _data_client().get_stock_trades(StockTradesRequest(
        symbol_or_symbols=symbol, start=start, end=end,
        feed=DataFeed.SIP)).df
    kept = pd.DataFrame(columns=["price", "size", "conditions"])
    if trades is not None and not trades.empty:
        if isinstance(trades.index, pd.MultiIndex):
            trades = trades.xs(symbol, level="symbol")
        trades = trades.tz_convert(ET)
        venues = set(trades["exchange"].astype(str))
        if OFF_EXCHANGE not in venues:
            # Not "no dark pool trades": a tape with none at all means the
            # code is wrong, and silence would read as a quiet day.
            print(f"    warning: no exchange '{OFF_EXCHANGE}' trades among "
                  f"{len(trades):,}; venues seen: {sorted(venues)}")
        dark = trades[(trades["exchange"].astype(str) == OFF_EXCHANGE)
                      & (trades["size"] >= CACHE_MIN_SHARES)]
        kept = dark[["price", "size", "conditions"]].copy()
        kept["conditions"] = kept["conditions"].astype(str)
        print(f"    {len(trades):,} trades, {len(kept):,} off-exchange of "
              f"{CACHE_MIN_SHARES:,}+ shares")
    CACHE_DIR.mkdir(exist_ok=True)
    kept.rename_axis("timestamp").reset_index().to_csv(path, index=False)
    return kept


# --------------------------------------------------------------------------
# Prints into levels
# --------------------------------------------------------------------------

@dataclass
class Level:
    day: date
    at: datetime        # the first print at this price
    price: float
    prints: int
    shares: int
    notional: float


def at_market(price: float, at: datetime, bars: pd.DataFrame,
              tolerance: float = 0.001) -> bool:
    """Was the market at this price around the minute it was reported?

    Average-price and prior-reference prints arrive long after the deal,
    at a price the market has left; the bars, which such prints do not
    update, show it. The minute either side counts too: a report can
    trail its trade by seconds, across a minute boundary.
    """
    minute = pd.Timestamp(at).floor("1min")
    near = bars[(bars.index >= minute - pd.Timedelta(minutes=1))
                & (bars.index <= minute + pd.Timedelta(minutes=1))]
    if near.empty:
        return False
    slack = price * tolerance
    return near["low"].min() - slack <= price <= near["high"].max() + slack


def to_levels(prints: pd.DataFrame, day: date, bars: pd.DataFrame,
              min_notional: float, merge: float = 0.0005,
              check_market: bool = True) -> Tuple[List[Level], int]:
    """(levels, how many blocks were away from the market).

    Blocks within `merge` of an existing level that day join it rather
    than starting another: forty-three prints at $234.32 is one price.
    """
    if prints.empty:
        return [], 0
    blocks = prints[prints["price"] * prints["size"] >= min_notional]
    levels: List[Level] = []
    away = 0
    for stamp, row in blocks.sort_index().iterrows():
        price, size = float(row["price"]), int(row["size"])
        if check_market and not at_market(price, stamp, bars):
            away += 1
            continue
        for level in levels:
            if abs(level.price - price) <= level.price * merge:
                level.prints += 1
                level.shares += size
                level.notional += price * size
                break
        else:
            levels.append(Level(day, stamp.to_pydatetime(), price, 1, size,
                                price * size))
    return levels, away


# --------------------------------------------------------------------------
# The level test
# --------------------------------------------------------------------------

def first_test(bars: pd.DataFrame, level: float, after: datetime,
               away: float = AWAY, react: float = REACT,
               window: int = WINDOW) -> Optional[str]:
    """"held", "broke", or None for the first return to `level` after
    `after`.

    Only bars strictly after `after` are read: a print cannot be credited
    with price action that came before anyone could have seen it.

    A bar that reaches both outcomes counts as broke. A minute bar does
    not record which came first, and the cautious reading is the one a
    stop would have suffered.
    """
    later = bars[bars.index > pd.Timestamp(after)]
    side: Optional[str] = None
    lows, highs = later["low"].to_numpy(), later["high"].to_numpy()
    closes, stamps = later["close"].to_numpy(), later.index
    for i in range(len(later)):
        if side is not None and lows[i] <= level <= highs[i]:
            return _resolve(later.iloc[i + 1:], stamps[i], level, side,
                            react, window)
        gap = (closes[i] - level) / level * 100
        if abs(gap) >= away:
            side = "above" if gap > 0 else "below"
    return None


def _resolve(after: pd.DataFrame, touched: pd.Timestamp, level: float,
             side: str, react: float, window: int) -> Optional[str]:
    limit = touched + pd.Timedelta(minutes=window)
    up, down = level * (1 + react / 100), level * (1 - react / 100)
    for stamp, bar in after.iterrows():
        if stamp > limit:
            break
        hit_up, hit_down = bar["high"] >= up, bar["low"] <= down
        if side == "above":         # came down to it from above: support
            held, broke = hit_up, hit_down
        else:                       # came up to it from below: resistance
            held, broke = hit_down, hit_up
        if broke:
            return "broke"
        if held:
            return "held"
    return None


def control_prices(level: float, count: int, rng: random.Random,
                   away: float = AWAY, span: float = SPAN) -> List[float]:
    """Random prices near `level`, never close enough to be the same one."""
    out = []
    for _ in range(count):
        offset = rng.uniform(away, span) * rng.choice((-1, 1))
        out.append(level * (1 + offset / 100))
    return out


@dataclass
class Tally:
    held: int = 0
    broke: int = 0

    def add(self, outcome: Optional[str]) -> None:
        if outcome == "held":
            self.held += 1
        elif outcome == "broke":
            self.broke += 1

    @property
    def n(self) -> int:
        return self.held + self.broke

    @property
    def rate(self) -> float:
        return self.held / self.n if self.n else float("nan")


def verdict(prints: Tally, controls: Tally) -> str:
    """One plain sentence, refusing to call noise a finding."""
    if prints.n < MIN_SAMPLE or controls.n < MIN_SAMPLE:
        return (f"too few to say: {prints.n} print-level tests, "
                f"{controls.n} random ({MIN_SAMPLE} of each needed)")
    p1, p2 = prints.rate, controls.rate
    noise = 2 * math.sqrt(p1 * (1 - p1) / prints.n
                          + p2 * (1 - p2) / controls.n)
    diff = (p1 - p2) * 100
    if abs(p1 - p2) <= noise:
        return (f"no difference beyond noise: print levels held {p1:.0%}, "
                f"random prices {p2:.0%} (noise ±{noise * 100:.0f} points)")
    more = "MORE" if diff > 0 else "LESS"
    return (f"print levels held {more} often than random prices: {p1:.0%} "
            f"against {p2:.0%}, {abs(diff):.0f} points (noise ±"
            f"{noise * 100:.0f})")


# --------------------------------------------------------------------------
# Aftermath
# --------------------------------------------------------------------------

def move_after(bars: pd.DataFrame, at: datetime, minutes: int
               ) -> Optional[float]:
    """Percent change from the close at `at` to the close `minutes` later.

    By the clock, not by bar count: a quiet minute with no trades has no
    bar, and counting bars would stretch the horizon on exactly the
    thinnest stretches.
    """
    stamp = pd.Timestamp(at).floor("1min")
    before = bars[bars.index <= stamp]
    later = bars[bars.index <= stamp + pd.Timedelta(minutes=minutes)]
    if before.empty or later.empty or later.index[-1] <= stamp:
        return None
    if stamp + pd.Timedelta(minutes=minutes) > bars.index[-1]:
        return None
    start = float(before["close"].iloc[-1])
    return (float(later["close"].iloc[-1]) - start) / start * 100


def median(values: Sequence[float]) -> float:
    return float(pd.Series(list(values), dtype=float).median()) if values \
        else float("nan")


# --------------------------------------------------------------------------
# A FlowAlgo export
# --------------------------------------------------------------------------

#: The prints FlowAlgo labels, and what they are. "Lit Pool" is a block on
#: a public exchange -- seen by everyone as it printed, not dark at all.
FLOW_KINDS = {"Dark Pool": "dark", "Lit Pool": "lit"}


@dataclass
class FlowFile:
    prints: pd.DataFrame        # price, size, kind; indexed by ET time
    rows: int                   # in the file
    duplicates: int             # identical rows dropped
    other: int                  # rows for other symbols, or unknown kinds


def read_flowalgo(path: str, symbol: str) -> FlowFile:
    """FlowAlgo's Equity Exporter CSV, cleaned.

    Identical rows -- same second, size and price -- are dropped. The
    export repeats some trades verbatim, and its own daily totals count
    them twice; one trade is one trade.

    Times are read as US/Eastern: the export's 16:00 prints sit at the
    closing price, which only makes sense on the exchange's own clock.
    """
    raw = pd.read_csv(path, dtype=str)
    rows = len(raw)
    raw = raw.drop_duplicates(["Date", "Time", "Ticker", "Quantity", "Spot"])
    duplicates = rows - len(raw)
    keep = ((raw["Ticker"].str.upper() == symbol)
            & raw["Type"].isin(FLOW_KINDS))
    mine = raw[keep]
    stamps = pd.to_datetime(mine["Date"] + " " + mine["Time"],
                            format="%m/%d/%y %H:%M:%S.%f")
    prints = pd.DataFrame({
        "price": mine["Spot"].str.replace(",", "").astype(float).values,
        "size": mine["Quantity"].str.replace(",", "").astype(int).values,
        "kind": mine["Type"].map(FLOW_KINDS).values,
    }, index=pd.DatetimeIndex(stamps).tz_localize(ET)).sort_index()
    return FlowFile(prints, rows, duplicates, int((~keep).sum()))


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

@dataclass
class Results:
    printed: Tally
    controlled: Tally
    after_print: Dict[int, List[float]]
    after_any: Dict[int, List[float]]


def next_session(day: date, bars_for, tries: int = 5
                 ) -> Tuple[Optional[date], pd.DataFrame]:
    """The first later weekday that has bars, and its bars."""
    for step in range(1, tries + 1):
        later = day + timedelta(days=step)
        if later.weekday() >= 5:
            continue
        bars = bars_for(later)
        if not bars.empty:
            return later, bars
    return None, pd.DataFrame()


def score(levels: Sequence[Level], bars_for, rng: random.Random,
          overnight: bool = False) -> Results:
    """The level test and the aftermath over `levels`.

    `overnight` tests each level on the NEXT session instead of its own:
    for prints at the close, nothing is left of the day to test on. The
    aftermath is skipped for those -- fifteen minutes after a 16:05 print
    is after-hours tape, not the market the print was priced in.
    """
    res = Results(Tally(), Tally(), {h: [] for h in HORIZONS},
                  {h: [] for h in HORIZONS})
    by_day: Dict[date, List[Level]] = {}
    for level in levels:
        by_day.setdefault(level.day, []).append(level)
    for day in sorted(by_day):
        todays = by_day[day]
        if overnight:
            _, bars = next_session(day, bars_for)
        else:
            bars = bars_for(day)
        if bars.empty:
            continue
        for level in todays:
            res.printed.add(first_test(bars, level.price, level.at))
            for price in control_prices(level.price, CONTROLS, rng):
                res.controlled.add(first_test(bars, price, level.at))
            if not overnight:
                for h in HORIZONS:
                    move = move_after(bars, level.at, h)
                    if move is not None:
                        res.after_print[h].append(move)
        if overnight:
            continue
        # The comparison for the aftermath: every minute of the hours the
        # prints happened in, on days they happened.
        hours = {lv.at.hour for lv in todays}
        for stamp in bars.index:
            if stamp.hour in hours:
                for h in HORIZONS:
                    move = move_after(bars, stamp, h)
                    if move is not None:
                        res.after_any[h].append(move)
    return res


def cached_bars(symbol: str):
    """fetch_minutes, asked once per day however many groups need it."""
    seen: Dict[date, pd.DataFrame] = {}

    def bars_for(day: date) -> pd.DataFrame:
        if day not in seen:
            seen[day] = fetch_minutes(symbol, day)
        return seen[day]
    return bars_for


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def print_largest(levels: Sequence[Level], count: int = 5) -> None:
    for lv in sorted(levels, key=lambda lv: -lv.notional)[:count]:
        print(f"      {lv.at:%Y-%m-%d %H:%M}  ${lv.price:,.2f}  "
              f"{lv.prints} print(s)  {lv.shares:,} sh  "
              f"${lv.notional / 1e6:,.0f}M")


def print_results(title: str, levels: Sequence[Level], res: Results,
                  overnight: bool = False) -> None:
    print(f"\n  {title}: {len(levels)} level(s)")
    if levels:
        print("    largest:")
        print_largest(levels)
    where = "on the next session" if overnight else "after the print"
    print(f"    LEVELS -- first return to the price {where}, then "
          f"{REACT:.2f}% back or {REACT:.2f}% through within {WINDOW} min")
    print(f"      print levels   held {res.printed.held:>4}  broke "
          f"{res.printed.broke:>4}")
    print(f"      random prices  held {res.controlled.held:>4}  broke "
          f"{res.controlled.broke:>4}")
    print(f"      -> {verdict(res.printed, res.controlled)}")
    if overnight:
        return
    print("    AFTERMATH -- median move after a print, against every minute "
          "of the same hours")
    print(f"      {'':10}{'prints':>18}{'any minute':>18}")
    for h in HORIZONS:
        p, a = res.after_print[h], res.after_any[h]
        if len(p) < MIN_SAMPLE:
            print(f"      {h:>3} min    {'too few (' + str(len(p)) + ')':>18}")
            continue
        print(f"      {h:>3} min    {median(p):>+8.2f}% ±"
              f"{median([abs(x) for x in p]):.2f}{median(a):>+11.2f}% ±"
              f"{median([abs(x) for x in a]):.2f}")
    print("      (first number: direction; ± number: typical size either "
          "way)")


def run(symbol: str, days: int, min_notional: float) -> int:
    """The study on Alpaca's own off-exchange prints."""
    load_env()
    end = date.today() - timedelta(days=1)
    bars_for = cached_bars(symbol)
    all_levels: List[Level] = []
    away_total = 0

    print(f"{symbol}: off-exchange prints of ${min_notional / 1e6:,.0f}M+ "
          f"over the last {days} trading days (SIP tape)\n")
    for day in trading_days(end, days):
        print(f"  {day}")
        prints = fetch_offexchange(symbol, day)
        if prints.empty:
            print("    no off-exchange prints (holiday, or nothing traded)")
            continue
        bars = bars_for(day)
        if bars.empty:
            continue
        levels, away = to_levels(prints, day, bars, min_notional)
        away_total += away
        all_levels += levels
        print(f"    {len(levels)} level(s) from blocks"
              + (f", {away} block(s) away from market" if away else ""))

    print("\n" + "=" * 64)
    print(f"  {away_total} blocks away from the market (late or average-price "
          f"reports, left out)")
    res = score(all_levels, bars_for, random.Random(SEED))
    print_results("Off-exchange blocks", all_levels, res)
    print("=" * 64)
    return 0


def run_flowalgo(path: str, symbol: str) -> int:
    """The same study on the prints in a FlowAlgo export.

    Three groups, never pooled: dark pool prints during the session, lit
    (exchange) blocks during the session, and prints after 16:00, which
    are mostly one price per day -- the close -- and so are tested on the
    following session as that day's closing price.
    """
    load_env()
    flow = read_flowalgo(path, symbol)
    prints = flow.prints
    print(f"FlowAlgo export: {flow.rows} rows, {flow.duplicates} identical "
          f"duplicate(s) dropped, {flow.other} for other symbols or kinds")
    if prints.empty:
        print(f"No {symbol} prints in the file.")
        return 1
    after = prints.index.time >= time(16, 0)
    print(f"{symbol}: {len(prints)} prints, {prints.index[0]:%Y-%m-%d} to "
          f"{prints.index[-1]:%Y-%m-%d} -- dark {int((prints.kind == 'dark').sum())},"
          f" lit {int((prints.kind == 'lit').sum())}, after 16:00 "
          f"{int(after.sum())}\n")

    bars_for = cached_bars(symbol)
    groups = {"dark": [], "lit": [], "close": []}
    away_total = 0
    for day in sorted(set(prints.index.date)):
        print(f"  {day}")
        todays = prints[prints.index.date == day]
        late = todays.index.time >= time(16, 0)
        bars = bars_for(day)
        if bars.empty:
            print("    no bars for this day")
            continue
        for kind in ("dark", "lit"):
            levels, away = to_levels(todays[~late & (todays.kind == kind)],
                                     day, bars, 0)
            groups[kind] += levels
            away_total += away
        # The close is the close: no point asking whether the after-hours
        # tape was at it.
        levels, _ = to_levels(todays[late], day, bars, 0, check_market=False)
        groups["close"] += levels

    print("\n" + "=" * 64)
    print(f"  {away_total} session print(s) away from the market (late or "
          f"average-price reports, left out)")
    rng = random.Random(SEED)
    print_results("Dark pool, during the session", groups["dark"],
                  score(groups["dark"], bars_for, rng))
    print_results("Lit (exchange) blocks, during the session", groups["lit"],
                  score(groups["lit"], bars_for, rng))
    print_results("After 16:00 (mostly at the closing price)", groups["close"],
                  score(groups["close"], bars_for, rng, overnight=True),
                  overnight=True)
    print("=" * 64)
    return 0


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

def _bars(rows: Sequence[Tuple[float, float, float]], first: datetime,
          gaps: Sequence[int] = ()) -> pd.DataFrame:
    """(low, high, close) per minute; `gaps` are minute offsets to skip."""
    stamps, minute = [], 0
    for _ in rows:
        while minute in gaps:
            minute += 1
        stamps.append(first + timedelta(minutes=minute))
        minute += 1
    lows, highs, closes = zip(*rows)
    return pd.DataFrame({"low": lows, "high": highs, "close": closes,
                         "open": closes}, index=pd.DatetimeIndex(stamps))


def self_test() -> int:
    print("Self-test: checking the study's logic offline...\n")
    failures: List[str] = []
    t0 = datetime(2026, 10, 14, 10, 0, tzinfo=ET)

    # Leaves 100 upward, comes back, bounces 0.3%: support held.
    held = _bars([(100.0, 100.1, 100.05), (100.3, 100.5, 100.4),
                  (99.95, 100.2, 100.1), (100.1, 100.4, 100.35)], t0)
    if first_test(held, 100.0, t0 - timedelta(minutes=1)) != "held":
        failures.append("a bounce off the level from above must count as held")
    # Same approach, carries 0.3% through: broke.
    broke = _bars([(100.3, 100.5, 100.4), (99.95, 100.2, 100.0),
                   (99.6, 99.9, 99.65)], t0)
    if first_test(broke, 100.0, t0 - timedelta(minutes=1)) != "broke":
        failures.append("carrying through the level must count as broke")
    # Both in one bar: the cautious reading.
    both = _bars([(100.3, 100.5, 100.4), (99.95, 100.2, 100.0),
                  (99.6, 100.4, 100.0)], t0)
    if first_test(both, 100.0, t0 - timedelta(minutes=1)) != "broke":
        failures.append("a bar reaching both outcomes must count as broke")
    # From below and turned back down: resistance held.
    below = _bars([(99.5, 99.7, 99.6), (99.8, 100.05, 99.9),
                   (99.6, 99.8, 99.65)], t0)
    if first_test(below, 100.0, t0 - timedelta(minutes=1)) != "held":
        failures.append("a rejection from below must count as held")
    # Never went away first: touching without leaving is not a test.
    hugging = _bars([(99.95, 100.05, 100.0), (99.9, 100.1, 100.0)], t0)
    if first_test(hugging, 100.0, t0 - timedelta(minutes=1)) is not None:
        failures.append("price that never left the level has not returned")
    # No look-ahead: the test before the print must not be credited.
    if first_test(held, 100.0, t0 + timedelta(minutes=2)) is not None:
        failures.append("bars at or before the print must not be read")
    # Undecided within the window.
    slow = _bars([(100.3, 100.5, 100.4), (99.95, 100.2, 100.1)]
                 + [(100.0, 100.2, 100.1)] * 40, t0)
    if first_test(slow, 100.0, t0 - timedelta(minutes=1)) is not None:
        failures.append("no outcome within the window must be undecided")

    # Prints into levels: clustered, and away-from-market kept out.
    day_bars = _bars([(153.70, 154.00, 153.90)] * 10, t0)
    prints = pd.DataFrame(
        {"price": [153.88, 153.89, 153.88, 155.00, 151.00, 153.88],
         "size": [100_000, 50_000, 60_000, 40_000, 100_000, 1_000],
         "conditions": [""] * 6},
        index=pd.DatetimeIndex([t0 + timedelta(minutes=m)
                                for m in (0, 1, 2, 3, 4, 5)]))
    levels, away = to_levels(prints, t0.date(), day_bars, 5_000_000)
    if len(levels) != 1 or levels[0].prints != 3 or away != 2:
        failures.append(f"three prints at one price are one level; two away "
                        f"from the market are left out; a small print is not "
                        f"a block: {levels} away={away}")

    # Controls never sit on the level itself.
    rng = random.Random(1)
    near = [abs(p / 100.0 - 1) * 100 for p in control_prices(100.0, 500, rng)]
    if min(near) < AWAY - 1e-9 or max(near) > SPAN + 1e-9:
        failures.append("random prices must fall between AWAY and SPAN away")

    # Horizons are clock time: a missing minute does not stretch them.
    gappy = _bars([(1, 1, 100.0), (1, 1, 101.0), (1, 1, 102.0),
                   (1, 1, 103.0)], t0, gaps=(1, 2))
    # bars at 10:00, 10:03, 10:04, 10:05 -> 3 minutes after 10:00 is 101
    if move_after(gappy, t0, 3) != 1.0:
        failures.append(f"the 3-minute move must read the 10:03 close: "
                        f"{move_after(gappy, t0, 3)}")
    if move_after(gappy, t0, 30) is not None:
        failures.append("a horizon past the day's data must be unknown")

    # The verdict refuses small samples and noise.
    if not verdict(Tally(5, 5), Tally(50, 50)).startswith("too few"):
        failures.append("under MIN_SAMPLE must say too few")
    if not verdict(Tally(28, 22), Tally(130, 120)).startswith("no difference"):
        failures.append("56% against 52% on these counts is noise")
    if "MORE" not in verdict(Tally(80, 20), Tally(250, 250)):
        failures.append("80% against 50% on 100 tests is a difference")

    # A FlowAlgo export: duplicates dropped, other symbols and kinds left
    # out, times on the exchange's clock.
    import os
    import tempfile
    sample = (
        '"Date","Time","Ticker","Quantity","Spot","$MM","Notional Val","Type"\n'
        '"10/13/26","16:27:00.076","SPCX","749,363","156.54","$117M","$117,305,284","Dark Pool"\n'
        '"10/13/26","13:39:51.680","SPCX","472,500","157.20","$74M","$74,277,000","Dark Pool"\n'
        '"10/13/26","13:39:51.680","SPCX","472,500","157.20","$74M","$74,277,000","Dark Pool"\n'
        '"10/9/26","09:42:09.439","SPCX","198,794","164.55","$33M","$32,711,553","Lit Pool"\n'
        '"10/9/26","09:42:09.439","NVDA","198,794","164.55","$33M","$32,711,553","Dark Pool"\n')
    handle, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(handle, "w") as out:
        out.write(sample)
    try:
        flow = read_flowalgo(path, "SPCX")
    finally:
        os.remove(path)
    p = flow.prints
    if (flow.rows, flow.duplicates, flow.other, len(p)) != (5, 1, 1, 3):
        failures.append(f"export: 5 rows, 1 duplicate, 1 other symbol, 3 "
                        f"kept: {(flow.rows, flow.duplicates, flow.other, len(p))}")
    elif (p.index[0] != pd.Timestamp("2026-10-09 09:42:09.439", tz=ET)
          or list(p.kind) != ["lit", "dark", "dark"]
          or p["size"].iloc[-1] != 749_363 or p["price"].iloc[-1] != 156.54):
        failures.append(f"export rows misread: {p!r}")

    # A print at the close is tested on the next session, skipping the
    # weekend and any day without bars.
    friday = date(2026, 10, 9)
    tuesday_bars = _bars([(100.3, 100.5, 100.4), (99.95, 100.2, 100.1),
                          (100.1, 100.4, 100.35)],
                         datetime(2026, 10, 13, 9, 30, tzinfo=ET))
    asked = []

    def fake_bars(day):
        asked.append(day)
        return tuesday_bars if day == date(2026, 10, 13) else pd.DataFrame()
    found, _ = next_session(friday, fake_bars)
    if found != date(2026, 10, 13) or date(2026, 10, 10) in asked:
        failures.append(f"next session after a Friday with Monday empty is "
                        f"Tuesday, never Saturday: {found} asked {asked}")
    close = Level(friday, datetime(2026, 10, 9, 16, 5, tzinfo=ET), 100.0, 1,
                  1, 1.0)
    res = score([close], fake_bars, random.Random(1), overnight=True)
    if (res.printed.held, res.printed.broke) != (1, 0) or res.after_print[15]:
        failures.append(f"a closing print is tested on the next session, "
                        f"with no aftermath: {res}")

    print(f"  Level test (held/broke/both/undecided) : "
          f"{'ok' if not failures else 'see below'}")
    print("  No look-ahead past the print           : checked")
    print("  Prints clustered into levels           : checked")
    print("  Late prints kept out                   : checked")
    print("  Horizons by clock, not bar count       : checked")
    print("  FlowAlgo export read, duplicates out   : checked")
    print("  Closing prints tested next session     : checked")
    print("  Trading client in this file            : none")
    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nAll checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--symbol", default=SYMBOL)
    parser.add_argument("--days", type=int, default=DAYS)
    parser.add_argument("--min-notional", type=float, default=MIN_NOTIONAL,
                        metavar="DOLLARS",
                        help=f"smallest print counted as a block (default "
                             f"{MIN_NOTIONAL:,})")
    parser.add_argument("--flowalgo", metavar="FILE",
                        help="test the prints in a FlowAlgo Equity Exporter "
                             "CSV instead of Alpaca's")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.days < 1 or args.min_notional <= 0:
        raise SystemExit("--days and --min-notional must be above 0")
    if args.flowalgo:
        return run_flowalgo(args.flowalgo, args.symbol.upper())
    return run(args.symbol.upper(), args.days, args.min_notional)


if __name__ == "__main__":
    raise SystemExit(main())
