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
from typing import Dict, List, Optional

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import Rectangle

from feed_check import ET, parse_clock, trading_days
from open_candles import BAR_MINUTES, aggregate, fetch_minutes, read_lean, thousands
from spcx_alert import open_db

SYMBOL = "SPCX"
WINDOW_START = time(9, 25)
WINDOW_END = time(16, 0)
BASELINE_SESSIONS = 10

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


def session_vwap(minutes: pd.DataFrame) -> pd.Series:
    """Anchored at the open, as every platform draws it."""
    typical = (minutes["high"] + minutes["low"] + minutes["close"]) / 3.0
    return (typical * minutes["volume"]).cumsum() / minutes["volume"].cumsum().replace(0, pd.NA)


def slot_baseline(symbol: str, day: date, start: time = time(9, 30),
                  end: time = time(16, 0)) -> Dict[time, float]:
    """Median 5-minute volume per clock slot over recent sessions."""
    gathered: Dict[time, List[float]] = {}
    for past in trading_days(day - timedelta(days=1), BASELINE_SESSIONS):
        try:
            bars = fetch_minutes(
                symbol,
                datetime.combine(past, start, tzinfo=ET),
                datetime.combine(past, end, tzinfo=ET),
                force_sip=True)
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


def gather(symbol: str, day: date, start: time, end: time, db_path: str,
           force_sip: bool = True) -> Optional[Session]:
    """Build a session. `force_sip` is right for a past day -- the free plan
    serves the full tape historically -- and wrong for today, where SIP is
    15 minutes behind and the live feed is what the alerts are reading."""
    minutes = fetch_minutes(symbol,
                            datetime.combine(day, start, tzinfo=ET),
                            datetime.combine(day, end, tzinfo=ET),
                            force_sip=force_sip)
    if minutes.empty:
        return None
    return Session(
        symbol=symbol, day=day, minutes=minutes,
        candles=aggregate(minutes), vwap=session_vwap(minutes),
        baseline=slot_baseline(symbol, day, start, end),
        signals=logged_signals(db_path, symbol, day),
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

    fig.text(0.045, 0.952, f"{session.symbol}", size=21, weight="bold", color=INK)
    fig.text(0.045, 0.928, f"{session.day:%A %d %B %Y}", size=10.5, color=INK_2)

    stats = [
        ("Open", f"${first['open']:,.2f}", INK),
        ("Close", f"${last['close']:,.2f}", INK),
        ("Change", f"{move:+.2f}  ({pct:+.2f}%)", colour),
        ("High", f"${candles['high'].max():,.2f}", INK_2),
        ("Low", f"${candles['low'].min():,.2f}", INK_2),
        ("Volume", thousands(candles["volume"].sum()), INK_2),
    ]
    for i, (label, value, tone) in enumerate(stats):
        x = 0.045 + i * 0.152
        fig.text(x, 0.884, label.upper(), size=7.5, color=MUTED)
        fig.text(x, 0.856, value, size=13, color=tone, weight="normal")

    fig.add_artist(plt.Line2D([0.045, 0.965], [0.838, 0.838],
                              color=AXIS, linewidth=0.8, transform=fig.transFigure))


def tick_positions(stamps, every: int):
    idx = [i for i, s in enumerate(stamps) if s.minute % every == 0]
    return idx, [f"{stamps[i]:%H:%M}" for i in idx]


def page_session(pdf: PdfPages, session: Session) -> None:
    """Candles, VWAP and volume against one shared time axis."""
    candles = session.candles
    stamps = list(candles.index)
    x = range(len(stamps))

    fig = plt.figure(figsize=(11.7, 8.3))
    band(fig, session)
    grid = fig.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.08,
                            left=0.06, right=0.965, top=0.785, bottom=0.09)
    price = fig.add_subplot(grid[0])
    vol = fig.add_subplot(grid[1], sharex=price)

    for i, (_, row) in enumerate(candles.iterrows()):
        rising = row["close"] >= row["open"]
        colour = UP if rising else DOWN
        price.vlines(i, row["low"], row["high"], color=colour, linewidth=1.1)
        bottom = min(row["open"], row["close"])
        height = abs(row["close"] - row["open"]) or 0.005
        price.add_patch(Rectangle((i - 0.3, bottom), 0.6, height,
                                  facecolor=colour, edgecolor=colour, linewidth=0.5))

    vwap_at = session.vwap.reindex(candles.index, method="ffill")
    price.plot(list(x), vwap_at.values, color=VWAP_HUE, linewidth=1.6,
               linestyle=(0, (5, 2)), label="VWAP", zorder=3)

    # Signals, if any have been logged for this day.
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

    price.set_ylabel("Price")
    price.set_title(f"{BAR_MINUTES}-minute candles", loc="left", size=11,
                    weight="normal", pad=10)
    price.legend(frameon=False, loc="upper left", fontsize=8.5)
    price.tick_params(labelbottom=False)
    price.spines[["top", "right"]].set_visible(False)

    colours = [UP if r["close"] >= r["open"] else DOWN for _, r in candles.iterrows()]
    vol.bar(list(x), candles["volume"], color=colours, width=0.6, alpha=0.85)
    if session.baseline:
        usual = [session.baseline.get(s.time(), float("nan")) for s in stamps]
        vol.plot(list(x), usual, color=INK_2, linewidth=1.2, linestyle=(0, (2, 2)),
                 label=f"usual for the slot ({BASELINE_SESSIONS}-session median)")
        vol.legend(frameon=False, loc="upper right", fontsize=8)
    vol.set_ylabel("Volume")
    vol.spines[["top", "right"]].set_visible(False)
    vol.yaxis.set_major_formatter(lambda v, _: thousands(v) if v else "0")

    idx, labels = tick_positions(stamps, 15)
    price.set_xlim(-0.8, len(stamps) - 0.2)
    vol.set_xlim(-0.8, len(stamps) - 0.2)
    vol.set_xticks(idx)
    vol.set_xticklabels(labels)
    vol.set_xlabel("Eastern time")

    pdf.savefig(fig)
    plt.close(fig)


def page_participation(pdf: PdfPages, session: Session) -> None:
    """Three separate scales, three separate panels. Never a second y-axis."""
    candles = session.candles
    stamps = list(candles.index)
    x = list(range(len(stamps)))

    fig = plt.figure(figsize=(11.7, 8.3))
    fig.text(0.045, 0.955, "Participation", size=18, weight="bold", color=INK)
    fig.text(0.045, 0.925,
             f"{session.symbol} · {session.day:%d %B %Y} · how busy, how large, which way",
             size=10, color=INK_2)

    grid = fig.add_gridspec(3, 1, hspace=0.42, left=0.06, right=0.965,
                            top=0.87, bottom=0.075)

    # --- volume against the usual for that slot ---------------------------
    ratio_ax = fig.add_subplot(grid[0])
    if session.baseline:
        ratios = [row["volume"] / session.baseline[s.time()]
                  if s.time() in session.baseline and session.baseline[s.time()] else float("nan")
                  for s, (_, row) in zip(stamps, candles.iterrows())]
        ratio_ax.bar(x, ratios, width=0.6,
                     color=[ACCENT if (r == r and r >= 1) else MUTED for r in ratios])
        ratio_ax.axhline(1.0, color=INK_2, linewidth=1, linestyle=(0, (2, 2)))
        ratio_ax.text(len(x) - 0.4, 1.0, "  usual", va="center", size=8, color=INK_2)
        ratio_ax.set_ylabel("× usual")
        for i, r in enumerate(ratios):
            if r == r and r >= 1.5:
                ratio_ax.text(i, r, f"{r:.1f}×", ha="center", va="bottom",
                              size=7.5, color=INK_2)
    else:
        ratio_ax.text(0.5, 0.5, "no history for a baseline", ha="center",
                      va="center", transform=ratio_ax.transAxes, color=MUTED)
    ratio_ax.set_title("Volume against the same slot on recent sessions",
                       loc="left", size=10.5, weight="normal", pad=8)
    ratio_ax.spines[["top", "right"]].set_visible(False)

    # --- average trade size -----------------------------------------------
    size_ax = fig.add_subplot(grid[1])
    if "trade_count" in candles.columns:
        avg = (candles["volume"] / candles["trade_count"].replace(0, pd.NA)).tolist()
        size_ax.plot(x, avg, color=SECOND, linewidth=1.8, marker="o", markersize=3.5)
        size_ax.set_ylabel("shares / trade")
        size_ax.set_title("Average trade size — blocks, or many small orders",
                          loc="left", size=10.5, weight="normal", pad=8)
    else:
        size_ax.text(0.5, 0.5, "the feed returned no trade counts", ha="center",
                     va="center", transform=size_ax.transAxes, color=MUTED)
    size_ax.spines[["top", "right"]].set_visible(False)

    # --- the lean ----------------------------------------------------------
    lean_ax = fig.add_subplot(grid[2])
    scores = []
    for stamp in stamps:
        upto = session.minutes[session.minutes.index <
                               stamp + timedelta(minutes=BAR_MINUTES)]
        reading = read_lean(upto)
        scores.append(reading.score * 100 if reading else float("nan"))
    lean_ax.axhspan(50, 100, color=UP, alpha=0.05)
    lean_ax.axhspan(0, 50, color=DOWN, alpha=0.05)
    lean_ax.axhline(50, color=AXIS, linewidth=1)
    lean_ax.plot(x, scores, color=INK, linewidth=1.8)
    lean_ax.set_ylim(0, 100)
    lean_ax.set_ylabel("0 = lows · 100 = highs")
    lean_ax.set_title("Where volume traded inside each range — a hint, not order flow",
                      loc="left", size=10.5, weight="normal", pad=8)
    lean_ax.spines[["top", "right"]].set_visible(False)

    # One x range across all three, so a moment sits at the same place in
    # each. Panels that scale independently invite reading a coincidence.
    idx, labels = tick_positions(stamps, 15)
    for ax in (ratio_ax, size_ax, lean_ax):
        ax.set_xlim(-0.8, len(stamps) - 0.2)
        ax.set_xticks(idx)
        ax.set_xticklabels(labels, size=8)
    lean_ax.set_xlabel("Eastern time")

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
        page_session(pdf, session)
        page_participation(pdf, session)
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

    page_session(_Sink(), session)
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
    parser = argparse.ArgumentParser(description="One session as a PDF.")
    parser.add_argument("--symbol", default=SYMBOL)
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
    session = gather(symbol, day, start, end, args.db)
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
