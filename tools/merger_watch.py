"""
merger_watch.py -- ring the phone the moment a Tesla-SpaceX merger shows up.

A merger announcement can land on a Sunday evening or at 2 AM, when no
chart is open and the overnight session is the only place to act on it.
This watches the three places where an announcement leaves hard evidence,
around the clock, weekends included:

  SEC filings      Tesla and SpaceX are both public, so an agreement must
                   be filed. A Form 425 (merger communication) or an S-4
                   is close to proof; an 8-K that names the other company
                   beside a merger word is the announcement itself.
  Trading halts    During trading hours Nasdaq usually halts the stock
                   "news pending" (code T1) before the news crosses.
  Polymarket       The crowd's odds that a merger is announced by each
                   date. A sharp jump means bettors are reacting to
                   something; a market settling "Yes" means it happened.

How the phone rings:

  EMERGENCY  repeats until acknowledged
             - Form 425 / S-4 / merger proxy from either company
             - an 8-K naming the other company with a merger word
             - an 8-K "material definitive agreement" that could not be read
             - SPCX or TSLA halted for news
             - any Polymarket date at 90% or more, or settled "Yes"
  LOUD       - an 8-K "material agreement" that does not name the other
             - SPCX or TSLA paused for volatility
             - a source unreachable for 30 minutes ("merger watch is blind")
  NORMAL     - odds up or down 15+ points in a day, or 10+ in an hour
               (LOUD at 10 / 5 until 24 Oct: see JUMP_DAY_POINTS)
             - any other 8-K from either company
             - a deadline that passed with no announcement
             - SPCX headlines that can move the stock (spcx_news.py), one
               sound per topic every 2 hours, silent 10 PM - 7 AM
  SILENT     - the odds at 19:45 ET each day, before the overnight session

The Polymarket markets are small -- a few thousand dollars behind each
date -- so one bettor can move them. That is why a jump in the odds is
only a normal sound and only the filings, halts and a settled market are
EMERGENCY.

Read-only. It reads public pages: no account, no order, no bet. There is
no trading client in this file.

    python merger_watch.py --status        # the odds and latest filings, now
    python merger_watch.py --watch         # run forever (what the task runs)
    python merger_watch.py --once          # one pass over every source
    python merger_watch.py --watch --dry-run
    python merger_watch.py --test-push     # one loud test message
    python merger_watch.py --self-test     # check the rules, no network

Setup: the SEC asks every automated reader to say who it is. Add your
email to the same .env as the Pushover keys:

    SEC_CONTACT=you@example.com

Without it the SEC check is off and the other two still run. Keep this
file beside spcx_alert.py and feed_check.py, which it imports.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time as time_mod
import urllib.error
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import calendar_watch
import heartbeat
import spcx_news
from feed_check import ET, load_env
from spcx_alert import (
    PRIORITY_EMERGENCY,
    PRIORITY_SUMMARY,
    PRIORITY_UPDATE,
    send_pushover,
)

PRIORITY_LOUD = PRIORITY_SUMMARY
PRIORITY_NORMAL = 0
PRIORITY_SILENT = PRIORITY_UPDATE

TITLE = "MERGER WATCH"

#: The Polymarket event holding one Yes/No market per deadline. Its slug
#: still says "june-30" because that was the first deadline it listed.
POLYMARKET_EVENT = "tesla-and-spacex-merger-officially-announced-by-june-30"
POLYMARKET_API = "https://gamma-api.polymarket.com/events?slug={slug}"
POLYMARKET_PAGE = "https://polymarket.com/event/{slug}"

#: SEC central index keys. SpaceX is the listed SPCX, not the private
#: "Space Exploration I" fund that shares part of its name.
COMPANIES: Dict[str, int] = {"Tesla": 1318605, "SpaceX": 1181412}
COUNTERPART: Dict[str, Tuple[str, ...]] = {
    "Tesla": ("spacex", "space exploration technologies"),
    "SpaceX": ("tesla",),
}
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_DOCUMENT = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{doc}"

#: Forms that exist only because a business combination is under way.
MERGER_FORMS = {
    "425", "S-4", "S-4/A", "F-4", "F-4/A", "PREM14A", "DEFM14A", "PREM14C",
    "DEFM14C", "SC TO-T", "SC TO-T/A", "SCHEDULE TO-T", "SC 14D9", "SC 14D9/A",
}
MERGER_WORDS = ("merger", "merge with", "business combination", "acquisition",
                "acquire", "combine")
ITEM_NAMES = {
    "1.01": "material definitive agreement", "1.02": "agreement ended",
    "2.01": "acquisition completed", "2.02": "earnings", "2.03": "new debt",
    "3.02": "share sale", "5.02": "director or officer change",
    "5.07": "shareholder vote", "7.01": "public disclosure",
    "8.01": "other events", "9.01": "exhibits",
}

HALTS_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
HALTS_NS = "{http://www.nasdaqtrader.com/}"
HALT_SYMBOLS = ("SPCX", "TSLA")
NEWS_HALTS = {
    "T1": "news pending", "T2": "news released", "T12": "more information requested",
    "H10": "SEC trading suspension", "H11": "regulatory concern",
}
VOLATILITY_HALTS = {"LUDP": "volatility pause", "LUDS": "volatility pause",
                    "M": "volatility pause"}

#: Odds moves, in percentage points, that make the phone ring.
#: Raised from 10 / 5 on 24 Oct 2026, and the ring turned from LOUD to
#: normal: in the week to then, nine alarms were 5-8 point moves that the
#: same few bettors reversed within the hour. An announcement moves these
#: markets 30-50 points, and the filing and halt alarms ring with it.
JUMP_DAY_POINTS = 15.0
JUMP_HOUR_POINTS = 10.0
#: Once a jump has rung, the same market and direction rings again only
#: after this long -- or sooner, if the odds move another full day-step.
JUMP_QUIET = timedelta(hours=6)
CERTAIN_PCT = 90.0
SETTLED_YES_PCT = 99.0

#: How far back a filing or halt still counts as news. A filing found
#: after a restart still matters the next morning; a halt is history
#: within hours.
RECENT_FILING = timedelta(hours=24)
RECENT_HALT = timedelta(hours=6)
HISTORY_KEEP = timedelta(hours=48)

BLIND_AFTER = timedelta(minutes=30)
SUMMARY_AT = time(19, 45)
SUMMARY_LATEST = time(21, 45)
#: A message Pushover could not take is retried this long, then dropped.
PENDING_KEEP = timedelta(hours=2)

#: Seconds between checks of each source.
INTERVALS = {"halts": 60, "sec": 120, "polymarket": 300, "calendar": 3600, "news": 600}
SOURCE_NAMES = {"calendar": "Earnings & unlock calendar", "news": "SPCX news",
                "halts": "Nasdaq halts", "sec": "SEC filings",
                "polymarket": "Polymarket odds"}

STATE_PATH = "merger_watch_state.json"
LOG_PATH = "merger_watch.log"
USER_AGENT = "merger_watch read-only monitor"


# --------------------------------------------------------------- the data

@dataclass
class Alert:
    priority: int
    message: str
    created: str = ""


@dataclass
class Market:
    slug: str
    label: str
    yes_pct: float
    closed: bool


@dataclass
class Halt:
    symbol: str
    when: datetime
    code: str
    name: str


@dataclass
class Filing:
    company: str
    cik: int
    form: str
    accepted: datetime
    accession: str
    items: Tuple[str, ...]
    document: str

    @property
    def url(self) -> str:
        return SEC_DOCUMENT.format(cik=self.cik, folder=self.accession.replace("-", ""),
                                   doc=self.document)


def new_state() -> dict:
    return {"seen_filings": [], "seen_halts": [], "odds": {}, "labels": {},
            "open_seen": [], "rung": {}, "last_ok": {}, "blind": [],
            "summary_date": "", "pending": [], "cal_sent": {}, "news_seen": [],
            "news_primed": False, "earnings_estimate": None}


def load_state(path: str) -> dict:
    state = new_state()
    try:
        with open(path, encoding="utf-8") as handle:
            state.update(json.load(handle))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        log(f"state file unreadable ({type(exc).__name__}); starting fresh")
    return state


def save_state(state: dict, path: str) -> None:
    temp = f"{path}.tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=1)
    os.replace(temp, path)


def log(line: str) -> None:
    stamped = f"{datetime.now(ET):%Y-%m-%d %H:%M:%S}  {line}"
    print(stamped, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(stamped + "\n")
    except OSError:
        pass


# ------------------------------------------------------------ the parsers

def parse_polymarket(payload: bytes) -> List[Market]:
    events = json.loads(payload.decode("utf-8"))
    if not events:
        raise ValueError("Polymarket returned no event")
    markets = []
    for raw in events[0].get("markets", []):
        outcomes = json.loads(raw.get("outcomes") or "[]")
        prices = json.loads(raw.get("outcomePrices") or "[]")
        if "Yes" not in outcomes or len(prices) != len(outcomes):
            continue
        yes = float(prices[outcomes.index("Yes")]) * 100
        # The question says "by December 31?" with no year; the end date
        # (11:59 PM ET on the deadline, stored in UTC) carries it.
        end = datetime.fromisoformat(raw["endDate"].replace("Z", "+00:00")).astimezone(ET)
        label = f"{end:%b} {end.day}, {end.year}"
        markets.append(Market(raw["slug"], label, round(yes, 1), bool(raw.get("closed"))))
    if not markets:
        raise ValueError("Polymarket event had no readable markets")
    return markets


def parse_halts(payload: bytes) -> List[Halt]:
    root = ElementTree.fromstring(payload.decode("utf-8-sig"))
    halts = []
    for item in root.iter("item"):
        def field(name: str) -> str:
            return (item.findtext(HALTS_NS + name) or "").strip()
        try:
            when = datetime.strptime(f"{field('HaltDate')} {field('HaltTime')[:8]}",
                                     "%m/%d/%Y %H:%M:%S").replace(tzinfo=ET)
        except ValueError:
            continue
        halts.append(Halt(field("IssueSymbol"), when, field("ReasonCode"), field("IssueName")))
    return halts


def parse_filings(payload: bytes, company: str) -> List[Filing]:
    recent = json.loads(payload.decode("utf-8"))["filings"]["recent"]
    filings = []
    for i, form in enumerate(recent["form"]):
        accepted = datetime.fromisoformat(
            recent["acceptanceDateTime"][i].replace("Z", "+00:00"))
        items = tuple(x.strip() for x in (recent["items"][i] or "").split(",") if x.strip())
        filings.append(Filing(company, COMPANIES[company], form, accepted,
                              recent["accessionNumber"][i], items,
                              recent["primaryDocument"][i]))
    return filings


def plain_text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).lower()


# --------------------------------------------------------------- the rules

def _iso(when: datetime) -> str:
    return when.astimezone(ET).isoformat(timespec="seconds")


def polymarket_alerts(markets: List[Market], state: dict, now: datetime) -> List[Alert]:
    alerts = []
    page = POLYMARKET_PAGE.format(slug=POLYMARKET_EVENT)
    for m in markets:
        state["labels"][m.slug] = m.label
        if m.closed:
            if m.yes_pct >= SETTLED_YES_PCT and _once(state, f"settled:{m.slug}", now):
                alerts.append(Alert(PRIORITY_EMERGENCY,
                    f"Polymarket SETTLED YES: merger announced by {m.label}.\n"
                    f"Check the news now.\n{page}"))
            elif (m.yes_pct < SETTLED_YES_PCT and m.slug in state["open_seen"]
                  and _once(state, f"expired:{m.slug}", now)):
                alerts.append(Alert(PRIORITY_NORMAL,
                    f"Deadline {m.label} passed with no merger announcement."))
            continue

        if m.slug not in state["open_seen"]:
            state["open_seen"].append(m.slug)
        history = state["odds"].setdefault(m.slug, [])
        history.append([_iso(now), m.yes_pct])
        cutoff = now - HISTORY_KEEP
        history[:] = [r for r in history if datetime.fromisoformat(r[0]) >= cutoff]

        if m.yes_pct >= CERTAIN_PCT and _once(state, f"certain:{m.slug}", now):
            alerts.append(Alert(PRIORITY_EMERGENCY,
                f"Merger odds {m.yes_pct:.0f}% for {m.label}. Bettors think it is "
                f"happening. Check the news now.\n{page}"))
            continue

        jump = _jump(history, m.yes_pct, now)
        if jump:
            direction, points, span = jump
            key = f"jump:{m.slug}:{direction}"
            last = state["rung"].get(key)
            quiet = (last and now - datetime.fromisoformat(last[0]) < JUMP_QUIET
                     and abs(m.yes_pct - last[1]) < JUMP_DAY_POINTS)
            if not quiet:
                state["rung"][key] = [_iso(now), m.yes_pct]
                word = "UP" if direction == "up" else "DOWN"
                alerts.append(Alert(PRIORITY_NORMAL,
                    f"Merger odds {word} {points:.0f} points in {span} for "
                    f"{m.label}: now {m.yes_pct:.0f}%.\n"
                    f"Small market: a few bettors can move it. Check the news.\n{page}"))
    return alerts


def _jump(history: list, now_pct: float, now: datetime) -> Optional[Tuple[str, float, str]]:
    """The biggest qualifying move against the last hour or day, if any."""
    for span, window, points in (("an hour", timedelta(hours=1), JUMP_HOUR_POINTS),
                                 ("a day", timedelta(hours=24), JUMP_DAY_POINTS)):
        past = [pct for when, pct in history[:-1]
                if now - datetime.fromisoformat(when) <= window]
        if not past:
            continue
        if now_pct - min(past) >= points:
            return "up", now_pct - min(past), span
        if max(past) - now_pct >= points:
            return "down", max(past) - now_pct, span
    return None


def _once(state: dict, key: str, now: datetime) -> bool:
    if key in state["rung"]:
        return False
    state["rung"][key] = [_iso(now), None]
    return True


def halt_alerts(halts: List[Halt], state: dict, now: datetime) -> List[Alert]:
    alerts = []
    for h in halts:
        if h.symbol not in HALT_SYMBOLS or now - h.when > RECENT_HALT:
            continue
        key = f"{h.symbol}|{h.when:%Y-%m-%d %H:%M:%S}|{h.code}"
        if key in state["seen_halts"]:
            continue
        state["seen_halts"].append(key)
        when = f"{h.when:%a %H:%M} ET"
        if h.code in NEWS_HALTS:
            alerts.append(Alert(PRIORITY_EMERGENCY,
                f"{h.symbol} HALTED {when}: {NEWS_HALTS[h.code]} ({h.code}).\n"
                f"Big news is coming. Check the news now."))
        else:
            reason = VOLATILITY_HALTS.get(h.code, f"halt code {h.code}")
            alerts.append(Alert(PRIORITY_LOUD,
                f"{h.symbol} halted {when}: {reason} ({h.code})."))
    state["seen_halts"] = state["seen_halts"][-200:]
    return alerts


def classify_filing(f: Filing, text: Optional[str]) -> Optional[Tuple[int, str]]:
    """(priority, why) for a filing worth a message, or None."""
    if f.form in MERGER_FORMS:
        return PRIORITY_EMERGENCY, f"filed a Form {f.form}, a MERGER document"
    if not f.form.startswith("8-K"):
        return None
    items = ", ".join(f"{i} {ITEM_NAMES.get(i, '')}".strip() for i in f.items) or "no items"
    other = "SpaceX" if f.company == "Tesla" else "Tesla"
    if text is None:
        if "1.01" in f.items:
            return (PRIORITY_EMERGENCY,
                    f"filed an 8-K with a material definitive agreement that could "
                    f"not be read ({items})")
        return PRIORITY_NORMAL, f"filed an 8-K ({items})"
    names_other = any(name in text for name in COUNTERPART[f.company])
    if names_other and any(word in text for word in MERGER_WORDS):
        return PRIORITY_EMERGENCY, f"filed an 8-K naming {other} with merger language ({items})"
    if "1.01" in f.items:
        return (PRIORITY_LOUD,
                f"filed an 8-K with a material definitive agreement, not naming "
                f"{other} ({items})")
    return PRIORITY_NORMAL, f"filed an 8-K ({items})"


def filing_alerts(filings: List[Filing], state: dict, now: datetime,
                  read: Callable[[Filing], Optional[str]]) -> List[Alert]:
    alerts = []
    for f in filings:
        if f.accession in state["seen_filings"] or now - f.accepted > RECENT_FILING:
            continue
        needs_text = f.form.startswith("8-K")
        verdict = classify_filing(f, read(f) if needs_text else None)
        state["seen_filings"].append(f.accession)
        if verdict is None:
            continue
        priority, why = verdict
        when = f"{f.accepted.astimezone(ET):%a %m/%d %H:%M} ET"
        if priority == PRIORITY_EMERGENCY:
            text = f"{f.company.upper()} {why}. {when}. Check the news now."
        else:
            text = f"{f.company} {why}. {when}."
        alerts.append(Alert(priority, f"{text}\n{f.url}"))
    state["seen_filings"] = state["seen_filings"][-500:]
    return alerts


def blind_alerts(state: dict, now: datetime, enabled: List[str],
                 started: datetime) -> List[Alert]:
    alerts = []
    blind = []
    for source in enabled:
        ok = state["last_ok"].get(source)
        last = max(datetime.fromisoformat(ok), started) if ok else started
        if now - last > BLIND_AFTER:
            blind.append(source)
    newly = [s for s in blind if s not in state["blind"]]
    back = [s for s in state["blind"] if s not in blind]
    if newly:
        names = ", ".join(SOURCE_NAMES[s] for s in newly)
        alerts.append(Alert(PRIORITY_LOUD,
            f"Merger watch is BLIND: no answer from {names} for 30 minutes. "
            f"Check the PC's internet connection."))
    if back:
        names = ", ".join(SOURCE_NAMES[s] for s in back)
        alerts.append(Alert(PRIORITY_NORMAL, f"Merger watch can see again: {names}."))
    state["blind"] = blind
    return alerts


def summary_alert(state: dict, now: datetime, sec_on: bool,
                  coming: Optional[List[str]] = None) -> List[Alert]:
    today = f"{now:%Y-%m-%d}"
    if state["summary_date"] == today or now.time() < SUMMARY_AT:
        return []
    state["summary_date"] = today
    if now.time() > SUMMARY_LATEST:
        return []   # started late: tomorrow's comes on time
    lines = []
    for slug, history in state["odds"].items():
        if not history or slug not in state["labels"]:
            continue
        latest = history[-1][1]
        day_ago = [pct for when, pct in history
                   if now - datetime.fromisoformat(when) >= timedelta(hours=23)]
        change = f" ({latest - day_ago[-1]:+.0f} in 24h)" if day_ago else ""
        lines.append((_label_key(state["labels"][slug]),
                      f"by {state['labels'][slug]}: {latest:.0f}%{change}"))
    body = "\n".join(line for _, line in sorted(lines)) or "no odds read yet"
    if not sec_on:
        body += "\nSEC check is OFF: add SEC_CONTACT to .env."
    if coming:
        body += "\n\nComing up (14 days):\n" + "\n".join(coming)
    return [Alert(PRIORITY_SILENT, f"Merger odds tonight:\n{body}")]


def _label_key(label: str) -> datetime:
    return datetime.strptime(label, "%b %d, %Y")


# --------------------------------------------------------------- the I/O

def fetch(url: str, agent: str = USER_AGENT, limit: int = 4_000_000,
          headers: Optional[dict] = None) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": agent,
                                                   "Accept-Encoding": "identity",
                                                   **(headers or {})})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read(limit)


def sec_agent(contact: str) -> str:
    return f"SPCX merger watch {contact}"


def check_source(source: str, state: dict, now: datetime, contact: str) -> List[Alert]:
    if source == "polymarket":
        markets = parse_polymarket(fetch(POLYMARKET_API.format(slug=POLYMARKET_EVENT)))
        alerts = polymarket_alerts(markets, state, now)
        # A line on screen every check, so a glance at the window shows it
        # is alive. The log file keeps only what happened.
        print(f"{now:%H:%M} odds  " + "  ".join(
            f"{m.label[:6]} {m.yes_pct:.0f}%" for m in
            sorted(markets, key=lambda m: _label_key(m.label)) if not m.closed), flush=True)
    elif source == "halts":
        alerts = halt_alerts(parse_halts(fetch(HALTS_URL)), state, now)
    elif source == "calendar":
        alerts = calendar_check(state, now)
    elif source == "news":
        alerts = news_check(state, now)
    else:
        agent = sec_agent(contact)

        def read(f: Filing) -> Optional[str]:
            try:
                return plain_text(fetch(f.url, agent).decode("utf-8", "replace"))
            except (urllib.error.URLError, OSError) as exc:
                log(f"could not read {f.url} ({type(exc).__name__})")
                return None

        alerts = []
        for company, cik in COMPANIES.items():
            filings = parse_filings(fetch(SEC_SUBMISSIONS.format(cik=cik), agent), company)
            alerts += filing_alerts(filings, state, now, read)
    state["last_ok"][source] = _iso(now)
    return alerts


def calendar_check(state: dict, now: datetime) -> List[Alert]:
    """Hourly: Nasdaq's earnings estimate, and the news for new dates.
    Either failing leaves the other; the reminders themselves need neither."""
    try:
        estimate = calendar_watch.parse_nasdaq_estimate(fetch(
            calendar_watch.NASDAQ_ESTIMATE.format(symbol=calendar_watch.SYMBOL),
            agent="Mozilla/5.0"))
        state["earnings_estimate"] = estimate.isoformat() if estimate else None
    except Exception as exc:  # noqa: BLE001
        log(f"earnings estimate: {type(exc).__name__}")
    alerts = []
    first = not state["news_primed"]
    for query in calendar_watch.NEWS_QUERIES:
        try:
            items = calendar_watch.parse_news(fetch(
                calendar_watch.NEWS_SEARCH.format(query=query), agent="Mozilla/5.0"))
        except Exception as exc:  # noqa: BLE001
            log(f"news ({query}): {type(exc).__name__}")
            continue
        alerts += [Alert(PRIORITY_NORMAL, text) for text in
                   calendar_watch.news_alerts(items, state["news_seen"], now, first)]
    state["news_primed"] = True
    return alerts


_IBKR_NEWS: Optional["spcx_news.IbkrNews"] = None


def news_check(state: dict, now: datetime, get=None, ibkr=None) -> List[Alert]:
    """Every 10 minutes: SPCX headlines (see spcx_news.py). A headline that
    can move the stock makes a sound; the rest arrive silently. One source
    failing leaves the others; all failing counts as a failed check. The
    Alpaca source is skipped when .env has no Alpaca keys; IBKR's when IB
    Gateway is not answering (logged, like any source)."""
    global _IBKR_NEWS
    get = get or (lambda url, headers: fetch(url, agent="Mozilla/5.0", headers=headers))
    if ibkr is None:
        _IBKR_NEWS = _IBKR_NEWS or spcx_news.IbkrNews()
        ibkr = _IBKR_NEWS
    keys = spcx_news.alpaca_headers()
    items, tried, failed = [], 0, 0
    for name, url, parser, needs_keys in spcx_news.SOURCES:
        if needs_keys and keys is None:
            continue
        tried += 1
        try:
            items += parser(get(url, keys if needs_keys else None))
        except Exception as exc:  # noqa: BLE001 -- never the headers: they hold the keys
            failed += 1
            log(f"news ({name}): {type(exc).__name__}")
    tried += 1
    try:
        items += ibkr.headlines()
    except Exception as exc:  # noqa: BLE001
        failed += 1
        log(f"news (IBKR): {type(exc).__name__}")
    if failed == tried:
        raise RuntimeError("no news source answered")
    return [Alert(PRIORITY_NORMAL if big else PRIORITY_SILENT, text)
            for big, text in spcx_news.check(items, state, now)]


def calendar_events(state: dict, now: datetime) -> List["calendar_watch.Event"]:
    raw = state.get("earnings_estimate")
    estimate = date.fromisoformat(raw) if raw else None
    return calendar_watch.all_events(now.date(), estimate)


def calendar_reminders(events: list, state: dict, now: datetime) -> List[Alert]:
    out = [Alert(PRIORITY_LOUD if r.loud else PRIORITY_NORMAL, f"CALENDAR: {r.text}")
           for r in calendar_watch.due(events, state["cal_sent"], now)]
    if len(state["cal_sent"]) > 500:
        for key in sorted(state["cal_sent"], key=state["cal_sent"].get)[:-500]:
            del state["cal_sent"][key]
    return out


def deliver(alerts: List[Alert], state: dict, now: datetime, dry_run: bool) -> None:
    for alert in alerts:
        alert.created = _iso(now)
        state["pending"].append([alert.priority, alert.message, alert.created])
    still = []
    for priority, message, created in state["pending"]:
        if now - datetime.fromisoformat(created) > PENDING_KEEP:
            log(f"gave up sending (Pushover unreachable for 2 hours): {message[:80]}")
            continue
        level = {PRIORITY_EMERGENCY: "EMERGENCY", PRIORITY_LOUD: "LOUD",
                 PRIORITY_NORMAL: "normal", PRIORITY_SILENT: "silent"}[priority]
        if dry_run:
            log(f"[dry run] {level}: {message}")
            continue
        error = send_pushover(message, title=TITLE, priority=priority)
        if error:
            log(f"not sent yet ({error}); will retry: {message[:80]}")
            still.append([priority, message, created])
        else:
            log(f"sent {level}: {message}")
    state["pending"] = still


def watch(dry_run: bool, once: bool, state_path: str) -> int:
    contact = os.getenv("SEC_CONTACT", "").strip()
    enabled = ["halts", "polymarket"] + (["sec"] if contact else []) + ["calendar", "news"]
    # The calendar is checked hourly, so it is not held to the 30-minute
    # blind rule; its reminders run from files and need no network at all.
    # News is a convenience: a news site that stops answering is logged,
    # not rung, or a slow night at Google would wake the phone.
    watched = [s for s in enabled if s not in ("calendar", "news")]
    state = load_state(state_path)
    started = datetime.now(ET)
    watch = None
    if not dry_run and not once:
        # One copy only, and an eye on the SPCX alarms. See heartbeat.py.
        running = heartbeat.already_running(heartbeat.MERGER_WATCH, started)
        if running:
            log(running)
            return 0
        watch = heartbeat.Watch(heartbeat.MERGER_WATCH, heartbeat.SPCX_ALARMS, started)
    log(f"merger watch started{' (dry run)' if dry_run else ''}: "
        + ", ".join(SOURCE_NAMES[s] for s in enabled))
    if not contact:
        log("SEC check is OFF: add SEC_CONTACT=your@email to .env to turn it on")
    if not once:
        deliver([Alert(PRIORITY_SILENT, "Merger watch started: "
                       + ", ".join(SOURCE_NAMES[s] for s in enabled))],
                state, started, dry_run)
    due = {s: 0.0 for s in enabled}
    last_cal_minute = -1
    events: list = []
    while True:
        now = datetime.now(ET)
        alerts: List[Alert] = []
        for source in enabled:
            if time_mod.monotonic() < due[source]:
                continue
            due[source] = time_mod.monotonic() + INTERVALS[source]
            try:
                alerts += check_source(source, state, now, contact)
            except Exception as exc:  # noqa: BLE001 -- one source failing must not stop the rest
                log(f"{SOURCE_NAMES[source]}: {type(exc).__name__}: {str(exc)[:120]}")
        if watch is not None:
            alerts += [Alert(PRIORITY_LOUD if loud else PRIORITY_NORMAL, text)
                       for loud, text in watch.beat(now)]
        if not once:
            if now.minute != last_cal_minute:      # once a minute is plenty
                last_cal_minute = now.minute
                events = calendar_events(state, now)
                alerts += calendar_reminders(events, state, now)
                morning = spcx_news.digest(state, now)
                if morning:
                    alerts.append(Alert(PRIORITY_NORMAL, morning))
            alerts += blind_alerts(state, now, watched, started)
            alerts += summary_alert(state, now, bool(contact),
                                    calendar_watch.coming_up(events, now.date()))
        deliver(alerts, state, now, dry_run)
        save_state(state, state_path)
        if once:
            return 0
        time_mod.sleep(5)


def status() -> int:
    contact = os.getenv("SEC_CONTACT", "").strip()
    try:
        markets = parse_polymarket(fetch(POLYMARKET_API.format(slug=POLYMARKET_EVENT)))
        print("Polymarket: Tesla-SpaceX merger officially announced by...")
        for m in sorted(markets, key=lambda m: _label_key(m.label)):
            print(f"  {m.label:<14} {m.yes_pct:5.1f}%{'   (closed)' if m.closed else ''}")
    except Exception as exc:  # noqa: BLE001
        print(f"Polymarket: could not read ({type(exc).__name__})")
    try:
        halts = [h for h in parse_halts(fetch(HALTS_URL)) if h.symbol in HALT_SYMBOLS]
        print("\nNasdaq halts for " + "/".join(HALT_SYMBOLS) + ": "
              + ("none listed" if not halts else ""))
        for h in halts:
            print(f"  {h.symbol} {h.when:%m/%d %H:%M} ET  {h.code}")
    except Exception as exc:  # noqa: BLE001
        print(f"\nNasdaq halts: could not read ({type(exc).__name__})")
    if not contact:
        print("\nSEC filings: OFF -- add SEC_CONTACT=your@email to .env")
        return 0
    for company, cik in COMPANIES.items():
        try:
            filings = parse_filings(fetch(SEC_SUBMISSIONS.format(cik=cik), sec_agent(contact)),
                                    company)
        except Exception as exc:  # noqa: BLE001
            print(f"\n{company} filings: could not read ({type(exc).__name__})")
            continue
        print(f"\n{company} latest filings:")
        for f in filings[:5]:
            print(f"  {f.accepted.astimezone(ET):%m/%d %H:%M} ET  {f.form:<8} "
                  f"{','.join(f.items)}")
    return 0


def test_push() -> int:
    error = send_pushover("Merger watch test. If you can read this, merger alarms "
                          "will reach this phone.", title=TITLE, priority=PRIORITY_LOUD)
    print("sent" if not error else f"NOT sent: {error}")
    return 0 if not error else 1


# ------------------------------------------------------------- self-test

def self_test() -> int:
    failures: List[str] = []
    t0 = datetime(2026, 10, 18, 12, 0, tzinfo=ET)

    def market(slug, pct, closed=False, label="Jan 31, 2027"):
        return Market(slug, label, pct, closed)

    # Polymarket: the year comes from the end date, Yes from its position.
    payload = json.dumps([{"markets": [
        {"slug": "dec", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.285", "0.715"]',
         "endDate": "2027-01-01T04:59:00Z", "closed": False},
        {"slug": "rev", "outcomes": '["No", "Yes"]', "outcomePrices": '["0.6", "0.4"]',
         "endDate": "2027-07-01T03:59:00Z", "closed": True},
    ]}]).encode()
    parsed = parse_polymarket(payload)
    if [(m.label, m.yes_pct, m.closed) for m in parsed] != [
            ("Dec 31, 2026", 28.5, False), ("Jun 30, 2027", 40.0, True)]:
        failures.append(f"Polymarket parsed wrong: {parsed}")

    # A 15-point day jump rings once, at normal priority; a smaller wobble
    # does not; another full step inside the quiet period rings again.
    s = new_state()
    polymarket_alerts([market("m", 30)], s, t0)
    if polymarket_alerts([market("m", 44)], s, t0 + timedelta(hours=20)):
        failures.append("a 14-point day move should not ring")
    a = polymarket_alerts([market("m", 46)], s, t0 + timedelta(hours=21))
    if len(a) != 1 or a[0].priority != PRIORITY_NORMAL or "UP 16" not in a[0].message:
        failures.append(f"a 16-point day jump should ring once, normal: {a}")
    if polymarket_alerts([market("m", 50)], s, t0 + timedelta(hours=22)):
        failures.append("a smaller follow-on move inside the quiet period should not ring")
    if not polymarket_alerts([market("m", 62)], s, t0 + timedelta(hours=23)):
        failures.append("another 15 points inside the quiet period should ring")

    s = new_state()
    polymarket_alerts([market("h", 50)], s, t0)
    a = polymarket_alerts([market("h", 39)], s, t0 + timedelta(minutes=40))
    if len(a) != 1 or "DOWN 11" not in a[0].message or "an hour" not in a[0].message:
        failures.append(f"an 11-point drop within the hour should ring: {a}")
    # 24 Oct's whipsaw: 28 -> 20 -> 26 -> 18 over three hours, nothing rings.
    s = new_state()
    for minutes, pct in ((0, 28), (35, 20), (60, 26), (165, 18)):
        if polymarket_alerts([market("w", pct)], s, t0 + timedelta(minutes=minutes)):
            failures.append(f"5-8 point back-and-forth must stay quiet (at {pct}%)")
    if polymarket_alerts([market("new", 80)], new_state(), t0):
        failures.append("the first reading has nothing to jump from")

    s = new_state()
    a = polymarket_alerts([market("c", 91)], s, t0)
    if len(a) != 1 or a[0].priority != PRIORITY_EMERGENCY:
        failures.append("90%+ should be an emergency")
    if polymarket_alerts([market("c", 93)], s, t0 + timedelta(minutes=5)):
        failures.append("the 90% emergency should ring once")

    s = new_state()
    if polymarket_alerts([market("old", 0.1, closed=True)], s, t0):
        failures.append("an old deadline that closed before we watched must stay quiet")
    polymarket_alerts([market("d", 20)], s, t0)
    a = polymarket_alerts([market("d", 0.1, closed=True)], s, t0 + timedelta(days=1))
    if len(a) != 1 or a[0].priority != PRIORITY_NORMAL:
        failures.append(f"a watched deadline passing should be one normal note: {a}")
    a = polymarket_alerts([market("y", 100, closed=True)], new_state(), t0)
    if len(a) != 1 or a[0].priority != PRIORITY_EMERGENCY or "SETTLED YES" not in a[0].message:
        failures.append("a market settled Yes is an emergency, watched before or not")

    # Halts: the feed starts with a byte-order mark; only our symbols, only
    # recent, only once; news halts are emergencies.
    rss = ("﻿<?xml version='1.0' encoding='utf-8'?><rss xmlns:ndaq="
           "'http://www.nasdaqtrader.com/'><channel>"
           + "".join(f"<item><ndaq:HaltDate>{d}</ndaq:HaltDate><ndaq:HaltTime>{t}"
                     f"</ndaq:HaltTime><ndaq:IssueSymbol>{sym}</ndaq:IssueSymbol>"
                     f"<ndaq:IssueName>x</ndaq:IssueName><ndaq:ReasonCode>{c}"
                     f"</ndaq:ReasonCode></item>"
                     for d, t, sym, c in [("10/18/2026", "11:30:00.000", "SPCX", "T1"),
                                          ("10/18/2026", "11:31:00.000", "OBAI", "T1"),
                                          ("10/18/2026", "11:40:00.000", "TSLA", "LUDP"),
                                          ("10/16/2026", "15:00:00.000", "SPCX", "T1")])
           + "</channel></rss>").encode("utf-8")
    halts = parse_halts(rss)
    if len(halts) != 4 or halts[0].when != datetime(2026, 10, 18, 11, 30, tzinfo=ET):
        failures.append(f"halts parsed wrong: {halts}")
    s = new_state()
    a = halt_alerts(halts, s, t0)
    if [x.priority for x in a] != [PRIORITY_EMERGENCY, PRIORITY_LOUD]:
        failures.append(f"SPCX T1 should be an emergency, TSLA LUDP loud, the rest "
                        f"ignored: {[x.message for x in a]}")
    if halt_alerts(halts, s, t0 + timedelta(minutes=1)):
        failures.append("a halt should ring once")

    # Filings: merger forms are emergencies without reading anything; an
    # 8-K is judged on its text; old filings and Form 4s stay quiet.
    def filing(form, items=(), hours_ago=1, acc="1", company="Tesla"):
        return Filing(company, COMPANIES[company], form,
                      t0 - timedelta(hours=hours_ago), acc, tuple(items), "d.htm")

    texts = {"a": "tesla and spacex entered into an agreement and plan of merger",
             "b": "credit agreement with a syndicate of banks",
             "c": "quarterly results"}
    reads: List[str] = []

    def read(f: Filing) -> Optional[str]:
        reads.append(f.accession)
        return texts.get(f.accession)

    s = new_state()
    batch = [filing("425", acc="m"), filing("8-K", ["1.01", "9.01"], acc="a"),
             filing("8-K", ["1.01"], acc="b"), filing("8-K", ["2.02"], acc="c"),
             filing("8-K", ["1.01"], acc="u"), filing("4", acc="f4"),
             filing("8-K", ["1.01"], hours_ago=30, acc="old"),
             filing("8-K", ["8.01"], acc="x", company="SpaceX")]
    texts["x"] = "spacex announced a business combination with tesla"
    a = filing_alerts(batch, s, t0, read)
    got = [x.priority for x in a]
    want = [PRIORITY_EMERGENCY, PRIORITY_EMERGENCY, PRIORITY_LOUD, PRIORITY_NORMAL,
            PRIORITY_EMERGENCY, PRIORITY_EMERGENCY]
    if got != want:
        failures.append(f"filing priorities {got}, expected {want}: "
                        + " | ".join(x.message for x in a))
    if "m" in reads or "f4" in reads or "old" in reads:
        failures.append(f"only new 8-Ks should be fetched, read {reads}")
    if filing_alerts(batch, s, t0 + timedelta(minutes=2), read):
        failures.append("a filing should ring once")
    if "/1318605/m/d.htm" not in batch[0].url:
        failures.append(f"filing link is wrong: {batch[0].url}")
    filings = parse_filings(json.dumps({"filings": {"recent": {
        "form": ["8-K"], "acceptanceDateTime": ["2026-10-02T13:04:26.000Z"],
        "accessionNumber": ["0001628280-26-064366"], "items": ["2.02,9.01"],
        "primaryDocument": ["tsla-20261002.htm"]}}}).encode(), "Tesla")
    if (filings[0].items != ("2.02", "9.01")
            or filings[0].accepted != datetime(2026, 10, 2, 13, 4, 26, tzinfo=timezone.utc)
            or "/000162828026064366/" not in filings[0].url):
        failures.append(f"SEC filings parsed wrong: {filings}")

    # Blind: one loud alert after 30 minutes, one note on recovery.
    s = new_state()
    s["last_ok"] = {"halts": _iso(t0), "polymarket": _iso(t0 + timedelta(minutes=25))}
    if blind_alerts(s, t0 + timedelta(minutes=29), ["halts", "polymarket"], t0):
        failures.append("29 minutes is not blind yet")
    a = blind_alerts(s, t0 + timedelta(minutes=31), ["halts", "polymarket"], t0)
    if len(a) != 1 or "Nasdaq halts" not in a[0].message or "Polymarket" in a[0].message:
        failures.append(f"only the silent source should be reported blind: {a}")
    if blind_alerts(s, t0 + timedelta(minutes=40), ["halts", "polymarket"], t0):
        failures.append("blind should be reported once")
    s["last_ok"]["halts"] = _iso(t0 + timedelta(minutes=41))
    a = blind_alerts(s, t0 + timedelta(minutes=41), ["halts", "polymarket"], t0)
    if len(a) != 1 or a[0].priority != PRIORITY_NORMAL:
        failures.append("recovery should be one normal note")

    # Summary: once a day at 19:45, silent; a late start skips the day.
    s = new_state()
    s["labels"] = {"m": "Jan 31, 2027"}
    s["odds"] = {"m": [[_iso(t0), 30.0], [_iso(t0 + timedelta(hours=7, minutes=50)), 38.0]]}
    evening = t0 + timedelta(hours=7, minutes=50)
    a = summary_alert(s, evening, True)
    if len(a) != 1 or a[0].priority != PRIORITY_SILENT or "38%" not in a[0].message:
        failures.append(f"the evening summary is one silent note: {a}")
    if summary_alert(s, evening + timedelta(minutes=5), True):
        failures.append("the summary should come once a day")
    if summary_alert(new_state(), t0 + timedelta(hours=11), True):
        failures.append("a watcher started at 23:00 should not send that day's summary")
    if summary_alert(new_state(), t0, True):
        failures.append("no summary before 19:45")

    # A dry run prints and queues nothing. Its log line goes nowhere, so
    # the self-test leaves no file behind.
    global LOG_PATH
    saved_log, LOG_PATH = LOG_PATH, os.devnull
    try:
        s = new_state()
        deliver([Alert(PRIORITY_LOUD, "dry-run check")], s, t0, dry_run=True)
        if s["pending"]:
            failures.append("a dry run should not queue anything")
    finally:
        LOG_PATH = saved_log

    # Read-only, structurally.
    source = open(__file__, encoding="utf-8").read()
    for forbidden in ("TradingClient", "submit_order", "clob.polymarket", "post_order"):
        if forbidden in source.replace(f'"{forbidden}"', ""):
            failures.append(f"this file must not mention {forbidden}")

    # SPCX news: big headlines make a sound, the rest are silent; one
    # feed down is fine, both down is a failed check.
    feed = (b"<rss><channel><item><title>SpaceX prices share offering - Reuters</title>"
            b"<link>l1</link><pubDate>Fri, 23 Oct 2026 12:00:00 GMT</pubDate>"
            b"<source>Reuters</source></item><item><title>Starlink adds airline</title>"
            b"<link>l2</link><pubDate>Fri, 23 Oct 2026 12:05:00 GMT</pubDate></item>"
            b"</channel></rss>")
    news_now = datetime(2026, 10, 23, 8, 30, tzinfo=ET)
    s = new_state()
    s["spcx_news_primed"] = True

    asked = []

    class FakeIbkr:
        def __init__(self, items=(), down=False):
            self.items, self.down, self.asked = list(items), down, 0

        def headlines(self):
            self.asked += 1
            if self.down:
                raise ConnectionRefusedError("gateway down")
            return self.items

    gateway_down = FakeIbkr(down=True)

    def one_down(url, headers):
        asked.append((url, headers))
        if "yahoo" in url:
            raise OSError("down")
        if "alpaca" in url:
            return b'{"news": []}'
        return feed

    saved_log, LOG_PATH = LOG_PATH, os.devnull
    saved_keys = {k: os.environ.pop(k, None) for k in
                  ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_API_SECRET")}
    try:
        got = news_check(s, news_now, get=one_down, ibkr=gateway_down)
        if [a.priority for a in got] != [PRIORITY_NORMAL, PRIORITY_SILENT]:
            failures.append(f"an offering rings, a plain headline is silent: {got}")
        if any("alpaca" in u for u, _ in asked) or any(h for _, h in asked):
            failures.append(f"without Alpaca keys, Alpaca is not asked: {asked}")
        try:
            news_check(s, news_now, ibkr=gateway_down,
                       get=lambda url, headers: (_ for _ in ()).throw(OSError("down")))
            failures.append("without Alpaca keys, both feeds down is a failed check")
        except RuntimeError:
            pass
        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"] = "id", "sec"
        asked.clear()
        news_check(s, news_now, get=one_down, ibkr=gateway_down)
        sent = {u: h for u, h in asked}
        if sent.get(spcx_news.ALPACA_NEWS, {}).get("APCA-API-KEY-ID") != "id" or \
                any(h for u, h in sent.items() if "alpaca" not in u):
            failures.append(f"the keys go to Alpaca and nowhere else: {asked}")
        try:
            news_check(s, news_now, ibkr=gateway_down,
                       get=lambda url, headers: (_ for _ in ()).throw(OSError("down")))
            failures.append("every news source down must count as a failed check")
        except RuntimeError:
            pass
        # IB Gateway up, every web feed down: IBKR alone carries the check.
        # (a different topic from the offering above, which rang already)
        dj = spcx_news.Headline("Analyst downgrades SpaceX", "", "Dow Jones",
                                datetime(2026, 10, 23, 12, 20, tzinfo=timezone.utc), tagged=True)
        got = news_check(s, news_now, ibkr=FakeIbkr([dj]),
                         get=lambda url, headers: (_ for _ in ()).throw(OSError("down")))
        if [a.priority for a in got] != [PRIORITY_NORMAL] or "Dow Jones" not in got[0].message:
            failures.append(f"IBKR's headline arrives when the web feeds are down: {got}")
        if gateway_down.asked < 3:
            failures.append("IBKR is asked on every check")
    finally:
        LOG_PATH = saved_log
        for k, v in saved_keys.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    if spcx_news.self_test.__module__ != "spcx_news":
        failures.append("spcx_news.py is missing")

    import inspect
    if "spcx_news.digest(" not in inspect.getsource(watch):
        failures.append("the watch loop must send the 07:00 news list")
    if "calendar_reminders(" not in inspect.getsource(watch):
        failures.append("the watch loop must send the calendar's reminders")
    if calendar_watch.self_test.__module__ != "calendar_watch":
        failures.append("calendar_watch.py is missing")
    if "watch.beat(" not in inspect.getsource(watch):
        failures.append("the watch loop must beat the heartbeat")
    for fn in (main,):
        if "load_env()" not in inspect.getsource(fn):
            failures.append(f"{fn.__name__}() must load .env before reading a key")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("self-test passed")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Watch for a Tesla-SpaceX merger. Read-only.")
    parser.add_argument("--watch", action="store_true", help="run forever")
    parser.add_argument("--once", action="store_true", help="one pass over every source")
    parser.add_argument("--status", action="store_true", help="print the odds and filings now")
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    parser.add_argument("--test-push", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--state", default=STATE_PATH)
    args = parser.parse_args()

    load_env()
    if args.self_test:
        return self_test()
    if args.test_push:
        return test_push()
    if args.watch or args.once:
        # A dry run keeps its own memory, so rehearsing never marks a real
        # alert as already sent.
        state = f"{args.state}.dry" if args.dry_run else args.state
        try:
            return watch(args.dry_run, args.once, state)
        except KeyboardInterrupt:
            log("merger watch stopped")
            return 0
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
