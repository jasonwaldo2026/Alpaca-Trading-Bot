"""
Open candles: the session read out to your phone, minute by minute.

The day runs in two phases. From 09:25 to 10:00 the phone gets
everything. After 10:00 the readings keep being taken, recorded and
drawn into the PDF, but only a volume spike is worth interrupting a
working day for. `--detail-until` moves the line.

In the detail phase:

  * every minute, a quiet update -- price, that minute's volume against
    what that minute usually carries, how the 5-minute candle is shaping
    up so far, and which way the volume is leaning;
  * every five minutes, an alarm -- the completed candle in full, with
    open, close and both wick tips;
  * at the end of the window, one summary listing every candle in order.

The minute updates go out at Pushover priority -1: they arrive without a
sound and sit in the tray to be glanced at. The five-minute summaries go
out at priority 1, which sounds through a focus mode. Sixty-five buzzing
notifications in sixty-five minutes would train you to ignore all of
them, alarms included.

It states what the tape did. It does not say what it means: no signal, no
score, no suggestion. Reading the morning is the job you are keeping.

About "volume leaning"
----------------------
Bars do not carry signed order flow -- Alpaca sells trades and quotes for
that, and this reads neither. What it computes instead is where each
minute CLOSED inside its own range, weighted by that minute's volume: a
minute that closes at its high with heavy volume says buyers took the
range, one that closes at its low says sellers did. Over five minutes
that is a reasonable read of who is winning, and it is not the same
thing astrue buy/sell delta. Treated as a hint, it is useful; treated as
fact, it will mislead you.

READ-ONLY. Market-data client only. No trading client, no order object.

    python open_candles.py                        # live, 09:25-16:00 ET
    python open_candles.py --detail-until 10:30   # move the quiet line
    python open_candles.py --until 11:00          # stop early
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

A note on the pre-market five minutes of this window. Alpaca's free IEX feed is
one exchange and carries very little before 09:30 -- measured at 0 to 17
one-minute bars a day against SIP's 300-plus. Minutes with no trades are
skipped rather than pushed as "nothing happened" (pass --push-empty to
send them anyway). Replay always uses SIP, which the free plan serves
historically, so a past morning reads in full.
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

import lockups
from feed_check import ET, load_credentials, load_env, parse_clock, trading_days
from spcx_alert import (
    PRIORITY_SUMMARY,
    PRIORITY_UPDATE,
    open_db,
    send_pushover,
)

SYMBOL = "SPCX"
BAR_MINUTES = 5
WINDOW_START = time(9, 25)
WINDOW_END = time(16, 0)

# Up to here the phone gets everything: a quiet line each minute and an
# alarm each candle. After it, only the unusual -- a volume spike -- is
# worth an interruption at a desk job. The readings keep being computed,
# recorded and drawn into the PDF either way; what changes is whether
# they buzz. Seventy-nine routine alarms a day is how an alert channel
# gets ignored, and an alert you ignore is worse than one never built.
DETAIL_UNTIL = time(10, 0)

#: A candle or minute carrying this many times its usual volume for that
#: clock slot breaks through the quiet channel and sounds the alarm. The
#: rest of the stream stays silent, so a spike is the thing that gets
#: attention rather than one more line in a tray.
#:
#: 1.5 is a starting point, not a finding. On 18 September the busiest
#: 5-minute candle of the morning ran 1.4x, so this would have stayed
#: silent all session -- lower it to see more, raise it to see less, and
#: let a week of real mornings decide. Minute volume swings harder than
#: candle volume, so the same multiple fires more often on minutes.
VOLUME_ALERT_MULTIPLE = 1.5

#: Minutes of one-minute bars behind the moment, used to read which way
#: volume is leaning. Five matches the candle, so the reading and the
#: candle describe the same stretch of tape.
PRESSURE_MINUTES = 5

#: Sessions used to build the "usual volume at this time of day" baseline.
#: A rolling average across a handful of morning candles would compare
#: 09:35 against 09:30 -- two different animals. The same clock slot on
#: previous days is the honest comparison.
BASELINE_SESSIONS = 10

DB_PATH = "spcx_alerts.db"

#: Minutes between rebuilds of the session PDF while the market is open,
#: so the file on disk is never far behind the phone. 0 turns it off.
PDF_EVERY_MINUTES = 15

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



MINUTE_SCHEMA = """
CREATE TABLE IF NOT EXISTS minutes (
    id        INTEGER PRIMARY KEY,
    symbol    TEXT NOT NULL,
    bar_time  TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume    INTEGER,
    trades    INTEGER,
    vol_ratio REAL,
    lean      REAL,
    sent_at   TEXT,
    UNIQUE (symbol, bar_time)
);
"""


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------

def _client():
    from alpaca.data.historical import StockHistoricalDataClient

    return StockHistoricalDataClient(*load_credentials())


def fetch_minutes(symbol: str, start: datetime, end: datetime,
                  force_sip: bool = False) -> pd.DataFrame:
    """One-minute bars, extended hours included.

    Everything is built from minutes: the per-minute update reads them
    directly and the 5-minute candle is aggregated from them, so both
    cadences describe the same bars rather than two separate requests
    that could disagree at the edges.
    """
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    feed = os.getenv("ALPACA_DATA_FEED", "").strip().lower()
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
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


def aggregate(frame: pd.DataFrame, minutes: int = BAR_MINUTES) -> pd.DataFrame:
    """Roll one-minute bars into candles aligned to the clock.

    `label="left"` and `closed="left"` put the 09:30-09:34 minutes into a
    candle stamped 09:30, which is how every charting platform draws it.
    """
    if frame.empty:
        return frame
    how = {"open": "first", "high": "max", "low": "min",
           "close": "last", "volume": "sum"}
    if "trade_count" in frame.columns:
        how["trade_count"] = "sum"
    rolled = frame.resample(f"{minutes}min", label="left", closed="left").agg(how)
    return rolled.dropna(subset=["open"])


def drop_forming(frame: pd.DataFrame, now: datetime, minutes: int) -> pd.DataFrame:
    """Remove the bar still being built.

    A candle stamped 09:35 is not finished until 09:40. Reading it early
    reads a price that has not finished happening -- it can still reverse
    before it closes.
    """
    if frame.empty:
        return frame
    edge = now.replace(minute=now.minute - (now.minute % minutes),
                       second=0, microsecond=0)
    return frame[frame.index < edge]


def volume_baselines(symbol: str, day: date,
                     force_sip: bool = False) -> tuple:
    """Median volume per clock slot, for minutes and for 5-minute candles.

    Both come from one pass over the same history. The comparison is
    against the SAME slot on previous sessions, never a rolling average of
    the morning so far -- 09:35 and 09:30 are different animals at the
    open, and averaging across them is what made an earlier relative-
    volume rule fire in the deadest hours of the day.

    `force_sip` must match the feed the readings come from. SIP is the
    whole tape and IEX is one venue carrying a fraction of it, so a
    baseline built on one and compared against the other is not a ratio
    at all: it reads near zero however busy the market is, and the spike
    alarm can never fire. Like against like, or not at all.
    """
    per_minute: Dict[time, List[float]] = {}
    per_candle: Dict[time, List[float]] = {}
    for past in trading_days(day - timedelta(days=1), BASELINE_SESSIONS):
        try:
            minutes = fetch_minutes(
                symbol,
                datetime.combine(past, time(4, 0), tzinfo=ET),
                datetime.combine(past, time(16, 0), tzinfo=ET),
                force_sip=force_sip,
            )
        except Exception:  # noqa: BLE001 -- a baseline is a nicety, not a requirement
            continue
        if minutes.empty:
            continue
        for stamp, row in minutes.iterrows():
            per_minute.setdefault(stamp.time(), []).append(float(row["volume"]))
        for stamp, row in aggregate(minutes).iterrows():
            per_candle.setdefault(stamp.time(), []).append(float(row["volume"]))

    return (
        {slot: statistics.median(v) for slot, v in per_minute.items() if v},
        {slot: statistics.median(v) for slot, v in per_candle.items() if v},
    )


# --------------------------------------------------------------------------
# Which way the volume is leaning
# --------------------------------------------------------------------------

@dataclass
class Lean:
    """Where price closed within each minute's range, weighted by volume.

    Not order flow. Bars carry no buy/sell tag, so this cannot be a true
    delta -- what it measures is whether the heavy minutes closed near
    their highs or near their lows. A hint about who is winning the
    range, and nothing stronger.
    """

    score: float          # 0 = every heavy minute closed on its low, 1 = on its high
    up_volume: float
    down_volume: float
    minutes: int
    volume_ratio: Optional[float] = None   # this stretch against its usual

    #: Score bands, low to high, and what each is called. The words are
    #: active rather than adjectival -- "pressing" says what participants
    #: are doing, where "strong" only says how much. They describe the
    #: tape and never advise: this file states what happened, and the
    #: judgement stays with whoever is reading it.
    BANDS = (
        (0.18, "sellers in control", "↓"),
        (0.30, "sellers pressing", "↓"),
        (0.42, "sellers showing up", "↓"),
        (0.58, "balanced", "→"),
        (0.70, "buyers showing up", "↑"),
        (0.82, "buyers pressing", "↑"),
        (1.01, "buyers in control", "↑"),
    )

    #: "Taking it" is the only word gated on participation as well as
    #: direction, and it uses the same multiple as the spike alarm -- so
    #: the loudest word and the noise the phone makes mean one thing.
    #:
    #: The gate exists because the score cannot tell size. A minute of
    #: three hundred shares that closed on its high scores the same 100
    #: as a minute of two million that did. Without it the loudest word
    #: in the vocabulary would eventually land on a dead minute and read
    #: like a reason to act.
    TAKING_SCORE = 0.18
    TAKING_VOLUME = VOLUME_ALERT_MULTIPLE

    @property
    def _band(self) -> tuple:
        for edge, word, arrow in self.BANDS:
            if self.score < edge:
                return word, arrow
        return self.BANDS[-1][1], self.BANDS[-1][2]

    @property
    def taking(self) -> bool:
        """At an extreme, and on real participation."""
        if self.volume_ratio is None or self.volume_ratio < self.TAKING_VOLUME:
            return False
        return self.score <= self.TAKING_SCORE or self.score >= 1 - self.TAKING_SCORE

    @property
    def word(self) -> str:
        if self.taking:
            return "buyers taking it" if self.score >= 0.5 else "sellers taking it"
        return self._band[0]

    @property
    def arrow(self) -> str:
        if self.taking:
            return "↑↑" if self.score >= 0.5 else "↓↓"
        return self._band[1]

    def __str__(self) -> str:
        share = self.up_volume + self.down_volume
        split = ""
        if share:
            split = f" · {100 * self.up_volume / share:.0f}% of volume on up minutes"
        return (f"Volume leaning {self.word} {self.arrow} "
                f"({self.score * 100:.0f}/100 over {self.minutes} min){split}")


def read_lean(frame: pd.DataFrame, minutes: int = PRESSURE_MINUTES,
              baseline: Optional[Dict[time, float]] = None) -> Optional[Lean]:
    """Volume-weighted close position across the last `minutes` bars.

    Given a per-slot baseline, the reading also carries how busy that
    stretch was against its usual -- measured over the same window the
    score covers, not over the last minute alone, so the two halves of
    the word describe one piece of tape.
    """
    window = frame.tail(minutes)
    if window.empty:
        return None
    total = float(window["volume"].sum())
    if total <= 0:
        return None

    weighted = 0.0
    up = down = 0.0
    for _, row in window.iterrows():
        high, low = float(row["high"]), float(row["low"])
        volume = float(row["volume"])
        # A minute with no range is a minute with no opinion.
        position = 0.5 if high <= low else (float(row["close"]) - low) / (high - low)
        weighted += position * volume
        if float(row["close"]) > float(row["open"]):
            up += volume
        elif float(row["close"]) < float(row["open"]):
            down += volume

    ratio = None
    if baseline:
        usual = sum(baseline.get(stamp.time(), 0.0) for stamp in window.index)
        if usual > 0:
            ratio = total / usual

    return Lean(score=weighted / total, up_volume=up, down_volume=down,
                minutes=len(window), volume_ratio=ratio)


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


def is_spike(candle: Candle, multiple: float = VOLUME_ALERT_MULTIPLE) -> bool:
    """Did this bar carry unusual volume for its time of day?

    Unusual means against the SAME clock slot on recent sessions, never a
    rolling average of today -- 09:35 and 14:35 are different animals, and
    comparing them is how "busy" quietly comes to mean "is it morning".
    With no history there is no claim to make, so it is not a spike.
    """
    return candle.vol_ratio is not None and candle.vol_ratio >= multiple


def spike_line(candle: Candle) -> str:
    return (f"** VOLUME {candle.vol_ratio:.1f}x usual for "
            f"{candle.at:%H:%M} **")


def describe_minute(symbol: str, minute: Candle, forming: Optional[Candle],
                    lean: Optional[Lean],
                    multiple: float = VOLUME_ALERT_MULTIPLE) -> str:
    """The once-a-minute update. Quiet, unless the volume is not."""
    arrow = "▲" if minute.up else "▼"
    change = minute.close - minute.open
    volume = f"Minute volume {thousands(minute.volume)}"
    if minute.vol_ratio is not None:
        volume += f" ({minute.vol_ratio:.1f}× usual)"
    if minute.trades:
        volume += f" in {int(minute.trades):,} trades"

    lines = []
    if is_spike(minute, multiple):
        lines.append(spike_line(minute))
    lines += [
        f"{symbol} {minute.at:%H:%M} {arrow} {money(minute.close)} "
        f"({'+' if change >= 0 else ''}{cents(change)})",
        volume,
    ]
    if forming is not None:
        elapsed = int((minute.at - forming.at).total_seconds() // 60) + 1
        lines.append(
            f"Candle {forming.at:%H:%M} forming ({elapsed}/{BAR_MINUTES} min): "
            f"O {forming.open:,.2f}  H {forming.high:,.2f}  "
            f"L {forming.low:,.2f}  now {forming.close:,.2f}  "
            f"{thousands(forming.volume)}"
        )
    if lean is not None:
        lines.append(str(lean))
    if minute.at.time() < time(9, 30):
        lines.append("pre-market")
    return "\n".join(lines)


def describe(symbol: str, candle: Candle, lean: Optional[Lean] = None,
             multiple: float = VOLUME_ALERT_MULTIPLE) -> str:
    """The five-minute alarm: one completed candle, spelled out."""
    arrow = "▲" if candle.up else "▼"
    change = candle.close - candle.open
    lines = []
    if is_spike(candle, multiple):
        lines.append(spike_line(candle))
    lines += [
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
    if lean is not None:
        lines.append(str(lean))
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
    db.executescript(MINUTE_SCHEMA)


def remember(db: sqlite3.Connection, symbol: str, candle: Candle, sent: bool) -> bool:
    """Write one 5-minute candle. False if it was already recorded."""
    try:
        db.execute(
            """INSERT INTO candles
               (symbol, bar_time, minutes, open, high, low, close, volume,
                trades, vol_ratio, sent_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, candle.at.isoformat(), BAR_MINUTES, candle.open, candle.high,
             candle.low, candle.close, int(candle.volume),
             int(candle.trades) if candle.trades else None, candle.vol_ratio,
             datetime.now(ET).isoformat() if sent else None),
        )
        db.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def remember_minute(db: sqlite3.Connection, symbol: str, minute: Candle,
                    lean: Optional[Lean], sent: bool) -> bool:
    try:
        db.execute(
            """INSERT INTO minutes
               (symbol, bar_time, open, high, low, close, volume, trades,
                vol_ratio, lean, sent_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, minute.at.isoformat(), minute.open, minute.high, minute.low,
             minute.close, int(minute.volume),
             int(minute.trades) if minute.trades else None, minute.vol_ratio,
             lean.score if lean else None,
             datetime.now(ET).isoformat() if sent else None),
        )
        db.commit()
        return True
    except sqlite3.IntegrityError:
        return False


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------

def reaches_phone(at: time, spiked: bool, detail_until: time) -> bool:
    """Does this reading earn an interruption?

    Inside the detail window, everything does. Outside it, only a volume
    spike -- the rest is still computed, recorded and drawn, it just does
    not buzz. Written once because the minute loop and the candle loop
    must never disagree about it: two copies of a rule is two rules.
    """
    return spiked or at < detail_until


def deliver(message: str, title: str, priority: int, dry_run: bool,
            attachment: Optional[str] = None) -> str:
    if dry_run:
        return "dry run" + (" (chart drawn)" if attachment else "")
    return send_pushover(message, title=title, priority=priority,
                         attachment=attachment) or "sent"


def rebuild_report(symbol: str, day: date, start: time, end: time,
                   db_path: str, out_path: str,
                   live: bool = True) -> Optional[str]:
    """Rebuild the session PDF, and return page one as an image.

    Imported here rather than at the top because daily_report imports this
    module -- taking it at call time breaks the cycle without either file
    having to know about the other's import order.
    """
    try:
        import daily_report
    except ImportError:
        return None
    try:
        session = daily_report.gather(symbol, day, start, end, db_path,
                                      force_sip=not live)
        if session is None:
            return None
        daily_report.build(session, out_path)
        return daily_report.session_png(session, "spcx_latest.png")
    except Exception as exc:  # noqa: BLE001 -- a report must never stop the alerts
        print(f"  (report not rebuilt: {type(exc).__name__}: {exc})")
        return None


def run_replay(symbol: str, day: date, start: time, end: time,
               dry_run: bool, db: sqlite3.Connection,
               multiple: float = VOLUME_ALERT_MULTIPLE) -> int:
    """Read a past morning. Always the full tape -- history is free on SIP."""
    minutes = fetch_minutes(
        symbol,
        datetime.combine(day, start, tzinfo=ET),
        datetime.combine(day, end, tzinfo=ET),
        force_sip=True,
    )
    if minutes.empty:
        print(f"No bars for {symbol} on {day}. Market closed that day?")
        return 1

    # Replay reads the whole tape, so its baseline must come from it too.
    minute_base, candle_base = volume_baselines(symbol, day, force_sip=True)
    candles = to_candles(aggregate(minutes), candle_base)

    for candle in candles:
        upto = minutes[minutes.index < candle.at + timedelta(minutes=BAR_MINUTES)]
        print(describe(symbol, candle,
                       read_lean(upto, baseline=minute_base), multiple))
        print()
        remember(db, symbol, candle, sent=False)

    # How often would the spike alarm have sounded on this day? That is the
    # question a threshold can only be chosen by answering.
    spikes = [c for c in candles if is_spike(c, multiple)]
    print("=" * 56)
    if candles and candles[0].vol_ratio is None:
        print("  No baseline available, so no volume comparison was made.")
    else:
        print(f"  Volume alarm at {multiple:.1f}x: {len(spikes)} of {len(candles)} candles")
        for c in spikes:
            print(f"    {c.at:%H:%M}  {c.vol_ratio:.1f}x  {thousands(c.volume)}")
        busiest = max((c for c in candles if c.vol_ratio is not None),
                      key=lambda c: c.vol_ratio, default=None)
        if busiest is not None and not spikes:
            print(f"    busiest was {busiest.at:%H:%M} at {busiest.vol_ratio:.1f}x "
                  f"-- lower the threshold to catch it")
    print("=" * 56)
    text = summarise(symbol, candles)
    print(text)
    if not dry_run:
        print("\n[summary push: "
              f"{deliver(text, f'{symbol} replay', PRIORITY_SUMMARY, False)}]")
    return 0


def run_live(symbol: str, start: time, end: time, dry_run: bool,
             db: sqlite3.Connection, push_empty: bool = False,
             multiple: float = VOLUME_ALERT_MULTIPLE,
             pdf_every: int = PDF_EVERY_MINUTES,
             db_path: str = DB_PATH,
             detail_until: time = DETAIL_UNTIL) -> int:
    """Follow the session: full detail early, then only the unusual."""
    today = datetime.now(ET).date()
    window_start = datetime.combine(today, start, tzinfo=ET)
    window_end = datetime.combine(today, end, tzinfo=ET)
    pdf_path = f"{symbol}_{today:%Y%m%d}.pdf"

    print(f"Baselines: median volume per slot over {BASELINE_SESSIONS} sessions...")
    try:
        # The same feed the live readings come from, or the ratio is a
        # comparison between one venue and all of them.
        live_sip = os.getenv("ALPACA_DATA_FEED", "").strip().lower() == "sip"
        minute_base, candle_base = volume_baselines(symbol, today,
                                                    force_sip=live_sip)
        print(f"  {len(minute_base)} minute slots, {len(candle_base)} candle slots.\n")
    except Exception as exc:  # noqa: BLE001
        print(f"  unavailable ({type(exc).__name__}) — volumes will be raw.\n")
        minute_base, candle_base = {}, {}

    # The calendar before the tape. A known share unlock outweighs anything
    # the next six hours of minute bars will say, and it is the one thing
    # here that is knowable in advance.
    notice = lockups.headline(symbol, today)
    if notice:
        print("=" * 56)
        print(f"  {notice}")
        print("=" * 56 + "\n")

    feed = os.getenv("ALPACA_DATA_FEED", "").strip().lower() or "iex"
    print(f"{symbol} · {start:%H:%M}-{end:%H:%M} ET · {feed} feed")
    print(f"  {start:%H:%M}-{detail_until:%H:%M}  every minute → quiet update "
          f"(priority {PRIORITY_UPDATE}),")
    print(f"{'':16}every {BAR_MINUTES} min → alarm (priority {PRIORITY_SUMMARY})")
    print(f"  {detail_until:%H:%M}-{end:%H:%M}  volume spikes only; the rest is "
          f"recorded, not sent")
    if feed != "sip" and start < time(9, 30):
        print("  note: IEX carries very little before 09:30; empty minutes are")
        print("        skipped unless --push-empty.")
    print()

    seen_minutes, seen_candles, collected = set(), set(), []
    chart: Optional[str] = None
    last_pdf = datetime.now(ET) - timedelta(minutes=pdf_every or 0)
    try:
        while True:
            now = datetime.now(ET)
            if now >= window_end + timedelta(minutes=BAR_MINUTES):
                break
            if now < window_start:
                time_mod.sleep(min(30, max(1, (window_start - now).total_seconds())))
                continue

            try:
                minutes = fetch_minutes(symbol, window_start, min(now, window_end))
            except Exception as exc:  # noqa: BLE001 -- one bad minute is not the morning
                print(f"  {now:%H:%M}  error: {type(exc).__name__}: {exc}")
                minutes = pd.DataFrame()

            done_minutes = drop_forming(minutes, now, 1)

            # --- the quiet per-minute update ---------------------------
            for stamp, row in done_minutes.iterrows():
                if stamp in seen_minutes:
                    continue
                seen_minutes.add(stamp)
                minute = to_candles(done_minutes.loc[[stamp]], minute_base)[0]
                if minute.volume <= 0 and not push_empty:
                    print(f"  {stamp:%H:%M}  no trades — skipped")
                    continue

                upto = done_minutes[done_minutes.index <= stamp]
                lean = read_lean(upto, baseline=minute_base)

                # The candle this minute belongs to, as far as it has got.
                edge = stamp.replace(minute=stamp.minute - (stamp.minute % BAR_MINUTES),
                                     second=0, microsecond=0)
                part = upto[upto.index >= edge]
                forming = to_candles(aggregate(part), {})[0] if not part.empty else None
                if forming is not None and forming.at + timedelta(minutes=BAR_MINUTES) <= stamp:
                    forming = None

                # A spike leaves the silent channel. That is the whole
                # point of having two: the stream stays glanceable, and the
                # unusual minute is the one that makes a noise.
                spiked = is_spike(minute, multiple)
                # Past the detail window only the unusual leaves the machine.
                push = reaches_phone(stamp.time(), spiked, detail_until)
                message = describe_minute(symbol, minute, forming, lean, multiple)
                fresh = remember_minute(db, symbol, minute, lean,
                                        sent=push and not dry_run)
                title = (f"{symbol} {stamp:%H:%M} volume {minute.vol_ratio:.1f}x"
                         if spiked else f"{symbol} {stamp:%H:%M}")
                chart = None
                if spiked and pdf_every:
                    chart = rebuild_report(symbol, today, start, end,
                                           db_path, pdf_path)
                    last_pdf = datetime.now(ET)
                if not fresh:
                    status = "already recorded"
                elif not push:
                    status = "logged, quiet after " + f"{detail_until:%H:%M}"
                else:
                    status = deliver(message, title,
                                     PRIORITY_SUMMARY if spiked else PRIORITY_UPDATE,
                                     dry_run, attachment=chart)
                print(message)
                print(f"  [{status}]\n")

            # --- the five-minute alarm ---------------------------------
            for candle in to_candles(drop_forming(aggregate(minutes), now, BAR_MINUTES),
                                     candle_base):
                if candle.at in seen_candles:
                    continue
                seen_candles.add(candle.at)
                collected.append(candle)
                upto = done_minutes[done_minutes.index <
                                    candle.at + timedelta(minutes=BAR_MINUTES)]
                message = describe(symbol, candle,
                                   read_lean(upto, baseline=minute_base),
                                   multiple)
                candle_spike = is_spike(candle, multiple)
                push = reaches_phone(candle.at.time(), candle_spike,
                                     detail_until)
                fresh = remember(db, symbol, candle, sent=push and not dry_run)
                title = (f"{symbol} {candle.at:%H:%M} volume {candle.vol_ratio:.1f}x"
                         if candle_spike else f"{symbol} candle {candle.at:%H:%M}")
                chart = None
                if candle_spike and pdf_every:
                    chart = rebuild_report(symbol, today, start, end,
                                           db_path, pdf_path)
                    last_pdf = datetime.now(ET)
                if not fresh:
                    status = "already recorded"
                elif not push:
                    status = "logged, quiet after " + f"{detail_until:%H:%M}"
                else:
                    status = deliver(message, title, PRIORITY_SUMMARY, dry_run,
                                     attachment=chart)
                print("-" * 56)
                print(message)
                print(f"  [{status}]")
                print("-" * 56 + "\n")

            if pdf_every and (datetime.now(ET) - last_pdf) >= timedelta(minutes=pdf_every):
                if rebuild_report(symbol, today, start, end, db_path, pdf_path):
                    print(f"  {datetime.now(ET):%H:%M}  {pdf_path} rebuilt\n")
                last_pdf = datetime.now(ET)

            time_mod.sleep(max(5, 62 - datetime.now(ET).second))
    except KeyboardInterrupt:
        print("\nStopped early.")

    if collected:
        text = summarise(symbol, collected)
        print("=" * 56)
        print(text)
        print("\n[summary push: "
              f"{deliver(text, f'{symbol} morning', PRIORITY_SUMMARY, dry_run)}]")
    else:
        print("No candles collected.")
    return 0


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

def self_test() -> int:
    """Check the arithmetic and the wording offline. No network, no keys."""
    print("Self-test: checking candle maths, lean and messages...\n")
    failures = []

    at = datetime.combine(date(2026, 9, 18), time(9, 35), tzinfo=ET)
    up = Candle(at=at, open=152.18, high=152.63, low=152.05, close=152.41,
                volume=84_200, trades=612, usual_volume=46_000)
    down = Candle(at=at + timedelta(minutes=BAR_MINUTES), open=152.41, high=152.44,
                  low=151.90, close=152.02, volume=51_000)

    if abs(up.upper_wick - 0.22) > 1e-9:
        failures.append(f"upper wick should be 22c, got {up.upper_wick}")
    if abs(up.lower_wick - 0.13) > 1e-9:
        failures.append(f"lower wick should be 13c, got {up.lower_wick}")
    if abs(up.vol_ratio - 84_200 / 46_000) > 1e-9:
        failures.append("volume ratio should divide by the usual volume")
    if down.up or not up.up:
        failures.append("up and down candles are being told apart wrongly")
    if down.vol_ratio is not None:
        failures.append("with no history there should be no ratio, not a made-up one")
    for candle in (up, down):
        span = candle.upper_wick + candle.body + candle.lower_wick
        if abs(span - (candle.high - candle.low)) > 1e-9:
            failures.append(f"wicks plus body should equal the range at {candle.at:%H:%M}")
        if candle.upper_wick < 0 or candle.lower_wick < 0:
            failures.append("a wick cannot be negative")

    # --- aggregation -------------------------------------------------------
    index = pd.DatetimeIndex([at + timedelta(minutes=i) for i in range(10)])
    minutes = pd.DataFrame({
        "open":  [10, 11, 12, 13, 14, 20, 21, 22, 23, 24],
        "high":  [11, 12, 13, 14, 15, 21, 22, 23, 24, 25],
        "low":   [9, 10, 11, 12, 13, 19, 20, 21, 22, 23],
        "close": [11, 12, 13, 14, 15, 21, 22, 23, 24, 25],
        "volume": [100] * 10,
        "trade_count": [5] * 10,
    }, index=index)
    rolled = aggregate(minutes)
    if len(rolled) != 2:
        failures.append(f"ten minutes should roll into two candles, got {len(rolled)}")
    else:
        first = rolled.iloc[0]
        if not (first["open"] == 10 and first["high"] == 15 and first["low"] == 9
                and first["close"] == 15 and first["volume"] == 500):
            failures.append(f"the first candle aggregated wrongly: {dict(first)}")
        if rolled.index[0].time() != time(9, 35):
            failures.append(f"candles should align to the clock, got {rolled.index[0]}")

    # --- the forming bar ---------------------------------------------------
    kept = drop_forming(rolled, at + timedelta(minutes=7), BAR_MINUTES)
    if len(kept) != 1:
        failures.append(f"at 09:42 only the 09:35 candle is done, kept {len(kept)}")
    if len(drop_forming(minutes, at + timedelta(minutes=4, seconds=30), 1)) != 4:
        failures.append("the in-progress minute leaked through")

    # --- the lean ----------------------------------------------------------
    strong = pd.DataFrame({
        "open": [10.0] * 3, "high": [11.0] * 3, "low": [10.0] * 3,
        "close": [11.0] * 3, "volume": [100.0] * 3,
    }, index=pd.DatetimeIndex([at + timedelta(minutes=i) for i in range(3)]))
    buyers = read_lean(strong)
    if buyers is None or buyers.score < 0.99 or not buyers.word.startswith("buyers"):
        failures.append(f"three minutes closing on their highs should read buyers: {buyers}")

    weak = strong.copy()
    weak["close"] = 10.0
    weak["open"] = 11.0
    sellers = read_lean(weak)
    if sellers is None or sellers.score > 0.01 or not sellers.word.startswith("sellers"):
        failures.append(f"three minutes closing on their lows should read sellers: {sellers}")

    # The vocabulary itself: every band reachable, ordered, and the loudest
    # word gated on participation rather than on conviction alone.
    ladder = [(0.05, "sellers in control"), (0.24, "sellers pressing"),
              (0.36, "sellers showing up"), (0.50, "balanced"),
              (0.64, "buyers showing up"), (0.76, "buyers pressing"),
              (0.95, "buyers in control")]
    for score, expected in ladder:
        got = Lean(score=score, up_volume=1.0, down_volume=0.0, minutes=5).word
        if got != expected:
            failures.append(f"a score of {score:.2f} should read '{expected}', got '{got}'")

    quiet_extreme = Lean(score=0.99, up_volume=1.0, down_volume=0.0, minutes=5,
                         volume_ratio=0.4)
    if quiet_extreme.taking:
        failures.append("a dead minute must never read 'taking it' however it closed")
    busy_extreme = Lean(score=0.99, up_volume=1.0, down_volume=0.0, minutes=5,
                        volume_ratio=VOLUME_ALERT_MULTIPLE)
    if busy_extreme.word != "buyers taking it" or busy_extreme.arrow != "↑↑":
        failures.append(f"an extreme on real volume should read 'buyers taking it', "
                        f"got '{busy_extreme.word}'")
    busy_low = Lean(score=0.01, up_volume=0.0, down_volume=1.0, minutes=5,
                    volume_ratio=VOLUME_ALERT_MULTIPLE)
    if busy_low.word != "sellers taking it":
        failures.append(f"the mirror should read 'sellers taking it', got '{busy_low.word}'")
    if Lean.TAKING_VOLUME != VOLUME_ALERT_MULTIPLE:
        failures.append("the loudest word and the spike alarm should share one threshold")

    flat = strong.copy()
    flat["high"] = flat["low"] = flat["open"] = flat["close"] = 10.0
    middling = read_lean(flat)
    if middling is None or abs(middling.score - 0.5) > 1e-9 or middling.word != "balanced":
        failures.append(f"a rangeless stretch has no opinion, got {middling}")

    if read_lean(pd.DataFrame()) is not None:
        failures.append("no bars should yield no reading, not a default one")

    # --- messages ----------------------------------------------------------
    minute = Candle(at=at, open=152.30, high=152.45, low=152.28, close=152.41,
                    volume=18_200, trades=131, usual_volume=13_000)
    forming = Candle(at=at.replace(minute=35), open=152.18, high=152.45,
                     low=152.05, close=152.41, volume=42_100)
    update = describe_minute("SPCX", minute, forming, buyers)
    for needed in ("Minute volume", "forming", "Volume leaning"):
        if needed not in update:
            failures.append(f"the minute update should carry {needed!r}")

    alarm = describe("SPCX", up, buyers)
    for needed in ("Open", "Close", "High", "Low", "Upper wick", "Lower wick", "Volume"):
        if needed not in alarm:
            failures.append(f"the candle alarm should carry {needed!r}")
    if "pre-market" in alarm:
        failures.append("09:35 is not pre-market")
    if "pre-market" not in describe("SPCX", Candle(
            at=at.replace(hour=8, minute=55), open=152.0, high=152.1, low=151.9,
            close=152.05, volume=1_200)):
        failures.append("08:55 should be marked pre-market")

    if PRIORITY_UPDATE >= PRIORITY_SUMMARY:
        failures.append("the routine stream must be quieter than the alarm")

    # --- the volume alarm --------------------------------------------------
    busy = Candle(at=at, open=1.0, high=1.1, low=0.9, close=1.05,
                  volume=100_000, usual_volume=40_000)          # 2.5x
    calm = Candle(at=at, open=1.0, high=1.1, low=0.9, close=1.05,
                  volume=44_000, usual_volume=40_000)           # 1.1x
    blind = Candle(at=at, open=1.0, high=1.1, low=0.9, close=1.05,
                   volume=999_999)                              # no history
    if not is_spike(busy):
        failures.append("2.5x usual should be a spike")
    if is_spike(calm):
        failures.append("1.1x usual should not be a spike")
    if is_spike(blind):
        failures.append("with no baseline there is no claim to make, so no spike")
    if is_spike(busy, multiple=3.0):
        failures.append("the threshold should be respected")
    if not is_spike(calm, multiple=1.05):
        failures.append("lowering the threshold should catch more")

    spike_message = describe("SPCX", busy)
    if "VOLUME 2.5x usual" not in spike_message:
        failures.append(f"a spike should be called out first: {spike_message!r}")
    if not spike_message.startswith("**"):
        failures.append("the spike line should lead, not be buried")
    if "VOLUME" in describe("SPCX", calm).split("\n")[0]:
        failures.append("an ordinary candle should not be marked")

    source = open(__file__, encoding="utf-8").read()
    for forbidden in ("TradingClient", "submit_order", "MarketOrderRequest"):
        if forbidden in source.replace(f'"{forbidden}"', ""):
            failures.append(f"this file must not mention {forbidden}")

    # The quiet phase is the whole reason this runs all day. A regression
    # here means 79 alarms in a working day, which is how an alert channel
    # stops being read.
    quiet = time(10, 0)
    cases = [
        (time(9, 40), False, True,  "a routine minute inside the detail window"),
        (time(9, 40), True,  True,  "a spike inside the detail window"),
        (time(11, 0), False, False, "a routine minute after it"),
        (time(11, 0), True,  True,  "a spike after it"),
        (time(10, 0), False, False, "the switchover minute itself"),
    ]
    for at, spiked, expected, what in cases:
        if reaches_phone(at, spiked, quiet) is not expected:
            failures.append(f"{what} should "
                            f"{'reach' if expected else 'not reach'} the phone")

    # And the window it defaults to must actually be a trading day.
    if not (WINDOW_START < DETAIL_UNTIL <= WINDOW_END):
        failures.append("the detail window should sit inside the session window")

    print(update)
    print()
    print("-" * 56)
    print(alarm)
    print("-" * 56)
    print(f"\n  Ten minutes → candles          : {len(rolled)}, aligned to the clock")
    print("  Forming bar dropped            : minute and candle")
    print(f"  Lean, highs / lows / no range  : {buyers.score:.2f} / "
          f"{sellers.score:.2f} / {middling.score:.2f}")
    print(f"  Priorities, update vs alarm    : {PRIORITY_UPDATE} vs {PRIORITY_SUMMARY}")
    print(f"  Phone quiet after              : {DETAIL_UNTIL:%H:%M} (spikes still sound)")
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
    load_env()
    parser = argparse.ArgumentParser(description="Read the morning's tape to your phone.")
    parser.add_argument("--symbol", default=SYMBOL)
    parser.add_argument("--from", dest="start", default=f"{WINDOW_START:%H:%M}",
                        help=f"Window start, ET (default {WINDOW_START:%H:%M})")
    parser.add_argument("--until", dest="end", default=f"{WINDOW_END:%H:%M}",
                        help=f"Window end, ET (default {WINDOW_END:%H:%M})")
    parser.add_argument("--replay", metavar="YYYY-MM-DD", help="Read a past session instead")
    parser.add_argument("--push-empty", action="store_true",
                        help="Send an update for minutes with no trades too")
    parser.add_argument("--volume-alert", type=float, default=VOLUME_ALERT_MULTIPLE,
                        metavar="N",
                        help=f"Sound the alarm at N times the usual volume for that "
                             f"slot (default {VOLUME_ALERT_MULTIPLE})")
    parser.add_argument("--pdf-every", type=int, default=PDF_EVERY_MINUTES,
                        metavar="N",
                        help=f"Rebuild the session PDF every N minutes, and send "
                             f"the chart with a volume alarm (default "
                             f"{PDF_EVERY_MINUTES}; 0 turns it off)")
    parser.add_argument("--detail-until", dest="detail", metavar="HH:MM",
                        default=f"{DETAIL_UNTIL:%H:%M}",
                        help=f"Minute updates and candle summaries reach the "
                             f"phone until this time; after it only volume "
                             f"spikes do (default {DETAIL_UNTIL:%H:%M})")
    parser.add_argument("--dry-run", action="store_true", help="Print, do not send")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    start, end = parse_clock(args.start), parse_clock(args.end)
    if start >= end:
        raise SystemExit(f"--from {start:%H:%M} must be before --until {end:%H:%M}")
    detail = parse_clock(args.detail)
    if not (start <= detail <= end):
        raise SystemExit(f"--detail-until {detail:%H:%M} must sit inside "
                         f"{start:%H:%M}-{end:%H:%M}")

    db = open_db(args.db)
    ensure_schema(db)
    symbol = args.symbol.upper()

    if args.replay:
        day = datetime.strptime(args.replay, "%Y-%m-%d").date()
        return run_replay(symbol, day, start, end, args.dry_run, db,
                          args.volume_alert)
    return run_live(symbol, start, end, args.dry_run, db, args.push_empty,
                    args.volume_alert, args.pdf_every, args.db, detail)


if __name__ == "__main__":
    raise SystemExit(main())
