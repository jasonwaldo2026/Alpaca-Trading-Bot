"""
calendar_watch.py -- warn ahead of earnings, share unlocks and launches.

On 20 Oct SpaceX reported after the close, and it was noticed at 15:50.
The company had announced the date four days earlier; the automatic
earnings calendars still said "estimated 3 Nov" the morning after. So the
dates come from three places, each labelled for what it is:

  events.json     dates you or Claude entered from the company's own
                  announcement -- CONFIRMED
  Nasdaq/Zacks    an algorithm's guess from past reporting dates, used only
                  when events.json has no earnings date ahead -- ESTIMATED
  the news        headlines that mention an earnings date or an unlock,
                  sent as they appear, so a new date is never four days old
                  before anyone sees it

plus the unlock calendar (lockups.json) and the launch calendar
(launches.json) the other tools already read.

Reminders, for each dated event:
  3 days before, 08:00        normal
  the evening before, 20:00   normal
  the morning of, 08:00       LOUD (earnings and unlocks)
  30 minutes before           LOUD (earnings with a known time)
and every evening the merger watch's 19:45 note lists the next 14 days.

Read-only: it reads files and public pages. Nothing here trades.
The merger watch runs it; on its own:

    python calendar_watch.py              # the next 30 days, now
    python calendar_watch.py --self-test
"""

from __future__ import annotations

import json
import os
import re
import sys
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional, Tuple

try:
    from feed_check import ET
except ImportError:  # pragma: no cover -- run beside the other tools
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")

import launches
import lockups

SYMBOL = "SPCX"
HERE = os.path.dirname(os.path.abspath(__file__))
EVENTS_PATH = os.path.join(HERE, "events.json")

NASDAQ_ESTIMATE = "https://api.nasdaq.com/api/analyst/{symbol}/earnings-date"
NEWS_SEARCH = ("https://news.google.com/rss/search?q={query}+when:7d"
               "&hl=en-US&gl=US&ceid=US:en")
NEWS_QUERIES = ("SpaceX+earnings+date", "SpaceX+earnings+report",
                "SpaceX+lockup+expiration", "SpaceX+share+unlock")
#: A headline is worth a message only if it says something about WHEN.
NEWS_WORDS = ("earnings date", "to report", "will report", "reports on",
              "results on", "webcast", "earnings call", "lockup", "lock-up",
              "unlock", "mark your calendar")
NEWS_PER_CHECK = 3

#: When earnings land, by the announcement's own word.
REPORT_TIMES = {"after-close": time(16, 0), "pre-market": time(7, 0)}

#: An estimate this close to a date already in events.json is the same
#: quarter's report, not the next one.
SAME_QUARTER = 60

LOUD, NORMAL, SILENT = 1, 0, -1
MORNING = time(8, 0)
EVENING = time(20, 0)
#: A reminder found this late is dropped rather than sent stale.
STALE_AFTER = timedelta(hours=3)


@dataclass(frozen=True)
class Event:
    kind: str                 # "earnings", "unlock" or "launch"
    day: Optional[date]
    at: Optional[time]        # Eastern; None when the time is unknown
    title: str
    sure: str                 # "CONFIRMED", "ESTIMATED", "UNCONFIRMED", "TBC"...

    @property
    def key(self) -> str:
        return f"{self.kind}|{self.day}|{self.title}"

    def when_words(self) -> str:
        if self.kind == "earnings":
            if self.at == REPORT_TIMES["after-close"]:
                return "after the close (from 4:00 PM)"
            if self.at == REPORT_TIMES["pre-market"]:
                return "before the open (about 7:00 AM)"
            return "time not announced"
        if self.kind == "unlock":
            return "shares sellable from the open"
        return f"{self.at:%H:%M} ET" if self.at else "time not set"

    def flag(self) -> str:
        return "" if self.sure == "CONFIRMED" else f" [{self.sure}]"


@dataclass(frozen=True)
class Reminder:
    at: datetime
    loud: bool
    text: str
    key: str


# ----------------------------------------------------------------- events

def earnings_from_file(path: str = EVENTS_PATH, symbol: str = SYMBOL) -> List[Event]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    out = []
    for row in (data.get(symbol) or {}).get("earnings", []):
        try:
            day = datetime.strptime(row["date"], "%Y-%m-%d").date()
        except (KeyError, TypeError, ValueError):
            continue
        out.append(Event("earnings", day, REPORT_TIMES.get(row.get("time")),
                         f"{symbol} earnings{' (' + row['quarter'] + ')' if row.get('quarter') else ''}",
                         "CONFIRMED" if row.get("confirmed") else "UNCONFIRMED"))
    return out


def parse_nasdaq_estimate(payload: bytes) -> Optional[date]:
    """The date in Nasdaq's 'Earnings announcement* for SPCX: Nov 3, 2026'."""
    data = json.loads(payload.decode("utf-8")).get("data") or {}
    match = re.search(r":\s*([A-Z][a-z]{2} \d{1,2}, \d{4})", data.get("announcement") or "")
    if not match:
        return None
    return datetime.strptime(match.group(1), "%b %d, %Y").date()


def all_events(today: date, estimate: Optional[date] = None,
               path: str = EVENTS_PATH, symbol: str = SYMBOL) -> List[Event]:
    events = earnings_from_file(path, symbol)
    # The estimate only fills a gap: once the company has named the day,
    # an algorithm's guess for the same quarter is noise -- including after
    # the report, when the vendor can be slow to move on (on 21 Oct it still
    # said "3 Nov" for the quarter reported on the 20th).
    if estimate and estimate >= today and not any(
            e.kind == "earnings" and e.day and abs((estimate - e.day).days) <= SAME_QUARTER
            for e in events):
        events.append(Event("earnings", estimate, None, f"{symbol} earnings",
                            "ESTIMATED by Nasdaq/Zacks, not announced"))
    for u in lockups.for_symbol(symbol):
        day, note = u.day, ""
        # An unlock dated on a weekend first bites at Monday's open.
        if day is not None and day.weekday() >= 5:
            note = f" (dated {day:%a %b %d}; first trading day)"
            day += timedelta(days=7 - day.weekday())
        events.append(Event("unlock", day, None,
                            f"{symbol} share unlock, {u.size()}: {u.label}{note}",
                            "CONFIRMED" if u.confirmed else "UNCONFIRMED"))
    for launch in launches.for_symbol(symbol):
        if launch.day is None:
            continue
        events.append(Event("launch", launch.day, launch.at,
                            f"{launch.mission} ({launch.vehicle})",
                            "CONFIRMED" if launch.confirmed else launch.status))
    return sorted(events, key=lambda e: (e.day is None, e.day or date.max, e.at or time(0)))


# -------------------------------------------------------------- reminders

def _on(day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=ET)


def reminders(event: Event) -> List[Reminder]:
    if event.day is None:
        return []
    out = []
    big = event.kind in ("earnings", "unlock")
    what = f"{event.title}, {event.when_words()}{event.flag()}"
    out.append(Reminder(_on(event.day - timedelta(days=3), MORNING), False,
                        f"In 3 days ({event.day:%a %b %d}): {what}.", f"{event.key}|3d"))
    out.append(Reminder(_on(event.day - timedelta(days=1), EVENING), False,
                        f"Tomorrow ({event.day:%a %b %d}): {what}.", f"{event.key}|eve"))
    morning = f"TODAY: {what}."
    if event.kind == "earnings":
        morning += " Decide before it lands whether you are holding through it."
    out.append(Reminder(_on(event.day, MORNING), big, morning, f"{event.key}|day"))
    if event.kind == "earnings" and event.at:
        out.append(Reminder(_on(event.day, event.at) - timedelta(minutes=30), True,
                            f"{event.title} in 30 minutes, {event.when_words()}"
                            f"{event.flag()}. Decide now whether you are holding.",
                            f"{event.key}|30m"))
    if event.kind == "launch" and event.at:
        out.append(Reminder(_on(event.day, event.at) - timedelta(hours=1), False,
                            f"In 1 hour: {what}.", f"{event.key}|1h"))
    return out


def due(events: List[Event], sent: Dict[str, str], now: datetime) -> List[Reminder]:
    """Reminders whose time has come, not yet sent and not yet stale.
    Marks each one sent in `sent` (key -> when)."""
    out = []
    for event in events:
        for r in reminders(event):
            if r.key in sent or not (r.at <= now < r.at + STALE_AFTER):
                continue
            sent[r.key] = now.isoformat(timespec="seconds")
            out.append(r)
    return out


def coming_up(events: List[Event], today: date, days: int = 14) -> List[str]:
    """Lines for the evening note: what is dated in the next `days`, and the
    unlocks no one has dated yet -- the ones easiest to forget."""
    lines = []
    for e in events:
        if e.day and today <= e.day <= today + timedelta(days=days):
            lines.append(f"{e.day:%a %b %d}  {e.title}, {e.when_words()}{e.flag()}")
    for e in events:
        if e.day is None and e.kind == "unlock":
            lines.append(f"no date yet  {e.title}{e.flag()}")
    return lines


# ------------------------------------------------------------------- news

def parse_news(payload: bytes) -> List[Tuple[str, str, Optional[datetime]]]:
    root = ElementTree.fromstring(payload)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        try:
            when = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            when = None
        out.append((title, link, when))
    return out


def news_alerts(items: List[Tuple[str, str, Optional[datetime]]], seen: List[str],
                now: datetime, first_run: bool) -> List[str]:
    """Messages for new headlines about a date. The first run only learns
    what is already out there, so starting up never sends a backlog."""
    out = []
    for title, link, when in items:
        key = title.lower()[:120]
        if key in seen:
            continue
        seen.append(key)
        if first_run or when is None or now - when > timedelta(days=2):
            continue
        if not any(word in title.lower() for word in NEWS_WORDS):
            continue
        if len(out) < NEWS_PER_CHECK:
            out.append(f"News about a date: {title}\nCheck it; if it names an earnings "
                       f"or unlock date, add it to the calendar.\n{link}")
    del seen[:-400]
    return out


# -------------------------------------------------------------- self-test

def self_test() -> int:
    import tempfile

    failures = []
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "events.json")
        with open(path, "w", encoding="utf-8") as h:
            json.dump({"SPCX": {"earnings": [
                {"date": "2026-10-20", "time": "after-close", "quarter": "Q3 2026",
                 "confirmed": True}]}}, h)
        files = earnings_from_file(path)
        if len(files) != 1 or files[0].at != time(16, 0) or files[0].sure != "CONFIRMED":
            failures.append(f"events.json read wrong: {files}")

        # Reminders for an after-close report on Tue 20 Oct.
        e = files[0]
        times = {r.key.split("|")[-1]: (r.at.strftime("%a %H:%M"), r.loud) for r in reminders(e)}
        want = {"3d": ("Sat 08:00", False), "eve": ("Mon 20:00", False),
                "day": ("Tue 08:00", True), "30m": ("Tue 15:30", True)}
        if times != want:
            failures.append(f"earnings reminders: {times}")

        sent: Dict[str, str] = {}
        at = datetime(2026, 10, 20, 15, 36, tzinfo=ET)
        got = due([e], sent, at)
        if [r.key.split("|")[-1] for r in got] != ["30m"]:
            failures.append(f"at 15:36 only the 30-minute warning is due "
                            f"(the 08:00 one is stale): {[r.key for r in got]}")
        if due([e], sent, at + timedelta(minutes=5)):
            failures.append("a reminder is sent once")
        if "in 30 minutes" not in got[0].text or "after the close" not in got[0].text:
            failures.append(f"30-minute text: {got[0].text}")

        # The estimate fills a gap only, and says what it is.
        today = date(2026, 10, 21)
        evs = all_events(today, estimate=date(2026, 11, 3), path=path)
        if any(x.day == date(2026, 11, 3) for x in evs):
            failures.append("a stale estimate for the quarter just reported is dropped")
        evs = all_events(today, estimate=date(2027, 1, 28), path=path)
        est = [x for x in evs if x.kind == "earnings" and x.day == date(2027, 1, 28)]
        if len(est) != 1 or "ESTIMATED" not in est[0].sure:
            failures.append(f"an estimate for the next quarter is used and labelled: {est}")
        with open(path, "w", encoding="utf-8") as h:
            json.dump({"SPCX": {"earnings": [
                {"date": "2026-11-05", "time": "after-close", "confirmed": True}]}}, h)
        evs = all_events(today, estimate=date(2026, 11, 3), path=path)
        if any(x.day == date(2026, 11, 3) for x in evs):
            failures.append("a confirmed date ahead replaces the estimate")
        if parse_nasdaq_estimate(json.dumps({"data": {"announcement":
                "Earnings announcement* for SPCX: Nov 3, 2026"}}).encode()) != date(2026, 11, 3):
            failures.append("Nasdaq's estimate parsed wrong")
        if parse_nasdaq_estimate(json.dumps({"data": {"announcement":
                "Earnings announcement* for TSLA: "}}).encode()) is not None:
            failures.append("no date from Nasdaq is no estimate")

        lines = coming_up(evs, today, days=30)
        sat = [x for x in evs if x.kind == "unlock" and "dated Sat Oct 24" in x.title]
        if not sat or sat[0].day != date(2026, 10, 26):
            failures.append(f"an unlock dated Saturday counts from Monday: {sat}")
        if not any("Nov 05" in x and "after the close" in x for x in lines):
            failures.append(f"coming up should list the earnings: {lines}")
        undated = Event("unlock", None, None, "SPCX share unlock, 1.30bn shares", "UNCONFIRMED")
        if not any(x.startswith("no date yet") for x in coming_up(evs + [undated], today)):
            failures.append("an undated unlock is always listed")

    rss = (b"<rss><channel>"
           b"<item><title>SpaceX surprises Wall Street with Oct. 20 earnings date</title>"
           b"<link>https://example.com/a</link><pubDate>Mon, 19 Oct 2026 14:00:00 GMT</pubDate></item>"
           b"<item><title>SpaceX stock moves higher</title>"
           b"<link>https://example.com/b</link><pubDate>Mon, 19 Oct 2026 15:00:00 GMT</pubDate></item>"
           b"</channel></rss>")
    items = parse_news(rss)
    seen: List[str] = []
    if news_alerts(items, seen, datetime(2026, 10, 19, 16, 0, tzinfo=ET), first_run=True):
        failures.append("the first run only learns the headlines")
    seen = []
    got = news_alerts(items, seen, datetime(2026, 10, 19, 16, 0, tzinfo=ET), first_run=False)
    if len(got) != 1 or "Oct. 20 earnings date" not in got[0]:
        failures.append(f"only the headline about a date is sent: {got}")
    if news_alerts(items, seen, datetime(2026, 10, 19, 17, 0, tzinfo=ET), first_run=False):
        failures.append("a headline is sent once")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("calendar self-test passed")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if "--self-test" in sys.argv:
        return self_test()
    today = datetime.now(ET).date()
    estimate = None
    try:
        import urllib.request
        request = urllib.request.Request(NASDAQ_ESTIMATE.format(symbol=SYMBOL),
                                         headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            estimate = parse_nasdaq_estimate(response.read())
    except Exception as exc:  # noqa: BLE001
        print(f"(Nasdaq estimate unavailable: {type(exc).__name__})")
    print(f"Coming up for {SYMBOL}, next 30 days:")
    for line in coming_up(all_events(today, estimate), today, days=30) or ["nothing dated"]:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
