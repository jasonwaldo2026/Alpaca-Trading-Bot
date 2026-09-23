"""
Open candles: the morning read out to your phone, five minutes at a time.

From 08:55 ET, one push as each 5-minute candle completes -- price, volume,
open, close, and both wick tips -- then a single summary of every candle in
order once the window closes. The point is to follow the open without
sitting in front of the screen.

It states what the candles did. It does not say what they mean: no signal,
no score, no suggestion. Reading the morning is the job you are keeping
for yourself.

READ-ONLY. Market-data client only. No trading client, no order object.

    python open_candles.py                        # live, 08:55-10:00 ET
    python open_candles.py --until 11:00
    python open_candles.py --replay 2026-09-18    # any past session, SIP
    python open_candles.py --dry-run              # print, do not send
    python open_candles.py --self-test            # check the logic offline

Setup
-----
    pip install alpaca-py pandas python-dotenv

    ALPACA_API_KEY=...
    ALPACA_SECRET_KEY=...
    ALPACA_DATA_FEED=sip          # optional; see the note below
    PUSHOVER_APP_TOKEN=...
    PUSHOVER_USER_KEY=...

A note on the pre-market half of this window. Alpaca's free IEX feed is
one exchange and carries very little before 09:30 -- measured at 0 to 17
one-minute bars a day against SIP's 300-plus. So 08:55 to 09:30 will be
sparse or empty on the free feed, and the volume figures in it are a
small sample of the real tape. Replay always uses SIP, which the free
plan serves historically, so a past morning reads in full.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import time as time_mod
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional

import pandas as pd

from feed_check import ET, load_credentials, parse_clock, trading_days
from spcx_alert import open_db, send_pushover

SYMBOL = "SPCX"
BAR_MINUTES = 5
WINDOW_START = time(8, 55)
WINDOW_END = time(10, 0)

#: Sessions used to build the "usual volume at this time of day" baseline.
#: A rolling average across a handful of morning candles would compare
#: 09:35 against 09:30 -- two different animals. The same clock slot on
#: previous days is the honest comparison.
BASELINE_SESSIONS = 10

DB_PATH = "spcx_alerts.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    id        INTEGER PRIMARY KEY,
    symbol    TEXT NOT NULL,
    bar_time  TEXT NOT NULL,
    minutes   INTEGER NOT NULL,
    open      REAL, high REAL, low REAL, close REAL,
    volume    INTEGER,
    trades    INTEGER,
    vol_ratio REAL,
    sent_at   TEXT,
    UNIQUE (symbol, bar_time, minutes)
);
"""


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------

def _client():
    from alpaca.data.historical import StockHistoricalDataClient

    return StockHistoricalDataClient(*load_credentials())


def fetch_candles(symbol: str, start: datetime, end: datetime,
                  force_sip: bool = False) -> pd.DataFrame:
    """5-minute bars between two moments, extended hours included."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    feed = os.getenv("ALPACA_DATA_FEED", "").strip().lower()
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(BAR_MINUTES, TimeFrameUnit.Minute),
        start=start,
        end=end,
        feed=DataFeed.SIP if (force_sip or feed == "sip") else DataFeed.IEX,
    )
    frame = _client().get_stock_bars(request).df
    if frame is None or frame.empty:
        return pd.DataFrame()
    if isinstance(frame.index, pd.MultiIndex):
        frame = frame.xs(symbol, level="symbol")
    return frame.tz_convert(ET).sort_index()


def completed_only(frame: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Drop the candle still being built.

    A 5-minute candle stamped 09:35 is not finished until 09:40. Reading it
    early means reading a price that has not finished happening -- it can
    still reverse before it closes.
    """
    if frame.empty:
        return frame
    minute = now.minute - (now.minute % BAR_MINUTES)
    boundary = now.replace(minute=minute, second=0, microsecond=0)
    return frame[frame.index < boundary]


def volume_baseline(symbol: str, day: date, slots: List[time]) -> Dict[time, float]:
    """Median volume for each clock slot over recent sessions.

    Returns an empty mapping if history cannot be had; the caller then
    simply reports volume without a comparison rather than inventing one.
    """
    baseline: Dict[time, List[float]] = {slot: [] for slot in slots}
    for past in trading_days(day - timedelta(days=1), BASELINE_SESSIONS):
        try:
            frame = fetch_candles(
                symbol,
                datetime.combine(past, WINDOW_START, tzinfo=ET) - timedelta(minutes=BAR_MINUTES),
                datetime.combine(past, time(16, 0), tzinfo=ET),
                force_sip=True,
            )
        except Exception:  # noqa: BLE001 -- a baseline is a nicety, not a requirement
            continue
        for stamp, row in frame.iterrows():
            if stamp.time() in baseline:
                baseline[stamp.time()].append(float(row["volume"]))
    return {slot: statistics.median(values) for slot, values in baseline.items() if values}


# --------------------------------------------------------------------------
# Words
# --------------------------------------------------------------------------

def money(value: float) -> str:
    return f"${value:,.2f}"


def cents(value: float) -> str:
    return f"{round(value * 100):.0f}¢"


def thousands(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{value:.0f}"


@dataclass
class Candle:
    at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: Optional[float] = None
    usual_volume: Optional[float] = None

    @property
    def up(self) -> bool:
        return self.close >= self.open

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def vol_ratio(self) -> Optional[float]:
        if not self.usual_volume:
            return None
        return self.volume / self.usual_volume


def describe(symbol: str, candle: Candle) -> str:
    """One candle, spelled out. Sent as its five minutes close."""
    arrow = "▲" if candle.up else "▼"
    change = candle.close - candle.open
    lines = [
        f"{symbol} {candle.at:%H:%M} {arrow} {money(candle.close)} "
        f"({'+' if change >= 0 else ''}{cents(change)})",
        f"Open {money(candle.open)}   Close {money(candle.close)}",
        f"High {money(candle.high)}   Low {money(candle.low)}",
        f"Upper wick {cents(candle.upper_wick)}   "
        f"Lower wick {cents(candle.lower_wick)}   Body {cents(candle.body)}",
    ]
    volume = f"Volume {thousands(candle.volume)}"
    if candle.vol_ratio is not None:
        volume += f" ({candle.vol_ratio:.1f}× usual for {candle.at:%H:%M})"
    if candle.trades:
        volume += f" in {int(candle.trades):,} trades"
    lines.append(volume)
    if candle.at.time() < time(9, 30):
        lines.append("pre-market")
    return "\n".join(lines)


def summarise(symbol: str, candles: List[Candle]) -> str:
    """Every candle of the window, in order, on one line each."""
    if not candles:
        return f"{symbol}: no candles in the window."

    first, last = candles[0], candles[-1]
    move = last.close - first.open
    header = (
        f"{symbol} {first.at:%a %d %b} · {len(candles)} candles "
        f"{first.at:%H:%M}-{(last.at + timedelta(minutes=BAR_MINUTES)):%H:%M}",
        f"{money(first.open)} → {money(last.close)}  "
        f"({'+' if move >= 0 else ''}{cents(move)}, "
        f"{'+' if move >= 0 else ''}{100 * move / first.open:.2f}%)",
        f"High {money(max(c.high for c in candles))}  "
        f"Low {money(min(c.low for c in candles))}  "
        f"Volume {thousands(sum(c.volume for c in candles))}",
        "",
    )
    rows = [
        f"{c.at:%H:%M} {'▲' if c.up else '▼'} "
        f"{c.open:,.2f}→{c.close:,.2f}  "
        f"H {c.high:,.2f} L {c.low:,.2f}  {thousands(c.volume)}"
        for c in candles
    ]
    return "\n".join([*header, *rows])


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def remember(db: sqlite3.Connection, symbol: str, candle: Candle, sent: bool) -> bool:
    """Write one candle. False if this one was already recorded."""
    try:
        db.execute(
            """INSERT INTO candles
               (symbol, bar_time, minutes, open, high, low, close, volume,
                trades, vol_ratio, sent_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, candle.at.isoformat(), BAR_MINUTES, candle.open, candle.high,
             candle.low, candle.close, int(candle.volume),
             int(candle.trades) if candle.trades else None,
             candle.vol_ratio,
             datetime.now(ET).isoformat() if sent else None),
        )
        db.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def to_candles(frame: pd.DataFrame, baseline: Dict[time, float]) -> List[Candle]:
    return [
        Candle(
            at=stamp,
            open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]),
            volume=float(row["volume"]),
            trades=float(row["trade_count"]) if "trade_count" in row else None,
            usual_volume=baseline.get(stamp.time()),
        )
        for stamp, row in frame.iterrows()
    ]


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------

def deliver(message: str, title: str, dry_run: bool) -> str:
    if dry_run:
        return "dry run"
    error = send_pushover(message, title=title)
    return error or "sent"


def run_replay(symbol: str, day: date, start: time, end: time,
               dry_run: bool, db: sqlite3.Connection) -> int:
    """Read a past morning. Always the full tape -- history is free on SIP."""
    frame = fetch_candles(
        symbol,
        datetime.combine(day, start, tzinfo=ET),
        datetime.combine(day, end, tzinfo=ET),
        force_sip=True,
    )
    if frame.empty:
        print(f"No candles for {symbol} on {day}. Market closed that day?")
        return 1

    slots = sorted({stamp.time() for stamp in frame.index})
    baseline = volume_baseline(symbol, day, slots)
    candles = to_candles(frame, baseline)

    for candle in candles:
        print(describe(symbol, candle))
        print()
        remember(db, symbol, candle, sent=False)

    print("=" * 56)
    print(summarise(symbol, candles))
    if not dry_run:
        print(f"\n[summary push: {deliver(summarise(symbol, candles), f'{symbol} replay', False)}]")
    return 0


def run_live(symbol: str, start: time, end: time, dry_run: bool,
             db: sqlite3.Connection) -> int:
    """Follow this morning, pushing each candle as it closes."""
    today = datetime.now(ET).date()
    window_start = datetime.combine(today, start, tzinfo=ET)
    window_end = datetime.combine(today, end, tzinfo=ET)

    slots, cursor = [], window_start
    while cursor < window_end:
        slots.append(cursor.time())
        cursor += timedelta(minutes=BAR_MINUTES)

    print(f"Baseline: median volume per slot over {BASELINE_SESSIONS} sessions...")
    try:
        baseline = volume_baseline(symbol, today, slots)
        print(f"  {len(baseline)} of {len(slots)} slots have history.\n")
    except Exception as exc:  # noqa: BLE001
        print(f"  unavailable ({type(exc).__name__}) — volumes will be raw.\n")
        baseline = {}

    feed = os.getenv("ALPACA_DATA_FEED", "").strip().lower() or "iex"
    print(f"{symbol} · {BAR_MINUTES}-minute candles · {start:%H:%M}-{end:%H:%M} ET · {feed} feed")
    if feed != "sip" and start < time(9, 30):
        print("Note: IEX carries very little before 09:30, so the pre-market")
        print("candles may be sparse or missing. Replay uses the full tape.\n")

    seen, collected = set(), []
    try:
        while True:
            now = datetime.now(ET)
            if now >= window_end + timedelta(minutes=BAR_MINUTES):
                break
            if now >= window_start:
                try:
                    frame = completed_only(
                        fetch_candles(symbol, window_start, min(now, window_end)), now)
                except Exception as exc:  # noqa: BLE001 -- one bad minute is not the morning
                    print(f"  {now:%H:%M}  error: {type(exc).__name__}: {exc}")
                    frame = pd.DataFrame()

                for candle in to_candles(frame, baseline):
                    if candle.at in seen:
                        continue
                    seen.add(candle.at)
                    collected.append(candle)
                    message = describe(symbol, candle)
                    fresh = remember(db, symbol, candle, sent=not dry_run)
                    status = deliver(message, f"{symbol} {candle.at:%H:%M}", dry_run) \
                        if fresh else "already recorded"
                    print(message)
                    print(f"  [{status}]\n")
            time_mod.sleep(max(5, 20 - datetime.now(ET).second % 20))
    except KeyboardInterrupt:
        print("\nStopped early.")

    if collected:
        text = summarise(symbol, collected)
        print("=" * 56)
        print(text)
        print(f"\n[summary push: {deliver(text, f'{symbol} morning', dry_run)}]")
    else:
        print("No candles collected.")
    return 0


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

def self_test() -> int:
    """Check the arithmetic and the wording offline. No network, no keys."""
    print("Self-test: checking candle maths and messages...\n")
    failures = []

    at = datetime.combine(date(2026, 9, 18), time(9, 35), tzinfo=ET)
    # A candle that opened low, ran up, was pushed back, and closed mid-body.
    up = Candle(at=at, open=152.18, high=152.63, low=152.05, close=152.41,
                volume=84_200, trades=612, usual_volume=46_000)

    if abs(up.upper_wick - 0.22) > 1e-9:
        failures.append(f"upper wick should be 22c, got {up.upper_wick}")
    if abs(up.lower_wick - 0.13) > 1e-9:
        failures.append(f"lower wick should be 13c, got {up.lower_wick}")
    if abs(up.body - 0.23) > 1e-9:
        failures.append(f"body should be 23c, got {up.body}")
    if not up.up:
        failures.append("a candle closing above its open is an up candle")
    if abs(up.vol_ratio - 84_200 / 46_000) > 1e-9:
        failures.append("volume ratio should divide by the usual volume")

    down = Candle(at=at + timedelta(minutes=BAR_MINUTES), open=152.41, high=152.44,
                  low=151.90, close=152.02, volume=51_000)
    if down.up:
        failures.append("a candle closing below its open is a down candle")
    if abs(down.upper_wick - 0.03) > 1e-9:
        failures.append(f"down-candle upper wick should be 3c, got {down.upper_wick}")
    if abs(down.lower_wick - 0.12) > 1e-9:
        failures.append(f"down-candle lower wick should be 12c, got {down.lower_wick}")
    if down.vol_ratio is not None:
        failures.append("with no history there should be no ratio, not a made-up one")

    # Wicks and body must account for the whole range, both directions.
    for candle in (up, down):
        span = candle.upper_wick + candle.body + candle.lower_wick
        if abs(span - (candle.high - candle.low)) > 1e-9:
            failures.append(f"wicks plus body should equal the range for {candle.at:%H:%M}")
        if candle.upper_wick < 0 or candle.lower_wick < 0:
            failures.append("a wick cannot be negative")

    message = describe("SPCX", up)
    for needed in ("Open", "Close", "High", "Low", "Upper wick", "Lower wick", "Volume"):
        if needed not in message:
            failures.append(f"the message should carry {needed!r}")
    if "pre-market" in message:
        failures.append("09:35 is not pre-market")
    early = Candle(at=at.replace(hour=8, minute=55), open=152.0, high=152.1,
                   low=151.9, close=152.05, volume=1_200)
    if "pre-market" not in describe("SPCX", early):
        failures.append("08:55 should be marked pre-market")

    # The forming candle must never survive.
    index = pd.DatetimeIndex([at + timedelta(minutes=5 * i) for i in range(4)])
    frame = pd.DataFrame({"open": [1.0] * 4, "high": [1.0] * 4, "low": [1.0] * 4,
                          "close": [1.0] * 4, "volume": [1] * 4}, index=index)
    now = at + timedelta(minutes=17)          # 09:52, so 09:50 is still forming
    kept = completed_only(frame, now)
    if len(kept) != 3 or kept.index[-1].time() != time(9, 45):
        failures.append(f"the forming candle leaked: kept {[str(t.time()) for t in kept.index]}")

    text = summarise("SPCX", [up, down])
    if text.count("\n") < 4 or "2 candles" not in text:
        failures.append("the summary should head with a count and list each candle")

    source = open(__file__, encoding="utf-8").read()
    for forbidden in ("TradingClient", "submit_order", "MarketOrderRequest"):
        if forbidden in source.replace(f'"{forbidden}"', ""):
            failures.append(f"this file must not mention {forbidden}")

    print(describe("SPCX", up))
    print()
    print(summarise("SPCX", [up, down]))
    print("\n  Forming candle dropped         : yes (kept through 09:45 at 09:52)")
    print("  Wicks + body = range           : both candles")
    print("  Trading client in this file    : none")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nAll checks passed.")
    return 0


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Read the morning's candles to your phone.")
    parser.add_argument("--symbol", default=SYMBOL)
    parser.add_argument("--from", dest="start", default=f"{WINDOW_START:%H:%M}",
                        help=f"Window start, ET (default {WINDOW_START:%H:%M})")
    parser.add_argument("--until", dest="end", default=f"{WINDOW_END:%H:%M}",
                        help=f"Window end, ET (default {WINDOW_END:%H:%M})")
    parser.add_argument("--replay", metavar="YYYY-MM-DD", help="Read a past session instead")
    parser.add_argument("--dry-run", action="store_true", help="Print, do not send")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    start, end = parse_clock(args.start), parse_clock(args.end)
    if start >= end:
        raise SystemExit(f"--from {start:%H:%M} must be before --until {end:%H:%M}")

    db = open_db(args.db)
    ensure_schema(db)
    symbol = args.symbol.upper()

    if args.replay:
        day = datetime.strptime(args.replay, "%Y-%m-%d").date()
        return run_replay(symbol, day, start, end, args.dry_run, db)
    return run_live(symbol, start, end, args.dry_run, db)


if __name__ == "__main__":
    raise SystemExit(main())
