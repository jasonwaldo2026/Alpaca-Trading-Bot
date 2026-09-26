"""
Share-unlock dates, read from lockups.json and reported, never predicted.

A lockup expiry is not a pattern anyone hopes to detect. It is arithmetic:
shares that could not be sold become sellable, and the same demand meets a
larger supply. Unlike everything else these tools measure, it is known in
advance -- which makes a tool that stays silent about it the only thing in
the setup that does not know what day it is.

Nothing here forecasts. It states what the calendar says and how sure the
calendar is, and leaves the trading to the person reading it.

READ-ONLY. No market data, no network, no orders.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional

#: How far ahead a date has to be before the morning line stops mentioning
#: it. Far enough to plan around, close enough that it is still this week's
#: problem rather than a distraction.
HORIZON_DAYS = 7

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "lockups.json")


@dataclass(frozen=True)
class Unlock:
    symbol: str
    day: Optional[date]          # None when the date itself is unknown
    shares: Optional[int]
    label: str
    confirmed: bool

    def when(self, today: date) -> str:
        if self.day is None:
            return "date unknown"
        delta = (self.day - today).days
        if delta == 0:
            return "TODAY"
        if delta == 1:
            return "tomorrow"
        if delta > 0:
            return f"in {delta} days"
        if delta == -1:
            return "yesterday"
        return f"{-delta} days ago"

    def size(self) -> str:
        if self.shares is None:
            return "size unstated"
        if self.shares >= 1_000_000_000:
            return f"{self.shares / 1_000_000_000:.2f}bn shares"
        return f"{self.shares / 1_000_000:.0f}M shares"

    def line(self, today: date) -> str:
        stamp = f"{self.day:%d %b}" if self.day else "??"
        flag = "" if self.confirmed else "  [UNCONFIRMED]"
        return (f"{stamp}  {self.when(today):<12} {self.size():<16} "
                f"{self.label}{flag}")


def load(path: str = DEFAULT_PATH) -> dict:
    """Read the calendar. A missing or broken file is not an error.

    These tools have to start on a morning when this file has been edited
    badly at 09:20. A calendar is a convenience; the session is not.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def for_symbol(symbol: str, path: str = DEFAULT_PATH) -> List[Unlock]:
    entry = load(path).get(symbol.upper())
    if not entry:
        return []
    out = []
    for event in entry.get("events", []):
        raw = event.get("date")
        try:
            day = datetime.strptime(raw, "%Y-%m-%d").date() if raw else None
        except (TypeError, ValueError):
            day = None
        out.append(Unlock(symbol=symbol.upper(), day=day,
                          shares=event.get("shares"),
                          label=event.get("label", ""),
                          confirmed=bool(event.get("confirmed"))))
    return sorted(out, key=lambda u: (u.day is None, u.day or date.max))


def near(symbol: str, today: date, within: int = HORIZON_DAYS,
         path: str = DEFAULT_PATH) -> List[Unlock]:
    """Unlocks worth mentioning this morning.

    Today counts, and so does the recent past: supply released on Monday is
    still being worked through on Wednesday. An unlock whose date is not
    known yet is left out of the morning line entirely -- it belongs in the
    full calendar, not in a sentence that implies it is imminent.
    """
    out = []
    for unlock in for_symbol(symbol, path):
        if unlock.day is None:
            continue
        delta = (unlock.day - today).days
        if -3 <= delta <= within:
            out.append(unlock)
    return out


def headline(symbol: str, today: date, within: int = HORIZON_DAYS,
             path: str = DEFAULT_PATH) -> Optional[str]:
    """One line for the top of a morning, or None when the week is clear."""
    upcoming = near(symbol, today, within, path)
    if not upcoming:
        return None
    first = upcoming[0]
    rest = f" (+{len(upcoming) - 1} more within {within}d)" if len(upcoming) > 1 else ""
    sure = "" if first.confirmed else ", unconfirmed"
    return (f"SHARE UNLOCK {first.when(today)}: {first.size()} — "
            f"{first.label}{sure}{rest}")


def render_pdf(symbol: str, path: str, today: Optional[date] = None,
               calendar: str = DEFAULT_PATH) -> Optional[str]:
    """The calendar as one printable page.

    matplotlib is imported here rather than at the top because the live
    watchers import this module at 09:25 and have no use for a plotting
    library: a calendar they read in milliseconds should not drag a
    rendering stack in behind it.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"
    AXIS, SURFACE = "#c3c2b7", "#fcfcfb"
    ACCENT, SECOND, DOWN = "#2a78d6", "#eb6834", "#d03b3b"

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9,
                         "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
                         "axes.edgecolor": AXIS, "text.color": INK,
                         "xtick.color": INK_2, "ytick.color": INK_2})

    today = today or date.today()
    events = for_symbol(symbol, calendar)
    if not events:
        return None
    dated = [e for e in events if e.day]
    undated = [e for e in events if not e.day]
    if not dated:
        return None

    entry = load(calendar).get(symbol.upper(), {})
    ipo = entry.get("ipo", "")
    known = sum(e.shares or 0 for e in events)

    fig = plt.figure(figsize=(11.7, 8.3))
    fig.text(0.045, 0.955, f"{symbol.upper()} share unlocks", size=17,
             weight="bold", color=INK)
    line = f"listed {ipo}" if ipo else ""
    if known:
        line += f" · {known / 1_000_000_000:.2f}bn shares scheduled in total"
    fig.text(0.045, 0.928, line, size=10, color=INK_2)
    fig.text(0.045, 0.902,
             "EVERY DATE UNCONFIRMED unless marked otherwise — transcribed from "
             "secondary sources, not the prospectus. Confirm on SEC EDGAR.",
             size=8.6, color=DOWN)
    fig.add_artist(plt.Line2D([0.045, 0.965], [0.886, 0.886], color=AXIS,
                              linewidth=0.8, transform=fig.transFigure))

    grid = fig.add_gridspec(2, 1, height_ratios=[1.5, 1], hspace=0.34,
                            left=0.075, right=0.965, top=0.845, bottom=0.46)
    bars_ax = fig.add_subplot(grid[0])
    cum_ax = fig.add_subplot(grid[1], sharex=bars_ax)

    xs = [e.day.toordinal() for e in dated]
    heights = [(e.shares or 0) / 1_000_000 for e in dated]
    colours = [MUTED if e.day < today else (SECOND if e.day == today else ACCENT)
               for e in dated]
    bars_ax.bar(xs, heights, width=9, color=colours)
    for x, h, e in zip(xs, heights, dated):
        if h:
            bars_ax.text(x, h, f"{h:.0f}M", ha="center", va="bottom", size=8,
                         color=INK_2)
        else:
            bars_ax.text(x, 0, " size\n unstated", ha="center", va="bottom",
                         size=7.5, color=MUTED)
    bars_ax.axvline(today.toordinal(), color=SECOND, linewidth=1.2,
                    linestyle=(0, (4, 2)))
    bars_ax.set_ylabel("millions of shares")
    bars_ax.set_title("Each tranche. Grey is past, orange is today, blue is "
                      "ahead of you.", loc="left", size=10, color=INK_2, pad=6)
    bars_ax.spines[["top", "right"]].set_visible(False)

    running, cx, cy = 0.0, [], []
    for e in dated:
        running += (e.shares or 0) / 1_000_000_000
        cx.append(e.day.toordinal())
        cy.append(running)
    cum_ax.step(cx, cy, where="post", color=ACCENT, linewidth=2)
    cum_ax.fill_between(cx, cy, step="post", color=ACCENT, alpha=0.12)
    cum_ax.axvline(today.toordinal(), color=SECOND, linewidth=1.2,
                   linestyle=(0, (4, 2)))
    released = sum((e.shares or 0) for e in dated if e.day <= today) / 1e9
    cum_ax.set_ylabel("billions, cumulative")
    cum_ax.set_title(f"Running total. {released:.2f}bn of the dated shares are "
                     f"sellable as of today.", loc="left", size=10,
                     color=INK_2, pad=6)
    cum_ax.spines[["top", "right"]].set_visible(False)

    span = max(xs) - min(xs)
    tick_days = [min(xs) + int(span * i / 5) for i in range(6)]
    cum_ax.set_xticks(tick_days)
    cum_ax.set_xticklabels([date.fromordinal(t).strftime("%d %b %y")
                            for t in tick_days], size=8)
    bars_ax.tick_params(labelbottom=False)

    heads = ("", "Date", "When", "Shares", "What")
    xpos = (0.045, 0.085, 0.205, 0.315, 0.425)
    for x, head in zip(xpos, heads):
        fig.text(x, 0.400, head.upper(), size=7.5, color=MUTED)
    y = 0.366
    for i, event in enumerate(events, 1):
        stamp = f"{event.day:%d %b %Y}" if event.day else "unknown"
        tone = SECOND if event.day == today else (
            MUTED if event.day and event.day < today else INK_2)
        cells = (f"{i}/{len(events)}", stamp, event.when(today), event.size(),
                 event.label)
        for x, cell in zip(xpos, cells):
            fig.text(x, y, cell, size=9, color=tone)
        y -= 0.036

    if undated:
        fig.text(0.045, y - 0.02,
                 "The undated tranche is the largest on the list and cannot be "
                 "placed on the chart. Finding its date is the single most "
                 "useful thing you could add here.",
                 size=9, color=DOWN)

    with PdfPages(path) as pdf:
        pdf.savefig(fig)
        info = pdf.infodict()
        info["Title"] = f"{symbol.upper()} share unlocks"
        info["Subject"] = "Unconfirmed calendar — reported, not predicted"
    plt.close(fig)
    return path


def self_test() -> int:
    print("Self-test: checking the unlock calendar...\n")
    failures = []
    today = date(2026, 9, 24)

    events = for_symbol("SPCX")
    if not events:
        failures.append("SPCX should have events in lockups.json")
    if any(e.confirmed for e in events):
        failures.append("nothing in the shipped file is confirmed; none should "
                        "claim to be")

    # Ordering: dated events in order, undated ones last rather than crashing.
    dated = [e.day for e in events if e.day]
    if dated != sorted(dated):
        failures.append("dated events should come back in date order")
    if events and events[-1].day is not None and any(e.day is None for e in events):
        failures.append("an undated event should sort to the end, not the middle")

    # The window, at both edges.
    if not any(u.when(today) == "TODAY" for u in near("SPCX", today)):
        failures.append("24 Sep 2026 is an unlock date and should read TODAY")
    if near("SPCX", date(2026, 11, 15)):
        failures.append("mid-November is clear in the shipped file; "
                        "nothing should be reported")
    if not near("SPCX", date(2026, 12, 3)):
        failures.append("8 Dec is within a week of 3 Dec and should show")

    # An unknown date must never be presented as imminent.
    if any(u.day is None for u in near("SPCX", today, within=400)):
        failures.append("an undated event must not appear in the morning line")

    # A broken or missing file must not take the morning down with it.
    if load("does-not-exist.json") != {}:
        failures.append("a missing calendar should read as empty, not raise")
    if for_symbol("SPCX", "does-not-exist.json"):
        failures.append("a missing calendar should yield no events")
    if headline("NOSUCH", today) is not None:
        failures.append("a symbol with no calendar should produce no headline")

    line = headline("SPCX", today)
    if not line or "UNCONFIRM" not in line.upper():
        failures.append(f"an unconfirmed date must say so in the headline: {line}")

    source = open(__file__, encoding="utf-8").read()
    for forbidden in ("TradingClient", "submit_order"):
        if forbidden in source.replace(f'"{forbidden}"', ""):
            failures.append(f"this file must not mention {forbidden}")

    print(f"  Events for SPCX                : {len(events)}")
    print(f"  Within a week of {today}    : {len(near('SPCX', today))}")
    print(f"  Morning line                   : {line}")
    print("  Missing file                   : handled")
    print("\n  Full calendar:")
    for event in events:
        print(f"    {event.line(today)}")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nAll checks passed.")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="The share-unlock calendar: printed, or as a page to print.")
    parser.add_argument("--symbol", default="SPCX")
    parser.add_argument("--pdf", nargs="?", const="", metavar="FILE",
                        help="Write the calendar as a one-page PDF instead of "
                             "printing it (default <SYMBOL>_unlocks.pdf)")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.pdf is not None:
        path = args.pdf or f"{args.symbol.upper()}_unlocks.pdf"
        written = render_pdf(args.symbol, path)
        if not written:
            print(f"No calendar for {args.symbol.upper()} in {DEFAULT_PATH}.")
            return 1
        print(f"written to {written}")
        return 0

    return self_test()


if __name__ == "__main__":
    raise SystemExit(main())
