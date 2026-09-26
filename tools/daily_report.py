"""
Daily report: a session as a PDF, instead of a screenful of scrollback.

Fetches one trading day's one-minute bars, folds in any signals already
logged, and writes a three-page PDF:

  1. The session   candles, VWAP, volume, and where signals fired
  2. Participation volume against what that slot usually carries, the
                   size of the average trade, and which way volume leaned
  3. Signals       every one, with what price did afterwards

Past days come from the full SIP tape, which the free plan serves
historically -- so this works today, on any past session, without
waiting for a week of live logging to accumulate.

    python daily_report.py                      # the last trading day
    python daily_report.py --date 2026-09-18
    python daily_report.py --until 16:00        # the whole session
    python daily_report.py --no-open            # write it, do not open it

READ-ONLY. Market-data client only. No trading client, no order object.
"""

from __future__ import annotations

import argparse
import csv
import re
import json
import os
import sqlite3
import statistics
import subprocess
import sys
import textwrap
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import Rectangle

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
from feed_check import ET, add_macd, load_env, parse_clock, trading_days
from spcx_alert import MACD_SETTING, WARMUP_MINUTES
from open_candles import BAR_MINUTES, aggregate, fetch_minutes, read_lean, thousands
from spcx_alert import open_db

SYMBOL = "SPCX"
#: Matches open_candles: the page covers the run-up as well as the
#: session, so the tape a signal was read from is on the chart.
WINDOW_START = time(9, 15)
WINDOW_END = time(16, 0)
BASELINE_SESSIONS = 10

#: The market, for comparison. A stock down 4% on a day everything fell 4%
#: has done nothing; the same number against a flat market is the whole
#: story. Measuring one symbol in isolation quietly attributes the market's
#: mood to the stock. SPY is the broad US market; --benchmark takes any
#: symbol, and an empty one turns the comparison off.
BENCHMARK = "SPY"

# ---------------------------------------------------------------------------
# Palette. The categorical pair is validated for colour-vision deficiency in
# both light and dark; candles use the fixed status pair, which is the one
# place hue convention beats palette theory -- a red candle is red.
# ---------------------------------------------------------------------------
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SURFACE = "#fcfcfb"
PLANE = "#f5f5f1"

UP = "#0ca30c"          # status: good
DOWN = "#d03b3b"        # status: critical
ACCENT = "#2a78d6"      # categorical slot 1 -- signals
SECOND = "#eb6834"      # categorical slot 2 -- the comparison series
VWAP_HUE = "#4a3aa7"    # a contrasting hue, dashed, so it never reads as a series

#: The sentiment ramp: the seven words the alerts use, as a diverging
#: scale. Two hues with a NEUTRAL middle, never a red-orange-yellow-green
#: rainbow -- a hue at the midpoint would colour "balanced" as though the
#: tape were saying something. Strength is carried by how far from the
#: middle a reading sits, which is what the eye reads on a diverging
#: scale anyway. Upper edge, label, colour, opacity.
SENTIMENT_BANDS = (
    (18, "sellers in control", DOWN, 0.30),
    (30, "sellers pressing", DOWN, 0.20),
    (42, "sellers showing up", DOWN, 0.10),
    (58, "balanced", MUTED, 0.10),
    (70, "buyers showing up", UP, 0.10),
    (82, "buyers pressing", UP, 0.20),
    (100, "buyers in control", UP, 0.30),
)


def strip_opacity(score: float) -> float:
    """How solid a ribbon block is: distance from the middle, not the band.

    The bands behind the lean line are washes under a line and have to
    stay out of its way. The ribbon has no line over it and one job --
    being read at a glance from across the room -- so it runs far more
    solid, and fades toward the middle so a balanced tape looks like
    nothing rather than like a colour.
    """
    distance = min(1.0, abs(score - 50.0) / 50.0)
    return round(0.14 + 0.72 * distance, 3)


def sentiment_colour(score: float) -> tuple:
    """A 0-100 reading to its band's colour and opacity."""
    for edge, _, colour, alpha in SENTIMENT_BANDS:
        if score < edge:
            return colour, alpha
    return SENTIMENT_BANDS[-1][2], SENTIMENT_BANDS[-1][3]

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 9,
    "axes.edgecolor": AXIS,
    "axes.labelcolor": INK_2,
    "axes.titlecolor": INK,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "text.color": INK,
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "axes.grid": True,
    "grid.color": GRID,
    "grid.linewidth": 0.6,
    "axes.axisbelow": True,
})


# ---------------------------------------------------------------------------
# Gathering
# ---------------------------------------------------------------------------

@dataclass
class Session:
    symbol: str
    day: date
    minutes: pd.DataFrame
    candles: pd.DataFrame
    vwap: pd.Series
    baseline: Dict[time, float]
    signals: pd.DataFrame
    macd: pd.DataFrame        # 1-minute, the bars the alerts read
    notes: List[Note] = field(default_factory=list)
    trades: List[Trade] = field(default_factory=list)
    start: time = WINDOW_START      # the window the page is drawn for,
    end: time = WINDOW_END          # not the part of it that has printed
    benchmark: Optional[Tuple[str, float]] = None   # (symbol, % over the window)


def session_vwap(minutes: pd.DataFrame) -> pd.Series:
    """Anchored at the open, as every platform draws it."""
    typical = (minutes["high"] + minutes["low"] + minutes["close"]) / 3.0
    return (typical * minutes["volume"]).cumsum() / minutes["volume"].cumsum().replace(0, pd.NA)


def slot_baseline(symbol: str, day: date, start: time = time(9, 30),
                  end: time = time(16, 0),
                  force_sip: bool = True) -> Dict[time, float]:
    """Median 5-minute volume per clock slot over recent sessions.

    `force_sip` has to match the feed the session's own bars came from.
    SIP is the whole tape, IEX one venue carrying a fraction of it, and
    a ratio between the two is not a ratio: it reads near zero however
    busy the market is. A past day is SIP on both sides; a live session
    is whatever the feed setting gives, on both sides.
    """
    gathered: Dict[time, List[float]] = {}
    for past in trading_days(day - timedelta(days=1), BASELINE_SESSIONS):
        try:
            bars = fetch_minutes(
                symbol,
                datetime.combine(past, start, tzinfo=ET),
                datetime.combine(past, end, tzinfo=ET),
                force_sip=force_sip)
        except Exception:  # noqa: BLE001 -- a baseline is a nicety
            continue
        if bars.empty:
            continue
        for stamp, row in aggregate(bars).iterrows():
            gathered.setdefault(stamp.time(), []).append(float(row["volume"]))
    return {slot: statistics.median(v) for slot, v in gathered.items() if v}


#: Where the read-at-the-time notes live. Data, not code: adding one is
#: editing a line of text, and the file is read fresh on every rebuild.
#: Like the two calendars, a missing or malformed file costs the notes
#: and nothing else -- a chart that refuses to draw because an annotation
#: had a typo would be a worse trade than a chart with no annotations.
NOTES_PATH = "notes.json"

#: A note's text is wrapped to this many characters. Wider than this and
#: a label swallows the candles it is pointing at; much narrower and a
#: sentence becomes a column.
NOTE_WRAP = 30

#: One 6.3pt line of note text as a share of the price panel's height,
#: and the padding a box adds around its lines. Fractions of the panel
#: rather than dollars: raising the ceiling to fit a stack changes how
#: many dollars tall a line of text is, so measuring in price is
#: circular and the boxes grow as fast as the room made for them.
NOTE_LINE, NOTE_PAD = 0.046, 0.020


#: Where the day's fills live. NEVER committed -- .gitignore carries a
#: rule for it, because these are account records rather than research.
#: Absent, the chart draws without them, so a fresh checkout still works.
TRADES_PATH = "trades.csv"

#: Column names the two brokers might use for the same thing. Robinhood
#: and DAS disagree with each other and with themselves across export
#: versions, so the reader sniffs rather than insisting. A header this
#: does not recognise is reported by name rather than silently dropped:
#: a trade log that quietly reads zero rows is worse than one that fails.
TRADE_COLUMNS = {
    "at": ("time", "datetime", "date/time", "filled at", "exec time",
           "execution time", "timestamp", "trade time", "date"),
    "side": ("side", "b/s", "action", "buy/sell", "type", "direction"),
    "quantity": ("qty", "quantity", "shares", "filled qty", "size", "exec qty"),
    "price": ("price", "fill price", "exec price", "avg price",
              "average price", "trade price"),
    "symbol": ("symbol", "ticker", "instrument", "stock"),
}


@dataclass
class Fill:
    """One execution. Long-only, so a buy opens and a sell closes."""
    at: datetime
    side: str                 # "buy" or "sell"
    quantity: float
    price: float


@dataclass
class Trade:
    """A round trip: shares bought, then sold.

    Brokers export fills, not trades. A position opened in two lots and
    closed in one is three rows that describe one decision, so the fills
    are paired oldest-first into round trips before anything is drawn.
    """
    opened: datetime
    closed: Optional[datetime]
    quantity: float
    entry: float
    exit: Optional[float]

    @property
    def profit(self) -> Optional[float]:
        if self.exit is None:
            return None
        return (self.exit - self.entry) * self.quantity

    @property
    def won(self) -> bool:
        return (self.profit or 0.0) > 0

    def label(self) -> str:
        shares = f"{self.quantity:,.0f}"
        if self.profit is None:
            return f"{shares} sh · open"
        return f"{shares} sh · {'+' if self.profit >= 0 else '-'}$" \
               f"{abs(self.profit):,.0f}"


def pair_fills(fills: Sequence[Fill]) -> List[Trade]:
    """Fills into round trips, oldest lot closed first.

    FIFO because that is what a broker reports and what the tax year
    assumes; matching some other way would make the chart disagree with
    the statement it came from.

    A buy left unmatched at the end of the day is an open position, not
    an error -- it is drawn with an entry and no exit.
    """
    open_lots: List[List[float]] = []          # [quantity, price, opened]
    trades: List[Trade] = []
    for fill in sorted(fills, key=lambda f: f.at):
        if fill.side == "buy":
            open_lots.append([fill.quantity, fill.price, fill.at])
            continue
        remaining = fill.quantity
        while remaining > 1e-9 and open_lots:
            lot = open_lots[0]
            took = min(lot[0], remaining)
            trades.append(Trade(opened=lot[2], closed=fill.at, quantity=took,
                                entry=lot[1], exit=fill.price))
            lot[0] -= took
            remaining -= took
            if lot[0] <= 1e-9:
                open_lots.pop(0)
        # A sell with nothing open is a short, and this is a long-only
        # book -- so it is someone else's row, and it is left alone.

    for quantity, price, opened in open_lots:
        trades.append(Trade(opened=opened, closed=None, quantity=quantity,
                            entry=price, exit=None))
    return sorted(trades, key=lambda t: t.opened)


def stack_notes(heights: Sequence[float], xs: Sequence[float],
                width: float) -> List[float]:
    """Bottom edge for each label so none overlaps another, from zero up.

    A label is placed above anything already occupying its stretch of x,
    where "its stretch" is `width` slots either side -- the width of the
    box being avoided. Everything is relative to zero; where the whole
    layer finally sits is the caller's decision, once the total height
    is known. Clamping each box as it is placed instead folds the top of
    a tall stack back down into the one beneath it.
    """
    placed: List[Tuple[float, float]] = []
    bottoms: List[float] = []
    for height, x in zip(heights, xs):
        bottom = 0.0
        for other_x, other_top in placed:
            if abs(x - other_x) < width:
                bottom = max(bottom, other_top)
        bottoms.append(bottom)
        placed.append((x, bottom + height))
    return bottoms


@dataclass
class Note:
    """One observation, made at a moment, by a named someone.

    `who` is carried by the ring's line style AND spelled out in the
    label, never by colour alone. Notes are annotation rather than a
    measure, so they wear ink and leave the categorical hues to the
    series that need them.
    """
    at: time
    who: str
    text: str

    @property
    def mine(self) -> bool:
        return self.who.lower() not in ("jason", "you")

    @property
    def label(self) -> str:
        head = "Me" if self.mine else "You"
        body = "\n".join(textwrap.wrap(self.text, NOTE_WRAP))
        return f"{self.at:%H:%M}  {head}\n{body}"


def load_notes(path: str, symbol: str, day: date) -> List[Note]:
    """The notes for one symbol on one day, oldest first.

    Every failure is silence: no file, bad JSON, a missing key, a time
    that will not parse. The chart is the deliverable and an annotation
    is a garnish on it.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        entries = raw.get(symbol, [])
    except (OSError, ValueError, AttributeError):
        return []

    notes: List[Note] = []
    for entry in entries:
        try:
            if entry["date"] != f"{day:%Y-%m-%d}":
                continue
            hh, mm = (int(part) for part in entry["time"].split(":"))
            notes.append(Note(at=time(hh, mm), who=str(entry.get("who", "")),
                              text=str(entry["note"])))
        except (KeyError, TypeError, ValueError):
            continue          # one bad entry is not the whole file
    return sorted(notes, key=lambda note: note.at)


def read_xlsx(path: str) -> List[Dict[str, str]]:
    """A spreadsheet's cells, by column letter, using only the standard library.

    An .xlsx is a zip of XML, so this needs no openpyxl and therefore no
    pip install on the machine that actually runs it. Values are keyed by
    COLUMN LETTER rather than position: IBKR's confirmation sheet merges
    cells, so a row's tenth value is not its tenth column, and reading
    positionally puts the price where the quantity should be.
    """
    with zipfile.ZipFile(path) as book:
        shared: List[str] = []
        if "xl/sharedStrings.xml" in book.namelist():
            raw = book.read("xl/sharedStrings.xml").decode("utf-8")
            shared = [re.sub(r"<[^>]+>", "", piece)
                      for piece in re.findall(r"<si>(.*?)</si>", raw, re.S)]
        sheet = book.read("xl/worksheets/sheet1.xml").decode("utf-8")

    rows: List[Dict[str, str]] = []
    for chunk in re.findall(r"<row[^>]*>.*?</row>", sheet, re.S):
        cells: Dict[str, str] = {}
        # An empty cell is written self-closing -- <c r="C22" s="100"/> --
        # and a pattern that only knows the <c ...>...</c> form runs
        # straight past it to the NEXT closing tag, swallowing the two
        # columns in between. Merged sheets are full of empty cells, so
        # this is the common case rather than an edge one.
        for cell in re.finditer(
                r'<c r="([A-Z]+)\d+"([^>]*?)(?:/>|>(.*?)</c>)', chunk, re.S):
            column, attrs, body = cell.groups()
            value = re.search(r"<v>(.*?)</v>", body or "", re.S)
            if not value:
                continue
            if 't="s"' in attrs:
                index = int(value.group(1))
                cells[column] = shared[index] if index < len(shared) else ""
            else:
                cells[column] = value.group(1)
        if cells:
            rows.append(cells)
    return rows


#: Where each field sits in an IBKR Trade Confirmation spreadsheet.
IBKR_COLUMNS = dict(symbol="B", at="E", exchange="I", side="J",
                    quantity="L", price="N")


def read_ibkr(path: str, symbol: str, day: date) -> List[Fill]:
    """Fills from an IBKR Trade Confirmation spreadsheet.

    The sheet lists every order TWICE: once as a summary with the
    exchange shown as "-", then once per venue that actually filled it.
    Reading both doubles the day. The summaries are dropped.

    Sell quantities arrive negative, which is IBKR's convention for a
    reduction rather than a direction to be preserved -- the side column
    already says which way the trade went.
    """
    fills: List[Fill] = []
    for row in read_xlsx(path):
        got = {name: row.get(column, "") for name, column in IBKR_COLUMNS.items()}
        if got["symbol"].strip().upper() != symbol.upper():
            continue
        if got["exchange"].strip() in ("", "-"):
            continue                      # the order summary, not a fill
        try:
            at = datetime.strptime(got["at"].strip(), "%Y-%m-%d, %H:%M:%S")
        except ValueError:
            continue
        if at.date() != day:
            continue
        try:
            fills.append(Fill(
                at=at.replace(tzinfo=ET),
                side="buy" if got["side"].strip().upper().startswith("B") else "sell",
                quantity=abs(float(got["quantity"].replace(",", ""))),
                price=float(got["price"])))
        except (TypeError, ValueError):
            continue
    return fills


def load_trades(path: str, symbol: str, day: date) -> List[Trade]:
    """The day's round trips for one symbol, or nothing at all.

    Unlike the notes, a problem here is worth saying out loud on the
    terminal: a chart drawn without trades looks exactly like a day you
    did not trade, and that is a difference worth knowing about. The
    chart is still built either way.
    """
    if path.lower().endswith((".xlsx", ".xlsm")):
        try:
            return pair_fills(read_ibkr(path, symbol, day))
        except (OSError, KeyError, zipfile.BadZipFile) as exc:
            print(f"  trades: could not read {path} — {type(exc).__name__}")
            return []

    try:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
    except OSError:
        return []
    if not rows:
        return []

    found = {}
    headers = {(name or "").strip().lower(): name for name in rows[0]}
    for field, names in TRADE_COLUMNS.items():
        for candidate in names:
            if candidate in headers:
                found[field] = headers[candidate]
                break
    missing = [f for f in ("at", "side", "quantity", "price") if f not in found]
    if missing:
        print(f"  trades: {path} has no column for {', '.join(missing)} "
              f"— found {list(headers)}")
        return []

    fills: List[Fill] = []
    for row in rows:
        try:
            if "symbol" in found:
                got = str(row[found["symbol"]]).strip().upper()
                if got and got != symbol.upper():
                    continue
            at = pd.to_datetime(row[found["at"]])
            at = (at.tz_localize(ET) if at.tzinfo is None
                  else at.tz_convert(ET))
            if at.date() != day:
                continue
            side = str(row[found["side"]]).strip().lower()
            side = "buy" if side.startswith("b") else "sell"
            fills.append(Fill(at=at.to_pydatetime(), side=side,
                              quantity=abs(float(str(row[found["quantity"]])
                                                 .replace(",", ""))),
                              price=float(str(row[found["price"]])
                                          .replace("$", "").replace(",", ""))))
        except (KeyError, TypeError, ValueError):
            continue          # one unreadable row is not the whole file
    return pair_fills(fills)


def draw_trades(price, trades: List[Trade], slot_of: Dict, candles) -> None:
    """Each round trip as a span from entry to exit.

    Drawn in ink rather than green and red: the candles already own
    those two hues, and a coloured horizontal bar among them reads as
    part of the price action rather than as something laid over it.
    Won or lost is carried by the marker at the exit -- a filled square
    for a winner, hollow for a loser -- and by the sign in the label, so
    it survives being printed in black and white.
    """
    if not trades:
        return

    def slot_x(when: datetime) -> Optional[float]:
        slot = when.replace(minute=when.minute - (when.minute % BAR_MINUTES),
                            second=0, microsecond=0)
        return slot_of.get(slot)

    for trade in trades:
        x0 = slot_x(trade.opened)
        if x0 is None:
            continue
        x1 = slot_x(trade.closed) if trade.closed else max(slot_of.values())

        price.plot([x0, x1], [trade.entry, trade.entry], color=INK,
                   linewidth=1.5, alpha=0.55, zorder=4,
                   solid_capstyle="butt")
        price.scatter([x0], [trade.entry], marker="o", s=34, color=INK,
                      zorder=6, edgecolors=SURFACE, linewidths=0.7)
        if trade.exit is not None and x1 is not None:
            price.plot([x1, x1], [trade.entry, trade.exit], color=INK,
                       linewidth=1.0, alpha=0.45, zorder=4)
            price.scatter([x1], [trade.exit], marker="s", s=38,
                          facecolors=INK if trade.won else SURFACE,
                          edgecolors=INK, linewidths=1.1, zorder=6)
        price.annotate(trade.label(), xy=((x0 + (x1 or x0)) / 2.0, trade.entry),
                       xytext=(0, -11), textcoords="offset points",
                       ha="center", va="top", fontsize=5.8, color=INK_2,
                       zorder=6)


def draw_notes(price, notes: List[Note], slot_of: Dict, candles) -> None:
    """Ring the bar a note is about, and write the note beside it.

    Two decisions worth keeping:

    The ring goes round the whole candle rather than a single price, so
    it marks the moment rather than a point inside it, and it is drawn
    hollow so the bar it circles stays readable.

    Labels alternate above and below the price and step further out when
    two land close together. That is cruder than a solver, and it is
    enough: a day carries a handful of notes, not a hundred. What it
    guarantees is that consecutive notes never write over each other --
    the failure that makes an annotated chart worse than a bare one.
    """
    if not notes:
        return

    span = float(candles["high"].max() - candles["low"].min()) or 1.0
    top = float(candles["high"].max())
    wide = max(slot_of.values()) if slot_of else 1

    # Everything goes ABOVE the candles. Below looks tempting -- it
    # halves the stacking -- but the price panel has the mood strip and
    # the volume chart immediately under it, so a label placed below
    # does not overflow into empty paper, it overflows onto another
    # chart. Above there is headroom, and what there is not can be made
    # by lifting the ceiling, which costs nothing.
    # Labels are stacked in AXES FRACTION, not in price. Measuring a
    # box's height in dollars is circular: raising the ceiling to fit
    # the stack changes how many dollars tall a line of text is, so the
    # boxes grow as fast as the room made for them and keep colliding.
    # A fraction of the panel is fixed, whatever the ylim ends up being.
    LINE = NOTE_LINE            # one 6.3pt line as a share of the panel
    PAD = 0.020                 # the box's own padding, plus a gap
    FLOOR = 0.52                # notes usually begin here
    FLOOR_MIN = 0.34            # and never squeeze the candles below this
    ROOF = 0.97                 # nothing is drawn above this
    #: How many slots wide a label is, near enough. At NOTE_WRAP
    #: characters of 6.3pt across a session of 5-minute slots this is
    #: about thirteen. Set it too wide and notes an hour apart stack on
    #: each other and climb off the top of the panel; too narrow and
    #: neighbours overlap. It is the width of the thing being avoided.
    BOX_SLOTS = 13

    heights: List[float] = []
    columns: List[float] = []
    spots: List[list] = []

    for note in notes:
        stamp = datetime.combine(candles.index[0].date(), note.at, tzinfo=ET)
        slot = stamp.replace(minute=stamp.minute - (stamp.minute % BAR_MINUTES),
                             second=0, microsecond=0)
        if slot not in slot_of or slot not in candles.index:
            continue
        x = slot_of[slot]
        bar = candles.loc[slot]
        middle = float(bar["high"] + bar["low"]) / 2.0

        # Sit above anything already occupying this stretch of x. Two
        # notes on the same minute -- yours and mine on the same moment
        # -- are the common case here, not the exception.
        height = (note.label.count("\n") + 1) * LINE + PAD
        heights.append(height)
        columns.append(x)
        # Keep the box inside the panel. At the last slot of the day a
        # centred label hangs off the right edge, which is how the 15:43
        # note got itself clipped the first time this was drawn.
        margin = wide * 0.085
        x_text = min(max(x, margin), wide - margin)

        spots.append([x, x_text, note, middle, 0.0])

    bottoms = stack_notes(heights, columns, BOX_SLOTS)
    for spot, bottom in zip(spots, bottoms):
        spot[4] = bottom
    ceiling = max((b + h for b, h in zip(bottoms, heights)), default=0.0)

    # The deepest stack decides where the notes begin. Usually that is
    # FLOOR; a crowded few minutes pushes the whole layer down, taking
    # room from the candles rather than running off the top of the page.
    # Below FLOOR_MIN the candles would be squeezed to a ribbon, so a
    # busier day than that accepts an overlap instead.
    floor = max(FLOOR_MIN, min(FLOOR, ROOF - ceiling))

    # Make room BEFORE drawing, so the stack computed above is the one
    # that gets drawn.
    low, _ = price.get_ylim()
    price.set_ylim(low, low + (top - low) / floor)

    for x, x_text, note, middle, bottom in spots:
        price.scatter([x], [middle], s=300, facecolors="none",
                      edgecolors=INK_2, linewidths=1.4, zorder=5,
                      linestyle=(0, (2, 1.5)) if note.mine else "solid")
        price.annotate(
            note.label, xy=(x, middle), xycoords="data",
            xytext=(x_text, floor + bottom),
            textcoords=price.get_xaxis_transform(),
            ha="center", va="bottom",
            fontsize=6.3, color=INK, zorder=6, linespacing=1.35,
            bbox=dict(boxstyle="round,pad=0.32", facecolor=SURFACE,
                      edgecolor=AXIS, linewidth=0.6, alpha=0.94),
            arrowprops=dict(arrowstyle="-", color=INK_2, linewidth=0.8,
                            shrinkA=1, shrinkB=9, alpha=0.75))


def logged_signals(db_path: str, symbol: str, day: date) -> pd.DataFrame:
    """Whatever the alert tool has recorded for this day. Often nothing yet."""
    if not os.path.exists(db_path):
        return pd.DataFrame()
    try:
        db: sqlite3.Connection = open_db(db_path)
        rows = db.execute(
            "SELECT * FROM signals WHERE symbol = ? AND bar_time LIKE ? ORDER BY bar_time",
            (symbol, f"{day.isoformat()}%")).fetchall()
    except sqlite3.Error:
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame([dict(r) for r in rows])
    frame["bar_time"] = pd.to_datetime(frame["bar_time"])
    return frame


def market_move(symbol: str, day: date, start: time, end: time,
                force_sip: bool) -> Optional[Tuple[str, float]]:
    """The benchmark's move across the same window, or None.

    Same window as the session, or the comparison is between two
    different questions. A failure here loses a line of context and
    must never cost the report: the stock's own day is the point.
    """
    if not symbol:
        return None
    try:
        bars = fetch_minutes(symbol,
                             datetime.combine(day, start, tzinfo=ET),
                             datetime.combine(day, end, tzinfo=ET),
                             force_sip=force_sip)
    except Exception:  # noqa: BLE001 -- context is a nicety, the session is not
        return None
    if bars.empty:
        return None
    first, last = bars.iloc[0], bars.iloc[-1]
    if not first["open"]:
        return None
    return symbol.upper(), 100.0 * (last["close"] - first["open"]) / first["open"]


def gather(symbol: str, day: date, start: time, end: time, db_path: str,
           force_sip: bool = True, benchmark: str = BENCHMARK) -> Optional[Session]:
    """Build a session. `force_sip` is right for a past day -- the free plan
    serves the full tape historically -- and wrong for today, where SIP is
    15 minutes behind and the live feed is what the alerts are reading."""
    opens = datetime.combine(day, start, tzinfo=ET)
    closes = datetime.combine(day, end, tzinfo=ET)

    # Reach back as far as the alert engine does. MACD is an exponential
    # average that carries across the session boundary, so a cold start at
    # 09:25 would draw roughly twenty minutes of meaningless wiggle across
    # exactly the part of the morning being studied -- and would not be the
    # line the alerts were reading.
    warm = fetch_minutes(symbol, opens - timedelta(minutes=WARMUP_MINUTES),
                         closes, force_sip=force_sip)
    minutes = warm[(warm.index >= opens) & (warm.index <= closes)]
    if minutes.empty:
        return None

    macd = add_macd(warm, MACD_SETTING)
    settled = macd.index[MACD_SETTING.warmup_bars:]
    macd = macd.loc[macd.index.isin(settled) & (macd.index >= opens)
                    & (macd.index <= closes)]

    return Session(
        symbol=symbol, day=day, minutes=minutes,
        candles=aggregate(minutes), vwap=session_vwap(minutes),
        baseline=slot_baseline(symbol, day, start, end, force_sip=force_sip),
        signals=logged_signals(db_path, symbol, day),
        notes=load_notes(NOTES_PATH, symbol, day),
        trades=load_trades(TRADES_PATH, symbol, day),
        macd=macd,
        start=start, end=end,
        benchmark=market_move(benchmark, day, start, end, force_sip),
    )


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def band(fig, session: Session) -> None:
    """The header: what this day did, in the numbers you would say aloud."""
    candles = session.candles
    first, last = candles.iloc[0], candles.iloc[-1]
    move = last["close"] - first["open"]
    pct = 100 * move / first["open"]
    colour = UP if move >= 0 else DOWN

    # Two lines, not four. Every tenth of an inch the header takes is a
    # tenth of an inch off five charts underneath it.
    fig.text(0.045, 0.963, f"{session.symbol}", size=15, weight="bold", color=INK)
    fig.text(0.108, 0.9625, f"{session.day:%A %d %B %Y}", size=9.5, color=INK_2)

    stats = [
        ("Open", f"${first['open']:,.2f}", INK),
        ("Close", f"${last['close']:,.2f}", INK),
        ("Change", f"{move:+.2f} ({pct:+.2f}%)", colour),
        ("High", f"${candles['high'].max():,.2f}", INK_2),
        ("Low", f"${candles['low'].min():,.2f}", INK_2),
        ("Volume", thousands(candles["volume"].sum()), INK_2),
    ]
    if session.benchmark:
        # The stock's own move minus the market's. This is the number that
        # says whether anything happened HERE, rather than everywhere.
        market, market_pct = session.benchmark
        relative = pct - market_pct
        stats.append((f"vs {market}", f"{relative:+.2f}%",
                      UP if relative >= 0 else DOWN))

    span = min(0.152, 0.90 / max(1, len(stats)))
    for i, (label, value, tone) in enumerate(stats):
        x = 0.045 + i * span
        fig.text(x, 0.9355, label.upper(), size=7, color=MUTED)
        fig.text(x + span * 0.29, 0.934, value, size=10, color=tone)

    fig.add_artist(plt.Line2D([0.045, 0.965], [0.920, 0.920],
                              color=AXIS, linewidth=0.8, transform=fig.transFigure))

    notes = []
    if session.benchmark:
        market, market_pct = session.benchmark
        notes.append(f"{market} {market_pct:+.2f}% over the same window")
    unlock = lockups.headline(session.symbol, session.day) if lockups else None
    if unlock:
        notes.append(unlock)
    flight = launches.headline(session.symbol, session.day) if launches else None
    if flight:
        notes.append(flight)
    if notes:
        fig.text(0.045, 0.9035, "  ·  ".join(notes), size=8, color=INK_2)


def label_every(slots) -> int:
    """Minutes between time labels, so a full session is not a picket fence.

    Fifteen-minute labels are right for an hour of tape and unreadable
    across a whole session -- twenty-seven of them, overlapping. Aim for
    roughly a dozen either way.
    """
    if len(slots) <= 36:
        return 15
    if len(slots) <= 96:
        return 30
    return 60


def tick_positions(stamps, every: int):
    idx = [i for i, s in enumerate(stamps) if s.minute % every == 0]
    return idx, [f"{stamps[i]:%H:%M}" for i in idx]


def panel_label(ax, text: str) -> None:
    """Title above the panel, never on it.

    An earlier version put these inside the axes to save vertical space.
    It saved the space and spent it on legibility: the title landed on
    whatever the line was doing at the left of the chart. A heading that
    covers the data is not a saving.
    """
    ax.set_title(text, loc="left", size=8.5, color=INK_2, weight="normal", pad=3.5)


def session_slots(session: "Session") -> list:
    """Every candle slot in the window, printed or not.

    The axis used to span however many candles had arrived, so at 09:55
    six candles shared the whole panel and each was drawn a sixth of it
    wide. The candles were not wide; the axis was narrow, and they
    inflated to fill it -- which also meant the same bar was a different
    size at 10:00 than at 14:00 and no two rebuilds were comparable.

    Positioning on the full grid fixes the geometry for the whole day and
    makes the axis time rather than bar number: a slot that never traded
    leaves a gap where it happened instead of closing up and shifting
    every later bar to the left.
    """
    opens = datetime.combine(session.day, session.start, tzinfo=ET)
    closes = datetime.combine(session.day, session.end, tzinfo=ET)
    slots, at = [], opens
    while at < closes:
        slots.append(at)
        at += timedelta(minutes=BAR_MINUTES)
    return slots


def minute_positions(stamps, macd_index):
    """Place 1-minute readings across the 5-minute candle axis.

    The candles are drawn at integer positions, each covering the half-open
    span [i-0.5, i+0.5). A minute inside that candle sits at its own share
    of the width, so a crossover lands under the candle it happened in
    rather than at the candle's edge.
    """
    slot_of = {stamp: i for i, stamp in enumerate(stamps)}
    xs = []
    for stamp in macd_index:
        slot = stamp.replace(minute=stamp.minute - (stamp.minute % BAR_MINUTES),
                             second=0, microsecond=0)
        i = slot_of.get(slot)
        if i is None:
            xs.append(float("nan"))
            continue
        offset = (stamp - slot).seconds // 60
        xs.append(i - 0.5 + (offset + 0.5) / BAR_MINUTES)
    return xs


def page_overview(pdf: PdfPages, session: Session) -> None:
    """The whole session on one page, every panel on one clock.

    Order is deliberate. Price on top because it is the thing being
    explained. MACD under it because that is where a chart reader expects
    it and where the signal markers point. Then the lean directly above
    the volume it is weighted by -- a rise in volume and the change in
    price action it precedes are read as one movement of the eye, which
    they cannot be on separate pages.
    """
    candles = session.candles
    stamps = list(candles.index)
    # Positions are slots in the whole session, not places in the list of
    # bars that happen to have printed. See session_slots().
    slots = session_slots(session)
    slot_of = {stamp: i for i, stamp in enumerate(slots)}
    x = [slot_of.get(stamp, float("nan")) for stamp in stamps]

    fig = plt.figure(figsize=(11.7, 8.3))
    band(fig, session)
    # Price, then volume beneath it, then MACD in its own pane -- the order
    # every charting platform uses, so the page reads the way the screen
    # does. The mood ribbon stays tucked under the candles: it is a
    # decoration of the price panel rather than a chart of its own.
    grid = fig.add_gridspec(6, 1,
                            height_ratios=[3.0, 0.20, 1.05, 1.30, 1.00, 0.72],
                            hspace=0.23, left=0.062, right=0.965,
                            top=0.879, bottom=0.052)
    price = fig.add_subplot(grid[0])
    strip_ax = fig.add_subplot(grid[1], sharex=price)
    vol_ax = fig.add_subplot(grid[2], sharex=price)
    macd_ax = fig.add_subplot(grid[3], sharex=price)
    lean_ax = fig.add_subplot(grid[4], sharex=price)
    size_ax = fig.add_subplot(grid[5], sharex=price)

    # --- price ------------------------------------------------------------
    for i, (_, row) in zip(x, candles.iterrows()):
        if i != i:      # a bar outside the drawn window
            continue
        rising = row["close"] >= row["open"]
        colour = UP if rising else DOWN
        price.vlines(i, row["low"], row["high"], color=colour, linewidth=1.1)
        bottom = min(row["open"], row["close"])
        height = abs(row["close"] - row["open"]) or 0.005
        price.add_patch(Rectangle((i - 0.3, bottom), 0.6, height,
                                  facecolor=colour, edgecolor=colour, linewidth=0.5))

    vwap_at = session.vwap.reindex(candles.index, method="ffill")
    price.plot(x, vwap_at.values, color=VWAP_HUE, linewidth=1.6,
               linestyle=(0, (5, 2)), label="VWAP", zorder=3)

    draw_trades(price, session.trades, slot_of, candles)
    draw_notes(price, session.notes, slot_of, candles)

    signal_x = []
    if not session.signals.empty:
        fired = session.signals[session.signals["alerted"] == 1]
        held = session.signals[session.signals["alerted"] == 0]
        for frame, marker, label, alpha in (
                (fired, "^", "Alerted", 1.0), (held, "v", "Logged, not sent", 0.45)):
            if frame.empty:
                continue
            xs, ys = [], []
            for _, row in frame.iterrows():
                stamp = row["bar_time"]
                slot = stamp.replace(minute=stamp.minute - (stamp.minute % BAR_MINUTES),
                                     second=0, microsecond=0)
                if slot in slot_of:
                    xs.append(slot_of[slot])
                    ys.append(candles.loc[slot, "low"] * 0.9985)
            if xs:
                price.scatter(xs, ys, marker=marker, s=58, color=ACCENT,
                              alpha=alpha, zorder=4, label=label,
                              edgecolors=SURFACE, linewidths=0.8)
                signal_x.extend(xs)

    price.set_ylabel("Price")
    panel_label(price, f"{BAR_MINUTES}-minute candles and VWAP")
    price.legend(loc="upper right", ncol=3, fontsize=8, frameon=True,
                 facecolor=SURFACE, edgecolor="none", framealpha=0.92)
    price.spines[["top", "right"]].set_visible(False)

    # --- MACD, on the 1-minute bars the alerts read -----------------------
    macd = session.macd
    if not macd.empty:
        mx = minute_positions(slots, macd.index)
        macd_ax.axhline(0, color=AXIS, linewidth=1)
        gaps = macd["macd_gap"].tolist()
        macd_ax.bar(mx, gaps, width=1.0 / BAR_MINUTES,
                    color=[UP if g >= 0 else DOWN for g in gaps],
                    alpha=0.30, linewidth=0)
        macd_ax.plot(mx, macd["macd"].tolist(), color=ACCENT, linewidth=1.5,
                     label="MACD", zorder=3)
        macd_ax.plot(mx, macd["macd_signal"].tolist(), color=SECOND,
                     linewidth=1.3, label="Signal", zorder=3)
        macd_ax.legend(loc="upper right", ncol=2, fontsize=8, frameon=True,
                       facecolor=SURFACE, edgecolor="none", framealpha=0.92)

        # The warm-up cannot always reach back far enough -- on a Monday,
        # 900 minutes lands in the weekend, leaving only a thin pre-market
        # to settle an average that needs 23 bars. Draw nothing there and
        # say why, rather than leaving an unexplained gap or, worse,
        # drawing the warm-up curve as though it meant something.
        if macd.index[0] > stamps[0]:
            edge = minute_positions(slots, [macd.index[0]])[0]
            macd_ax.axvspan(-0.8, edge, color=PLANE, zorder=0)
            macd_ax.text((edge - 0.8) / 2, macd_ax.get_ylim()[0],
                         f"settling until {macd.index[0]:%H:%M}",
                         va="bottom", ha="center", size=7.5, color=MUTED)
    else:
        macd_ax.text(0.5, 0.5, "not enough history to warm the MACD",
                     ha="center", va="center", transform=macd_ax.transAxes,
                     color=MUTED)
    macd_ax.set_ylabel("MACD")
    panel_label(macd_ax,
                f"MACD {MACD_SETTING.fast}/{MACD_SETTING.slow}/{MACD_SETTING.signal} "
                "on 1-minute bars \u2014 the line the alerts read")
    macd_ax.spines[["top", "right"]].set_visible(False)

    # --- where price closed in its range, directly above the volume -------
    scores = []
    for stamp in stamps:
        upto = session.minutes[session.minutes.index <
                               stamp + timedelta(minutes=BAR_MINUTES)]
        reading = read_lean(upto)
        scores.append(reading.score * 100 if reading else float("nan"))
    # The bands behind the line are the same seven words the phone uses.
    # Without them the panel says "68" and the alert says "buyers showing
    # up", and nothing on the page tells you those are one fact.
    floor = 0
    for edge, label, colour, alpha in SENTIMENT_BANDS:
        lean_ax.axhspan(floor, edge, color=colour, alpha=alpha, zorder=0,
                        linewidth=0)
        floor = edge
    lean_ax.axhline(50, color=AXIS, linewidth=1, zorder=1)
    lean_ax.plot(x, scores, color=INK, linewidth=1.6, zorder=3)
    lean_ax.set_ylim(0, 100)
    # Ticks at the band edges that carry meaning, named rather than
    # numbered: the number is arbitrary, the word is what he reads.
    lean_ax.set_yticks([9, 50, 91])
    lean_ax.set_yticklabels(["sellers", "balanced", "buyers"], size=7.5)
    lean_ax.set_ylabel("")
    panel_label(lean_ax, "Who is winning the range \u2014 the same reading your "
                         "alerts name in words")
    lean_ax.spines[["top", "right"]].set_visible(False)

    # --- the same reading as a ribbon, directly under the candles ---------
    # The line above answers "how strong, exactly"; this answers "who had
    # the tape, and for how long" without anyone tracing a line. Same
    # numbers, same bands, no axis -- the whole session's mood as a stripe.
    for i, score in zip(x, scores):
        if score != score or i != i:    # NaN: no reading, or outside the window
            continue
        colour, _ = sentiment_colour(score)
        strip_ax.axvspan(i - 0.5, i + 0.5, color=colour,
                         alpha=strip_opacity(score), linewidth=0)
    strip_ax.set_yticks([])
    strip_ax.set_ylabel("mood", rotation=0, ha="right", va="center",
                        size=7.5, color=MUTED, labelpad=8)
    strip_ax.tick_params(labelbottom=False, length=0)
    strip_ax.spines[:].set_visible(False)

    # --- volume against the usual for that slot ---------------------------
    if session.baseline:
        ratios = [row["volume"] / session.baseline[s.time()]
                  if s.time() in session.baseline and session.baseline[s.time()] else float("nan")
                  for s, (_, row) in zip(stamps, candles.iterrows())]
        # Red and green by the candle the volume belongs to, the way a
        # trading platform draws it: a heavy bar under a red candle and a
        # heavy bar under a green one mean opposite things, and a single
        # hue for both made the reader look up to find out which.
        rising = [row["close"] >= row["open"] for _, row in candles.iterrows()]
        vol_ax.bar(x, ratios, width=0.6,
                   color=[UP if up else DOWN for up in rising],
                   alpha=0.85)
        vol_ax.axhline(1.0, color=INK_2, linewidth=1, linestyle=(0, (2, 2)))
        for i, r in zip(x, ratios):
            if r == r and i == i and r >= 1.5:
                vol_ax.text(i, r, f"{r:.1f}\u00d7", ha="center", va="bottom",
                            size=7.5, color=INK_2)
        vol_ax.set_ylabel("\u00d7 usual")
        peak = max([r for r in ratios if r == r], default=1.0)
        vol_ax.set_ylim(0, peak * 1.55)   # room for the labels, clear of the panel title
    else:
        vol_ax.text(0.5, 0.5, "no history for a baseline", ha="center",
                    va="center", transform=vol_ax.transAxes, color=MUTED)
    panel_label(vol_ax, f"Volume against the same slot on recent sessions "
                        f"({BASELINE_SESSIONS}-session median = 1.0)")
    vol_ax.spines[["top", "right"]].set_visible(False)

    # --- average trade size -----------------------------------------------
    if "trade_count" in candles.columns:
        avg = (candles["volume"] / candles["trade_count"].replace(0, pd.NA)).tolist()
        size_ax.plot(x, avg, color=SECOND, linewidth=1.5)
        size_ax.set_ylabel("sh/trade")
        size_ax.yaxis.set_major_locator(plt.MaxNLocator(3))
    else:
        size_ax.text(0.5, 0.5, "the feed returned no trade counts", ha="center",
                     va="center", transform=size_ax.transAxes, color=MUTED)
    panel_label(size_ax, "Average trade size \u2014 blocks, or many small orders")
    size_ax.spines[["top", "right"]].set_visible(False)

    # A signal is a vertical line through every panel, so the crossover, the
    # volume behind it and what price did next are read as one moment.
    for ax in (price, macd_ax, lean_ax, vol_ax, size_ax):
        for sx in signal_x:
            ax.axvline(sx, color=ACCENT, linewidth=0.7, alpha=0.18, zorder=0)

    idx, labels = tick_positions(slots, label_every(slots))
    for ax in (price, strip_ax, vol_ax, macd_ax, lean_ax):
        ax.tick_params(labelbottom=False)
    size_ax.set_xlim(-0.8, len(slots) - 0.2)
    size_ax.set_xticks(idx)
    size_ax.set_xticklabels(labels, size=8)
    size_ax.set_xlabel("Eastern time")

    pdf.savefig(fig)
    plt.close(fig)


def page_signals(pdf: PdfPages, session: Session) -> None:
    """Every signal of the day, and what price did afterwards."""
    fig = plt.figure(figsize=(11.7, 8.3))
    fig.text(0.045, 0.955, "Signals", size=18, weight="bold", color=INK)

    if session.signals.empty:
        fig.text(0.045, 0.918,
                 f"{session.symbol} · {session.day:%d %B %Y}", size=10, color=INK_2)
        fig.text(0.5, 0.5,
                 "No signals logged for this day.\n\n"
                 "The alert tool writes them as it runs; a replayed day\n"
                 "has candles but no record of what fired.",
                 ha="center", va="center", size=11, color=MUTED, linespacing=1.8)
        pdf.savefig(fig)
        plt.close(fig)
        return

    frame = session.signals
    alerted = int(frame["alerted"].sum())
    fig.text(0.045, 0.918,
             f"{session.symbol} · {session.day:%d %B %Y} · {len(frame)} signals, "
             f"{alerted} alerted, {len(frame) - alerted} held back",
             size=10, color=INK_2)

    columns = ["Time", "", "Price", "VWAP", "+15m", "+30m", "+60m", "Note"]
    widths = [0.085, 0.05, 0.085, 0.085, 0.085, 0.085, 0.085, 0.36]
    left = 0.045
    top = 0.855
    row_height = 0.036

    xs, running = [], left
    for w in widths:
        xs.append(running)
        running += w
    for header, xpos in zip(columns, xs):
        fig.text(xpos, top, header.upper(), size=7.5, color=MUTED)
    fig.add_artist(plt.Line2D([left, 0.965], [top - 0.012, top - 0.012],
                              color=AXIS, linewidth=0.8, transform=fig.transFigure))

    def pct(value):
        return f"{value:+.2f}%" if value is not None and value == value else "\u00b7"

    def note_of(value) -> str:
        """SQLite NULL arrives as NaN once pandas has a mixed column, and
        NaN is truthy -- so `value or ""` hands back the NaN."""
        return "" if value is None or pd.isna(value) else str(value)

    for n, (_, row) in enumerate(frame.iterrows()):
        y = top - 0.032 - n * row_height
        if y < 0.05:
            fig.text(left, y, f"... and {len(frame) - n} more", size=9, color=MUTED)
            break
        tone = INK if row["alerted"] else MUTED
        values = [
            f"{row['bar_time']:%H:%M}",
            "alert" if row["alerted"] else "held",
            f"${row['price']:,.2f}",
            f"${row['vwap']:,.2f}",
            pct(row.get("ret_15")), pct(row.get("ret_30")), pct(row.get("ret_60")),
            note_of(row["suppressed"])[:52],
        ]
        for value, xpos in zip(values, xs):
            colour = tone
            if value.startswith("+") and value.endswith("%"):
                colour = UP if row["alerted"] else MUTED
            elif value.startswith("-") and value.endswith("%"):
                colour = DOWN if row["alerted"] else MUTED
            fig.text(xpos, y, value, size=9, color=colour)

    pdf.savefig(fig)
    plt.close(fig)


# ---------------------------------------------------------------------------

def build(session: Session, path: str) -> str:
    with PdfPages(path) as pdf:
        page_overview(pdf, session)
        page_signals(pdf, session)
        info = pdf.infodict()
        info["Title"] = f"{session.symbol} {session.day:%Y-%m-%d}"
        info["Subject"] = "Session report — read-only market data, no trades"
    return path


def session_png(session: Session, path: str, dpi: int = 100) -> str:
    """Page one as an image, small enough to travel with a notification."""
    class _Sink:
        def savefig(self, fig):
            fig.savefig(path, dpi=dpi, bbox_inches="tight")

    page_overview(_Sink(), session)
    return path


def reveal(path: str) -> None:
    """Open it, because a report nobody opens is a file."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606 -- the path is ours
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception as exc:  # noqa: BLE001
        print(f"  (could not open it automatically: {type(exc).__name__})")


def self_test() -> int:
    """Check the notes layer offline. No network, no credentials."""
    import tempfile

    print("Self-test: checking the notes and trades layers...\n")
    failures = []

    # --- the stacker ------------------------------------------------------
    # The property that matters: two labels whose x ranges overlap must
    # not have overlapping y ranges. Eyeballing a rendered page finds
    # this once; asserting it finds it every time.
    def overlapping(heights, xs, width=13.0):
        bottoms = stack_notes(heights, xs, width)
        bad = []
        for i in range(len(xs)):
            for j in range(i + 1, len(xs)):
                if abs(xs[i] - xs[j]) >= width:
                    continue
                a0, a1 = bottoms[i], bottoms[i] + heights[i]
                b0, b1 = bottoms[j], bottoms[j] + heights[j]
                if a0 < b1 - 1e-9 and b0 < a1 - 1e-9:
                    bad.append((i, j))
        return bad, bottoms

    # Three notes inside one another's width, of different heights --
    # the 12:45/13:00 cluster that overlapped on the first four drafts.
    bad, cluster = overlapping([0.20, 0.16, 0.20], [30.0, 33.0, 33.0])
    if bad:
        failures.append(f"labels within a box-width overlapped: {bad}")
    if cluster[0] != 0.0:
        failures.append("the first label should sit on the floor")
    if not cluster[1] >= 0.20:
        failures.append("a neighbour should clear the one below it")
    if not cluster[2] >= cluster[1] + 0.16:
        failures.append("the third should clear the second, not the first")

    # Far apart, so both belong on the floor rather than in a tower.
    bad, bottoms = overlapping([0.20, 0.20], [10.0, 60.0])
    if bad or bottoms != [0.0, 0.0]:
        failures.append(f"distant labels should not stack: {bottoms}")

    # Exactly a box-width apart counts as clear, and nothing stacks on
    # an empty list.
    if stack_notes([0.2, 0.2], [10.0, 23.0], 13.0) != [0.0, 0.0]:
        failures.append("a full box-width apart is far enough")
    if stack_notes([], [], 13.0) != []:
        failures.append("no notes should place no labels")

    # --- reading the file -------------------------------------------------
    day = date(2026, 9, 25)
    with tempfile.TemporaryDirectory() as folder:
        good = os.path.join(folder, "notes.json")
        with open(good, "w", encoding="utf-8") as handle:
            json.dump({"SPCX": [
                {"date": "2026-09-25", "time": "14:15", "who": "claude",
                 "note": "second by time, first in the file"},
                {"date": "2026-09-25", "time": "09:45", "who": "jason",
                 "note": "earlier"},
                {"date": "2026-09-24", "time": "10:00", "who": "jason",
                 "note": "a different day"},
                {"date": "2026-09-25", "time": "oops", "who": "jason",
                 "note": "unparseable time"},
                {"date": "2026-09-25", "who": "jason", "note": "no time"},
            ]}, handle)
        notes = load_notes(good, "SPCX", day)
        if [f"{n.at:%H:%M}" for n in notes] != ["09:45", "14:15"]:
            failures.append(f"notes should be this day's, in time order: "
                            f"{[str(n.at) for n in notes]}")
        if notes and notes[0].mine:
            failures.append("'jason' should read as You")
        if notes and not notes[1].mine:
            failures.append("anyone else should read as Me")
        if notes and not notes[0].label.startswith("09:45  You"):
            failures.append(f"the label leads with time and who: "
                            f"{notes[0].label!r}")
        if load_notes(good, "NOPE", day):
            failures.append("another symbol's notes are not this one's")

        # Every failure is silence: the chart is the deliverable.
        broken = os.path.join(folder, "broken.json")
        with open(broken, "w", encoding="utf-8") as handle:
            handle.write("{not json at all")
        if load_notes(broken, "SPCX", day) != []:
            failures.append("malformed JSON should cost the notes, not raise")
        if load_notes(os.path.join(folder, "absent.json"), "SPCX", day) != []:
            failures.append("a missing file should cost the notes, not raise")

    # --- pairing fills into round trips -----------------------------------
    def at(hh, mm):
        return datetime.combine(day, time(hh, mm), tzinfo=ET)

    # One in, one out.
    trips = pair_fills([Fill(at(10, 0), "buy", 100, 150.0),
                        Fill(at(10, 30), "sell", 100, 151.0)])
    if len(trips) != 1 or abs((trips[0].profit or 0) - 100.0) > 1e-9:
        failures.append(f"a simple round trip should make $100: "
                        f"{[t.profit for t in trips]}")
    if trips and not trips[0].won:
        failures.append("a profitable trip should read as won")

    # Two lots in, one sale out: the OLDEST lot closes first, so the
    # profit is not the same as pairing against the cheaper one.
    trips = pair_fills([Fill(at(10, 0), "buy", 100, 150.0),
                        Fill(at(10, 5), "buy", 100, 148.0),
                        Fill(at(11, 0), "sell", 150, 151.0)])
    if len(trips) != 3:
        failures.append(f"100 + 100 in, 150 out is 3 trips (100, 50, 50 open), "
                        f"got {len(trips)}")
    else:
        first, second, still_open = trips
        if first.entry != 150.0 or first.quantity != 100:
            failures.append("the oldest lot should close first (FIFO)")
        if second.entry != 148.0 or second.quantity != 50:
            failures.append("the remainder should come off the next lot")
        if still_open.exit is not None or still_open.quantity != 50:
            failures.append("50 shares should be left open, not dropped")
        if still_open.profit is not None:
            failures.append("an open position has no profit yet")
        if "open" not in still_open.label():
            failures.append(f"an open position should say so: "
                            f"{still_open.label()}")

    # A sell with nothing open is a short. This is a long-only book, so
    # it belongs to someone else and must not invent a trade.
    if pair_fills([Fill(at(10, 0), "sell", 100, 150.0)]):
        failures.append("a sell with no position open should make no trade")
    if pair_fills([]):
        failures.append("no fills should make no trades")

    # --- reading a broker file --------------------------------------------
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "trades.csv")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "Exec Time,B/S,Filled Qty,Fill Price,Ticker\n"
                '2026-09-25 10:12:00,B,"1,000",$152.40,SPCX\n'
                "2026-09-25 10:58:00,S,1000,151.62,SPCX\n"
                "2026-09-25 11:00:00,B,50,10.00,NVDA\n"
                "2026-09-24 10:00:00,B,50,99.00,SPCX\n"
                "not a time,B,50,99.00,SPCX\n")
        got = load_trades(path, "SPCX", day)
        if len(got) != 1:
            failures.append(f"one SPCX round trip on this day, got {len(got)}")
        elif got[0].quantity != 1000:
            failures.append("a thousands separator should not become 1 share")
        elif abs((got[0].profit or 0) + 780.0) > 1e-6:
            failures.append(f"1000 sh from 152.40 to 151.62 loses $780, "
                            f"got {got[0].profit}")
        if load_trades(os.path.join(folder, "none.csv"), "SPCX", day) != []:
            failures.append("a missing trades file should cost the trades only")

        # A file whose headers mean nothing is reported, not silently read
        # as a day with no trading.
        odd = os.path.join(folder, "odd.csv")
        with open(odd, "w", encoding="utf-8") as handle:
            handle.write("alpha,beta\n1,2\n")
        print("  Headers it cannot read, on purpose:")
        if load_trades(odd, "SPCX", day) != []:
            failures.append("unrecognised headers should yield no trades")
        print("    ^ that complaint is the expected outcome, not a failure\n")

    # --- reading a spreadsheet --------------------------------------------
    # The failure this guards against did not raise: empty cells are
    # written self-closing, and a pattern that only knows the
    # <c ...>...</c> form runs past them to the next closing tag and
    # swallows the columns in between. The reader returned rows, with
    # the price where the quantity should be.
    with tempfile.TemporaryDirectory() as folder:
        book = os.path.join(folder, "trades.xlsx")
        strings = "".join(f"<si><t>{s}</t></si>" for s in
                          ("SPCX", "2026-09-25, 10:12:00", "NASDAQ", "BUY", "-"))
        def cell(ref, value, shared=False):
            if value is None:
                return f'<c r="{ref}" s="1"/>'      # the self-closing kind
            t = ' t="s"' if shared else ""
            return f'<c r="{ref}" s="1"{t}><v>{value}</v></c>'
        rows = "".join([
            "<row r='1'>" + cell("B1", 0, True) + cell("C1", None)
            + cell("D1", None) + cell("E1", 1, True) + cell("F1", None)
            + cell("I1", 2, True) + cell("J1", 3, True) + cell("K1", None)
            + cell("L1", 400) + cell("M1", None) + cell("N1", 152.4) + "</row>",
            # The order summary for the same fill: exchange "-", and it
            # must not be counted a second time.
            "<row r='2'>" + cell("B2", 0, True) + cell("E2", 1, True)
            + cell("I2", 4, True) + cell("J2", 3, True) + cell("L2", 400)
            + cell("N2", 152.4) + "</row>",
        ])
        with zipfile.ZipFile(book, "w") as out:
            out.writestr("xl/sharedStrings.xml",
                         f'<sst xmlns="x">{strings}</sst>')
            out.writestr("xl/worksheets/sheet1.xml",
                         f'<worksheet xmlns="x"><sheetData>{rows}'
                         f'</sheetData></worksheet>')

        got = read_xlsx(book)
        if not got or got[0].get("L") != "400":
            failures.append(f"an empty cell should not swallow the columns "
                            f"after it: {got[0] if got else got}")
        if got and got[0].get("N") != "152.4":
            failures.append(f"the price column should survive the gaps: "
                            f"{got[0].get('N')}")
        if got and got[0].get("E") != "2026-09-25, 10:12:00":
            failures.append("a shared string should be resolved, not its index")

        fills = read_ibkr(book, "SPCX", day)
        if len(fills) != 1:
            failures.append(f"the order summary row should be dropped, "
                            f"leaving one fill, got {len(fills)}")
        elif fills[0].quantity != 400 or fills[0].price != 152.4:
            failures.append(f"fill read wrong: {fills[0]}")
        if read_ibkr(book, "NVDA", day):
            failures.append("another symbol's rows are not this one's")

    # The wrap keeps a label narrow enough to sit beside its candle.
    long_note = Note(at=time(10, 0), who="jason", text="word " * 40)
    if max(len(line) for line in long_note.label.splitlines()) > NOTE_WRAP + 2:
        failures.append("a long note should wrap to NOTE_WRAP")

    print(f"  Stacker, 3 in one cluster      : "
          f"{[round(b, 3) for b in cluster]} -> no overlap")
    print("  Distant labels                 : both on the floor, no tower")
    print("  Bad date / time / missing key  : skipped, rest still read")
    print("  Malformed or absent file       : no notes, no exception")
    print("  Author                         : jason -> You, else Me")
    print("  Fills -> round trips           : FIFO, oldest lot closes first")
    print("  Unmatched buy                  : an open position, not an error")
    print("  Unmatched sell                 : a short, left alone")
    print("  Broker headers                 : sniffed; unknown ones reported")
    print("  Spreadsheet, empty cells       : do not swallow the next columns")
    print("  IBKR order summaries           : dropped, so nothing counts twice")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nAll checks passed.")
    return 0


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(description="One session as a PDF.")
    parser.add_argument("--symbol", default=SYMBOL)
    parser.add_argument("--benchmark", default=BENCHMARK,
                        help=f"Compare the day against this symbol (default "
                             f"{BENCHMARK}). Empty string turns it off.")
    parser.add_argument("--date", help="YYYY-MM-DD (default: the last trading day)")
    parser.add_argument("--from", dest="start", default=f"{WINDOW_START:%H:%M}")
    parser.add_argument("--until", dest="end", default=f"{WINDOW_END:%H:%M}")
    parser.add_argument("--db", default="spcx_alerts.db")
    parser.add_argument("--out", help="Where to write it")
    parser.add_argument("--self-test", action="store_true",
                        help="Check the notes layer offline")
    parser.add_argument("--no-open", action="store_true", help="Write it, do not open it")
    args = parser.parse_args()

    symbol = args.symbol.upper()
    day = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
           else trading_days(date.today() - timedelta(days=1), 1)[0])
    if args.self_test:
        return self_test()

    start, end = parse_clock(args.start), parse_clock(args.end)

    print(f"Building {symbol} report for {day}...")
    session = gather(symbol, day, start, end, args.db,
                     benchmark=args.benchmark)
    if session is None:
        print(f"  no bars for {symbol} on {day}. Market closed that day?")
        return 1

    path = args.out or f"{symbol}_{day:%Y%m%d}.pdf"
    build(session, path)
    signals = "no signals logged" if session.signals.empty else \
        f"{len(session.signals)} signals"
    print(f"  {len(session.candles)} candles, {signals}")
    print(f"  written to {os.path.abspath(path)}")
    if not args.no_open:
        reveal(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
