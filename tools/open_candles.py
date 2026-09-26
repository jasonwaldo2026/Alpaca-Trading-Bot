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
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

try:
    import lockups
except ImportError:            # noqa: F401 -- the calendar is a convenience
    # ...and the session is not. lockups.py already treats a missing or
    # broken lockups.json as an empty calendar; importing it hard made the
    # module itself mandatory, so one file that failed to copy took the
    # whole morning down at 09:25. Losing the unlock line is a cost worth
    # paying; losing the tape is not.
    lockups = None
try:
    import launches
except ImportError:            # noqa: F401 -- same contract as lockups
    launches = None
from feed_check import ET, load_credentials, load_env, parse_clock, trading_days
from spcx_alert import (
    PRIORITY_SUMMARY,
    PRIORITY_UPDATE,
    open_db,
    registered_devices,
    send_pushover,
)

SYMBOL = "SPCX"
BAR_MINUTES = 5
#: Fifteen minutes before the bell. Early enough to watch the run-up
#: into the open, and far enough from it that the first real candle is
#: not also the first thing on the page. The clock dial deliberately
#: still starts at 09:30: it charts market hours, this charts a morning.
WINDOW_START = time(9, 15)
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

#: What earns a noise. The band edges -- "sellers pressing" and "buyers
#: pressing" -- rather than the extremes, and the reason is a measurement
#: rather than a preference. Scored against 23 Sep 2026, a session that
#: fell 3.53%, the extremes (<=18 / >=82) fired on NONE of that day's five
#: volume alarms: the phone would have stayed silent through the whole
#: decline. The edges fire once, at 13:50, sellers pressing at 26 on 2.7x
#: volume and $1.60 before the low. One interruption in a session is what
#: a working day can carry.
#:
#: Calibrated on a single day, which is exactly the kind of fit that
#: flatters itself in hindsight. If it turns out noisy or silent in
#: practice, these two numbers are where to look.
PRESSING_LOW, PRESSING_HIGH = 0.30, 0.70

#: An alarm needs participation as well as direction. Same multiple as
#: the spike alarm, so the loudest word and the noise mean one thing.
ALARM_VOLUME = VOLUME_ALERT_MULTIPLE

#: One event, one ring. Without this, a state that stays true rings every
#: minute it stays true. Time-based rather than reset-on-lapse: a reading
#: that dips below the edge for a single bar and comes back has not
#: happened twice.
SOUND_COOLDOWN_MINUTES = 15

#: Direction rides the sound, because the phone is in a pocket: which way
#: to look should be settled before you have looked at anything. These
#: are custom sounds uploaded to the Pushover account that owns the app
#: token, named exactly as that account lists them -- a name that does
#: not match falls back to the user's default sound, silently, which is
#: the failure you would not notice until a Monday. Both are overridable
#: per run with --buy-sound / --sell-sound.
SOUND_BUY, SOUND_SELL = "Buy_Stock", "Sell_Positions"

#: The slider's track. Eleven cells so there is an exact middle, and two
#: hues plus a neutral centre rather than a red-orange-yellow-green ramp:
#: a hue at the midpoint would colour "balanced" as though it meant
#: something. Position on the track carries the strength instead.
TRACK = ("🟥", "🟥", "🟥", "🟥",
         "⬜", "⬜", "⬜",
         "🟩", "🟩", "🟩", "🟩")
TRACK_CELLS = len(TRACK)
TRACK_KNOB = "🔘"


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

    @property
    def slider(self) -> str:
        """The reading as a slider: a red-to-green track with a knob on it.

        Built from emoji rather than HTML or box-drawing characters, and
        the reason is Pushover's own rules. `html=1` and `monospace=1` are
        mutually exclusive, and BOTH are stripped when the message is
        shown as a notification -- which is the moment this has to work.
        Emoji survive that, render in colour on the lock screen, and need
        no font to line up.

        The track is two hues either side of a neutral middle, never a
        rainbow: a graded hue ramp would put a third colour at the point
        where the tape is saying nothing, which is the one place a chart
        must stay quiet. Distance from the middle carries the strength.
        """
        cell = max(0, min(TRACK_CELLS - 1, round(self.score * (TRACK_CELLS - 1))))
        track = list(TRACK)
        track[cell] = TRACK_KNOB
        return "".join(track)

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


@dataclass
class Day:
    """Where the stock stands, as opposed to what the last bar did.

    The alerts used to describe one candle in isolation: "+2c" meant the
    move inside that bar, so a message could say the same thing whether
    the stock was up three percent on the day or down three. This is the
    context that was missing.
    """

    last: float
    high: float
    low: float
    prev_close: Optional[float] = None
    vwap: Optional[float] = None

    #: Where in the day's range the price sits, low to high. Five bands,
    #: not a percentage: "68% of the range" invites arithmetic that the
    #: number cannot support, where "upper half" says exactly what is
    #: known. A range narrower than a cent has no position to report.
    PLACES = ((0.08, "at the low"), (0.40, "lower half"),
              (0.60, "mid-range"), (0.92, "upper half"), (1.01, "at the high"))

    @property
    def span(self) -> float:
        return self.high - self.low

    @property
    def place(self) -> Optional[str]:
        if self.span < 0.01:
            return None
        share = (self.last - self.low) / self.span
        for edge, word in self.PLACES:
            if share < edge:
                return word
        return self.PLACES[-1][1]

    @property
    def change_pct(self) -> Optional[float]:
        """Against yesterday's close, which is the number every other
        screen shows him. Measuring from today's open instead would be
        defensible and would quietly disagree with his broker."""
        if not self.prev_close:
            return None
        return 100.0 * (self.last - self.prev_close) / self.prev_close

    #: Past a dollar, cents stop reading as a distance. "190c below VWAP"
    #: is arithmetic the reader has to do; "$1.90 below VWAP" is the
    #: number. Only this line changes -- cents are right for the small
    #: moves everywhere else.
    DOLLARS_FROM = 1.00

    def vwap_gap(self) -> Optional[str]:
        if self.vwap is None or pd.isna(self.vwap):
            return None
        gap = self.last - self.vwap
        side = "above" if gap >= 0 else "below"
        size = (f"${abs(gap):,.2f}" if abs(gap) >= self.DOLLARS_FROM
                else cents(abs(gap)))
        return f"{size} {side} VWAP"

    def line(self) -> str:
        parts = [f"Day {self.low:,.2f} – {self.high:,.2f}"]
        if self.place:
            parts[0] += f", {self.place}"
        gap = self.vwap_gap()
        if gap:
            parts.append(gap)
        return " · ".join(parts)


def read_day(frame: pd.DataFrame, prev_close: Optional[float] = None) -> Optional[Day]:
    """The day so far, from the session's own bars.

    Regular hours only. Pre-market prints would stretch the day's range
    with a handful of thin trades and move VWAP before the session that
    VWAP describes has started.
    """
    if frame.empty:
        return None
    session = frame[(frame.index.time >= time(9, 30))
                    & (frame.index.time < time(16, 0))]
    if session.empty:
        return None
    vwap = None
    try:
        from feed_check import add_vwap
        vwap = float(add_vwap(session)["vwap"].iloc[-1])
    except Exception:  # noqa: BLE001 -- a missing line is not a missing alert
        vwap = None
    return Day(last=float(session["close"].iloc[-1]),
               high=float(session["high"].max()),
               low=float(session["low"].min()),
               prev_close=prev_close, vwap=vwap)


def previous_close(symbol: str, before: date) -> Optional[float]:
    """Yesterday's close, fetched once a session rather than per alert."""
    try:
        # Imported inside the try, not above it: an ImportError here has
        # to cost the day's-move line and nothing else, exactly like a
        # failed request does.
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        frame = _client().get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
            start=datetime.combine(before - timedelta(days=10), time(0, 0),
                                   tzinfo=ET),
            end=datetime.combine(before - timedelta(days=1), time(23, 59),
                                 tzinfo=ET),
            feed=DataFeed.SIP)).df
        if frame is None or frame.empty:
            return None
        if isinstance(frame.index, pd.MultiIndex):
            frame = frame.xs(symbol, level="symbol")
        return float(frame["close"].iloc[-1])
    except Exception:  # noqa: BLE001 -- the day's move is a nicety
        return None


@dataclass
class Alarms:
    """Decides which readings are worth a noise, and how often.

    Two triggers, mirrored. A lean at or past a pressing band with real
    volume behind it, and a VWAP cross with the same volume behind it --
    above VWAP the average buyer today is in profit, below it they are
    underwater, so crossing is the moment the day's balance changes
    hands. Either one fires; the direction picks the sound.

    None of this claims an edge. Nothing measured here beats a coin
    flip, and a sound that meant "buy" would be asserting otherwise.
    What it says is: something is happening now, with participation
    behind it, in a direction you care about. Go and look.
    """

    cooldown: int = SOUND_COOLDOWN_MINUTES
    volume: float = ALARM_VOLUME
    fired: Dict[str, datetime] = field(default_factory=dict)
    above_vwap: Optional[bool] = None

    def _cross(self, day: Optional[Day]) -> Optional[str]:
        """Which way price just crossed VWAP, if it did. Updates state."""
        if day is None or day.vwap is None or pd.isna(day.vwap):
            return None
        now_above = day.last >= day.vwap
        was = self.above_vwap
        self.above_vwap = now_above
        if was is None or was == now_above:
            return None
        return "buy" if now_above else "sell"

    def reason(self, candle: Candle, lean: Optional[Lean],
               day: Optional[Day]) -> Optional[Tuple[str, str]]:
        """(direction, why) for a reading that earns a noise, or None.

        The VWAP side is updated on every reading, fired or not -- a
        cross has to be measured against the last bar, not the last
        alarm, or a quiet stretch would swallow the crossing.
        """
        crossed = self._cross(day)
        loud = candle.vol_ratio is not None and candle.vol_ratio >= self.volume
        if not loud:
            return None
        if lean is not None:
            if lean.score >= PRESSING_HIGH:
                return "buy", f"{lean.word} on {candle.vol_ratio:.1f}x volume"
            if lean.score <= PRESSING_LOW:
                return "sell", f"{lean.word} on {candle.vol_ratio:.1f}x volume"
        if crossed:
            side = "above" if crossed == "buy" else "below"
            return crossed, f"crossed {side} VWAP on {candle.vol_ratio:.1f}x volume"
        return None

    def should_sound(self, direction: str, at: datetime) -> bool:
        last = self.fired.get(direction)
        if last is not None and (at - last) < timedelta(minutes=self.cooldown):
            return False
        self.fired[direction] = at
        return True

    def sound_for(self, direction: str, buy: str = SOUND_BUY,
                  sell: str = SOUND_SELL) -> str:
        return buy if direction == "buy" else sell


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


#: The S&P 500 stand-in. SPY rather than the index itself: the index
#: has no minute bars on this feed and the ETF that tracks it does, and
#: for "is the whole market down or just this one" they are the same
#: statement to within a rounding error.
BENCHMARK = "SPY"


def market_line(change_pct: Optional[float]) -> Optional[str]:
    """The market's move today, or nothing at all.

    It sits under the stock's own move so the two read as a pair: a 1%
    fall while the market falls 1% is a different trade from a 1% fall
    while the market is flat, and the difference should not require
    opening another app at the moment it matters.

    Returns None rather than a placeholder when the number is missing.
    A line reading "S&P 500 --" spends a line of a lock-screen alert on
    the news that we do not know something.
    """
    if change_pct is None:
        return None
    arrow = "▲" if change_pct >= 0 else "▼"
    return f"S&P 500 {arrow} {change_pct:+.2f}%"


def market_move(prev_close: Optional[float], start: datetime,
                end: datetime) -> Optional[float]:
    """The benchmark's percentage move today, or None.

    Every failure path returns None and the alert simply omits the line.
    The S&P is context: an alert that did not send because a second
    symbol was unreachable would trade the thing the tool exists for
    against a nicety.
    """
    if prev_close is None or prev_close <= 0:
        return None
    try:
        frame = fetch_minutes(BENCHMARK, start, end)
        if frame is None or frame.empty:
            return None
        return 100.0 * (float(frame["close"].iloc[-1]) - prev_close) / prev_close
    except Exception:      # noqa: BLE001 -- context, never a blocker
        return None


def describe(symbol: str, candle: Candle, day: Optional[Day] = None,
             lean: Optional[Lean] = None,
             multiple: float = VOLUME_ALERT_MULTIPLE,
             market: Optional[float] = None) -> str:
    """One alert, short enough to read at a traffic light.

    Four lines at most: where the stock is, where that sits, which way
    the tape is leaning, and the slider. Everything this used to spell
    out -- open, high, low, close, wick and body measurements, the trade
    count, the forming candle's progress -- is on the PDF that rides
    along with a spike, and reading it on a phone was the cost of getting
    to the one number that mattered.

    Both cadences share this composer. Two of them drifted apart once.
    """
    lines = []
    if is_spike(candle, multiple):
        lines.append(spike_line(candle))

    head = f"{symbol} {money(candle.close)}"
    if day is not None:
        change = day.change_pct
        if change is not None:
            arrow = "\u25b2" if change >= 0 else "\u25bc"
            head += f"  {arrow} {change:+.2f}% today"
    if candle.at.time() < time(9, 30):
        head += "  (pre-market)"
    lines.append(head)

    benchmark = market_line(market)
    if benchmark is not None:
        lines.append(benchmark)

    if day is not None:
        lines.append(day.line())

    if lean is not None:
        tail = f"{lean.word.capitalize()} {lean.arrow} \u00b7 {lean.score * 100:.0f}/100"
        if candle.vol_ratio is not None:
            tail += f" \u00b7 volume {candle.vol_ratio:.1f}\u00d7 usual"
        lines.append(tail)
        lines.append(lean.slider)
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
            attachment: Optional[str] = None,
            sound: Optional[str] = None) -> str:
    if dry_run:
        noise = f" ({sound})" if sound else ""
        return "dry run" + noise + (" (chart drawn)" if attachment else "")
    return send_pushover(message, title=title, priority=priority,
                         attachment=attachment, sound=sound) or "sent"


PUSHOVER_SOUNDS_URL = "https://api.pushover.net/1/sounds.json"


def available_sounds() -> Tuple[Optional[List[str]], Optional[str]]:
    """Ask Pushover which sound names this app token can actually use.

    Worth asking, because a wrong name is not an error. Pushover accepts
    the message, plays the user's default sound instead, and reports
    success -- so a typo in a sound name is invisible until the morning
    it matters and the wrong noise comes out of a pocket.

    The token goes into the query string and is never printed.
    """
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    token = os.getenv("PUSHOVER_APP_TOKEN", "").strip()
    if not token:
        return None, "no app token"
    url = f"{PUSHOVER_SOUNDS_URL}?{urllib.parse.urlencode({'token': token})}"
    try:
        with urllib.request.urlopen(url, timeout=15) as reply:
            body = json.load(reply)
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code} — the app token was rejected"
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"
    sounds = body.get("sounds")
    if not isinstance(sounds, dict):
        return None, "no sounds in the reply"
    return sorted(sounds), None


def list_sounds() -> int:
    """Print every sound this app can name, custom ones included."""
    load_env()
    names, error = available_sounds()
    if error:
        print(f"Could not ask Pushover: {error}")
        return 1
    custom = [n for n in names if n not in BUILT_IN_SOUNDS]
    print(f"\n{len(names)} sounds available to this application.\n")
    if custom:
        print("  Your uploads:")
        for name in custom:
            print(f"    {name}")
        print()
    print("  Built in:")
    for name in [n for n in names if n in BUILT_IN_SOUNDS]:
        print(f"    {name}")
    print(f"\n  Currently configured: {SOUND_BUY} to buy, {SOUND_SELL} to sell.")
    for role, name in (("buy", SOUND_BUY), ("sell", SOUND_SELL)):
        if name not in names:
            print(f"  ** {name!r} ({role}) is NOT in this list. A message asking")
            print("     for it is delivered with your default sound instead,")
            print("     and Pushover reports that as a success.")
    print()
    return 0


#: Pushover's own sounds, so an upload can be told apart from a built-in
#: in the listing. Only used for that: a name missing from here is not an
#: error, it is a sound Pushover added since.
BUILT_IN_SOUNDS = frozenset((
    "pushover", "bike", "bugle", "cashregister", "classical", "cosmic",
    "falling", "gamelan", "incoming", "intermission", "magic", "mechanical",
    "pianobar", "siren", "spacealarm", "tugboat", "alien", "climb",
    "persistent", "echo", "updown", "vibrate", "none",
))


#: The commit that taught send_pushover to carry a sound. Named in the
#: message rather than left for someone to work out.
SOUNDS_FROM_COMMIT = "8c94c8e"


def sounds_supported() -> bool:
    """Does the send_pushover we imported accept a sound?

    Python resolves a keyword argument when the call runs, not when the
    module loads. These two files travel separately and have been mixed
    five times; the fifth was a new open_candles calling an old
    send_pushover, and it got through the credential check, the device
    check and the message before raising TypeError on the send.

    Live, that call happens only when an alarm fires -- so the watcher
    would have run all morning and died at the first ring, which is the
    one moment it exists for. Asking at startup turns that into a
    refusal to start.
    """
    import inspect

    try:
        params = inspect.signature(send_pushover).parameters
    except (TypeError, ValueError):  # not introspectable: not our business
        return True
    # A wrapper taking **kwargs passes a sound straight through, so it is
    # not stale even though no parameter is named.
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return True
    return "sound" in params


def stale_sender() -> Optional[str]:
    """One line naming the problem and the fix, or None when all is well."""
    if sounds_supported():
        return None
    return (f"spcx_alert.py is out of date: its send_pushover() takes no "
            f"sound, so no alarm could play one.\n"
            f"  Re-download it from {SOUNDS_FROM_COMMIT} or later, into the "
            f"same folder as this file.")


def test_push(buy_sound: str = SOUND_BUY, sell_sound: str = SOUND_SELL,
              pause: int = 6) -> int:
    """Send one of each kind to the phone, and say what happened.

    Three messages: the silent stream, a buy ring, a sell ring. Built by
    the same composer the live watcher uses, so what arrives is what a
    real alert looks like rather than an approximation of one.

    Every title is prefixed TEST. An alert indistinguishable from a live
    one is a trap -- you would find it on a Monday and act on it.
    """
    load_env()
    stale = stale_sender()
    if stale:
        print(f"{stale}\n")
        return 1
    print("Checking the path to your phone...\n")

    present = {name: bool(os.getenv(name, "").strip())
               for name in ("PUSHOVER_APP_TOKEN", "PUSHOVER_USER_KEY")}
    for name, ok in present.items():
        print(f"  {name:<22} {'set' if ok else 'MISSING'}")
    if not all(present.values()):
        print("\nNothing can be sent until both are in your .env.")
        return 1

    # Pushover accepts a message for an account with no devices and calls
    # it a success, so a send that "worked" proves nothing on its own.
    devices, error = registered_devices()
    if error:
        print(f"\n  devices                UNKNOWN — {error}")
        print("\nThe key was rejected, so nothing would arrive.")
        return 1
    if not devices:
        print("\n  devices                NONE")
        print("\nThis key is valid but no device is attached, so a message is")
        print("accepted and then reaches nobody. Open Pushover on the phone and")
        print("check which account it is signed in to.")
        return 1
    print(f"  devices                {', '.join(devices)}")

    names, sound_error = available_sounds()
    if sound_error:
        print(f"  sounds                 UNKNOWN — {sound_error}")
    else:
        for role, name in (("buy", buy_sound), ("sell", sell_sound)):
            if name in names:
                print(f"  {role + ' sound':<22} {name}")
            else:
                print(f"  {role + ' sound':<22} {name}  ** NOT FOUND **")
                print(f"{'':25}Pushover will use your default sound and")
                print(f"{'':25}report success. Check the spelling at")
                print(f"{'':25}pushover.net → Sounds.")

    at = datetime.now(ET).replace(second=0, microsecond=0)
    day = Day(last=149.90, high=154.26, low=149.85, prev_close=153.70,
              vwap=151.80)
    calm = Day(last=152.47, high=154.26, low=152.05, prev_close=153.70,
               vwap=153.10)

    rounds = (
        ("the silent stream", PRIORITY_UPDATE, None,
         Candle(at=at, open=152.25, high=152.59, low=152.12, close=152.47,
                volume=808_200, trades=11_325, usual_volume=1_400_000),
         Lean(0.48, 390_000, 418_000, 5, volume_ratio=0.6), calm),
        ("a buy ring", PRIORITY_SUMMARY, buy_sound,
         Candle(at=at, open=153.63, high=154.18, low=153.60, close=154.12,
                volume=1_620_000, trades=14_200, usual_volume=900_000),
         Lean(0.76, 1_280_000, 340_000, 5, volume_ratio=1.8),
         Day(last=154.12, high=154.18, low=153.04, prev_close=153.70,
             vwap=153.55)),
        ("a sell ring", PRIORITY_SUMMARY, sell_sound,
         Candle(at=at, open=150.27, high=150.31, low=149.85, close=149.90,
                volume=1_300_000, trades=18_764, usual_volume=481_000),
         Lean(0.26, 169_000, 1_131_000, 5, volume_ratio=2.7), day),
    )

    print(f"\nSending {len(rounds)}, {pause}s apart so the sounds do not "
          f"overlap...\n")
    for i, (label, priority, sound, bar, lean, context) in enumerate(rounds):
        if i:
            time_mod.sleep(pause)
        message = describe(SYMBOL, bar, context, lean)
        title = f"TEST — {label}"
        outcome = send_pushover(message, title=title, priority=priority,
                                sound=sound) or "sent"
        noise = sound or "silent"
        print(f"  {label:<20} priority {priority:<3} {noise:<16} {outcome}")

    print("\nThree should have arrived. The first without a sound, then the")
    print("buy, then the sell. If a sound played that you did not choose, the")
    print("name does not match the account — that is the failure this cannot")
    print("detect from here.\n")
    return 0


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

    # The replay knows yesterday's close too, so a replayed message is
    # the same message the day would have sent.
    prev_close = previous_close(symbol, day)
    for candle in candles:
        upto = minutes[minutes.index < candle.at + timedelta(minutes=BAR_MINUTES)]
        print(describe(symbol, candle, read_day(upto, prev_close),
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
             detail_until: time = DETAIL_UNTIL,
             buy_sound: str = SOUND_BUY,
             sell_sound: str = SOUND_SELL) -> int:
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
    notice = lockups.headline(symbol, today) if lockups else None
    flight = launches.headline(symbol, today) if launches else None
    if notice or flight:
        print("=" * 56)
        if notice:
            print(f"  {notice}")
        # Second, and visibly so. An unlock is supply arriving on a
        # schedule; a launch is a date in the news with nothing measured
        # behind it. Printing them in either order would be a claim.
        if flight:
            print(f"  {flight}")
        print("=" * 56 + "\n")

    feed = os.getenv("ALPACA_DATA_FEED", "").strip().lower() or "iex"
    print(f"{symbol} · {start:%H:%M}-{end:%H:%M} ET · {feed} feed")
    print(f"  {start:%H:%M}-{detail_until:%H:%M}  every reading → silent "
          f"update (priority {PRIORITY_UPDATE})")
    print(f"  {detail_until:%H:%M}-{end:%H:%M}  volume spikes only, still "
          f"silent; the rest is recorded")
    print(f"  all session      a lean past {PRESSING_LOW * 100:.0f}/"
          f"{PRESSING_HIGH * 100:.0f} or a VWAP cross, on "
          f"{ALARM_VOLUME:.1f}x volume,")
    print(f"{'':19}sounds at priority {PRIORITY_SUMMARY} — {buy_sound} to buy, "
          f"{sell_sound} to sell,")
    print(f"{'':19}at most one a direction every "
          f"{SOUND_COOLDOWN_MINUTES} minutes")
    if feed != "sip" and start < time(9, 30):
        print("  note: IEX carries very little before 09:30; empty minutes are")
        print("        skipped unless --push-empty.")
    print()

    seen_minutes, seen_candles, collected = set(), set(), []
    # Once a session, not once an alert. It only moves overnight, and a
    # per-message fetch would put a network call between a spike and the
    # phone. None is survivable: the day's move is the line that goes.
    alarms = Alarms()
    prev_close = previous_close(symbol, today)
    # The benchmark's own yesterday, fetched once. If either half is
    # missing the market line is simply absent -- it is context, and no
    # alert should fail to send because the S&P was unreachable.
    market_prev = previous_close(BENCHMARK, today)
    if prev_close:
        print(f"  yesterday's close {money(prev_close)} — today's move is "
              f"measured from it\n")
    else:
        print("  yesterday's close unavailable — alerts will omit the "
              "day's move\n")
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

            # The market's move, read once per pass rather than once per
            # minute: several minutes can arrive together after a slow
            # fetch, and they all happened under the same S&P reading.
            market = market_move(market_prev, window_start, min(now, window_end))

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

                spiked = is_spike(minute, multiple)
                day = read_day(upto, prev_close)
                # A spike is now visible, not audible. Only a direction
                # with participation behind it earns a noise, because a
                # phone that shouts at every busy minute is a phone whose
                # shouting stops meaning anything.
                call = alarms.reason(minute, lean, day)
                ringing = call is not None and alarms.should_sound(call[0], stamp)
                # Past the detail window only the unusual leaves the machine.
                push = reaches_phone(stamp.time(), spiked, detail_until) or ringing
                message = describe(symbol, minute, day, lean, multiple, market)
                fresh = remember_minute(db, symbol, minute, lean,
                                        sent=push and not dry_run)
                if ringing:
                    title = f"{symbol} {stamp:%H:%M} — {call[1]}"
                elif spiked:
                    title = f"{symbol} {stamp:%H:%M} volume {minute.vol_ratio:.1f}x"
                else:
                    title = f"{symbol} {stamp:%H:%M}"
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
                    status = deliver(
                        message, title,
                        PRIORITY_SUMMARY if ringing else PRIORITY_UPDATE,
                        dry_run, attachment=chart,
                        sound=alarms.sound_for(call[0], buy_sound, sell_sound)
                        if ringing else None)
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
                message = describe(symbol, candle, read_day(upto, prev_close),
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
                    # Always quiet. Every audible decision is made once, on
                    # the minute stream, which sees the same tape first --
                    # two paths judging the same thing is how the two
                    # message composers in this file drifted apart.
                    status = deliver(message, title, PRIORITY_UPDATE, dry_run,
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

    # --- the slider --------------------------------------------------------
    # A reading at either end must put the knob at that end, and the
    # middle must stay neutral: a hue at the midpoint would colour
    # "balanced" as though the tape were saying something.
    ends = (Lean(0.0, 0, 10, 5).slider, Lean(1.0, 10, 0, 5).slider)
    if ends[0].index(TRACK_KNOB) != 0:
        failures.append("a floor reading should park the knob at the left end")
    if ends[1].index(TRACK_KNOB) != len(ends[1]) - 1:
        failures.append("a ceiling reading should park the knob at the right end")
    if TRACK[TRACK_CELLS // 2] != "\u2b1c":
        failures.append("the middle of the track must be neutral, not a hue")
    if len(set(TRACK)) != 3:
        failures.append(f"the track should be two hues and a neutral, "
                        f"got {len(set(TRACK))} colours")
    for score in (0.0, 0.25, 0.5, 0.75, 1.0):
        if Lean(score, 5, 5, 5).slider.count(TRACK_KNOB) != 1:
            failures.append(f"exactly one knob, at {score}")

    # --- the guard against a half-updated folder ---------------------------
    # Five mixes so far. The fifth could have waited until an alarm fired
    # to show itself, which is the only moment this tool exists for.
    if not sounds_supported():
        failures.append("the sender in this folder takes no sound — these "
                        "files are mismatched")
    if stale_sender() is not None:
        failures.append("with a current sender there is nothing to report")

    def old_sender(message, title="", priority=0, attachment=None):
        return None

    # sys.modules[__name__], not "import open_candles": run as a script
    # this module is __main__, and importing it by name would load a
    # second copy whose globals the patch below would never reach.
    import sys as _sys
    _self = _sys.modules[__name__]
    kept = _self.send_pushover
    _self.send_pushover = old_sender
    try:
        if sounds_supported():
            failures.append("a sender without 'sound' must be detected")
        note = stale_sender()
        if not note or "spcx_alert.py" not in note:
            failures.append(f"the warning should name the file: {note!r}")
        if not note or SOUNDS_FROM_COMMIT not in note:
            failures.append("the warning should name the commit to fetch")
        if _self.test_push(pause=0) != 1:
            failures.append("test_push must refuse rather than crash on a "
                            "stale sender")
    finally:
        _self.send_pushover = kept

    # A wrapper that takes **kwargs passes a sound through untouched and
    # must not be mistaken for an old file.
    _self.send_pushover = lambda *a, **k: None
    try:
        if not sounds_supported():
            failures.append("a **kwargs sender should not be called stale")
    finally:
        _self.send_pushover = kept

    # --- the test push -----------------------------------------------------
    # These are promises about a function that talks to a phone, so they
    # are checked by reading it rather than by running it.
    import inspect
    push_source = inspect.getsource(test_push)
    if "registered_devices()" not in push_source:
        failures.append("test_push() should confirm a device is listening "
                        "before sending")
    if "available_sounds()" not in push_source:
        failures.append("test_push() should check the sound names exist — a "
                        "wrong one is delivered silently with the default")
    if 'f"TEST — {label}"' not in push_source:
        failures.append("every test title must be marked TEST, or one will be "
                        "mistaken for a live signal")
    if "PRIORITY_UPDATE" not in push_source or "PRIORITY_SUMMARY" not in push_source:
        failures.append("the test should exercise both the quiet and loud "
                        "channels")
    if "time_mod.sleep(pause)" not in push_source:
        failures.append("the rings should be spaced, or the two sounds overlap")

    # The token is a credential. It travels in a query string and must
    # never reach a print or a log.
    for fn in (available_sounds, list_sounds, test_push):
        body = inspect.getsource(fn)
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith(("print(", "print(f")) and "token" in stripped.lower():
                failures.append(f"{fn.__name__} prints something with 'token' "
                                f"in it: {stripped}")

    if "bike" not in BUILT_IN_SOUNDS or SOUND_BUY in BUILT_IN_SOUNDS:
        failures.append("the built-in list should know bike and not claim the "
                        "uploaded buy sound as its own")

    # --- the day's context -------------------------------------------------
    day = Day(last=153.60, high=154.00, low=150.00, prev_close=150.50,
              vwap=152.90)
    if abs(day.change_pct - 2.0598) > 0.01:
        failures.append(f"the day's move is measured from yesterday's close, "
                        f"got {day.change_pct}")
    if Day(last=1.0, high=2.0, low=0.0).change_pct is not None:
        failures.append("no previous close means no percentage, not a zero")
    if day.place != "upper half":
        failures.append(f"90% of the range is the upper half, got {day.place}")
    if Day(last=150.0, high=154.0, low=150.0).place != "at the low":
        failures.append("a price on the day's low reads 'at the low'")
    if Day(last=154.0, high=154.0, low=150.0).place != "at the high":
        failures.append("a price on the day's high reads 'at the high'")
    if Day(last=10.0, high=10.0, low=10.0).place is not None:
        failures.append("a day with no range has no position in it")
    if "above VWAP" not in (day.vwap_gap() or ""):
        failures.append(f"153.60 is above a VWAP of 152.90: {day.vwap_gap()}")
    # Cents below a dollar, dollars above it, and the boundary itself
    # belongs to dollars rather than to a 100c nobody says out loud.
    for last, vwap, expected in ((153.60, 152.90, "70\u00a2 above VWAP"),
                                 (149.90, 151.80, "$1.90 below VWAP"),
                                 (100.99, 100.00, "99\u00a2 above VWAP"),
                                 (101.00, 100.00, "$1.00 above VWAP"),
                                 (88.00, 100.00, "$12.00 below VWAP")):
        got = Day(last=last, high=last + 1, low=last - 1, vwap=vwap).vwap_gap()
        if got != expected:
            failures.append(f"vwap gap for {last}/{vwap}: {got!r}, "
                            f"wanted {expected!r}")
    if Day(last=1.0, high=2.0, low=0.0).vwap_gap() is not None:
        failures.append("no VWAP means no VWAP line, not a zero gap")

    # --- messages ----------------------------------------------------------
    alarm = describe("SPCX", up, day, buyers)
    if TRACK_KNOB not in alarm:
        failures.append("every reading should carry its slider")
    if "+2.06% today" not in alarm:
        failures.append(f"the day's move belongs on the first line: {alarm}")
    # Four lines of reading, plus the volume alarm when there is one.
    if len(alarm.splitlines()) > 5:
        failures.append(f"five lines at most, got {len(alarm.splitlines())}")
    if len(describe("SPCX", Candle(at=up.at, open=up.open, high=up.high,
                                   low=up.low, close=up.close,
                                   volume=up.volume), day,
                    buyers).splitlines()) != 4:
        failures.append("a quiet reading is four lines")
    for gone in ("Upper wick", "Lower wick", "Open ", "forming"):
        if gone in alarm:
            failures.append(f"{gone!r} belongs on the PDF, not the phone")
    if "pre-market" in alarm:
        failures.append("09:35 is not pre-market")
    if "pre-market" not in describe("SPCX", Candle(
            at=at.replace(hour=8, minute=55), open=152.0, high=152.1, low=151.9,
            close=152.05, volume=1_200)):
        failures.append("08:55 should be marked pre-market")
    # A message with nothing to say still says the price.
    bare = describe("SPCX", Candle(at=up.at, open=up.open, high=up.high,
                                   low=up.low, close=up.close, volume=up.volume))
    if "SPCX" not in bare or len(bare.splitlines()) != 1:
        failures.append(f"with no context, one line: {bare!r}")

    # ---- the market line -----------------------------------------------
    # It must track the benchmark's sign, sit directly under the stock's
    # own move so the two read as a pair, and vanish entirely when the
    # number is missing rather than printing that we do not know.
    if market_line(None) is not None:
        failures.append("an unknown market move should add no line at all")
    if "▲ +0.40%" not in (market_line(0.4) or ""):
        failures.append(f"a rising market reads up: {market_line(0.4)}")
    if "▼ -1.20%" not in (market_line(-1.2) or ""):
        failures.append(f"a falling market reads down: {market_line(-1.2)}")
    if market_line(0.0) is None or "▲" not in market_line(0.0):
        failures.append("an unchanged market is not a missing one")

    withmkt = describe("SPCX", up, day, buyers, market=-1.2).splitlines()
    if len(withmkt) != len(alarm.splitlines()) + 1:
        failures.append("the market line should add exactly one line")
    # Directly under the stock's own move, wherever that line lands -- a
    # volume spike pushes a banner above it, so this cannot be a fixed
    # line number.
    head = next(i for i, line in enumerate(withmkt) if line.startswith("SPCX $"))
    if not withmkt[head + 1].startswith("S&P 500"):
        failures.append(f"the market belongs directly under the stock's own "
                        f"move, got {withmkt[head + 1]!r}")
    if "S&P" in describe("SPCX", up, day, buyers):
        failures.append("no market line when no market number was passed")

    # And every failure path in the fetch is silence, not an exception.
    if market_move(None, at, at) is not None:
        failures.append("no benchmark close should yield no market move")
    if market_move(0.0, at, at) is not None:
        failures.append("a zero benchmark close should not divide")

    if PRIORITY_UPDATE >= PRIORITY_SUMMARY:
        failures.append("the routine stream must be quieter than the alarm")

    # --- what earns a noise ------------------------------------------------
    def reading(score, ratio, vwap_gap=1.0, at_minute=35):
        """A candle, its lean and its day, at one clock minute."""
        when = datetime.combine(date(2026, 9, 18), time(10, at_minute), tzinfo=ET)
        bar = Candle(at=when, open=150.0, high=151.0, low=149.5, close=150.5,
                     volume=ratio * 40_000, usual_volume=40_000)
        return (bar, Lean(score, 5, 5, 5, volume_ratio=ratio),
                Day(last=150.5, high=151.0, low=149.0,
                    prev_close=149.0, vwap=150.5 - vwap_gap), when)

    quiet = Alarms()
    bar, lean, dctx, when = reading(0.90, 1.0)          # direction, no volume
    if quiet.reason(bar, lean, dctx) is not None:
        failures.append("a pressing lean on ordinary volume must stay silent")
    bar, lean, dctx, when = reading(0.50, 3.0)          # volume, no direction
    if quiet.reason(bar, lean, dctx) is not None:
        failures.append("a volume spike with no direction must stay silent")

    ring = Alarms()
    bar, lean, dctx, when = reading(0.72, 2.0)
    call = ring.reason(bar, lean, dctx)
    if not call or call[0] != "buy":
        failures.append(f"72 on 2x volume should ring the buy side: {call}")
    bar, lean, dctx, when = reading(0.28, 2.0, at_minute=36)
    call = ring.reason(bar, lean, dctx)
    if not call or call[0] != "sell":
        failures.append(f"28 on 2x volume should ring the sell side: {call}")

    if ring.sound_for("buy") == ring.sound_for("sell"):
        failures.append("the two directions must not share a sound")
    if ring.sound_for("buy") != SOUND_BUY or ring.sound_for("sell") != SOUND_SELL:
        failures.append("the sounds should not be swapped")
    if ring.sound_for("buy", "butler", "klaxon") != "butler":
        failures.append("an overridden sound should be used")

    # One event, one ring.
    cool = Alarms()
    base = datetime.combine(date(2026, 9, 18), time(10, 0), tzinfo=ET)
    if not cool.should_sound("buy", base):
        failures.append("the first ring should always sound")
    if cool.should_sound("buy", base + timedelta(minutes=5)):
        failures.append("a second ring inside the cooldown should be suppressed")
    if not cool.should_sound("sell", base + timedelta(minutes=5)):
        failures.append("the other direction has its own cooldown")
    if not cool.should_sound("buy", base + timedelta(minutes=SOUND_COOLDOWN_MINUTES)):
        failures.append("the cooldown should expire")

    # A VWAP cross needs a previous side to cross from, and the side must
    # be tracked on every reading -- not only on the ones that ring.
    cross = Alarms()
    bar, lean, dctx, when = reading(0.50, 2.0, vwap_gap=-1.0)   # below
    cross.reason(bar, lean, dctx)
    bar, lean, dctx, when = reading(0.50, 2.0, vwap_gap=1.0)    # now above
    call = cross.reason(bar, lean, dctx)
    if not call or call[0] != "buy" or "VWAP" not in call[1]:
        failures.append(f"crossing above VWAP on volume rings buy: {call}")
    quiet_cross = Alarms()
    bar, lean, dctx, when = reading(0.50, 1.0, vwap_gap=-1.0)
    quiet_cross.reason(bar, lean, dctx)
    bar, lean, dctx, when = reading(0.50, 1.0, vwap_gap=1.0)
    if quiet_cross.reason(bar, lean, dctx) is not None:
        failures.append("a VWAP cross with no volume behind it is not news")

    # 23 Sep 2026, the session this threshold was chosen against: five
    # volume alarms, and only the 13:50 one carried a direction. If this
    # ever reads differently the thresholds moved without anyone saying so.
    observed = ((0.48, 2.7), (0.26, 2.7), (0.45, 1.8), (0.60, 1.5), (0.60, 1.5))
    rings = []
    for i, (score, ratio) in enumerate(observed):
        bar, lean, dctx, when = reading(score, ratio, at_minute=i)
        got = Alarms().reason(bar, lean, dctx)
        if got:
            rings.append((score, got[0]))
    if rings != [(0.26, "sell")]:
        failures.append(f"23 Sep should ring once, sell at 26: {rings}")

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

    print("-" * 56)
    print(alarm)
    print("-" * 56)
    noon = datetime.combine(date(2026, 9, 18), time(10, 35), tzinfo=ET)
    print(describe("SPCX", Candle(at=noon, open=153.1, high=154.05, low=153.0,
                                  close=153.95, volume=96_000, trades=540,
                                  usual_volume=40_000), day,
                   Lean(0.94, 99_000, 1_000, 5, volume_ratio=2.4)))
    print("-" * 56)
    print(f"\n  Ten minutes → candles          : {len(rolled)}, aligned to the clock")
    print("  Forming bar dropped            : minute and candle")
    print(f"  Lean, highs / lows / no range  : {buyers.score:.2f} / "
          f"{sellers.score:.2f} / {middling.score:.2f}")
    print(f"  Priorities, update vs alarm    : {PRIORITY_UPDATE} vs {PRIORITY_SUMMARY}")
    print(f"  Phone quiet after              : {DETAIL_UNTIL:%H:%M} "
          f"(spikes still arrive, silently)")
    print(f"  Rings at                       : lean past "
          f"{PRESSING_LOW * 100:.0f}/{PRESSING_HIGH * 100:.0f} or a VWAP "
          f"cross, on {ALARM_VOLUME:.1f}x volume")
    print(f"  Sounds, buy / sell             : {SOUND_BUY} / {SOUND_SELL}, "
          f"one per direction per {SOUND_COOLDOWN_MINUTES} min")
    print("  23 Sep replayed                : 1 ring of 5 volume spikes")
    print("  Test push                      : devices and sound names "
          "checked first")
    print("  Half-updated folder            : refused at startup, not at "
          "the first alarm")
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
    parser.add_argument("--buy-sound", default=SOUND_BUY, metavar="NAME",
                        help=f"Pushover sound for a buy-side alarm "
                             f"(default {SOUND_BUY}; a custom sound uploaded "
                             f"to your Pushover account works by name)")
    parser.add_argument("--sell-sound", default=SOUND_SELL, metavar="NAME",
                        help=f"Pushover sound for a sell-side alarm "
                             f"(default {SOUND_SELL})")
    parser.add_argument("--detail-until", dest="detail", metavar="HH:MM",
                        default=f"{DETAIL_UNTIL:%H:%M}",
                        help=f"Minute updates and candle summaries reach the "
                             f"phone until this time; after it only volume "
                             f"spikes do (default {DETAIL_UNTIL:%H:%M})")
    parser.add_argument("--dry-run", action="store_true", help="Print, do not send")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--test-push", action="store_true",
                        help="Send one of each kind to the phone: a silent "
                             "reading, a buy ring, a sell ring")
    parser.add_argument("--list-sounds", action="store_true",
                        help="Ask Pushover which sound names this app can use")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    # Before anything long-running. A mismatch found at 09:41 is a
    # mismatch found too late.
    stale = stale_sender()
    if stale and not args.list_sounds:
        print(f"{stale}\n")
        return 1
    if args.list_sounds:
        return list_sounds()
    if args.test_push:
        return test_push(args.buy_sound, args.sell_sound)

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
                    args.volume_alert, args.pdf_every, args.db, detail,
                    args.buy_sound, args.sell_sound)


if __name__ == "__main__":
    raise SystemExit(main())
