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
import sys
import sqlite3
import statistics
import time as time_mod
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
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
try:
    from spcx_alert import PRIORITY_EMERGENCY
except ImportError:      # an older spcx_alert.py: stale_sender() refuses it
    PRIORITY_EMERGENCY = None

SYMBOL = "SPCX"
BAR_MINUTES = 5
#: Fifteen minutes before the bell. Early enough to watch the run-up
#: into the open, and far enough from it that the first real candle is
#: not also the first thing on the page. The clock dial deliberately
#: still starts at 09:30: it charts market hours, this charts a morning.
WINDOW_START = time(9, 15)
WINDOW_END = time(16, 0)

#: When regular trading hours end. Past it the tape is a different
#: animal: a few hundred shares can print, and the volume baseline is
#: thin to absent. The move alarm stops requiring volume confirmation
#: after this -- see Alarms.reason, which is the only thing that made
#: an after-hours alarm possible at all.
REGULAR_CLOSE = time(16, 0)

#: When regular trading hours start. Before it, like after the close, the
#: stock drifts rather than jumps, which is what --ah-percent is for.
REGULAR_OPEN = time(9, 30)

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

    The window runs 04:00-20:00, the whole extended session. It used to
    stop at 16:00, which left every after-hours slot without a median --
    so vol_ratio was None after the close, the move alarm's volume gate
    could never pass, and no after-hours alarm could fire at all. The
    gate no longer applies then (see Alarms.reason), but the ratio is
    still worth having in the message.

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
                datetime.combine(past, time(20, 0), tzinfo=ET),
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

#: The lean bands that used to earn a noise -- 30 and 70 -- are gone,
#: along with the 23 Sep calibration behind them. They were chosen
#: against a session that fell 3.53%, where they fired usefully once; on
#: an ordinary afternoon a one-sided tape is the normal state and they
#: fired constantly. The lean still appears in every message, as the word
#: and the slider. It no longer decides whether the phone makes a sound.
#: See MOVE_PERCENT.

#: An alarm needs participation as well as direction. Same multiple as
#: the spike alarm, so the loudest word and the noise mean one thing.
ALARM_VOLUME = VOLUME_ALERT_MULTIPLE

#: A noise means PRICE IS MOVING, not that the tape is one-sided.
#:
#: The lean measures which side is hitting the tape. It says nothing
#: about where price goes, and on a busy afternoon that goes nowhere it
#: is one-sided almost continuously. 25 Sep 2026 is the case: the phone
#: rang six times between 12:20 and 15:43 -- including "sellers pressing
#: on 5.1x volume" at 14:14, which accompanied a move of 0.14% over
#: twelve minutes -- while the whole afternoon's range was 0.89%.
#: Eleven interruptions, counting the other two programs, on a day when
#: nothing happened.
#:
#: So the trigger is the move itself, measured against the oldest price
#: still inside the window. Direction comes from its sign, which is also
#: the first buy alarm this has ever had: every one of those six was a
#: sell, because a rising tape rarely presses hard enough to cross the
#: band.
MOVE_PERCENT, MOVE_MINUTES = 0.50, 5

#: A second threshold for outside 09:30-16:00, or None to use MOVE_PERCENT
#: there too. After hours the stock drifts: a move worth acting on can
#: take an hour to reach a size that takes five minutes at midday, so a
#: band calibrated on the session is late in the evening. None by default
#: because no evening number has been measured yet -- pick it with
#: --replay against your own evenings, not by ear.
AH_PERCENT: Optional[float] = None

#: After the level alarm's first ring, ring again each time price gets
#: this much further from the anchor, or None to step by the band itself
#: (0.5 rings at 0.5, 1.0, 1.5...). 0 rings once per crossing, which was
#: the only behaviour before 14 Oct -- and meant a +3% evening was
#: reported once, at +0.5%, and then never again.
LEVEL_STEP: Optional[float] = None

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

#: THE TREND ALARM (14 Oct). The speed alarm answers "is it moving fast
#: right now", so a zigzag climb rang buy, sell, buy, sell and never said
#: the one thing that mattered: each high was higher than the last. This
#: follows the RUN instead. It rings with its own sounds -- Pushover's
#: built-in "climb" and "falling" -- so the shape of the move is audible
#: from a pocket: climb... climb... climb... falling.
#:
#: TREND_STEP is how far a run must extend, from where it started, to
#: ring again. TREND_GIVEBACK is how far a run must reverse off its
#: extreme to count as over (None = the same as the step). Pullbacks
#: smaller than that are silent: they are the zig of a zigzag, not news.
#: Neither number is measured yet -- choose them with --replay.
TREND_STEP: float = 0.50
TREND_GIVEBACK: Optional[float] = None
SOUND_CLIMB, SOUND_FALLING = "climb", "falling"

#: THE MARKET ALARM (14 Oct). The S&P 500's move was already fetched
#: every pass and printed under the stock's -- as text, which nobody
#: hears. This rings once the market is down MARKET_DROP percent from
#: yesterday's close, and again every MARKET_STEP further, with a sound
#: that means "the whole market", not "this stock".
#:
#: 15 Oct: from -0.5% rather than -1%, and every ring at MARKET_EXTENDED
#: or deeper goes out as a Pushover emergency -- it repeats every minute
#: until tapped Acknowledge, for up to 30 minutes (spcx_alert.py holds
#: those two numbers). 0 turns the extended alarm off.
MARKET_DROP: float = 0.50
MARKET_STEP: float = 0.50
MARKET_EXTENDED: float = 2.00
SOUND_MARKET = "siren"

#: PERSISTENT ALARMS (16 Oct). The first ring of a new move -- the first
#: one in that direction for PERSIST_GAP minutes -- goes out as a Pushover
#: emergency, repeating until it is tapped Acknowledge; every later step
#: of the same move rings once. Up and down are treated alike: a drop
#: after hours, when no stop is working, matters as much as a climb.
#: 0 is off, the default; the "SPCX alarms" launcher option turns it on.
PERSIST_GAP: int = 0

#: Started late -- at 10:00 with --from 04:00, say -- the first pass reads
#: six hours of minutes. They still feed every alarm, so each knows where
#: price has been, but only minutes from the last LIVE_GRACE before start
#: onwards reach the phone: a ring about 06:40 arriving at 10:00 is not
#: news, and as an emergency it would be a false one.
LIVE_GRACE: int = 3

#: THE OVERNIGHT SESSION (18 Oct). Alpaca has nothing after 20:00, so the
#: night is read from IB Gateway's OVERNIGHT route instead -- probed live
#: on 18 Oct at 21:53: bid and ask, 15 prints a minute, and 1-minute
#: history current to the minute (the SMART route's stopped at Friday
#: 19:59). It trades Sunday to Thursday nights, 20:00 to 03:50.
OVERNIGHT_START = time(20, 0)
OVERNIGHT_END = time(3, 50)
#: Sunday (6) and Monday to Thursday: the evenings a session OPENS on.
OVERNIGHT_NIGHTS = (6, 0, 1, 2, 3)
IBKR_HOST = os.environ.get("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.environ.get("IBKR_PORT", "4001"))
#: Its own client id: Scanner Studio holds 7701 and the probe 7702, and a
#: second connection on a taken id is refused.
IBKR_CLIENT_ID = int(os.environ.get("IBKR_ALARM_CLIENT_ID", "7703"))
#: Minutes without an answer from IB Gateway before the phone is told the
#: overnight alarms are blind. A quiet tape is not silence -- an empty
#: answer is still an answer -- so this only counts failures.
GATEWAY_BLIND_MINUTES = 10


def overnight_hours(t: time) -> bool:
    """Whether a clock time falls in the overnight session's hours."""
    return t >= OVERNIGHT_START or t < OVERNIGHT_END

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
    #: What the range line calls this stretch of trading. Overnight it is
    #: the night's own range, and calling it the day's would be wrong.
    label: str = "Day"

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
        parts = [f"{self.label} {self.low:,.2f} – {self.high:,.2f}"]
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

    One trigger: price has travelled far enough, fast enough, with
    volume behind it. Direction is the sign of the move.

    It used to be the lean crossing a pressing band, or a VWAP cross, on
    the same volume. Both are readings about the tape rather than about
    price, and a one-sided tape that moves nothing is what most busy
    afternoons look like -- see MOVE_PERCENT for the day that settled
    it. The VWAP side is still tracked, because which side of it price
    sits on is worth knowing and cheap to keep, but a crossing with no
    movement behind it is not what anyone wants a phone to shout about.

    None of this claims an edge. Nothing measured here beats a coin
    flip, and a sound that meant "buy" would be asserting otherwise.
    What it says is: price is moving now, with participation behind it.
    Go and look.
    """

    cooldown: int = SOUND_COOLDOWN_MINUTES
    volume: float = ALARM_VOLUME
    percent: float = MOVE_PERCENT
    minutes: int = MOVE_MINUTES
    #: Ring on run-ups only, and stay silent on the way down. For a day
    #: spent out of the position watching for a reason to come back: a
    #: decline is not news when you are already flat, and an alarm that
    #: rings for both is an alarm you start ignoring.
    up_only: bool = False
    #: Anchor for the level alarm: a price you name, or None to use
    #: today's regular-session close. A held position asks "how far from
    #: the close", or "how far from what I paid" -- neither is a question
    #: about the last five minutes.
    anchor_price: Optional[float] = None
    #: Which side of the band price is currently outside, so a level that
    #: STAYS true rings once per crossing rather than every minute. It
    #: clears when price comes back inside, which re-arms it -- the same
    #: reasoning as the scanner's dedupe: a lapsed setup that returns has
    #: to be able to alert again, or it is silenced forever.
    beyond: Optional[str] = None
    #: The band used outside 09:30-16:00 by BOTH alarms, or None to use
    #: `percent` all day. See AH_PERCENT.
    ah_percent: Optional[float] = AH_PERCENT
    #: How much further from the anchor earns the level alarm another
    #: ring. None steps by the band; 0 rings once per crossing. See
    #: LEVEL_STEP.
    level_step: Optional[float] = LEVEL_STEP
    #: How many steps past the band this crossing has already rung for.
    #: Only a NEW furthest step rings, so price that sags back a step and
    #: returns does not ring twice for the same ground.
    rung: int = 0
    #: Whether the last reason() came from the level alarm, and whether
    #: that was a further step of a crossing that had already rung.
    from_level: bool = False
    stepped: bool = False
    fired: Dict[str, datetime] = field(default_factory=dict)
    above_vwap: Optional[bool] = None
    recent: List[Tuple[datetime, float]] = field(default_factory=list)

    def threshold(self, at: datetime) -> float:
        """The band for a reading at `at`: the evening one outside regular
        hours when one was given, the session one otherwise."""
        outside = at.time() < REGULAR_OPEN or at.time() >= REGULAR_CLOSE
        return self.ah_percent if (self.ah_percent and outside) else self.percent

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

    def anchor_for(self, day: Optional[Day],
                   premarket: bool = False) -> Optional[float]:
        """The price the level alarm measures from, or None if there is
        none yet.

        A named price wins. Otherwise today's regular-session close, which
        read_day freezes at 16:00 -- so after the bell it is exactly the
        number every quote screen compares an after-hours print against.
        None means the watcher was started with no regular-hours bars to
        close on; run_live says so out loud rather than leaving the alarm
        quietly switched off.
        """
        if self.anchor_price:
            return self.anchor_price
        if premarket:
            # Before the bell the close that matters is yesterday's -- the
            # number every pre-market quote is shown against.
            return day.prev_close if day is not None else None
        return day.last if day is not None else None

    def _level(self, candle: Candle,
               day: Optional[Day]) -> Optional[Tuple[str, float, float]]:
        """(direction, percent, anchor) once price sits far enough from the
        anchor -- with NO time window at all.

        The 5-minute window in _move measures SPEED, and after hours there
        is none: a 0.6% slide spread over forty minutes is 0.08% per
        five-minute slice and never trips it. Over an evening that is the
        difference between being told and not. So this asks the other
        question -- how far from the anchor are we, however long it took.

        Regular hours are left alone: inside the session the speed alarm
        is the one that has been calibrated, and a level alarm on top of
        it would ring all afternoon on any trending day.

        Once outside the band it rings again at each further step (see
        LEVEL_STEP), so an evening that keeps going is reported as it
        goes rather than once at the start. Only a new furthest step
        rings: sagging back a step and returning covers no new ground.
        """
        self.stepped = False
        t = candle.at.time()
        if REGULAR_OPEN <= t < REGULAR_CLOSE:
            self.beyond, self.rung = None, 0
            return None
        # 16 Oct: pre-market too, measured from yesterday's close. The
        # speed alarm alone missed slow pre-market drifts the same way it
        # missed slow evenings.
        anchor = self.anchor_for(day, premarket=t < REGULAR_OPEN)
        if not anchor:
            return None
        percent = 100.0 * (candle.close - anchor) / anchor
        limit = self.threshold(candle.at)
        if abs(percent) < limit or (self.up_only and percent <= 0):
            # Back inside the band -- or, watching run-ups only, on the
            # wrong side of it -- re-arms. Returning early for up_only
            # BEFORE this reset used to leave a buy crossing armed through
            # a dip, so the next run-up never rang.
            self.beyond, self.rung = None, 0
            return None
        side = "buy" if percent > 0 else "sell"
        step = limit if self.level_step is None else self.level_step
        reached = 1 + (int((abs(percent) - limit) / step + 1e-9) if step > 0 else 0)
        if self.beyond == side and reached <= self.rung:
            return None                 # this ground already rang
        self.stepped = self.beyond == side
        self.beyond, self.rung = side, reached
        return side, percent, anchor

    def _move(self, candle: Candle) -> Optional[Tuple[str, float, float]]:
        """(direction, percent, minutes) once price has travelled far enough.

        Measured against the OLDEST price still inside the window, so a
        slide that arrives in five one-minute steps counts as the slide
        it is. Bar to bar it would never trigger: on 25 Sep the largest
        single step all afternoon was 0.24%.
        """
        self.recent.append((candle.at, candle.close))
        oldest = candle.at - timedelta(minutes=self.minutes)
        self.recent = [(at, price) for at, price in self.recent if at >= oldest]
        if len(self.recent) < 2:
            return None
        then_at, then = self.recent[0]
        if not then:
            return None
        percent = 100.0 * (candle.close - then) / then
        if self.up_only and percent <= 0:
            return None
        if abs(percent) < self.threshold(candle.at):
            return None
        return ("buy" if percent > 0 else "sell", percent,
                (candle.at - then_at).total_seconds() / 60)

    def reason(self, candle: Candle, lean: Optional[Lean],
               day: Optional[Day]) -> Optional[Tuple[str, str]]:
        """(direction, why) for a reading that earns a noise, or None.

        Both sides are updated on every reading, fired or not. The VWAP
        side has to be measured against the last bar rather than the
        last alarm or a quiet stretch would swallow a crossing, and the
        price window has to see every bar or the move it measures would
        be the move since the last thing that happened to be interesting.
        """
        self._cross(day)
        self.from_level = False
        # Both sides see every bar, fired or not: _move's window and
        # _level's crossing state are only correct if nothing is skipped.
        moved = self._move(candle)
        level = self._level(candle, day)

        # The level alarm answers the question a HELD position asks, so it
        # speaks first when both have something to say.
        if level is not None:
            side, percent, anchor = level
            self.from_level = True
            whence = ("your price" if self.anchor_price
                      else f"the {REGULAR_CLOSE:%H:%M} close"
                      if overnight_hours(candle.at.time())
                      else "yesterday's close"
                      if candle.at.time() < REGULAR_OPEN
                      else f"the {REGULAR_CLOSE:%H:%M} close")
            return side, (f"{'further: ' if self.stepped else ''}"
                          f"{'up' if percent > 0 else 'down'} "
                          f"{abs(percent):.2f}% from {whence} "
                          f"({anchor:.2f} \u2192 {candle.close:.2f})")

        if not moved:
            return None
        direction, percent, over = moved

        # Volume confirmation is a regular-hours idea. After the close
        # there is barely any volume to be unusual against, and a gate
        # that cannot pass is not a filter -- it is silence. Price alone
        # rings then, and the message says the confirmation is missing
        # so the reading is never mistaken for a confirmed one.
        loud = candle.vol_ratio is not None and candle.vol_ratio >= self.volume
        t = candle.at.time()
        outside = not (REGULAR_OPEN <= t < REGULAR_CLOSE)
        if not (loud or outside):
            return None

        why = (f"{'up' if percent > 0 else 'down'} "
               f"{abs(percent):.2f}% in {over:.0f} min")
        if candle.vol_ratio is not None:
            why += f" on {candle.vol_ratio:.1f}x volume"
        if outside and not loud:
            when = ("overnight" if overnight_hours(t)
                    else "pre-market" if t < REGULAR_OPEN else "after hours")
            why += (f" \u00b7 {when}"
                    f", no volume confirmation")
        return direction, why

    def should_sound(self, direction: str, at: datetime) -> bool:
        last = self.fired.get(direction)
        if last is not None and (at - last) < timedelta(minutes=self.cooldown):
            return False
        self.fired[direction] = at
        return True

    def should_ring(self, call: Optional[Tuple[str, str]], at: datetime) -> bool:
        """Whether reason()'s verdict reaches the phone as a noise.

        A level ring skips the cooldown. Each one is a new step from the
        anchor, already deduplicated by _level, and a run that climbs two
        steps in ten minutes is exactly the run that must not be held
        back for fifteen. It still stamps the cooldown, so the speed alarm
        does not echo it a minute later.
        """
        if call is None:
            return False
        if self.from_level:
            self.fired[call[0]] = at
            return True
        return self.should_sound(call[0], at)

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


#: THE OPENING CALL (19 Oct). Once the first regular minute has closed,
#: one message a day says which way the S&P 500 opened against yesterday's
#: close. Information, not an alarm: a normal notification with the
#: phone's default sound, sent even with --rings-only. Until
#: OPEN_CALL_LATEST it keeps trying if the bar is not in yet; after that
#: the day's call is dropped rather than sent stale.
OPEN_CALL_AT = time(9, 31)
OPEN_CALL_LATEST = time(9, 40)


def opening_move(prev_close: Optional[float], day: date) -> Optional[float]:
    """The S&P 500's first regular minute against yesterday's close, in
    percent, or None. The 09:30 bar's close: the price once the opening
    auction has printed and the first minute has settled."""
    if prev_close is None or prev_close <= 0:
        return None
    at = datetime.combine(day, REGULAR_OPEN, tzinfo=ET)
    try:
        frame = fetch_minutes(BENCHMARK, at, at + timedelta(minutes=1))
        if frame is None or frame.empty:
            return None
        first = frame[frame.index == at]
        if first.empty:
            return None
        return 100.0 * (float(first["close"].iloc[0]) - prev_close) / prev_close
    except Exception:      # noqa: BLE001 -- a missing call is not a missing alarm
        return None


def open_call_due(now: datetime, done: bool) -> bool:
    """Whether to try the opening call on this pass."""
    return not done and OPEN_CALL_AT <= now.time() <= OPEN_CALL_LATEST


def opening_call(change_pct: float) -> str:
    """The opening call's text: direction first, so the lock screen says it."""
    up = change_pct >= 0
    return (f"S&P 500 OPEN {'▲ UP' if up else '▼ DOWN'} {change_pct:+.2f}% "
            f"from yesterday's close")


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
            # Overnight the session's own close is the reference, the one
            # a 24-hour quote screen shows the night's move against.
            since = ("since the close" if overnight_hours(candle.at.time())
                     else "today")
            head += f"  {arrow} {change:+.2f}% {since}"
    if overnight_hours(candle.at.time()):
        head += "  (overnight)"
    elif candle.at.time() < time(9, 30):
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


@dataclass
class Persist:
    """Which rings are the FIRST of a new move, and so persistent.

    A ring is urgent when nothing has rung in its direction for `gap`
    minutes. Every ring updates the clock, so a move that keeps stepping
    stays one emergency followed by ordinary rings, and up and down are
    tracked apart: a climb that turns into a drop alarms on the drop too.
    """

    gap: int = PERSIST_GAP
    last: Dict[str, datetime] = field(default_factory=dict)

    def urgent(self, direction: str, at: datetime) -> bool:
        if self.gap <= 0:
            return False
        prev = self.last.get(direction)
        self.last[direction] = at
        return prev is None or (at - prev) >= timedelta(minutes=self.gap)


def ring_direction(moved: Optional[Tuple[str, str]],
                   call: Optional[Tuple[str, str]]) -> str:
    """'up' or 'down' for a ring, whichever alarm made it."""
    if moved:
        return moved[0]
    return "up" if call and call[0] == "buy" else "down"


PERSIST_NOTE = "Repeats every minute until you tap Acknowledge."


def before_start(stamp: datetime, started: datetime) -> bool:
    """A minute that closed before the watcher was running."""
    return stamp < started - timedelta(minutes=LIVE_GRACE)


def to_phone(rings_only: bool, at: time, spiked: bool, detail_until: time,
             ringing: bool) -> bool:
    """Whether a minute's message leaves the machine. With rings_only only
    an alarm does -- no quiet updates, no volume-spike notes -- so a phone
    left on a desk all day hears from this watcher only when it should."""
    if rings_only:
        return ringing
    return reaches_phone(at, spiked, detail_until) or ringing


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
    if not sounds_supported():
        return (f"spcx_alert.py is out of date: its send_pushover() takes no "
                f"sound, so no alarm could play one.\n"
                f"  Re-download it from {SOUNDS_FROM_COMMIT} or later, into the "
                f"same folder as this file.")
    if PRIORITY_EMERGENCY is None:
        # Pushover refuses an emergency that does not say how to repeat,
        # so an old sender would fail at -2% -- the ring that matters most.
        return ("spcx_alert.py is out of date: it cannot send an emergency "
                "alarm, so the S&P 500's extended alarm would be refused.\n"
                "  Re-download it from the same branch as this file, into "
                "the same folder.")
    return None


def test_push(buy_sound: str = SOUND_BUY, sell_sound: str = SOUND_SELL,
              pause: int = 6, climb_sound: str = SOUND_CLIMB,
              falling_sound: str = SOUND_FALLING,
              market_sound: str = SOUND_MARKET) -> int:
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
        for role, name in (("buy", buy_sound), ("sell", sell_sound),
                           ("climb", climb_sound), ("falling", falling_sound),
                           ("market", market_sound)):
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

    # The extended alarm, so its first real use is not its first hearing.
    time_mod.sleep(pause)
    drill = MarketAlarm()
    why = drill.update(-MARKET_EXTENDED - 0.03)
    body, priority = market_push(drill, why, -MARKET_EXTENDED - 0.03)
    outcome = send_pushover(body, title=f"TEST — {why}", priority=priority,
                            sound=market_sound) or "sent"
    print(f"  {'the extended alarm':<20} priority {priority:<3} "
          f"{market_sound:<16} {outcome}")

    print("\nFour should have arrived. The first without a sound, then the")
    print("buy, then the sell, then the S&P 500 extended alarm -- which keeps")
    print("ringing every minute until you open it and tap Acknowledge. If a")
    print("sound played that you did not choose, the name does not match the")
    print("account — that is the failure this cannot detect from here.\n")
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


@dataclass
class Trend:
    """Follows the run, not the last five minutes. See TREND_STEP.

    One state machine. Until something rings it watches the lowest and
    highest price seen. A rise of a step off the low, made at a new high,
    starts an UP run ("climbing"); each further step at a new high rings
    "still climbing". A fall of `giveback` off the run's high ends it
    ("giving it back") and starts a DOWN run measured from that high, so
    a fade that keeps going keeps ringing ("still falling"). The mirror
    holds: a bounce of `giveback` off a falling run's low says so and
    starts an up run from that low.

    Anything smaller than `giveback` against the run is silent. That is
    the point: a 0.3% dip in a 2% climb is the zig of a zigzag, and the
    speed alarm already reports it if it was fast.
    """

    step: float = TREND_STEP
    giveback: Optional[float] = TREND_GIVEBACK
    mode: Optional[str] = None          # None, "up" or "down"
    base: float = 0.0                   # where the run started
    base_at: Optional[datetime] = None
    ext: float = 0.0                    # the run's extreme so far
    ext_at: Optional[datetime] = None
    rungs: int = 0                      # steps already rung for this run
    lo: float = 0.0                     # before any run: lowest and highest
    lo_at: Optional[datetime] = None
    hi: float = 0.0
    hi_at: Optional[datetime] = None

    @property
    def back(self) -> float:
        return self.step if self.giveback is None else self.giveback

    def _steps(self, pct: float) -> int:
        return int(pct / self.step + 1e-9)

    def _start(self, mode: str, base: float, base_at: datetime,
               price: float, at: datetime) -> float:
        self.mode, self.base, self.base_at = mode, base, base_at
        self.ext, self.ext_at = price, at
        moved = abs(price - base) / base * 100.0
        self.rungs = self._steps(moved)
        return moved

    def update(self, at: datetime, price: float) -> Optional[Tuple[str, str]]:
        """("up" or "down", message) when this price earns a ring."""
        if self.step <= 0 or price <= 0:
            return None
        if self.mode is None:
            if self.base_at is None:            # very first reading
                self.lo, self.lo_at = price, at
                self.hi, self.hi_at = price, at
                self.base_at = at
                return None
            if price < self.lo:
                self.lo, self.lo_at = price, at
            if price > self.hi:
                self.hi, self.hi_at = price, at
            if price >= self.hi and (price - self.lo) / self.lo * 100 >= self.step:
                rise = self._start("up", self.lo, self.lo_at, price, at)
                return "up", (f"climbing: +{rise:.2f}% from the "
                              f"{self.base_at:%H:%M} low "
                              f"({self.base:.2f} → {price:.2f})")
            if price <= self.lo and (self.hi - price) / self.hi * 100 >= self.step:
                drop = self._start("down", self.hi, self.hi_at, price, at)
                return "down", (f"falling: -{drop:.2f}% from the "
                                f"{self.base_at:%H:%M} high "
                                f"({self.base:.2f} → {price:.2f})")
            return None

        if self.mode == "up":
            if price > self.ext:
                self.ext, self.ext_at = price, at
                rise = (price - self.base) / self.base * 100
                if self._steps(rise) > self.rungs:
                    self.rungs = self._steps(rise)
                    return "up", (f"still climbing: +{rise:.2f}% from the "
                                  f"{self.base_at:%H:%M} low "
                                  f"({self.base:.2f} → {price:.2f}), new high")
                return None
            off = (self.ext - price) / self.ext * 100
            if off >= self.back:
                high, high_at = self.ext, self.ext_at
                self._start("down", high, high_at, price, at)
                return "down", (f"giving it back: -{off:.2f}% from the "
                                f"{high_at:%H:%M} high ({high:.2f} → {price:.2f})")
            return None

        # mode == "down"
        if price < self.ext:
            self.ext, self.ext_at = price, at
            drop = (self.base - price) / self.base * 100
            if self._steps(drop) > self.rungs:
                self.rungs = self._steps(drop)
                return "down", (f"still falling: -{drop:.2f}% from the "
                                f"{self.base_at:%H:%M} high "
                                f"({self.base:.2f} → {price:.2f}), new low")
            return None
        off = (price - self.ext) / self.ext * 100
        if off >= self.back:
            low, low_at = self.ext, self.ext_at
            self._start("up", low, low_at, price, at)
            return "up", (f"bouncing: +{off:.2f}% off the {low_at:%H:%M} low "
                          f"({low:.2f} → {price:.2f})")
        return None


@dataclass
class MarketAlarm:
    """Rings when the S&P 500 is down `drop`% from yesterday's close, and
    again every `step`% further. Back above the line re-arms it, so a
    market that recovers and then breaks down again is heard again.

    `urgent` says whether the ring just returned is at or past `extended`,
    the depth that goes out as an emergency rather than a single sound."""

    drop: float = MARKET_DROP
    step: float = MARKET_STEP
    extended: float = MARKET_EXTENDED
    rung: int = 0
    urgent: bool = False

    def update(self, change_pct: Optional[float]) -> Optional[str]:
        self.urgent = False
        if change_pct is None or self.drop <= 0:
            return None
        down = -change_pct
        if down < self.drop:
            self.rung = 0
            return None
        reached = 1 + (int((down - self.drop) / self.step + 1e-9) if self.step > 0 else 0)
        if reached <= self.rung:
            return None
        further = self.rung > 0
        self.rung = reached
        self.urgent = self.extended > 0 and down + 1e-9 >= self.extended
        return (f"MARKET{' ALARM' if self.urgent else ''}: S&P 500 "
                f"{'further, ' if further else ''}down "
                f"{down:.2f}% from yesterday's close")


def market_push(alarm: "MarketAlarm", why: str, change_pct: Optional[float]
                ) -> Tuple[str, int]:
    """The body and priority for a market ring the alarm just returned."""
    body = (market_line(change_pct) or why) + (
        f"\nRings again at every {alarm.step:.2f}% further.")
    if alarm.urgent:
        return (body + "\nRepeats every minute until you tap Acknowledge.",
                PRIORITY_EMERGENCY)
    return body, PRIORITY_SUMMARY


def market_policy(alarm: "MarketAlarm") -> str:
    """One line saying when the market alarm rings, and how loudly."""
    text = (f"S&P 500 down {alarm.drop:.2f}% from yesterday's close, then "
            f"every {alarm.step:.2f}% further")
    if alarm.extended > 0:
        text += f"; extended (repeats until acknowledged) from {alarm.extended:.2f}%"
    return text


def replay_alarms(alarms: "Alarms", minutes: pd.DataFrame,
                  minute_base: Dict[time, float],
                  prev_close: Optional[float],
                  trend: Optional["Trend"] = None,
                  ) -> List[Tuple[datetime, str, str, float]]:
    """(time, kind, why, price) for every minute that would have rung.

    kind is "buy"/"sell" for the price alarms and "up"/"down" for the
    trend alarm. The same per-minute judgement run_live makes -- the same
    candle, lean and day built the same way, minutes with no trades
    skipped as live skips them, the same should_ring, the trend fed the
    same closes in the same order -- so a threshold chosen here is a
    threshold that behaves this way on the phone.
    """
    rings = []
    for stamp in minutes.index:
        minute = to_candles(minutes.loc[[stamp]], minute_base)[0]
        if minute.volume <= 0:
            continue
        upto = minutes[minutes.index <= stamp]
        call = alarms.reason(minute, read_lean(upto, baseline=minute_base),
                             read_day(upto, prev_close))
        moved = trend.update(stamp, minute.close) if trend is not None else None
        if moved:
            rings.append((stamp, moved[0], moved[1], minute.close))
        if alarms.should_ring(call, stamp):
            rings.append((stamp, call[0], call[1], minute.close))
    return rings


def replay_market(alarm: "MarketAlarm", frame: pd.DataFrame,
                  prev_close: Optional[float]) -> List[Tuple[datetime, str]]:
    """(time, why) for every minute the market alarm would have rung."""
    if not prev_close or frame is None or frame.empty:
        return []
    rings = []
    for stamp, close in frame["close"].items():
        why = alarm.update(100.0 * (float(close) - prev_close) / prev_close)
        if why:
            rings.append((stamp, why))
    return rings


#: How each kind of ring is labelled in a replay listing.
RING_LABEL = {"buy": "BUY  ", "sell": "SELL ", "up": "CLIMB", "down": "FALL "}


def alarm_policy(alarms: "Alarms") -> str:
    """One line saying which bands a run or replay is judging by."""
    text = f"{alarms.percent:.2f}% in {alarms.minutes} min"
    if alarms.ah_percent:
        text += f", {alarms.ah_percent:.2f}% outside 09:30-16:00"
    step = alarms.level_step
    text += ("; level alarm once per crossing" if step == 0 else
             "; level alarm steps by the band" if step is None else
             f"; level alarm steps every {step:.2f}%")
    if alarms.up_only:
        text += "; run-ups only"
    return text


def run_replay(symbol: str, day: date, start: time, end: time,
               dry_run: bool, db: sqlite3.Connection,
               multiple: float = VOLUME_ALERT_MULTIPLE,
               alarms: Optional["Alarms"] = None,
               trend: Optional["Trend"] = None,
               market_alarm: Optional["MarketAlarm"] = None) -> int:
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

    # The price alarms -- the ones that make a NOISE. Until 14 Oct replay
    # counted only volume spikes, so --move-percent's own help ("tune it
    # with --replay") pointed at a replay that could not answer it.
    alarms = alarms if alarms is not None else Alarms()
    rings = replay_alarms(alarms, minutes, minute_base, prev_close, trend)
    print("-" * 56)
    trend_note = (f"; trend every {trend.step:.2f}%, over at "
                  f"{trend.back:.2f}% back" if trend is not None and trend.step > 0
                  else "; trend alarm off")
    print(f"  Price alarms ({alarm_policy(alarms)}{trend_note}): "
          f"{len(rings)} would have rung")
    for at, side, why, price in rings:
        print(f"    {at:%H:%M}  {RING_LABEL.get(side, side)}  "
              f"{price:8.2f}  {why}")
    if market_alarm is not None and market_alarm.drop > 0:
        try:
            spy = fetch_minutes(BENCHMARK, datetime.combine(day, start, tzinfo=ET),
                                datetime.combine(day, end, tzinfo=ET),
                                force_sip=True)
            market_rings = replay_market(market_alarm, spy,
                                         previous_close(BENCHMARK, day))
        except Exception as exc:  # noqa: BLE001 -- context, never a blocker
            market_rings = None
            print(f"  Market alarm: S&P 500 unavailable ({type(exc).__name__})")
        if market_rings is not None:
            print(f"  Market alarm ({market_policy(market_alarm)}): "
                  f"{len(market_rings)} would have rung")
            for at, why in market_rings:
                print(f"    {at:%H:%M}  MKT    {why}")
    if (end > REGULAR_CLOSE and not alarms.anchor_price
            and read_day(minutes, prev_close) is None):
        print("    note: no regular-hours bars in this window, so the "
              "after-hours level alarm had no close to measure from. Start "
              "--from at 15:59 or earlier, or pass --from-price.")
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
             sell_sound: str = SOUND_SELL,
             move_percent: float = MOVE_PERCENT,
             move_minutes: int = MOVE_MINUTES,
             alarm_volume: float = ALARM_VOLUME,
             up_only: bool = False,
             from_price: Optional[float] = None,
             ah_percent: Optional[float] = AH_PERCENT,
             level_step: Optional[float] = LEVEL_STEP,
             trend_step: float = TREND_STEP,
             trend_giveback: Optional[float] = TREND_GIVEBACK,
             climb_sound: str = SOUND_CLIMB,
             falling_sound: str = SOUND_FALLING,
             market_drop: float = MARKET_DROP,
             market_step: float = MARKET_STEP,
             market_sound: str = SOUND_MARKET,
             market_extended: float = MARKET_EXTENDED,
             persist_gap: int = PERSIST_GAP,
             rings_only: bool = False) -> int:
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
    print(f"  all session      price moving {move_percent:.2f}% within "
          f"{move_minutes} min on {ALARM_VOLUME:.1f}x volume,")
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
    alarms = Alarms(percent=move_percent, minutes=move_minutes,
                    volume=alarm_volume, up_only=up_only,
                    anchor_price=from_price, ah_percent=ah_percent,
                    level_step=level_step)
    print(f"  alarm bands: {alarm_policy(alarms)}\n")
    trend = Trend(step=trend_step, giveback=trend_giveback)
    if trend_step > 0:
        print(f"  trend alarm: a ring every {trend_step:.2f}% a run extends, "
              f"and when it gives back {trend.back:.2f}% -- "
              f"{climb_sound} up, {falling_sound} down\n")
    else:
        print("  trend alarm: off\n")
    market_alarm = MarketAlarm(drop=market_drop, step=market_step,
                               extended=market_extended)
    persist = Persist(gap=persist_gap)
    started = datetime.now(ET)
    if persist_gap > 0:
        print(f"  persistent: the first ring of a new move, up or down "
              f"(none that way for {persist_gap} min), repeats until "
              f"acknowledged\n")
    if rings_only:
        print("  rings only: nothing reaches the phone but an alarm\n")
    if market_drop > 0:
        print(f"  market alarm: {market_policy(market_alarm)} -- "
              f"{market_sound}\n")
    else:
        print("  market alarm: off\n")
    # Say which anchor the level alarm will use. Printing the policy is
    # the cheap half; the warning below is the half that matters, because
    # an alarm that cannot arm is indistinguishable from a quiet tape.
    level_band = ah_percent or move_percent
    if from_price:
        print(f"After-hours level alarm: {level_band:.2f}% from "
              f"{from_price:.2f} (your price).\n")
    else:
        print(f"After-hours level alarm: {level_band:.2f}% from today's "
              f"{REGULAR_CLOSE:%H:%M} close, once there is one.\n")
    warned_no_anchor = False

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
    open_called = False
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
            # The whole market, once per pass. Its own sound and its own
            # message: it is not a reading of this stock and must not be
            # mistaken for one.
            market_call = market_alarm.update(market)
            if market_call:
                body, priority = market_push(market_alarm, market_call, market)
                status = deliver(body, market_call, priority, dry_run,
                                 sound=market_sound)
                print(f"  {now:%H:%M}  ** {market_call} **  [{status}]\n")

            # Which way the market opened, once a day. See OPEN_CALL_AT.
            if open_call_due(now, open_called):
                opened = opening_move(market_prev, today)
                if opened is not None:
                    open_called = True
                    text = opening_call(opened)
                    status = deliver(text, "S&P 500 open", 0, dry_run)
                    print(f"  {now:%H:%M}  {text}  [{status}]\n")
            elif not open_called and now.time() > OPEN_CALL_LATEST:
                if now.time() < REGULAR_CLOSE and window_start.time() <= OPEN_CALL_AT:
                    print(f"  {now:%H:%M}  S&P opening call skipped: no 09:30 "
                          f"bar by {OPEN_CALL_LATEST:%H:%M}\n")
                open_called = True

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
                if (stamp.time() >= REGULAR_CLOSE
                        and alarms.anchor_for(day) is None
                        and not warned_no_anchor):
                    warned_no_anchor = True
                    print("  ** after-hours level alarm has NO ANCHOR: no "
                          "regular-hours bars to close on. Pass --from-price "
                          "to arm it. **")

                call = alarms.reason(minute, lean, day)
                ringing = alarms.should_ring(call, stamp)
                # The run, fed every traded minute in order -- replay feeds
                # it the same way, so a replayed ring is a live ring.
                moved = trend.update(stamp, minute.close)
                # Past the detail window only the unusual leaves the machine.
                backlog = before_start(stamp, started)
                push = (not backlog) and to_phone(
                    rings_only, stamp.time(), spiked, detail_until,
                    ringing or moved is not None)
                message = describe(symbol, minute, day, lean, multiple, market)
                fresh = remember_minute(db, symbol, minute, lean,
                                        sent=push and not dry_run)
                if moved or ringing:
                    title = ring_title(symbol, stamp, moved, call, ringing)
                elif spiked:
                    title = f"{symbol} {stamp:%H:%M} volume {minute.vol_ratio:.1f}x"
                else:
                    title = f"{symbol} {stamp:%H:%M}"
                chart = None
                # The page rides the NOISE, not the volume. Attached to
                # every spike it went out 70 times in a session of 75
                # messages -- a chart nobody opens, on a phone, for a
                # reading that did not even ring.
                if (ringing or moved) and pdf_every:
                    chart = rebuild_report(symbol, today, start, end,
                                           db_path, pdf_path)
                    last_pdf = datetime.now(ET)
                if not fresh:
                    status = "already recorded"
                elif not push:
                    status = ("before start - not sent" if backlog else
                              "logged (rings only)" if rings_only else
                              "logged, quiet after " + f"{detail_until:%H:%M}")
                else:
                    sound, priority, message = ring_out(
                        moved, call, ringing, alarms, persist, stamp, message,
                        (buy_sound, sell_sound, climb_sound, falling_sound))
                    status = deliver(message, title, priority, dry_run,
                                     attachment=chart, sound=sound)
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
                push = (not rings_only
                        and not before_start(candle.at + timedelta(minutes=BAR_MINUTES),
                                             started)
                        and reaches_phone(candle.at.time(), candle_spike, detail_until))
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
        if not rings_only:
            print("\n[summary push: "
                  f"{deliver(text, f'{symbol} morning', PRIORITY_SUMMARY, dry_run)}]")
    else:
        print("No candles collected.")
    return 0


# --------------------------------------------------------------------------
# Overnight: 20:00-03:50 through IB Gateway
# --------------------------------------------------------------------------

def overnight_window(now: datetime) -> Optional[Tuple[datetime, datetime]]:
    """The overnight session `now` falls inside, as (start, end), or None.

    A session belongs to the evening it opens on: Sunday's runs into
    Monday 03:50, Thursday's into Friday. Friday and Saturday evenings
    have none.
    """
    t = now.time()
    if t >= OVERNIGHT_START:
        opened = now.date()
    elif t < OVERNIGHT_END:
        opened = now.date() - timedelta(days=1)
    else:
        return None
    if opened.weekday() not in OVERNIGHT_NIGHTS:
        return None
    return (datetime.combine(opened, OVERNIGHT_START, tzinfo=ET),
            datetime.combine(opened + timedelta(days=1), OVERNIGHT_END, tzinfo=ET))


def next_session(now: datetime, start: time,
                 end: time) -> Tuple[str, datetime, datetime]:
    """("day" or "night", start, end): the session under way, or the next.

    Days are weekdays from `start` to `end` on Alpaca; nights are the IBKR
    overnight session. A day still counts as under way for one candle
    past `end`, the time run_live spends closing its last one.
    """
    night = overnight_window(now)
    if night:
        return ("night",) + night
    for ahead in range(8):
        on = now.date() + timedelta(days=ahead)
        found = []
        if on.weekday() < 5:
            s = datetime.combine(on, start, tzinfo=ET)
            e = datetime.combine(on, end, tzinfo=ET)
            if e + timedelta(minutes=BAR_MINUTES) > now:
                found.append(("day", s, e))
        if on.weekday() in OVERNIGHT_NIGHTS:
            s = datetime.combine(on, OVERNIGHT_START, tzinfo=ET)
            if s > now:
                found.append(("night", s, datetime.combine(
                    on + timedelta(days=1), OVERNIGHT_END, tzinfo=ET)))
        if found:
            return min(found, key=lambda f: f[1])
    raise RuntimeError("no session within a week")


def bars_frame(bars, start: datetime) -> pd.DataFrame:
    """IB Gateway's bars as the same frame Alpaca's minutes arrive in."""
    rows = []
    for bar in bars:
        at = pd.Timestamp(bar.date)
        at = at.tz_localize("UTC") if at.tzinfo is None else at
        rows.append((at.tz_convert(ET), float(bar.open), float(bar.high),
                     float(bar.low), float(bar.close), max(0.0, float(bar.volume)),
                     float(getattr(bar, "barCount", 0) or 0)))
    frame = pd.DataFrame(rows, columns=["at", "open", "high", "low", "close",
                                        "volume", "trade_count"])
    if frame.empty:
        return pd.DataFrame()
    frame = frame.set_index("at").sort_index()
    frame.index = pd.DatetimeIndex(frame.index)
    return frame[frame.index >= start]


class Gateway:
    """IB Gateway, read-only: one-minute bars on the OVERNIGHT route.

    Opened with readonly=True and its own client id. Nothing here can
    place an order: it asks for history and nothing else.
    """

    #: Status chatter, not failures: data farms connecting, and so on.
    QUIET = (300, 2104, 2106, 2107, 2108, 2119, 2158)
    TIMEOUT = 30

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.ib = None
        self.contract = None
        self.errors: List[str] = []

    def _on_error(self, req_id, code, message, contract=None) -> None:
        if code in self.QUIET:
            return
        # Asked at 20:00:30 there is nothing yet. That is an answer.
        if code == 162 and "no data" in str(message).lower():
            return
        self.errors.append(f"{code}: {message}"[:160])

    def _connect(self) -> None:
        from ib_async import IB, Stock

        ib = IB()
        ib.errorEvent += self._on_error
        ib.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID, timeout=10,
                   readonly=True)
        contract = Stock(self.symbol, "OVERNIGHT", "USD")
        if not ib.qualifyContracts(contract):
            ib.disconnect()
            raise RuntimeError(f"IBKR has no {self.symbol} on the OVERNIGHT route")
        self.ib, self.contract = ib, contract

    def minutes(self, start: datetime, now: datetime) -> pd.DataFrame:
        if self.ib is None or not self.ib.isConnected():
            self.close()
            self._connect()
        self.errors = []
        seconds = min(86400, int((now - start).total_seconds()) + 120)
        began = time_mod.monotonic()
        bars = self.ib.reqHistoricalData(
            self.contract, endDateTime="", durationStr=f"{seconds} S",
            barSizeSetting="1 min", whatToShow="TRADES", useRTH=False,
            formatDate=2, timeout=self.TIMEOUT)
        if self.errors:
            raise RuntimeError(self.errors[-1])
        if not bars and time_mod.monotonic() - began >= self.TIMEOUT - 1:
            raise TimeoutError(f"no answer in {self.TIMEOUT}s")
        return bars_frame(bars, start)

    def close(self) -> None:
        if self.ib is not None:
            try:
                self.ib.disconnect()
            except Exception:  # noqa: BLE001 -- closing a dead link is not news
                pass
        self.ib = None


def overnight_reference(symbol: str, night_start: datetime) -> Optional[Day]:
    """The regular session before this night, as the Day the alarms read.

    Its 16:00 close is the night's anchor. It stands in as the previous
    close too: after midnight the level alarm asks for "yesterday's
    close", and the close a night is measured against is still this one.
    Full tape (SIP), which the free plan serves once it is 15 minutes old.
    """
    for on in reversed(trading_days(night_start.date(), 5)):
        try:
            frame = fetch_minutes(symbol,
                                  datetime.combine(on, REGULAR_OPEN, tzinfo=ET),
                                  datetime.combine(on, REGULAR_CLOSE, tzinfo=ET),
                                  force_sip=True)
        except Exception:  # noqa: BLE001 -- try the session before
            continue
        day = read_day(frame)
        if day is not None:
            day.prev_close = day.last
            return day
    return None


def ring_title(symbol: str, stamp: datetime, moved: Optional[Tuple[str, str]],
               call: Optional[Tuple[str, str]], ringing: bool) -> str:
    """The notification title for a minute that rings. One notification,
    one sound: the trend speaks for the minute and a speed alarm in the
    same minute rides along in the title rather than ringing on top."""
    if moved:
        return (f"{symbol} {stamp:%H:%M} — {moved[1]}"
                + (f" · {call[1]}" if ringing else ""))
    return f"{symbol} {stamp:%H:%M} — {call[1]}"


def ring_out(moved: Optional[Tuple[str, str]], call: Optional[Tuple[str, str]],
             ringing: bool, alarms: "Alarms", persist: "Persist",
             stamp: datetime, message: str,
             sounds: Tuple[str, str, str, str]) -> Tuple[Optional[str], int, str]:
    """(sound, priority, message) for a minute that reaches the phone.

    The first ring of a new move is an emergency (see Persist); the
    message says so, because a phone repeating itself every minute needs
    to say how to make it stop. Shared by day and night, so the two can
    never disagree about what a ring sounds like.
    """
    buy_sound, sell_sound, climb_sound, falling_sound = sounds
    if moved:
        sound = climb_sound if moved[0] == "up" else falling_sound
    elif ringing:
        sound = alarms.sound_for(call[0], buy_sound, sell_sound)
    else:
        sound = None
    priority = PRIORITY_SUMMARY if (ringing or moved) else PRIORITY_UPDATE
    if (ringing or moved) and persist.urgent(
            ring_direction(moved, call if ringing else None), stamp):
        priority = PRIORITY_EMERGENCY
        message = message + "\n" + PERSIST_NOTE
    return sound, priority, message


def run_overnight(symbol: str, night_start: datetime, night_end: datetime,
                  dry_run: bool, db: sqlite3.Connection, alarms: "Alarms",
                  trend: "Trend", persist: "Persist",
                  sounds: Tuple[str, str, str, str], started: datetime,
                  gateway=None, reference: Optional[Day] = None,
                  clock=None, sleep=None, send=None) -> int:
    """One overnight session, read from IB Gateway, rung like the day.

    Every minute goes through the same Alarms, Trend and Persist the day
    uses, and a ring goes out on the first bar that earns it. Only rings
    leave the machine overnight: a stream of quiet updates at 2 AM is
    noise. No volume gate -- Alarms drops it outside regular hours -- and
    no market alarm, because the S&P does not trade overnight.

    If IB Gateway stops answering for GATEWAY_BLIND_MINUTES the phone is
    told once that the alarms are blind, and again when they can see.
    """
    clock = clock or (lambda: datetime.now(ET))
    sleep = sleep or time_mod.sleep
    send = send or deliver
    gateway = gateway or Gateway(symbol)
    day = reference if reference is not None else overnight_reference(symbol, night_start)

    print(f"\n{symbol} OVERNIGHT · {night_start:%a %H:%M} – {night_end:%a %H:%M} ET "
          f"· IB Gateway {IBKR_HOST}:{IBKR_PORT}, client {IBKR_CLIENT_ID}, read-only")
    print(f"  alarm bands: {alarm_policy(alarms)}")
    if day is not None:
        print(f"  measured from the {REGULAR_CLOSE:%H:%M} close, {money(day.last)}")
    else:
        print(f"  ** no {REGULAR_CLOSE:%H:%M} close to measure from: the level "
              f"alarm is off tonight; speed and trend still ring **")
    print("  only rings reach the phone overnight\n")

    seen = set()
    last_ok = clock()
    blind = False
    try:
        while True:
            now = clock()
            if now >= night_end + timedelta(minutes=1):
                break
            if now < night_start:
                sleep(min(30, max(1, (night_start - now).total_seconds())))
                continue

            try:
                frame = gateway.minutes(night_start, min(now, night_end))
                last_ok = now
                if blind:
                    blind = False
                    status = send(f"IB Gateway is answering again. Overnight "
                                  f"{symbol} alarms are back on.",
                                  f"{symbol} overnight", 0, dry_run)
                    print(f"  {now:%H:%M}  gateway back  [{status}]")
            except Exception as exc:  # noqa: BLE001 -- one bad pass is not the night
                reason = f"{type(exc).__name__}: {str(exc)[:100]}"
                print(f"  {now:%H:%M}  IB Gateway: {reason}")
                frame = pd.DataFrame()
                if not blind and now - last_ok >= timedelta(minutes=GATEWAY_BLIND_MINUTES):
                    blind = True
                    status = send(
                        f"IB Gateway has not answered for {GATEWAY_BLIND_MINUTES} "
                        f"minutes ({reason}). Overnight {symbol} alarms are BLIND "
                        f"until it does. Log IB Gateway back in. A login to IBKR "
                        f"on the phone or in DAS with the same username logs it out.",
                        f"{symbol} overnight BLIND", PRIORITY_SUMMARY, dry_run)
                    print(f"  {now:%H:%M}  ** overnight alarms blind **  [{status}]")

            done = drop_forming(frame, now, 1)
            for stamp in done.index:
                if stamp in seen:
                    continue
                seen.add(stamp)
                minute = to_candles(done.loc[[stamp]], {})[0]
                upto = done[done.index <= stamp]
                lean = read_lean(upto)
                call = alarms.reason(minute, lean, day)
                ringing = alarms.should_ring(call, stamp)
                moved = trend.update(stamp, minute.close)
                backlog = before_start(stamp, started)
                push = not backlog and (ringing or moved is not None)
                # The alarms measure from the 16:00 close; the message shows
                # where the night stands -- its move since that close and
                # its own range. The regular session's range and VWAP are
                # yesterday's news by now.
                shown = Day(last=minute.close, high=float(upto["high"].max()),
                            low=float(upto["low"].min()),
                            prev_close=day.last if day is not None else None,
                            label="Night")
                message = describe(symbol, minute, shown, lean)
                fresh = remember_minute(db, symbol, minute, lean,
                                        sent=push and not dry_run)
                if not fresh:
                    status = "already recorded"
                elif not push:
                    status = "before start - not sent" if backlog else "logged"
                else:
                    sound, priority, message = ring_out(
                        moved, call, ringing, alarms, persist, stamp, message, sounds)
                    status = send(message, ring_title(symbol, stamp, moved, call, ringing),
                                  priority, dry_run, sound=sound)
                print(message)
                print(f"  [{status}]\n")

            sleep(max(5, 62 - clock().second))
    except KeyboardInterrupt:
        print("\nStopped early.")
        raise
    finally:
        gateway.close()
    return 0


def run_around_the_clock(start: time, end: time, day_run, night_run,
                         clock=None, sleep=None, sessions: Optional[int] = None) -> int:
    """Day on Alpaca, night on IB Gateway, for as long as it is left running.

    `day_run()` runs one day window; `night_run(start, end, started)` one
    overnight session. Between them it waits -- Friday 20:00 to Sunday
    20:00 there is nothing to watch. A night it joins already under way
    is a late start, and the backlog guard keeps its old minutes off the
    phone; a night that follows straight on from the day is not, so the
    first minutes after 20:00 can ring.
    """
    clock = clock or (lambda: datetime.now(ET))
    sleep = sleep or time_mod.sleep
    first = True
    ran = 0
    while sessions is None or ran < sessions:
        now = clock()
        kind, s, e = next_session(now, start, end)
        if s - now > timedelta(minutes=1):
            print(f"\nNext: the {kind} session at {s:%a %H:%M} ET. Waiting.")
            while s - clock() > timedelta(minutes=1):
                sleep(min(300, max(1, (s - clock()).total_seconds() - 60)))
            first = False
            continue
        if kind == "day":
            day_run()
            if clock() < e:
                # run_live only comes back early when it was stopped by
                # hand (it catches Ctrl+C itself). Stopped means stopped,
                # not "on to the night".
                return 0
        else:
            night_run(s, e, now if first else s)
        first = False
        ran += 1
    return 0


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

def overnight_checks() -> List[str]:
    """The overnight session, offline: a fake IB Gateway and a fake clock
    run whole nights in a fraction of a second."""
    import contextlib
    import io
    from types import SimpleNamespace

    failures: List[str] = []

    def at(y, mo, d, h, mi, s=0):
        return datetime(y, mo, d, h, mi, s, tzinfo=ET)

    # 18 Oct 2026 is a Sunday.
    sun = (2026, 10, 18)
    windows = [
        (at(*sun, 21, 0), (at(*sun, 20, 0), at(2026, 10, 19, 3, 50)), "Sunday evening"),
        (at(2026, 10, 19, 2, 0), (at(*sun, 20, 0), at(2026, 10, 19, 3, 50)), "Monday 02:00"),
        (at(2026, 10, 19, 3, 55), None, "Monday 03:55, after the close"),
        (at(2026, 10, 22, 23, 0), (at(2026, 10, 22, 20, 0), at(2026, 10, 23, 3, 50)), "Thursday night"),
        (at(2026, 10, 23, 2, 0), (at(2026, 10, 22, 20, 0), at(2026, 10, 23, 3, 50)), "Friday 02:00"),
        (at(2026, 10, 23, 21, 0), None, "Friday evening"),
        (at(2026, 10, 24, 1, 0), None, "Saturday 01:00"),
        (at(*sun, 19, 59), None, "Sunday before 20:00"),
    ]
    for now, want, what in windows:
        if overnight_window(now) != want:
            failures.append(f"overnight window on {what}: {overnight_window(now)}")

    four, eight = time(4, 0), time(20, 0)
    plans = [
        (at(2026, 10, 23, 20, 30), ("night", at(2026, 10, 25, 20, 0)), "Friday 20:30 waits for Sunday"),
        (at(2026, 10, 24, 12, 0), ("night", at(2026, 10, 25, 20, 0)), "Saturday noon waits for Sunday"),
        (at(2026, 10, 19, 3, 52), ("day", at(2026, 10, 19, 4, 0)), "Monday 03:52 waits for the day"),
        (at(2026, 10, 21, 12, 0), ("day", at(2026, 10, 21, 4, 0)), "Wednesday noon is in the day"),
        (at(2026, 10, 21, 20, 2), ("night", at(2026, 10, 21, 20, 0)), "Wednesday 20:02 is in the night"),
        (at(2026, 10, 23, 20, 2), ("day", at(2026, 10, 23, 4, 0)), "Friday 20:02 is the day's last candle"),
    ]
    for now, (kind, start), what in plans:
        got = next_session(now, four, eight)
        if (got[0], got[1]) != (kind, start):
            failures.append(f"{what}: got {got[0]} at {got[1]:%a %H:%M}")

    # IB's bars become the frame Alpaca's minutes arrive in.
    bars = [SimpleNamespace(date=datetime(2026, 10, 19, 0, m, tzinfo=timezone.utc),
                            open=147.0, high=147.2, low=146.9, close=147.1,
                            volume=v, barCount=3) for m, v in ((0, 5), (1, -1))]
    frame = bars_frame(bars, at(*sun, 20, 0))
    if (list(frame.index) != [at(*sun, 20, 0), at(*sun, 20, 1)]
            or list(frame["volume"]) != [5.0, 0.0]):
        failures.append(f"IB bars converted wrong: {frame}")
    if not bars_frame([], at(*sun, 20, 0)).empty:
        failures.append("no bars should be an empty frame")

    class Clock:
        def __init__(self, now):
            self.now = now

        def __call__(self):
            return self.now

        def sleep(self, seconds):
            self.now += timedelta(seconds=seconds)

    close = 146.03
    reference = Day(last=close, high=147.0, low=145.0, prev_close=close, vwap=146.0)

    def tape(start, prices):
        index = pd.DatetimeIndex([start + timedelta(minutes=i) for i in range(len(prices))])
        return pd.DataFrame({"open": prices, "high": prices, "low": prices,
                             "close": prices, "volume": [100.0] * len(prices),
                             "trade_count": [4.0] * len(prices)}, index=index)

    class FakeGateway:
        def __init__(self, frame, clock, down=(), forming=None):
            self.frame, self.clock, self.down = frame, clock, down
            self.forming = forming
            self.closed = False

        def minutes(self, start, now):
            if any(a <= self.clock() < b for a, b in self.down):
                raise ConnectionRefusedError("refused")
            frame = self.frame[self.frame.index <= now].copy()
            # IB includes the bar still being built. With `forming` set it
            # shows a price the finished bar will not keep.
            building = now.replace(second=0, microsecond=0)
            if self.forming and building in frame.index:
                frame.loc[building, "close"] = self.forming
            return frame

        def close(self):
            self.closed = True

    def night(prices, started_at, start=None, end_minutes=30, down=(), anchor=reference,
              forming=None):
        start = start or at(*sun, 20, 0)
        clock = Clock(start)
        sent = []

        def send(message, title, priority, dry_run, attachment=None, sound=None):
            sent.append(SimpleNamespace(at=clock(), title=title, priority=priority,
                                        message=message, sound=sound))
            return "captured"

        db = sqlite3.connect(":memory:")
        ensure_schema(db)
        gateway = FakeGateway(tape(start, prices), clock, down, forming)
        with contextlib.redirect_stdout(io.StringIO()):
            run_overnight("SPCX", start, start + timedelta(minutes=end_minutes), True, db,
                          Alarms(), Trend(), Persist(gap=30),
                          ("buy", "sell", "climb", "falling"), started_at,
                          gateway=gateway, reference=anchor, clock=clock,
                          sleep=clock.sleep, send=send)
        if not gateway.closed:
            failures.append("the gateway connection should be closed at the end")
        return sent

    # Flat at the close, +0.5% at 20:05, flat, then -0.6% below the close
    # at 20:15. Each first ring of a direction is an emergency, rung on the
    # first bar that earns it.
    prices = ([close] * 5 + [146.80] * 10 + [145.15] * 10)
    sent = night(prices, started_at=at(*sun, 20, 0))
    rings = [(s.title[5:10], s.priority) for s in sent]
    if not sent or sent[0].title[5:10] != "20:05" or sent[0].priority != PRIORITY_EMERGENCY:
        failures.append(f"a +0.5% first bar overnight should ring an emergency at 20:05: {rings}")
    else:
        first = sent[0]
        for words in ("(overnight)", "+0.53% since the close", "Night 146.03",
                      PERSIST_NOTE):
            if words not in first.message:
                failures.append(f"the overnight ring should say {words!r}: {first.message}")
        if "the 16:00 close" not in first.title:
            failures.append(f"the overnight ring should be measured from the 16:00 close: "
                            f"{first.title}")
    downs = [s for s in sent if s.title[5:10] == "20:15"]
    if not downs or downs[0].priority != PRIORITY_EMERGENCY:
        failures.append(f"the first drop below the close should be an emergency too: {rings}")
    if any(s.title[5:10] < "20:05" for s in sent):
        failures.append(f"nothing should ring before the move: {rings}")

    # A bar still being built is never read: its spike may not last.
    if night([close] * 20, started_at=at(*sun, 20, 0), forming=close * 1.02):
        failures.append("a forming bar's price must not ring")

    # The anchor is the last regular session's close, found from the
    # night's own date: Sunday's night is measured from Friday.
    asked = []

    def fake_fetch(symbol, start, end, force_sip=False):
        asked.append(start.date())
        if start.date() != date(2026, 10, 16):
            return pd.DataFrame()
        return tape(start, [145.0, 146.5, 146.03])

    real_fetch = globals()["fetch_minutes"]
    globals()["fetch_minutes"] = fake_fetch
    try:
        ref = overnight_reference("SPCX", at(*sun, 20, 0))
    finally:
        globals()["fetch_minutes"] = real_fetch
    if ref is None or ref.last != 146.03 or ref.prev_close != 146.03 or asked[0] != date(2026, 10, 16):
        failures.append(f"Sunday's night should be anchored on Friday's close, as both the "
                        f"close and the previous close: {ref}, asked {asked}")

    # Joined late, the same night stays quiet about what it missed.
    if night(prices, started_at=at(*sun, 20, 20)):
        failures.append("a late start must not ring for minutes that closed before it")

    # After midnight the anchor is still the 16:00 close.
    late = at(2026, 10, 19, 0, 30)
    sent = night([close] * 3 + [146.90] * 5, started_at=late, start=late, end_minutes=8)
    if not sent or "the 16:00 close" not in sent[0].title:
        failures.append(f"after midnight the ring should still measure from the 16:00 "
                        f"close: {[s.title for s in sent]}")

    # IB Gateway down from 20:00 to 20:25: one loud BLIND after ten
    # minutes, one note when it answers again, nothing in between.
    sent = night([close] * 30, started_at=at(*sun, 20, 0),
                 down=[(at(*sun, 20, 0), at(*sun, 20, 25))])
    notes = [(s.at.strftime("%H:%M"), s.priority, "BLIND" in s.title) for s in sent]
    if (len(sent) != 2 or not notes[0][2] or notes[0][1] != PRIORITY_SUMMARY
            or not (at(*sun, 20, 10) <= sent[0].at < at(*sun, 20, 12))
            or "again" not in sent[1].message):
        failures.append(f"gateway down should be one BLIND alarm at ~20:10 and one "
                        f"recovery note: {notes}")

    # Around the clock: wait out the weekend, run the night, then the day,
    # then the next night -- which follows on, so it is not a late start.
    clock = Clock(at(2026, 10, 23, 21, 0))
    calls = []

    def day_run():
        calls.append(("day", clock()))
        clock.now = clock().replace(hour=20, minute=5)
        return 0

    def night_run(s, e, started):
        calls.append(("night", s, started))
        clock.now = e + timedelta(minutes=1)
        return 0

    with contextlib.redirect_stdout(io.StringIO()):
        run_around_the_clock(four, eight, day_run, night_run, clock=clock,
                             sleep=clock.sleep, sessions=3)
    want = [("night", at(2026, 10, 25, 20, 0), at(2026, 10, 25, 20, 0)),
            ("day", None), ("night", at(2026, 10, 26, 20, 0), at(2026, 10, 26, 20, 0))]
    got = [c if c[0] == "night" else ("day", None) for c in calls]
    if got != want or calls[1][1].date() != date(2026, 10, 26):
        failures.append(f"around the clock ran {calls}")

    # Started in the middle of a night: that night is a late start.
    clock = Clock(at(2026, 10, 20, 1, 0))
    calls.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        run_around_the_clock(four, eight, day_run, night_run, clock=clock,
                             sleep=clock.sleep, sessions=1)
    if calls != [("night", at(2026, 10, 19, 20, 0), at(2026, 10, 20, 1, 0))]:
        failures.append(f"a mid-night start should join that night late: {calls}")

    # A day stopped by hand stops everything.
    clock = Clock(at(2026, 10, 21, 12, 0))
    calls.clear()

    def stopped_day():
        calls.append(("day", clock()))
        return 0                       # back at 12:00: Ctrl+C

    with contextlib.redirect_stdout(io.StringIO()):
        run_around_the_clock(four, eight, stopped_day, night_run, clock=clock,
                             sleep=clock.sleep, sessions=3)
    if len(calls) != 1:
        failures.append(f"Ctrl+C in the day should not go on to the night: {calls}")
    return failures


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

    kept_emergency = _self.PRIORITY_EMERGENCY
    _self.PRIORITY_EMERGENCY = None
    try:
        note = stale_sender()
        if not note or "emergency" not in note or "spcx_alert.py" not in note:
            failures.append(f"a sender with no emergency priority must be "
                            f"refused by name: {note!r}")
    finally:
        _self.PRIORITY_EMERGENCY = kept_emergency

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
    def reading(score, ratio, price=150.5, vwap_gap=1.0, at_minute=35,
                hour=10, usual=40_000):
        """A candle, its lean and its day, at one clock minute.

        `usual=None` is an after-hours slot the baseline never saw, which
        is what makes vol_ratio None and the volume gate impassable.
        """
        when = datetime.combine(date(2026, 9, 18), time(hour, at_minute), tzinfo=ET)
        bar = Candle(at=when, open=price, high=price + 0.5, low=price - 0.5,
                     close=price, volume=ratio * 40_000, usual_volume=usual)
        return (bar, Lean(score, 5, 5, 5, volume_ratio=ratio),
                Day(last=price, high=151.0, low=149.0,
                    prev_close=149.0, vwap=price - vwap_gap), when)

    # The case this rule exists for: a one-sided tape, heavy volume, and
    # price going nowhere. Under the old rule this was the noise.
    quiet = Alarms()
    for minute, price in enumerate((150.50, 150.48, 150.52, 150.49, 150.51,
                                    150.50, 150.47)):
        bar, lean, dctx, when = reading(0.10, 5.1, price=price,
                                        at_minute=30 + minute)
        if quiet.reason(bar, lean, dctx) is not None:
            failures.append(f"sellers pressing on 5.1x volume with price "
                            f"flat must stay silent (minute {minute})")

    # Movement without participation, and participation without movement.
    for ratio, prices, why in (
            (1.0, (150.0, 151.0), "a move on ordinary volume"),
            (3.0, (150.0, 150.0), "a volume spike with price unchanged")):
        alone = Alarms()
        for minute, price in enumerate(prices):
            bar, lean, dctx, when = reading(0.50, ratio, price=price,
                                            at_minute=30 + minute)
            if alone.reason(bar, lean, dctx) is not None:
                failures.append(f"{why} must stay silent")

    # A real move, either way, and the reason has to say what it was.
    for target, direction in ((151.45, "buy"), (149.55, "sell")):
        moved = Alarms()
        bar, lean, dctx, when = reading(0.50, 2.0, price=150.5, at_minute=30)
        moved.reason(bar, lean, dctx)
        bar, lean, dctx, when = reading(0.50, 2.0, price=target, at_minute=33)
        call = moved.reason(bar, lean, dctx)
        if not call or call[0] != direction:
            failures.append(f"150.50 -> {target} in 3 min on 2x volume should "
                            f"ring {direction}: {call}")
        elif "%" not in call[1] or "min" not in call[1]:
            failures.append(f"the reason should name the move: {call[1]}")

    # Against the OLDEST price in the window, not the previous bar. Five
    # steps of 0.15% is a 0.75% slide and has to ring; bar to bar it would
    # never trigger, which is how 25 Sep stayed loud and said nothing.
    creep = Alarms()
    rang = None
    for minute in range(6):
        bar, lean, dctx, when = reading(0.50, 2.0, price=150.5 * (1 - 0.0015 * minute),
                                        at_minute=30 + minute)
        rang = rang or creep.reason(bar, lean, dctx)
    if not rang or rang[0] != "sell":
        failures.append(f"a slide arriving in small steps still has to ring: {rang}")

    # --up-only: a day spent flat, watching for a reason to come back in.
    # The same slide that must ring above has to stay SILENT here, and a
    # run-up of the same size still has to ring. If the direction test ever
    # inverts, the alarm goes quiet on exactly the move it was turned on
    # for and says nothing about it.
    for up_only, want_slide, want_rise in ((False, "sell", "buy"),
                                           (True, None, "buy")):
        for size, want in ((-0.0015, want_slide), (+0.0015, want_rise)):
            alarm = Alarms(up_only=up_only)
            fired = None
            for minute in range(6):
                bar, lean, dctx, when = reading(
                    0.50, 2.0, price=150.5 * (1 + size * minute),
                    at_minute=30 + minute)
                fired = fired or alarm.reason(bar, lean, dctx)
            got = fired[0] if fired else None
            if got != want:
                way = "a run-up" if size > 0 else "a slide"
                failures.append(
                    f"up_only={up_only}: {way} should give {want!r}, got "
                    f"{got!r}")

    # AFTER HOURS. The baseline is built from past sessions, so a 17:30
    # slot it never saw leaves usual_volume None, vol_ratio None, and the
    # volume gate impassable. Before this, no after-hours alarm could
    # fire at all -- the watcher ran, printed, and said nothing however
    # far price fell. Holding a position overnight is exactly when that
    # silence costs money, so price alone has to ring after the close.
    #
    # The control is the same slide at 10:30 on the same ordinary volume:
    # inside regular hours the gate still has to hold, or this fix has
    # quietly removed volume confirmation from the whole session.
    for hour, usual, want, where in ((17, None, "sell", "after hours"),
                                     (10, 40_000, None, "regular hours")):
        alarm = Alarms()
        fired = None
        for minute in range(6):
            bar, lean, dctx, when = reading(
                0.50, 1.0, price=150.5 * (1 - 0.0015 * minute),
                at_minute=30 + minute, hour=hour, usual=usual)
            fired = fired or alarm.reason(bar, lean, dctx)
        got = fired[0] if fired else None
        if got != want:
            failures.append(
                f"{where}: a 0.75% slide on ordinary volume should give "
                f"{want!r}, got {got!r}. After the close price alone must "
                f"ring; inside regular hours the volume gate must hold.")
        if hour == 17 and fired and "no volume confirmation" not in fired[1]:
            failures.append(
                f"an after-hours alarm fires without volume confirmation, so "
                f"the message has to say so: {fired[1]!r}")

    # THE SLOW SLIDE. 0.6% spread over forty minutes is 0.08% per
    # five-minute slice, so the speed alarm in _move cannot see it at any
    # threshold worth having -- and after hours that is the normal shape
    # of a move. Holding a position through an evening, the question is
    # not "how fast" but "how far from the close", so _level asks that
    # with no time window at all.
    #
    # Real numbers from 9 Oct: close 163.27, drifting to 162.29 is -0.60%.
    close_at_four = 163.27
    def evening(price, at_minute):
        when = datetime.combine(date(2026, 10, 9), time(16, 30), tzinfo=ET) \
            + timedelta(minutes=at_minute)
        bar = Candle(at=when, open=price, high=price + 0.02, low=price - 0.02,
                     close=price, volume=9_000, usual_volume=None)
        # day.last is the REGULAR-session close: read_day cuts at 16:00, so
        # after the bell it stays put however far the after-hours tape goes.
        return (bar, Lean(0.5, 5, 5, 5, volume_ratio=1.0),
                Day(last=close_at_four, high=164.0, low=161.0,
                    prev_close=158.90, vwap=close_at_four))

    slide = [close_at_four * (1 - 0.0065 * m / 39) for m in range(40)]

    # The speed alarm, alone, must be blind to it. If this ever starts
    # firing, the level alarm below is no longer earning its place.
    speed = Alarms(percent=0.6)
    if any(speed._move(evening(px, m)[0]) for m, px in enumerate(slide)):
        failures.append("a 0.65% drift over 40 minutes is 0.08% per 5-minute "
                        "slice; the speed alarm must not see it, or this "
                        "test is not testing the thing it was written for")

    # The level alarm must ring, ONCE, and say what it measured from.
    level = Alarms(percent=0.6)
    rings = [level.reason(*evening(px, m)) for m, px in enumerate(slide)]
    rang = [r for r in rings if r is not None]
    if len(rang) != 1:
        failures.append(
            f"a slow slide past 0.6% from the close must ring exactly once, "
            f"not {len(rang)} times: a level that stays true would otherwise "
            f"ring every minute until the alerts stopped meaning anything")
    elif rang[0][0] != "sell" or "from the 16:00 close" not in rang[0][1]:
        failures.append(f"the level alarm should sell and name its anchor, "
                        f"got {rang[0]!r}")

    # Back inside the band, then out again: the second crossing is a new
    # event and has to ring. Accumulating instead would silence it forever.
    #
    # Asserted against _level directly, not reason(). A fall quick enough
    # to re-cross in a couple of bars also trips the SPEED alarm, so
    # reason() would answer "something rang" either way and the re-arm
    # could rot untested behind it -- which is exactly what the first
    # draft of this test did.
    back = [close_at_four] * 3 + [close_at_four * 0.99]
    again = [level._level(evening(px, 40 + m)[0], evening(px, 40 + m)[2])
             for m, px in enumerate(back)]
    if again[-1] is None:
        failures.append("price that returns inside the band and falls out "
                        "again is a new crossing: the level alarm has to "
                        "re-arm when price comes back inside, or one lapsed "
                        "setup silences it for the rest of the evening")
    if any(r is not None for r in again[:3]):
        failures.append(f"back at the close is inside the band and must be "
                        f"silent, not {again[:3]!r}")

    # A named anchor wins over the close, and the SAME BARS then give the
    # opposite verdict: 163 is 0.6% BELOW the 163.27 close and 2% ABOVE a
    # 160.00 entry. If the anchor were ignored, both would read alike.
    mine = Alarms(percent=0.6, anchor_price=160.00)
    said = None
    for m, px in enumerate(slide):
        said = said or mine.reason(*evening(px, m))
    if not said or said[0] != "buy":
        failures.append(
            f"anchored on a 160.00 entry, bars near 163 are 2% UP and must "
            f"ring buy -- the same bars the close anchor calls a 0.6% fall. "
            f"Got {said!r}; the anchor is being ignored.")
    elif "from your price" not in said[1]:
        failures.append(f"with --from-price the message must name the anchor "
                        f"as yours, not the close: {said[1]!r}")

    # --- 14 Oct: an evening that keeps going is reported as it goes ------
    # A slide to -1.4% from the close, band 0.5: the old alarm rang once,
    # at -0.5%, and said nothing about the next 0.9%. Stepping by the band
    # it must ring at -0.5 and again at -1.0, and the second must say it
    # is further, not a fresh crossing.
    deep = [close_at_four * (1 - 0.014 * m / 59) for m in range(60)]
    stepper = Alarms(percent=0.5)
    steps = [r for r in (stepper.reason(*evening(px, m))
                         for m, px in enumerate(deep)) if r is not None]
    if len(steps) != 2:
        failures.append(f"a slide to -1.4% with a 0.5 band must ring at -0.5 "
                        f"and -1.0, twice, not {len(steps)}x: {steps!r}")
    elif not steps[1][1].startswith("further: ") or steps[0][1].startswith("further"):
        failures.append(f"the second ring is a further step and must say so; "
                        f"the first must not: {steps!r}")
    once = Alarms(percent=0.5, level_step=0)
    if sum(once.reason(*evening(px, m)) is not None
           for m, px in enumerate(deep)) != 1:
        failures.append("--level-step 0 must keep the old once-per-crossing "
                        "behaviour")

    # Sagging back a step and returning covers no new ground: silent. A
    # NEW furthest step rings. Asserted on _level, like the re-arm above:
    # a bounce from -1.4% to -0.8% in a minute is a genuine speed move,
    # and reason() would report THAT, hiding whatever _level decided.
    sag = Alarms(percent=0.5)
    for m, px in enumerate(deep):
        sag.reason(*evening(px, m))                 # rung for -0.5 and -1.0
    back_and_forth = [close_at_four * (1 + d) for d in (-0.008, -0.011, -0.016)]
    again2 = [sag._level(evening(px, 60 + m)[0], evening(px, 60 + m)[2])
              for m, px in enumerate(back_and_forth)]
    if again2[0] is not None or again2[1] is not None:
        failures.append(f"back to -0.8% then -1.1% revisits ground that "
                        f"already rang and must be silent: {again2[:2]!r}")
    if again2[2] is None:
        failures.append("-1.6% is a new furthest step and must ring")

    # A further step is new information and skips the cooldown; a speed
    # alarm a minute later does not.
    hurry = Alarms(percent=0.5)
    sounded = []
    for m, px in enumerate([close_at_four * (1 + d) for d in
                            (0, -0.0052, -0.0060, -0.0103)]):
        bar, lean, dctx = evening(px, m)
        if hurry.should_ring(hurry.reason(bar, lean, dctx), bar.at):
            sounded.append(m)
    if sounded != [1, 3]:
        failures.append(f"level steps 2 minutes apart must both reach the "
                        f"phone despite the cooldown, nothing else: "
                        f"rang at minutes {sounded}")

    # Watching run-ups only, a dip below the close re-arms the buy side.
    # It used to return before the reset, so the second run-up was silent.
    ups = Alarms(percent=0.5, up_only=True)
    ups_said = [ups.reason(*evening(close_at_four * (1 + d), m)) is not None
                for m, d in enumerate((0.006, -0.006, 0.006))]
    if ups_said != [True, False, True]:
        failures.append(f"--up-only: up, dip, up again must ring, stay "
                        f"silent, ring -- got {ups_said}")

    # The evening band. A 0.35% move inside five minutes at 16:30 rings on
    # a 0.30 after-hours band and not on the 0.50 session band; the same
    # move at 10:30 is judged by the session band whatever the evening one.
    def fast(hour, minute, price):
        when = datetime.combine(date(2026, 10, 9), time(hour, minute), tzinfo=ET)
        return Candle(at=when, open=price, high=price, low=price, close=price,
                      volume=9_000, usual_volume=None)
    for band, hour, want in ((0.30, 16, True), (None, 16, False), (0.30, 10, False)):
        a = Alarms(percent=0.5, ah_percent=band)
        a._move(fast(hour, 30, 100.0))
        hit = a._move(fast(hour, 33, 100.35)) is not None
        if hit != want:
            failures.append(f"a 0.35% move at {hour}:33 with --ah-percent "
                            f"{band} should {'ring' if want else 'stay silent'}")

    # Replay judges the same way the live loop does. A synthetic evening:
    # regular session closes at 100.00, then drifts to 98.90 by 16:40.
    stamps = pd.date_range(datetime(2026, 10, 9, 15, 50), periods=51,
                           freq="1min", tz=ET)
    closes = [100.0] * 10 + [100.0 - 1.1 * m / 40 for m in range(41)]
    frame = pd.DataFrame({"open": closes, "high": closes, "low": closes,
                          "close": closes, "volume": 500.0}, index=stamps)
    # No trades in the very minute the first ring would come (-0.52%):
    # live skips such a minute, so replay must ring a minute later.
    frame.loc[stamps[29], "volume"] = 0.0
    replayed = replay_alarms(Alarms(percent=0.5), frame, {}, 101.0)
    if [side for _, side, _, _ in replayed] != ["sell", "sell"]:
        failures.append(f"replaying a drift to -1.1% after the close must show "
                        f"two sell rings (-0.5, -1.0): {replayed!r}")
    elif any(at == stamps[29] for at, _, _, _ in replayed):
        failures.append("replay must skip a minute with no trades, as live does")
    if "0.30% outside 09:30-16:00" not in alarm_policy(Alarms(ah_percent=0.3)):
        failures.append("the policy line must name the evening band")

    # --- 14 Oct: the trend alarm -------------------------------------------
    # A zigzag climb: up, dip, up, dip, higher each time, then a real
    # giveback and a further slide. The speed alarm hears buy/sell/buy;
    # the trend must hear climbing, still climbing, still climbing,
    # giving it back, still falling -- and stay silent on every dip
    # smaller than the giveback.
    t0 = datetime.combine(date(2026, 10, 14), time(10, 0), tzinfo=ET)
    zig = (100.00, 100.30, 100.10, 100.55, 100.30, 101.10, 100.80, 101.60,
           101.00, 100.70, 100.40)
    run = Trend(step=0.5)
    heard = [(m, r) for m, r in ((m, run.update(t0 + timedelta(minutes=m), px))
                                 for m, px in enumerate(zig)) if r]
    words = [r[1].split(":")[0] for _, r in heard]
    if words != ["climbing", "still climbing", "still climbing",
                 "giving it back", "still falling"]:
        failures.append(f"a zigzag climb then a slide must read climbing, still "
                        f"climbing x2, giving it back, still falling: {heard!r}")
    elif [m for m, _ in heard] != [3, 5, 7, 8, 10]:
        failures.append(f"each ring belongs on its own minute (3,5,7,8,10), and "
                        f"every dip smaller than the giveback is silent: "
                        f"{[m for m, _ in heard]}")
    elif [r[0] for _, r in heard] != ["up", "up", "up", "down", "down"]:
        failures.append("climbing rings up, giving back and falling ring down")
    elif "from the 10:00 low" not in heard[0][1][1] or \
            "from the 10:07 high" not in heard[3][1][1]:
        failures.append(f"a ring must name where the run started or peaked: "
                        f"{heard[0][1][1]!r} / {heard[3][1][1]!r}")

    # Chop smaller than a step in both directions is never a trend.
    chop = Trend(step=0.5)
    if any(chop.update(t0 + timedelta(minutes=m), px) for m, px in
           enumerate((100.0, 100.3, 100.0, 100.35, 99.9, 100.2, 99.85, 100.1))):
        failures.append("chop inside +/-0.4% must never ring the trend alarm")

    # A falling run that bounces by the giveback says so, upward.
    dip = Trend(step=0.5)
    said_dip = [r for r in (dip.update(t0 + timedelta(minutes=m), px)
                            for m, px in enumerate((100.0, 99.4, 99.0, 99.6)))
                if r]
    if [r[0] for r in said_dip] != ["down", "down", "up"] or \
            not said_dip[-1][1].startswith("bouncing"):
        failures.append(f"a slide to -1.0% that bounces 0.6% off its low must "
                        f"ring falling, still falling, then bouncing: "
                        f"{said_dip!r}")

    # A wider giveback tolerates a bigger dip; step 0 is off entirely.
    wide = Trend(step=0.5, giveback=1.0)
    wide_said = [r for r in (wide.update(t0 + timedelta(minutes=m), px)
                             for m, px in enumerate((100.0, 100.6, 100.0)))
                 if r]
    if len(wide_said) != 1:
        failures.append(f"with a 1.0 giveback a 0.6% dip is still the same run: "
                        f"{wide_said!r}")
    off = Trend(step=0)
    if any(off.update(t0 + timedelta(minutes=m), px)
           for m, px in enumerate((100.0, 103.0, 99.0))):
        failures.append("--trend-step 0 must turn the trend alarm off")

    # --- 14 Oct: the market alarm ------------------------------------------
    mkt = MarketAlarm(drop=1.0, extended=0)
    tape = (-0.50, -1.02, -1.20, -1.55, -0.80, -1.10, None, -2.20)
    calls = [mkt.update(c) for c in tape]
    rang_at = [i for i, c in enumerate(calls) if c]
    if rang_at != [1, 3, 5, 7]:
        failures.append(f"the market alarm must ring crossing -1%, again at "
                        f"-1.5%, re-arm above -1% and ring again, and ring at "
                        f"a new further step: rang at {rang_at} of {tape}")
    elif "further" not in calls[3] or "further" in calls[5]:
        failures.append(f"a deeper step says further; a fresh crossing does "
                        f"not: {calls!r}")
    if MarketAlarm(drop=0).update(-5.0) is not None:
        failures.append("--market-drop 0 must turn the market alarm off")
    spy_stamps = pd.date_range(datetime(2026, 10, 14, 10, 0), periods=3,
                               freq="1min", tz=ET)
    spy = pd.DataFrame({"close": [99.5, 98.9, 98.4]}, index=spy_stamps)
    if [at for at, _ in replay_market(MarketAlarm(drop=1.0), spy, 100.0)] != \
            list(spy_stamps[1:]):
        failures.append("replaying the S&P must ring at -1.1% and again at -1.6%")

    # --- 15 Oct: from -0.5%, and an emergency from -2% ---------------------
    tape = (-0.30, -0.52, -0.80, -1.01, -1.60, -1.99, -2.05, -2.30, -2.60,
            -0.40, -0.60)
    mkt, rang_at, loud = MarketAlarm(), [], []
    for i, c in enumerate(tape):
        if mkt.update(c):
            rang_at.append(i)
            if mkt.urgent:
                loud.append(i)
    if rang_at != [1, 3, 4, 6, 8, 10]:
        failures.append(f"the market alarm must ring at -0.5% and every 0.5% "
                        f"further, and again after re-arming: rang at "
                        f"{rang_at} of {tape}")
    if loud != [6, 8]:
        failures.append(f"only rings at -2% or deeper are extended: {loud} "
                        f"of {tape}")
    quiet = MarketAlarm()
    quiet.update(-2.10)
    if quiet.update(-2.20) is not None or quiet.urgent:
        failures.append("a minute with no new ring must not read as urgent")
    off = MarketAlarm(extended=0)
    if not off.update(-3.0) or off.urgent:
        failures.append("--market-extended 0 must ring without the emergency")
    deep = MarketAlarm()
    why = deep.update(-2.05)
    body, priority = market_push(deep, why, -2.05)
    if priority != PRIORITY_EMERGENCY or "Acknowledge" not in body \
            or "ALARM" not in why:
        failures.append(f"a -2% ring must go out as an emergency: "
                        f"{priority} {why!r} {body!r}")
    shallow = MarketAlarm()
    why = shallow.update(-0.55)
    body, priority = market_push(shallow, why, -0.55)
    if priority != PRIORITY_SUMMARY or "Acknowledge" in body:
        failures.append(f"a -0.5% ring is a single sound: {priority} {body!r}")

    # --- 16 Oct: pre-market alarms, persistence, rings only ---------------
    pm_day = Day(last=101.0, high=101.0, low=100.0, prev_close=100.0)
    pm = Alarms(percent=0.5)

    def bar(hh, mm, close, usual=None):
        return Candle(at=datetime(2026, 10, 16, hh, mm, tzinfo=ET), open=close,
                      high=close, low=close, close=close, volume=1000,
                      usual_volume=usual)
    said = pm.reason(bar(8, 0, 101.0), None, pm_day)
    if not said or said[0] != "buy" or "yesterday's close" not in said[1]:
        failures.append(f"pre-market: 1% over yesterday's close must ring, "
                        f"measured from it: {said!r}")
    if pm.reason(bar(10, 0, 101.0), None, pm_day) is not None:
        failures.append("the level alarm stays silent in regular hours")
    pm_down = Alarms(percent=0.5)
    said = pm_down.reason(bar(7, 30, 98.9), None, pm_day)
    if not said or said[0] != "sell":
        failures.append(f"a pre-market drop rings as well: {said!r}")
    fast = Alarms(percent=0.5)
    fast.reason(bar(7, 0, 100.0), None, None)
    said = fast.reason(bar(7, 3, 100.8), None, None)
    if not said or "pre-market, no volume confirmation" not in said[1]:
        failures.append(f"a fast pre-market move rings on price alone, and "
                        f"says so: {said!r}")
    slow = Alarms(percent=0.5)
    slow.reason(bar(10, 0, 100.0), None, None)
    if slow.reason(bar(10, 3, 100.8), None, None) is not None:
        failures.append("regular hours still need the volume")

    keen = Persist(gap=30)
    t9 = datetime(2026, 10, 16, 9, 0, tzinfo=ET)
    marks = [keen.urgent("up", t9), keen.urgent("up", t9 + timedelta(minutes=5)),
             keen.urgent("down", t9 + timedelta(minutes=6)),
             keen.urgent("up", t9 + timedelta(minutes=34)),
             keen.urgent("up", t9 + timedelta(minutes=70))]
    if marks != [True, False, True, False, True]:
        failures.append(f"persistent: first ring each way, again only after "
                        f"30 quiet minutes that way: {marks}")
    if Persist(gap=0).urgent("up", t9):
        failures.append("--persist 0 must never send an emergency")
    if ring_direction(None, ("sell", "x")) != "down" or \
            ring_direction(("up", "x"), ("sell", "y")) != "up":
        failures.append("a ring's direction comes from the trend first, then "
                        "the price alarm")
    began = datetime(2026, 10, 16, 10, 0, tzinfo=ET)
    if not before_start(datetime(2026, 10, 16, 6, 40, tzinfo=ET), began) or \
            before_start(began - timedelta(minutes=2), began):
        failures.append("a late start sends nothing from before it started, "
                        "but keeps the last couple of minutes")
    t10 = time(10, 30)
    if to_phone(True, t10, True, time(10, 0), False) or \
            not to_phone(True, t10, False, time(10, 0), True) or \
            not to_phone(False, t10, True, time(10, 0), False):
        failures.append("rings only: alarms reach the phone, spikes do not; "
                        "without it spikes still do")

    # Replay feeds the trend the same minutes live does: the drift to
    # -1.1% after the close is a falling run as well as a level alarm.
    with_trend = replay_alarms(Alarms(percent=0.5), frame, {}, 101.0,
                               Trend(step=0.5))
    if [k for _, k, _, _ in with_trend if k in ("up", "down")] != ["down", "down"]:
        failures.append(f"replay must list the trend's own rings beside the "
                        f"price alarms: {with_trend!r}")

    # ...but only while it is inside the window.
    stale = Alarms()
    bar, lean, dctx, when = reading(0.50, 2.0, price=150.5, at_minute=0)
    stale.reason(bar, lean, dctx)
    bar, lean, dctx, when = reading(0.50, 2.0, price=149.0,
                                    at_minute=MOVE_MINUTES + 2)
    if stale.reason(bar, lean, dctx) is not None:
        failures.append("a move older than the window is not news now")

    ring = Alarms()
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

    # The VWAP side is still tracked on every reading -- worth knowing, and
    # the state has to be right if it is ever wanted again -- but crossing
    # it with no movement behind it no longer rings.
    cross = Alarms()
    bar, lean, dctx, when = reading(0.50, 2.0, vwap_gap=-1.0, at_minute=30)
    cross.reason(bar, lean, dctx)
    if cross.above_vwap is not False:
        failures.append("below VWAP should be recorded as below")
    bar, lean, dctx, when = reading(0.50, 2.0, vwap_gap=1.0, at_minute=31)
    if cross.reason(bar, lean, dctx) is not None:
        failures.append("a VWAP cross with price unmoved no longer rings")
    if cross.above_vwap is not True:
        failures.append("the crossing should still have been recorded")

    # 25 Sep 2026, read back out of the Pushover export: 70 real readings
    # between 12:04 and 15:51, of which SIX rang under the old rule --
    # while the afternoon's entire range was 0.89% and the largest step
    # between any two readings was 0.24%. Nothing happened, and the phone
    # said so six times. It now says nothing.
    #
    # This replaces the 23 Sep fixture, which pinned the lean bands. Those
    # bands no longer decide anything, so that test could only pass by
    # accident. Deliberate change: the alarm means price is moving.
    friday = (
        (0, 148.69, 3.2), (0, 148.59, 2.6), (1, 148.59, 2.0), (1, 148.78, 1.8),
        (2, 148.71, 2.0), (3, 148.76, 1.9), (4, 148.97, 4.5), (5, 148.96, 7.0),
        (6, 148.96, 3.2), (6, 149.06, 8.9), (7, 149.42, 5.8), (8, 149.27, 2.5),
        (9, 149.12, 1.7), (10, 149.24, 1.8), (11, 149.24, 3.7), (11, 149.38, 2.4),
        (12, 149.38, 2.4), (13, 149.23, 2.4), (14, 149.29, 1.5), (15, 149.19, 2.1),
        (16, 148.94, 2.3), (17, 148.80, 1.5), (20, 149.09, 2.5), (21, 149.09, 1.7),
        (21, 149.20, 1.6), (22, 149.23, 2.0), (23, 149.18, 1.7), (24, 148.91, 2.8),
        (25, 149.01, 2.2), (26, 149.01, 1.8), (28, 148.95, 2.5), (29, 148.89, 1.7),
        (31, 148.72, 2.6), (32, 148.88, 1.8), (32, 148.90, 3.1), (33, 148.88, 1.7),
        (34, 148.64, 4.2), (35, 148.74, 4.0), (36, 148.74, 2.2), (36, 148.47, 3.2),
        (37, 148.47, 4.2), (38, 148.50, 4.0), (39, 148.55, 3.1), (40, 148.53, 2.8),
        (41, 148.48, 2.8), (44, 148.42, 1.7), (45, 148.29, 1.7), (46, 148.29, 1.7),
        (47, 148.55, 1.5), (52, 148.66, 2.4), (53, 148.53, 1.8), (54, 148.45, 3.1),
        (55, 148.46, 2.3), (56, 148.46, 1.7), (62, 148.55, 2.5), (71, 148.58, 1.6),
        (89, 148.53, 2.2), (118, 148.46, 1.6), (129, 148.25, 5.1), (132, 148.18, 1.9),
        (142, 148.15, 2.5), (185, 148.19, 1.8), (215, 148.93, 1.9), (218, 148.94, 2.3),
        (219, 149.17, 1.5), (220, 148.97, 1.7), (221, 148.90, 1.9), (222, 148.80, 1.7),
        (225, 148.94, 1.6), (226, 148.84, 1.8))
    noon = datetime.combine(date(2026, 9, 25), time(12, 4), tzinfo=ET)
    afternoon = Alarms()
    rang = []
    for offset, price, ratio in friday:
        at = noon + timedelta(minutes=offset)
        bar = Candle(at=at, open=price, high=price, low=price, close=price,
                     volume=ratio * 40_000, usual_volume=40_000)
        call = afternoon.reason(bar, Lean(0.5, 5, 5, 5, volume_ratio=ratio),
                                Day(last=price, high=149.65, low=146.03,
                                    prev_close=147.79, vwap=price - 0.5))
        if call and afternoon.should_sound(call[0], at):
            rang.append((at.strftime("%H:%M"), call[1]))
    if rang:
        failures.append(f"25 Sep afternoon moved 0.89% in four hours and must "
                        f"not ring at all; it rang {len(rang)}x: {rang}")

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

    failures += overnight_checks()

    # --- the S&P opening call ---------------------------------------------
    d = date(2026, 10, 19)
    for hhmm, done, want in (((9, 30), False, False), ((9, 31), False, True),
                             ((9, 40), False, True), ((9, 41), False, False),
                             ((9, 35), True, False)):
        if open_call_due(datetime.combine(d, time(*hhmm), tzinfo=ET), done) is not want:
            failures.append(f"opening call due at {hhmm} (done={done}) should be {want}")
    if opening_call(0.42) != "S&P 500 OPEN ▲ UP +0.42% from yesterday's close":
        failures.append(f"opening call text: {opening_call(0.42)}")
    if not opening_call(-0.65).startswith("S&P 500 OPEN ▼ DOWN -0.65%"):
        failures.append(f"opening call text: {opening_call(-0.65)}")
    opened_at = datetime.combine(d, REGULAR_OPEN, tzinfo=ET)

    def spy(symbol, start, end, force_sip=False):
        return pd.DataFrame({"close": [99.0, 101.0, 102.0]}, index=pd.DatetimeIndex(
            [opened_at - timedelta(minutes=1), opened_at, opened_at + timedelta(minutes=1)]))

    real_fetch = globals()["fetch_minutes"]
    globals()["fetch_minutes"] = spy
    try:
        move = opening_move(100.0, d)
        none_without_close = opening_move(None, d)
    finally:
        globals()["fetch_minutes"] = real_fetch
    if move is None or abs(move - 1.0) > 1e-9 or none_without_close is not None:
        failures.append(f"the opening move is the 09:30 bar against the close: {move}")

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
    print(f"  Rings at                       : price moving "
          f"{MOVE_PERCENT:.2f}% within {MOVE_MINUTES} min, on "
          f"{ALARM_VOLUME:.1f}x volume")
    print(f"  Sounds, buy / sell             : {SOUND_BUY} / {SOUND_SELL}, "
          f"one per direction per {SOUND_COOLDOWN_MINUTES} min")
    print("  25 Sep afternoon replayed      : 0 rings of 70 readings (was 6)")
    print("  Test push                      : devices and sound names "
          "checked first")
    print("  Half-updated folder            : refused at startup, not at "
          "the first alarm")
    print(f"  S&P opening call               : once a day at {OPEN_CALL_AT:%H:%M}, "
          f"up or down from yesterday's close")
    print("  Overnight, IB Gateway          : rings on the first bar, from "
          "the 16:00 close; blind after 10 min says so")
    print("  Trading client in this file    : none")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nAll checks passed.")
    return 0


# --------------------------------------------------------------------------

def utf8_output() -> None:
    """Print ▲, → and ± whatever Windows does with the output.

    Into a console Python writes Unicode. Piped (`| Select-Object`) or
    redirected -- as a scheduled task's output is -- Windows hands it
    cp1252, which has no ▲, and the first candle line raised
    UnicodeEncodeError. Run that way the watcher would have died at its
    first print, silently, in the background. Unencodable characters are
    replaced rather than fatal; a stream that cannot be reconfigured is
    left alone.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main() -> int:
    utf8_output()
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
    parser.add_argument("--move-percent", type=float, default=MOVE_PERCENT,
                        metavar="PCT",
                        help=f"How far price must travel to earn a noise "
                             f"(default {MOVE_PERCENT}%%). Tune it with "
                             f"--replay against a past session rather than "
                             f"by ear")
    parser.add_argument("--ah-percent", type=float, default=AH_PERCENT,
                        metavar="PCT",
                        help="A separate band for outside 09:30-16:00, used "
                             "by both the speed and the level alarm (default: "
                             "the same as --move-percent). After hours the "
                             "stock drifts, so a smaller band rings sooner; "
                             "choose it with --replay on your own evenings")
    parser.add_argument("--level-step", type=float, default=LEVEL_STEP,
                        metavar="PCT",
                        help="After the after-hours level alarm rings, ring "
                             "again each time price gets PCT further from "
                             "the anchor (default: the band itself, so 0.5 "
                             "rings at 0.5, 1.0, 1.5...). 0 rings once per "
                             "crossing")
    parser.add_argument("--trend-step", type=float, default=TREND_STEP,
                        metavar="PCT",
                        help=f"Trend alarm: ring each time a run extends PCT "
                             f"further from where it started, at a new high or "
                             f"low (default {TREND_STEP}; 0 turns it off)")
    parser.add_argument("--trend-giveback", type=float, default=TREND_GIVEBACK,
                        metavar="PCT",
                        help="Trend alarm: how far a run must reverse off its "
                             "extreme to count as over -- 'giving it back' "
                             "(default: the same as --trend-step)")
    parser.add_argument("--climb-sound", default=SOUND_CLIMB, metavar="NAME",
                        help=f"Pushover sound for a trend ring upward "
                             f"(default {SOUND_CLIMB})")
    parser.add_argument("--falling-sound", default=SOUND_FALLING, metavar="NAME",
                        help=f"Pushover sound for a trend ring downward "
                             f"(default {SOUND_FALLING})")
    parser.add_argument("--market-drop", type=float, default=MARKET_DROP,
                        metavar="PCT",
                        help=f"Ring when the S&P 500 is down PCT from "
                             f"yesterday's close (default {MARKET_DROP}; 0 turns "
                             f"it off)")
    parser.add_argument("--market-step", type=float, default=MARKET_STEP,
                        metavar="PCT",
                        help=f"...and again every PCT further (default "
                             f"{MARKET_STEP})")
    parser.add_argument("--market-extended", type=float,
                        default=MARKET_EXTENDED, metavar="PCT",
                        help=f"From PCT down, each market ring is a Pushover "
                             f"emergency that repeats every minute until "
                             f"acknowledged (default {MARKET_EXTENDED}; 0 "
                             f"turns it off)")
    parser.add_argument("--market-sound", default=SOUND_MARKET, metavar="NAME",
                        help=f"Pushover sound for the market alarm "
                             f"(default {SOUND_MARKET})")
    parser.add_argument("--from-price", type=float, default=None,
                        metavar="PRICE",
                        help="Anchor the after-hours level alarm on this "
                             "price -- your entry, say -- instead of today's "
                             "regular-session close")
    parser.add_argument("--persist", type=int, default=PERSIST_GAP,
                        metavar="MIN",
                        help="The first ring of a new move, up or down -- none "
                             "that way for MIN minutes -- is a Pushover "
                             "emergency that repeats until acknowledged "
                             f"(default {PERSIST_GAP}: off)")
    parser.add_argument("--rings-only", action="store_true",
                        help="Send the phone alarms only: no quiet minute "
                             "updates, no volume-spike notes, no summary")
    parser.add_argument("--up-only", action="store_true",
                        help="Ring on run-ups only; stay silent on declines. "
                             "For a day spent flat, watching for a reason to "
                             "come back in.")
    parser.add_argument("--alarm-volume", type=float, default=ALARM_VOLUME,
                        help=f"Volume the move must ALSO carry to ring "
                             f"(default {ALARM_VOLUME}x the recent average). "
                             f"Set 1.0 to ring on the price move alone -- a "
                             f"quiet grind upward never reaches the default.")
    parser.add_argument("--move-minutes", type=int, default=MOVE_MINUTES,
                        metavar="MIN",
                        help=f"The window that move has to happen inside "
                             f"(default {MOVE_MINUTES})")
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
    parser.add_argument("--overnight", action="store_true",
                        help="Also watch the overnight session (20:00-03:50, "
                             "Sunday to Thursday nights) through IB Gateway, "
                             "and keep running day and night. Needs IB Gateway "
                             "logged in on port 4001 and ib_async installed")
    parser.add_argument("--dry-run", action="store_true", help="Print, do not send")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--test-push", action="store_true",
                        help="Send one of each kind to the phone: a silent "
                             "reading, a buy ring, a sell ring")
    parser.add_argument("--list-sounds", action="store_true",
                        help="Ask Pushover which sound names this app can use")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.ah_percent is not None and args.ah_percent <= 0:
        raise SystemExit("--ah-percent must be above 0")
    if args.level_step is not None and args.level_step < 0:
        raise SystemExit("--level-step must be 0 or more")
    for flag, value in (("--trend-step", args.trend_step),
                        ("--market-drop", args.market_drop),
                        ("--market-extended", args.market_extended)):
        if value < 0:
            raise SystemExit(f"{flag} must be 0 (off) or more")
    for flag, value in (("--trend-giveback", args.trend_giveback),
                        ("--market-step", args.market_step)):
        if value is not None and value <= 0:
            raise SystemExit(f"{flag} must be above 0")

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
        return test_push(args.buy_sound, args.sell_sound,
                         climb_sound=args.climb_sound,
                         falling_sound=args.falling_sound,
                         market_sound=args.market_sound)

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
                          args.volume_alert,
                          Alarms(percent=args.move_percent,
                                 minutes=args.move_minutes,
                                 volume=args.alarm_volume,
                                 up_only=args.up_only,
                                 anchor_price=args.from_price,
                                 ah_percent=args.ah_percent,
                                 level_step=args.level_step),
                          Trend(step=args.trend_step,
                                giveback=args.trend_giveback),
                          MarketAlarm(drop=args.market_drop,
                                      step=args.market_step,
                                      extended=args.market_extended))

    def day_run() -> int:
        return run_live(symbol, start, end, args.dry_run, db, args.push_empty,
                        args.volume_alert, args.pdf_every, args.db, detail,
                        args.buy_sound, args.sell_sound,
                        args.move_percent, args.move_minutes,
                        args.alarm_volume, args.up_only, args.from_price,
                        args.ah_percent, args.level_step,
                        args.trend_step, args.trend_giveback,
                        args.climb_sound, args.falling_sound,
                        args.market_drop, args.market_step, args.market_sound,
                        args.market_extended, args.persist, args.rings_only)

    if not args.overnight:
        return day_run()
    if start < OVERNIGHT_END or end > OVERNIGHT_START:
        raise SystemExit(f"--overnight needs the day inside "
                         f"{OVERNIGHT_END:%H:%M}-{OVERNIGHT_START:%H:%M}; "
                         f"got {start:%H:%M}-{end:%H:%M}")
    try:
        import ib_async  # noqa: F401 -- only the night needs it
    except ImportError:
        raise SystemExit("--overnight reads IB Gateway through ib_async, which "
                         "is not installed for this Python. Install it with:\n"
                         "    python -m pip install ib_async")

    def night_run(night_start: datetime, night_end: datetime,
                  started: datetime) -> int:
        return run_overnight(
            symbol, night_start, night_end, args.dry_run, db,
            Alarms(percent=args.move_percent, minutes=args.move_minutes,
                   volume=args.alarm_volume, up_only=args.up_only,
                   anchor_price=args.from_price, ah_percent=args.ah_percent,
                   level_step=args.level_step),
            Trend(step=args.trend_step, giveback=args.trend_giveback),
            Persist(gap=args.persist),
            (args.buy_sound, args.sell_sound, args.climb_sound,
             args.falling_sound),
            started)

    print("Around the clock: the day on Alpaca, the overnight session on "
          "IB Gateway.")
    try:
        return run_around_the_clock(start, end, day_run, night_run)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
