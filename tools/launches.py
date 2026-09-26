"""
Launch dates, read from launches.json and reported, never predicted.

A launch is not an unlock. An unlock is arithmetic -- a known number of
shares becomes sellable whether anyone notices or not -- and there are
eight of them on the whole calendar. SpaceX has flown 42 times since SPCX
listed on 12 June 2026, one every two and a half days, with 130 more on
the schedule. Marking all of those would bury the unlock dates under a
picket fence and leave the calendar less useful than it was.

So this file holds only the events that stand out from that background:
Starship flights, crewed missions, Falcon Heavy, first-of-type. Four to
six a year rather than 160. Routine Starlink and rideshare missions are
deliberately absent, and adding them back would be the change that makes
this calendar worthless.

Two honest limits, both of which the tool states rather than hides:

  1. A launch date is among the least reliable dates in industry. Weather,
     a range conflict, or a scrub with the vehicle already fuelled moves
     it. The source's own confidence -- Go, TBC, TBD -- travels with every
     entry and is never upgraded here.
  2. Nothing measures whether a launch moves the stock. Exactly one
     Starship flight has happened in SPCX's entire trading history, so
     there is no sample to test. This file reports the calendar and makes
     no claim about price.

READ-ONLY. No market data, no network, no orders.

    python launches.py                  # the calendar
    python launches.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import List, Optional

try:  # the session boundaries belong in one place, not in four
    from feed_check import SESSION_CLOSE, SESSION_OPEN
except ImportError:  # ...but this module must stand on its own
    SESSION_OPEN, SESSION_CLOSE = time(9, 30), time(16, 0)

#: Same horizon as the unlock calendar, for the same reason: far enough
#: ahead to plan around, close enough to still be this week's business.
HORIZON_DAYS = 7

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "launches.json")

#: The source's confidence, kept verbatim. "Go" is confirmed, "TBC" means
#: the date is expected but unconfirmed, "TBD" means nobody knows -- and a
#: TBD entry is stored undated rather than pinned to the placeholder date
#: the schedule prints for it.
CONFIDENT = "Go"


@dataclass(frozen=True)
class Launch:
    symbol: str
    day: Optional[date]          # None when the date is genuinely unknown
    at: Optional[time]           # Eastern, to match the trading day
    mission: str
    vehicle: str
    status: str                  # Go / TBC / TBD, from the source

    @property
    def confirmed(self) -> bool:
        return self.status == CONFIDENT

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

    def session(self) -> str:
        """Where the launch falls relative to the hours you can trade.

        Worth saying out loud, because a launch at 08:15 and one at 11:10
        reach you in completely different ways: one is something to know
        before the open, the other happens while you are holding.
        """
        if self.day is None or self.at is None:
            return "time unknown"
        if self.day.weekday() >= 5:
            return "market closed"
        if self.at < SESSION_OPEN:
            return "pre-market"
        if self.at >= SESSION_CLOSE:
            return "after the close"
        return "during the session"

    def line(self, today: date) -> str:
        stamp = f"{self.day:%d %b}" if self.day else "??"
        clock = f"{self.at:%H:%M} ET" if self.at else "--:-- ET"
        return (f"{stamp:<6}  {clock:<9} {self.when(today):<13} "
                f"{self.status:<4} {self.mission} ({self.vehicle})"
                f"  [{self.session()}]")


def load(path: str = DEFAULT_PATH) -> dict:
    """Read the calendar. A missing or broken file is not an error.

    Same contract as lockups.py, for the same reason: this is read at
    09:25 by tools whose job is the session, and a calendar edited badly
    at 09:20 must cost a line of output rather than the morning.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def for_symbol(symbol: str, path: str = DEFAULT_PATH) -> List[Launch]:
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
        raw_at = event.get("time_et")
        try:
            at = datetime.strptime(raw_at, "%H:%M").time() if raw_at else None
        except (TypeError, ValueError):
            at = None
        out.append(Launch(symbol=symbol.upper(), day=day, at=at,
                          mission=event.get("mission", ""),
                          vehicle=event.get("vehicle", ""),
                          status=event.get("status", "TBD")))
    return sorted(out, key=lambda e: (e.day is None, e.day or date.max,
                                      e.at or time(0, 0)))


def near(symbol: str, today: date, within: int = HORIZON_DAYS,
         path: str = DEFAULT_PATH) -> List[Launch]:
    """Launches worth a line this morning.

    Undated entries never appear here. A Falcon Heavy pencilled in for
    "sometime in November" is calendar material, not something to mention
    in a sentence that implies it is imminent. Yesterday still counts --
    a flight that happened after Thursday's close is Friday's news.
    """
    out = []
    for launch in for_symbol(symbol, path):
        if launch.day is None:
            continue
        if -1 <= (launch.day - today).days <= within:
            out.append(launch)
    return out


def headline(symbol: str, today: date, within: int = HORIZON_DAYS,
             path: str = DEFAULT_PATH) -> Optional[str]:
    """One line for the top of a morning, or None when the week is clear."""
    upcoming = near(symbol, today, within, path)
    if not upcoming:
        return None
    first = upcoming[0]
    rest = (f" (+{len(upcoming) - 1} more within {within}d)"
            if len(upcoming) > 1 else "")
    clock = f" {first.at:%H:%M} ET, {first.session()}" if first.at else ""
    sure = "" if first.confirmed else f", {first.status}"
    return (f"LAUNCH {first.when(today)}: {first.mission} "
            f"({first.vehicle}){clock}{sure}{rest}")


def report(symbol: str, today: Optional[date] = None,
           path: str = DEFAULT_PATH) -> None:
    today = today or date.today()
    events = for_symbol(symbol, path)
    entry = load(path).get(symbol.upper(), {})
    rule = "=" * 88
    print(f"\n{rule}")
    print(f"  {symbol.upper()} LAUNCH CALENDAR  --  {len(events)} notable events")
    print(rule)
    if not events:
        print("  Nothing in the calendar. Check launches.json exists and parses.")
        print(f"{rule}\n")
        return
    for event in events:
        print(f"  {event.line(today)}")
    print("\n  Go   confirmed for that date        TBC  expected, unconfirmed")
    print("  TBD  no date yet -- listed last, never drawn on a chart")
    print("\n  Routine Starlink and rideshare missions are deliberately absent:")
    print("  SpaceX flies every couple of days, and a calendar that lists all")
    print("  of them hides the share unlocks, which are the dates that carry")
    print("  a known consequence.")
    print("\n  Nothing here says a launch moves the price. One Starship flight")
    print("  has happened in this stock's whole history, so there is no sample.")
    if entry.get("source"):
        print(f"\n  Source: {entry['source']}")
    print(f"{rule}\n")


def self_test() -> int:
    print("Self-test: checking the launch calendar...\n")
    failures = []
    today = date(2026, 9, 24)

    # A weekday mid-session flight, a pre-market one, a weekend one.
    midday = Launch("SPCX", date(2026, 10, 1), time(11, 10), "Crew-13",
                    "Falcon 9", "Go")
    early = Launch("SPCX", date(2026, 9, 28), time(8, 15), "Flight 14",
                   "Starship", "TBC")
    weekend = Launch("SPCX", date(2026, 9, 26), time(7, 56), "USSF-385",
                     "Falcon 9", "Go")
    undated = Launch("SPCX", None, None, "Flight 15", "Starship", "TBD")

    if midday.session() != "during the session":
        failures.append(f"11:10 on a Thursday: {midday.session()}")
    if early.session() != "pre-market":
        failures.append(f"08:15 on a Monday: {early.session()}")
    if weekend.session() != "market closed":
        failures.append(f"Saturday: {weekend.session()}")
    if undated.session() != "time unknown":
        failures.append(f"no date at all: {undated.session()}")

    if not midday.confirmed or early.confirmed or undated.confirmed:
        failures.append("only 'Go' counts as confirmed")

    # Undated entries sort last and never reach the morning line.
    order = sorted([undated, midday, early],
                   key=lambda e: (e.day is None, e.day or date.max,
                                  e.at or time(0, 0)))
    if order[-1] is not undated:
        failures.append("an undated launch must sort last")

    # A missing or corrupt file costs a line of output, not the session.
    if load("does-not-exist.json") != {}:
        failures.append("a missing calendar should read as empty, not raise")
    if for_symbol("SPCX", "does-not-exist.json") != []:
        failures.append("a missing calendar should yield no events")
    if headline("SPCX", today, path="does-not-exist.json") is not None:
        failures.append("no calendar means no morning line")

    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        handle.write("{ this is not json")
        broken = handle.name
    try:
        if load(broken) != {}:
            failures.append("a corrupt calendar should read as empty")
    finally:
        os.unlink(broken)

    # A placeholder date must not arrive as a date. The schedule prints
    # "31 Dec, midnight" to mean "sometime next year"; storing that would
    # put a fictitious marker on a chart.
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump({"SPCX": {"events": [
            {"date": None, "mission": "Flight 15", "vehicle": "Starship",
             "status": "TBD"},
            {"date": "2026-09-28", "time_et": "08:15", "mission": "Flight 14",
             "vehicle": "Starship", "status": "TBC"}]}}, handle)
        temp = handle.name
    try:
        loaded = for_symbol("SPCX", temp)
        if [e.mission for e in loaded] != ["Flight 14", "Flight 15"]:
            failures.append(f"sort order: {[e.mission for e in loaded]}")
        if [e.mission for e in near("SPCX", today, path=temp)] != ["Flight 14"]:
            failures.append("an undated launch must stay out of the week ahead")
        note = headline("SPCX", today, path=temp)
        if not note or "Flight 14" not in note or "TBC" not in note:
            failures.append(f"the morning line should name the mission and "
                            f"its confidence: {note}")
    finally:
        os.unlink(temp)

    # The promise every file in this folder makes.
    source = open(__file__, encoding="utf-8").read()
    for forbidden in ("TradingClient", "submit_order", "MarketOrderRequest"):
        if forbidden in source.replace(f'"{forbidden}"', ""):
            failures.append(f"this file must not mention {forbidden}")

    print("  Session placement              : mid-session, pre-market, weekend")
    print("  Confidence                     : only 'Go' is confirmed")
    print("  Undated entries                : sort last, never in the week ahead")
    print("  Missing or corrupt calendar    : empty, never an exception")
    print("  Trading client in this file    : none")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nAll checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Notable launch dates, reported and never predicted.")
    parser.add_argument("--symbol", default="SPCX")
    parser.add_argument("--path", default=DEFAULT_PATH)
    parser.add_argument("--date", help="Pretend today is YYYY-MM-DD")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    today = (datetime.strptime(args.date, "%Y-%m-%d").date()
             if args.date else date.today())
    report(args.symbol, today, args.path)
    note = headline(args.symbol, today, path=args.path)
    if note:
        print(f"  {note}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
