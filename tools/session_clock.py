"""
The trading day as a clock face: where the movement is, where the signals are.

A single session is a line from the open to the close, and the daily report
draws it that way. But the *habits* of a session are cyclical -- 09:40 comes
round every morning -- and a clock is the honest shape for something that
comes back around.

So: 09:30 sits where 09:30 sits on a clock, the dial sweeps clockwise past
twelve to four, and the rest of the face is the market being shut. Six and a
half hours is 195 degrees of the 360.

Two rings, because one number would hide the finding:

    inner   how far price typically travels in that slot
    outer   how often a MACD crossover fires in it

The gap between them is the point. On 90 days of SPCX the movement clusters
in the first ninety minutes while the signals scatter across the afternoon,
which is why the alerts are gated to 09:40-11:00 -- and a table of half-hour
rows never made that as plain as two rings do.

Colour carries the magnitude and the radius does not. On a dial a value
twice as large would occupy four times the area if it were drawn by radius,
which flatters the big numbers and is the usual reason circular charts
mislead. Fixed-width rings have no such problem.

READ-ONLY. Market-data client only. No trading client, no order object.

    python session_clock.py                  # 90 sessions of SPCX
    python session_clock.py --days 30 --symbol TSLA
    python session_clock.py --self-test

Setup
-----
    pip install alpaca-py pandas matplotlib python-dotenv
    ALPACA_API_KEY / ALPACA_SECRET_KEY in .env, as the other tools use.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional


from feed_check import ET, Macd, load_env, trading_days

SYMBOL = "SPCX"
DEFAULT_DAYS = 90

#: The session, and its place on a twelve-hour face. 09:30 is at 285
#: degrees clockwise from twelve, 16:00 at 120 -- the arc between them is
#: 195 degrees, which is the six and a half hours the market is open.
SESSION_OPEN, SESSION_CLOSE = time(9, 30), time(16, 0)
DEGREES_PER_HOUR = 30.0

#: Minutes to a wedge. Fifteen gives twenty-six wedges across the arc:
#: fine enough to see the shape of a morning, coarse enough that each one
#: is a median over real bars rather than noise.
SLOT_MINUTES = 15

MACD_SETTING = Macd(9, 17, 6)


def clock_angle(at: time) -> float:
    """Degrees clockwise from twelve, as a clock's hour hand would point."""
    hours = (at.hour % 12) + at.minute / 60.0
    return hours * DEGREES_PER_HOUR


def session_slots(slot_minutes: int = SLOT_MINUTES) -> List[time]:
    """Every wedge's start time, open to close."""
    out, at = [], datetime.combine(date(2000, 1, 1), SESSION_OPEN)
    closes = datetime.combine(date(2000, 1, 1), SESSION_CLOSE)
    while at < closes:
        out.append(at.time())
        at += timedelta(minutes=slot_minutes)
    return out


def slot_of(at: time, slot_minutes: int = SLOT_MINUTES) -> Optional[time]:
    """Which wedge a timestamp belongs to, or None outside the session."""
    if at < SESSION_OPEN or at >= SESSION_CLOSE:
        return None
    minutes = at.hour * 60 + at.minute
    start = SESSION_OPEN.hour * 60 + SESSION_OPEN.minute
    edge = start + ((minutes - start) // slot_minutes) * slot_minutes
    return time(edge // 60, edge % 60)


@dataclass
class Dial:
    symbol: str
    days: int
    slot_minutes: int
    #: set when the dial is one session rather than a habit across many
    single_day: Optional[date] = None
    #: slot -> median distance price travelled in it, across the days
    travel: Dict[time, float] = field(default_factory=dict)
    #: slot -> how many MACD crossovers fired in it, across the days
    signals: Dict[time, int] = field(default_factory=dict)

    def busiest(self) -> Optional[time]:
        return max(self.travel, key=self.travel.get) if self.travel else None

    def loudest(self) -> Optional[time]:
        return max(self.signals, key=self.signals.get) if self.signals else None

    def share_before(self, cutoff: time, of: Dict[time, float]) -> Optional[float]:
        """What fraction of the total sits before a time of day."""
        total = sum(of.values())
        if not total:
            return None
        return sum(v for s, v in of.items() if s < cutoff) / total


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------

def measure_today(symbol: str, day: Optional[date] = None,
                  slot_minutes: int = SLOT_MINUTES) -> Dial:
    """One session, however much of it has happened.

    The dial is always the whole 09:30-16:00 arc: the wedges ahead of the
    clock stay empty and fill in as each quarter hour completes, so the
    page has its final shape from the first rebuild of the morning and
    the empty part is how much day is left. Same reasoning as the linear
    report's axis, and the same reason not to redraw the arc to fit.
    """
    return measure(symbol, 1, slot_minutes, day=day)


def measure(symbol: str, days: int, slot_minutes: int = SLOT_MINUTES,
            day: Optional[date] = None) -> Dial:
    """Walk the sessions once, filling both rings."""
    from feed_check import add_macd
    from open_candles import fetch_minutes

    dial = Dial(symbol=symbol, days=days, slot_minutes=slot_minutes,
                single_day=day)
    per_slot: Dict[time, List[float]] = {s: [] for s in session_slots(slot_minutes)}
    fired: Dict[time, int] = {s: 0 for s in per_slot}

    window = [day] if day else trading_days(date.today(), days)
    seen = 0
    for day in window:
        try:
            bars = fetch_minutes(symbol,
                                 datetime.combine(day, time(9, 0), tzinfo=ET),
                                 datetime.combine(day, SESSION_CLOSE, tzinfo=ET),
                                 force_sip=True)
        except Exception as exc:  # noqa: BLE001 -- one bad day is not the study
            print(f"  {day}  {type(exc).__name__}: {exc}")
            continue
        if bars.empty:
            continue
        seen += 1

        # MACD warms on the pre-open bars, then they are dropped -- the
        # same choice feed_check.prepare makes, for the same reason.
        framed = add_macd(bars, MACD_SETTING)
        session = framed[(framed.index.time >= SESSION_OPEN)
                         & (framed.index.time < SESSION_CLOSE)]
        if session.empty:
            continue

        step = session["close"].diff().abs()
        for stamp, distance in step.items():
            if distance != distance:
                continue
            slot = slot_of(stamp.time(), slot_minutes)
            if slot is not None:
                per_slot[slot].append(float(distance))

        gap = session["macd_gap"]
        crossed = (gap.shift(1) <= 0) & (gap > 0) | (gap.shift(1) >= 0) & (gap < 0)
        for stamp in session.index[crossed.fillna(False)]:
            slot = slot_of(stamp.time(), slot_minutes)
            if slot is not None:
                fired[slot] += 1

        print(f"  {day}  {len(session)} bars", end="\r")

    print(f"  {seen} sessions read" + " " * 30)
    # Sum rather than median: a slot's share of the day's travel is the
    # question, and a median per minute would not add up to a day.
    dial.travel = {s: float(sum(v)) for s, v in per_slot.items()}
    dial.signals = fired
    return dial


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def shade(base: str, weight: float, surface: str = "#fcfcfb") -> tuple:
    """A step on a single-hue ramp: light at 0, full strength at 1.

    Sequential means one hue getting darker, never a march through the
    rainbow -- a rainbow ramp invents categories out of a quantity and
    makes the reader decode a legend to recover an order they already
    knew.
    """
    import matplotlib.colors as mcolors

    a = mcolors.to_rgb(surface)
    b = mcolors.to_rgb(base)
    t = 0.12 + 0.88 * max(0.0, min(1.0, weight))
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def render(dial: Dial, path: str) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.backends.backend_pdf import PdfPages

    INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"
    AXIS, SURFACE = "#c3c2b7", "#fcfcfb"
    TRAVEL_HUE, SIGNAL_HUE = "#2a78d6", "#eb6834"

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9,
                         "figure.facecolor": SURFACE, "text.color": INK})

    slots = session_slots(dial.slot_minutes)
    width = np.deg2rad(dial.slot_minutes / 60.0 * DEGREES_PER_HOUR)
    top_travel = max(dial.travel.values()) or 1.0
    top_signal = max(dial.signals.values()) or 1
    total_signals = sum(dial.signals.values())

    fig = plt.figure(figsize=(8.3, 8.6))
    if dial.single_day:
        headline = f"{dial.symbol} — {dial.single_day:%A %d %B %Y}"
        sub = (f"one session, filling as it goes · each wedge is "
               f"{dial.slot_minutes} minutes · 09:30 to 16:00 on a clock face")
    else:
        headline = f"{dial.symbol} — the shape of a trading day"
        sub = (f"{dial.days} sessions · each wedge is {dial.slot_minutes} "
               f"minutes · 09:30 to 16:00 on a clock face")
    fig.text(0.08, 0.955, headline, size=17, weight="bold", color=INK)
    fig.text(0.08, 0.934, sub, size=10, color=INK_2)
    fig.add_artist(plt.Line2D([0.08, 0.92], [0.920, 0.920], color=AXIS,
                              linewidth=0.8, transform=fig.transFigure))

    ax = fig.add_axes([0.085, 0.345, 0.83, 0.575], projection="polar")
    # The polar patch defaults to white, which on this paper reads as a
    # disc the reader has to explain to themselves. It is not a mark.
    ax.set_facecolor(SURFACE)
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    ax.set_rlim(0, 1.0)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.spines["polar"].set_visible(False)
    ax.grid(False)

    # The clock behind the data, so the arc is read as a time of day and
    # the empty part is read as the market being shut.
    for hour in range(1, 13):
        angle = np.deg2rad(hour * DEGREES_PER_HOUR)
        ax.plot([angle, angle], [0.285, 0.335], color=AXIS, linewidth=1,
                zorder=1)
        ax.text(angle, 0.205, str(hour), ha="center", va="center", size=10,
                color=MUTED, zorder=1)

    inner, outer = 0.38, 0.66
    band = 0.24
    for slot in slots:
        angle = np.deg2rad(clock_angle(slot))
        ax.bar(angle + width / 2, band, width=width * 0.94, bottom=inner,
               color=shade(TRAVEL_HUE, dial.travel.get(slot, 0.0) / top_travel,
                           SURFACE),
               edgecolor=SURFACE, linewidth=0.8, zorder=3)
        ax.bar(angle + width / 2, band * 0.86, width=width * 0.94, bottom=outer,
               color=shade(SIGNAL_HUE, dial.signals.get(slot, 0) / top_signal,
                           SURFACE),
               edgecolor=SURFACE, linewidth=0.8, zorder=3)

    for at, label in ((time(9, 30), "09:30 open"), (time(12, 0), "noon"),
                      (time(16, 0), "16:00 close")):
        angle = np.deg2rad(clock_angle(at))
        ax.text(angle, 0.925, label, ha="center", va="center", size=8.5,
                color=INK_2, zorder=5,
                bbox=dict(facecolor=SURFACE, edgecolor="none", pad=1.6))

    busiest, loudest = dial.busiest(), dial.loudest()
    if busiest:
        ax.text(0, 0, f"{busiest:%H:%M}\nis busiest", ha="center", va="center",
                size=11, color=INK, linespacing=1.4, zorder=6)

    # --- the two ramps, named ------------------------------------------
    for i, (hue, title, note) in enumerate((
            (TRAVEL_HUE, "inner ring — where price moves",
             "distance travelled in that quarter hour"
             + ("" if dial.single_day else ", summed over the sessions")),
            (SIGNAL_HUE, "outer ring — where the MACD fires",
             f"{total_signals} crossovers"
             + ("" if dial.single_day else f" across {dial.days} sessions")))):
        y = 0.315 - i * 0.062
        fig.text(0.08, y + 0.016, title, size=9.5, color=INK)
        fig.text(0.08, y - 0.002, note, size=8, color=MUTED)
        for step in range(10):
            fig.add_artist(plt.Rectangle((0.58 + step * 0.031, y),
                                         0.030, 0.016,
                                         facecolor=shade(hue, step / 9.0),
                                         edgecolor="none",
                                         transform=fig.transFigure))
        fig.text(0.58, y - 0.014, "less", size=7.5, color=MUTED)
        fig.text(0.90, y - 0.014, "more", size=7.5, color=MUTED,
                 ha="right")

    lines = []
    morning = dial.share_before(time(11, 0), dial.travel)
    if morning is not None:
        when = "has happened" if dial.single_day else "happens"
        lines.append(f"{morning * 100:.0f}% of the movement so far {when} "
                     f"before 11:00." if dial.single_day else
                     f"{morning * 100:.0f}% of the day's movement happens "
                     f"before 11:00.")
    sig_morning = dial.share_before(time(11, 0),
                                    {k: float(v) for k, v in dial.signals.items()})
    if sig_morning is not None:
        lines.append(f"{sig_morning * 100:.0f}% of the crossovers fire there.")
    if busiest and loudest and busiest != loudest:
        lines.append(f"The busiest quarter hour is {busiest:%H:%M}; the one "
                     f"that fires most is {loudest:%H:%M}.")
    lines.append("Where the rings disagree, the signal is arriving somewhere "
                 "other than where the movement is.")
    for i, line in enumerate(lines):
        fig.text(0.08, 0.195 - i * 0.032, line, size=9.5, color=INK_2)

    fig.text(0.08, 0.020,
             "Colour carries the magnitude; the rings are a fixed width on "
             "purpose —\ndrawn by radius, a value twice as large would cover "
             "four times the area.\nMeasured, not predicted.",
             size=8, color=MUTED, linespacing=1.5)

    with PdfPages(path) as pdf:
        pdf.savefig(fig)
        info = pdf.infodict()
        info["Title"] = f"{dial.symbol} session clock"
        info["Subject"] = "Read-only market data"
    plt.close(fig)
    return path


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def self_test() -> int:
    print("Self-test: checking the dial...\n")
    failures = []

    # The clock is a clock. 09:30 points where a clock's hour hand would.
    cases = ((time(12, 0), 0.0), (time(3, 0), 90.0), (time(9, 30), 285.0),
             (time(16, 0), 120.0), (time(10, 0), 300.0), (time(13, 30), 45.0))
    for at, expected in cases:
        got = clock_angle(at)
        if abs(got - expected) > 1e-9:
            failures.append(f"{at:%H:%M} should sit at {expected}°, got {got}")

    # The session is six and a half hours, so it covers 195° of the face.
    span = (clock_angle(SESSION_CLOSE) - clock_angle(SESSION_OPEN)) % 360
    if abs(span - 195.0) > 1e-9:
        failures.append(f"the session arc should be 195°, got {span}")

    slots = session_slots(15)
    if len(slots) != 26:
        failures.append(f"26 quarter hours between 09:30 and 16:00, got {len(slots)}")
    if slots[0] != time(9, 30) or slots[-1] != time(15, 45):
        failures.append(f"slots should run 09:30..15:45, got {slots[0]}..{slots[-1]}")

    # Every minute of the session lands in exactly one wedge, and nothing
    # outside it lands anywhere.
    if slot_of(time(9, 29)) is not None or slot_of(time(16, 0)) is not None:
        failures.append("outside the session should belong to no wedge")
    for at, expected in ((time(9, 30), time(9, 30)), (time(9, 44), time(9, 30)),
                         (time(9, 45), time(9, 45)), (time(15, 59), time(15, 45))):
        if slot_of(at) != expected:
            failures.append(f"{at:%H:%M} belongs to {expected:%H:%M}, "
                            f"got {slot_of(at)}")

    # The ramp is one hue getting darker, and it never reaches white --
    # an empty wedge still has to read as a wedge.
    pale, full = shade("#2a78d6", 0.0), shade("#2a78d6", 1.0)
    if pale == full:
        failures.append("the ramp should vary with the weight")
    if sum(pale) <= sum(full):
        failures.append("low values should be the lighter end of the ramp")
    if sum(pale) > 2.97:
        failures.append("an empty wedge must not vanish into the paper")
    if shade("#2a78d6", 5.0) != full or shade("#2a78d6", -3.0) != pale:
        failures.append("weights outside 0..1 should clamp, not wrap")

    dial = Dial("SPCX", 90, 15,
                travel={time(9, 30): 8.0, time(10, 0): 4.0, time(14, 0): 1.0},
                signals={time(9, 30): 1, time(10, 0): 2, time(14, 0): 9})
    if dial.busiest() != time(9, 30):
        failures.append(f"busiest should be 09:30, got {dial.busiest()}")
    if dial.loudest() != time(14, 0):
        failures.append(f"loudest should be 14:00, got {dial.loudest()}")
    share = dial.share_before(time(11, 0), dial.travel)
    if share is None or abs(share - 12.0 / 13.0) > 1e-9:
        failures.append(f"movement before 11:00 should be 12/13, got {share}")
    if Dial("X", 1, 15).share_before(time(11, 0), {}) is not None:
        failures.append("no data should be no share, not a zero")

    # A part-printed session still draws the whole arc: the wedges ahead
    # stay empty rather than the dial being redrawn to fit what exists.
    partial = Dial("SPCX", 1, 15, single_day=date(2026, 9, 23),
                   travel={time(9, 30): 3.0, time(9, 45): 2.0},
                   signals={time(9, 30): 1})
    drawn = session_slots(partial.slot_minutes)
    if len(drawn) != 26:
        failures.append("a part-printed day still spans 26 wedges, "
                        f"got {len(drawn)}")
    if partial.travel.get(time(14, 0), 0.0) != 0.0:
        failures.append("a slot that has not happened yet should be empty")
    if partial.busiest() != time(9, 30):
        failures.append("the busiest of what has printed is still the busiest")

    source = open(__file__, encoding="utf-8").read()
    for forbidden in ("TradingClient", "submit_order", "MarketOrderRequest"):
        if forbidden in source.replace(f'"{forbidden}"', ""):
            failures.append(f"this file must not mention {forbidden}")

    print("  Clock geometry                 : 09:30 at 285°, 16:00 at 120°")
    print("  Session arc                    : 195° of the face")
    print(f"  Wedges                         : {len(slots)} quarter hours")
    print("  Every session minute           : lands in exactly one wedge")
    print("  Ramp                           : one hue, light to dark, clamped")
    print("  A part-printed session         : whole arc, empty wedges ahead")
    print("  Trading client in this file    : none")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nAll checks passed. Now run it against real sessions.")
    return 0


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(
        description="The trading day as a clock face.")
    parser.add_argument("--symbol", default=SYMBOL)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--today", action="store_true",
                        help="One session, filling as it goes, instead of "
                             "the habit across many")
    parser.add_argument("--date", metavar="YYYY-MM-DD",
                        help="With --today, a past session instead of this one")
    parser.add_argument("--slot", type=int, default=SLOT_MINUTES, metavar="MIN",
                        help=f"Minutes to a wedge (default {SLOT_MINUTES})")
    parser.add_argument("--out", default=None, metavar="FILE")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    if args.today or args.date:
        day = (datetime.strptime(args.date, "%Y-%m-%d").date()
               if args.date else datetime.now(ET).date())
        print(f"Reading {args.symbol} for {day} (1-minute bars)...")
        dial = measure_today(args.symbol, day, args.slot)
    else:
        print(f"Reading {args.days} sessions of {args.symbol} "
              f"(SIP 1-minute bars)...")
        dial = measure(args.symbol, args.days, args.slot)
    if not sum(dial.travel.values()):
        print("\nNothing measurable. Check the symbol and your connection.")
        return 1

    out = args.out or (f"{args.symbol}_clock_{dial.single_day:%Y%m%d}.pdf"
                       if dial.single_day
                       else f"{args.symbol}_session_clock.pdf")
    print(f"\nwritten to {render(dial, out)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
