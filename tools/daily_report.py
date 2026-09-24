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
import os
import sqlite3
import statistics
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

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
WINDOW_START = time(9, 25)
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
        macd=macd,
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
    x = list(range(len(stamps)))

    fig = plt.figure(figsize=(11.7, 8.3))
    band(fig, session)
    grid = fig.add_gridspec(6, 1,
                            height_ratios=[3.0, 0.20, 1.30, 1.00, 1.10, 0.72],
                            hspace=0.23, left=0.062, right=0.965,
                            top=0.879, bottom=0.052)
    price = fig.add_subplot(grid[0])
    strip_ax = fig.add_subplot(grid[1], sharex=price)
    macd_ax = fig.add_subplot(grid[2], sharex=price)
    lean_ax = fig.add_subplot(grid[3], sharex=price)
    vol_ax = fig.add_subplot(grid[4], sharex=price)
    size_ax = fig.add_subplot(grid[5], sharex=price)

    # --- price ------------------------------------------------------------
    for i, (_, row) in enumerate(candles.iterrows()):
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
                if slot in candles.index:
                    xs.append(list(candles.index).index(slot))
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
        mx = minute_positions(stamps, macd.index)
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
            edge = minute_positions(stamps, [macd.index[0]])[0]
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
    for i, score in enumerate(scores):
        if score != score:      # NaN: a stretch with no reading
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
        vol_ax.bar(x, ratios, width=0.6,
                   color=[ACCENT if (r == r and r >= 1) else MUTED for r in ratios])
        vol_ax.axhline(1.0, color=INK_2, linewidth=1, linestyle=(0, (2, 2)))
        for i, r in enumerate(ratios):
            if r == r and r >= 1.5:
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

    idx, labels = tick_positions(stamps, 15)
    for ax in (price, strip_ax, macd_ax, lean_ax, vol_ax):
        ax.tick_params(labelbottom=False)
    size_ax.set_xlim(-0.8, len(stamps) - 0.2)
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
    parser.add_argument("--no-open", action="store_true", help="Write it, do not open it")
    args = parser.parse_args()

    symbol = args.symbol.upper()
    day = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
           else trading_days(date.today() - timedelta(days=1), 1)[0])
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
