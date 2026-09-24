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


if __name__ == "__main__":
    raise SystemExit(self_test())
