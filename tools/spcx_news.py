"""
spcx_news.py -- SPCX headlines to the phone, day and night (2026-10-23).

Market Scanner reads news from about 8:00 AM to 4:35 PM, so anything that
broke overnight waited until 8 AM to show up. The merger watch runs 24/7,
so it now also reads, every 10 minutes:

  Google News   "SpaceX OR SPCX", the last day
  Yahoo Finance the SPCX headline feed
  Alpaca        Benzinga's newswire, every story tagged SPCX -- often
                minutes ahead of the other two. Uses the ALPACA_API_KEY
                and ALPACA_SECRET_KEY already in .env; skipped without them.
  IBKR          Dow Jones and Briefing.com (analyst actions), through the
                IB Gateway already running -- a READ-ONLY connection on
                its own client id, 7707. Gateway down: skipped quietly.

and sends:

  SILENT   each new headline, at most 6 a check (the rest wait for the
           digest) -- it sits in the tray to be read when you look
  NORMAL   a headline with a word that can move the stock at the open:
           offering, downgrade, halt, lawsuit, unlock, guidance...
           One sound per topic every 2 hours, and none 10 PM - 7 AM: the
           rest of those arrive silently, still marked SPCX NEWS (24 Oct:
           17 sounds in a day, most of them one story from five outlets).
  NORMAL   07:00 ET, weekdays: everything since the last regular close
           (Friday 4 PM for a Monday), the big ones first

The same story from two feeds counts once (titles are compared with the
publisher's name taken off). The first check after a restart only learns
what is already out, so a restart never sends a backlog.

Read-only: public RSS feeds, no account. Imported by merger_watch.py.

    python spcx_news.py              # the headlines it would see now
    python spcx_news.py --self-test  # check the rules, no network
"""

from __future__ import annotations

import json
import os
import re
import sys
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from email.utils import parsedate_to_datetime
from typing import List, Optional, Tuple

FEEDS = (
    "https://news.google.com/rss/search?q=SpaceX+OR+SPCX+when:1d"
    "&hl=en-US&gl=US&ceid=US:en",
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPCX&region=US&lang=en-US",
)
ALPACA_NEWS = ("https://data.alpaca.markets/v1beta1/news?symbols=SPCX"
               "&limit=50&sort=desc")
SYMBOL = "SPCX"
#: Only headlines that are about the company.
ABOUT = ("spacex", "spcx", "starlink", "starship")
#: Words that can move the stock at the open. Matched as whole words.
#: "merger" alone is not here: opinion pieces about a Tesla merger run
#: daily. A merger headline is big when it also says talks, a deal, a
#: review or a vote -- see MERGER_MOVES.
BIG_WORDS = ("offering", "secondary", "dilution", "downgrade", "downgrades",
             "upgrade", "upgrades", "price target", "halt", "halted", "lawsuit",
             "sues", "sued", "probe", "investigation", "subpoena", "sec charges",
             "guidance", "unlock", "lockup", "lock-up", "explodes", "explosion",
             "anomaly", "failure", "grounded", "faa", "merger agreement",
             "agree to merge", "agreed to merge", "to acquire", "acquisition",
             "buyback", "stock split", "resigns", "steps down", "recall",
             # Borrowing can move the stock like an offering (23 Oct:
             # "SpaceX $40B debt deal gets pricey").
             "debt deal", "bond", "bonds", "convertible")
MERGER_WORDS = ("merger", "merge", "merging", "combine", "combination", "tie-up", "tie up")
MERGER_MOVES = ("talks", "deal", "agreement", "agree", "agreed", "announce",
                "announces", "announced", "review", "board", "vote", "approve",
                "approves", "approved", "special committee", "terms", "endorse",
                "endorsement", "endorses",
                # Missed on 23 Oct: "Musk's Approval Required in Potential
                # Merger", "Sign-Off For Any Tesla Tie-Up", "merger could
                # face antitrust scrutiny".
                "approval", "sign-off", "antitrust", "scrutiny")
PER_CHECK = 6
#: A topic ("merger talks", "upgrade") makes a sound at most this often.
TOPIC_EVERY = timedelta(hours=2)
#: No news sounds overnight; the 07:00 list carries what came in.
QUIET_FROM, QUIET_UNTIL = time(22, 0), time(7, 0)
FRESH = timedelta(hours=12)
KEEP = timedelta(days=4)
SEEN_KEEP = 1500
DIGEST_AT, DIGEST_LATEST = time(7, 0), time(9, 30)
DIGEST_LINES = 15
REGULAR_CLOSE = time(16, 0)


@dataclass
class Headline:
    title: str
    link: str
    source: str
    when: Optional[datetime]
    #: The feed itself tagged the story SPCX, so the title need not say so.
    tagged: bool = False


def parse(payload: bytes) -> List[Headline]:
    root = ElementTree.fromstring(payload)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        # Google appends " - Publisher" to every title.
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        try:
            when = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            when = None
        if title:
            out.append(Headline(title, (item.findtext("link") or "").strip(), source, when))
    return out


def parse_alpaca(payload: bytes) -> List[Headline]:
    """Alpaca's news: JSON, one entry per story, with the symbols it is about."""
    out = []
    for n in json.loads(payload).get("news", []):
        title = (n.get("headline") or "").strip()
        try:
            when = datetime.fromisoformat((n.get("created_at") or "").replace("Z", "+00:00"))
        except ValueError:
            when = None
        if title:
            out.append(Headline(title, (n.get("url") or "").strip(),
                                (n.get("source") or "").strip().capitalize(), when,
                                tagged=SYMBOL in (n.get("symbols") or [])))
    return out


def alpaca_headers() -> Optional[dict]:
    """The Alpaca key pair as request headers, or None if .env has none.
    Never printed or logged."""
    key_id = os.getenv("ALPACA_API_KEY", "").strip()
    secret = (os.getenv("ALPACA_SECRET_KEY", "").strip()
              or os.getenv("ALPACA_API_SECRET", "").strip())
    if not key_id or not secret:
        return None
    return {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}


IBKR_HOST = os.environ.get("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.environ.get("IBKR_PORT", "4001"))
IBKR_CLIENT_ID = int(os.environ.get("IBKR_NEWS_CLIENT_ID", "7707"))
#: IBKR's newest headlines per check. Asked with no time window: given
#: one, the 23 Oct probe got only 19-20 Oct back.
IBKR_LATEST = 50
IBKR_NAMES = {"DJ": "Dow Jones", "BRF": "Briefing.com"}


def _ibkr_title(headline: str) -> str:
    """'{A:800015:L:en}Musk Vows ... Share--5th Update' -> 'Musk Vows ... Share'.
    The code in braces is IBKR's; the update count would make each
    revision look like a new story; '-- WSJ' is the publisher, which
    other feeds leave off, so the same story compares equal."""
    title = re.sub(r"^\{[^}]*\}", "", headline or "").strip()
    title = re.sub(r"\s*--\s*\d+(st|nd|rd|th) Update$", "", title, flags=re.I)
    return re.sub(r"\s+--\s+[A-Za-z.&' ]{1,30}$", "", title).strip()


def parse_ibkr(items) -> List[Headline]:
    """IBKR historical-news rows (time, providerCode, articleId, headline).
    A time without a zone is taken as UTC."""
    from datetime import timezone

    out = []
    for n in items or []:
        title = _ibkr_title(getattr(n, "headline", ""))
        when = getattr(n, "time", None)
        if isinstance(when, datetime) and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        code = getattr(n, "providerCode", "") or ""
        source = next((v for k, v in IBKR_NAMES.items() if code.startswith(k)), code)
        if title:
            out.append(Headline(title, "", source, when if isinstance(when, datetime) else None,
                                tagged=True))
    return out


class IbkrNews:
    """SPCX headlines from every IBKR news provider this account has.

    One READ-ONLY connection, opened when first needed and again after it
    drops (IB Gateway restarts nightly). It asks for news and nothing else.
    """

    def __init__(self):
        self.ib = None
        self.codes = ""
        self.con_id = 0

    def headlines(self) -> List[Headline]:
        if self.ib is None or not self.ib.isConnected():
            self.close()
            from ib_async import IB, Stock

            ib = IB()
            ib.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID, timeout=10,
                       readonly=True)
            try:
                self.codes = "+".join(p.code for p in ib.reqNewsProviders())
                stock = Stock(SYMBOL, "SMART", "USD")
                if not self.codes or not [c for c in ib.qualifyContracts(stock) if c]:
                    raise RuntimeError("no IBKR news providers, or no SPCX contract")
                self.con_id = stock.conId
            except BaseException:
                ib.disconnect()
                raise
            self.ib = ib
        try:
            return parse_ibkr(self.ib.reqHistoricalNews(self.con_id, self.codes, "", "",
                                                        IBKR_LATEST))
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.ib is not None:
            try:
                self.ib.disconnect()
            except Exception:  # noqa: BLE001 -- closing a dead link is not news
                pass
        self.ib = None


#: (name, url, parser, needs the Alpaca keys)
SOURCES = (("Google News", FEEDS[0], parse, False),
           ("Yahoo Finance", FEEDS[1], parse, False),
           ("Alpaca/Benzinga", ALPACA_NEWS, parse_alpaca, True))


def key(title: str) -> str:
    """The same story's title from two feeds, made to compare equal."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", title.lower())).strip()[:70]


def about_company(title: str) -> bool:
    low = title.lower()
    return any(word in low for word in ABOUT)


def _has(low: str, word: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", low) is not None


def big_word(title: str) -> Optional[str]:
    low = title.lower()
    ours = "tesla" in low or "spacex" in low or "spcx" in low
    merger = any(_has(low, w) for w in MERGER_WORDS)
    # "SpaceX-Tesla deal" is the merger without the word; then "deal" alone
    # cannot also be the news in it.
    tesla_deal = "tesla" in low and _has(low, "deal")
    moves = [w for w in MERGER_MOVES if _has(low, w) and (merger or w != "deal")]
    if ours and (merger or tesla_deal) and moves:
        return "merger talks"
    for word in BIG_WORDS:
        if _has(low, word):
            return word
    return None


def last_close(now: datetime) -> datetime:
    """The most recent regular close before this morning: yesterday 4 PM,
    or Friday's for a Monday."""
    day = now.date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return datetime.combine(day, REGULAR_CLOSE, tzinfo=now.tzinfo)


def _clock(when: datetime, tz) -> str:
    return f"{when.astimezone(tz):%a %I:%M %p}".replace(" 0", " ")


def quiet_hours(now: datetime) -> bool:
    t = now.time()
    return t >= QUIET_FROM or t < QUIET_UNTIL


def topic(word: str) -> str:
    """'upgrades' and 'upgrade' are one topic; so are 'bonds' and 'bond'."""
    return word[:-1] if word.endswith("s") and not word.endswith("ss") else word


def check(items: List[Headline], state: dict, now: datetime) -> List[Tuple[bool, str]]:
    """New headlines as (big, message). Remembers every one it sees, and
    keeps the recent ones for the morning digest."""
    seen: List[str] = state.setdefault("spcx_news_seen", [])
    recent: list = state.setdefault("spcx_news", [])
    first = not state.get("spcx_news_primed", False)
    seen_set = set(seen)
    rang: dict = state.setdefault("spcx_news_rang", {})
    out: List[Tuple[bool, str]] = []
    held = plain = 0
    for h in sorted(items, key=lambda h: h.when or now):
        k = key(h.title)
        if not k or k in seen_set:
            continue
        seen.append(k)
        seen_set.add(k)
        if first or not (h.tagged or about_company(h.title)):
            continue
        if h.when is None or now - h.when > FRESH:
            continue
        word = big_word(h.title)
        recent.append([now.isoformat(), h.title, h.link, h.source, word or ""])
        stamp = _clock(h.when, now.tzinfo)
        by = f"{h.source}, " if h.source else ""
        link = f"\n{h.link}" if h.link else ""
        if word:
            last = rang.get(topic(word))
            sound = not quiet_hours(now) and (
                last is None or now - datetime.fromisoformat(last) >= TOPIC_EVERY)
            if sound:
                rang[topic(word)] = now.isoformat()
            out.append((sound, f"SPCX NEWS ({word}): {h.title} ({by}{stamp}){link}"))
        elif plain < PER_CHECK:
            plain += 1
            out.append((False, f"SPCX news: {h.title} ({by}{stamp}){link}"))
        else:
            held += 1
    if held:
        out.append((False, f"SPCX news: {held} more headline(s); they will be in the "
                           f"7:00 AM list."))
    state["spcx_news_primed"] = True
    del seen[:-SEEN_KEEP]
    state["spcx_news"] = [r for r in recent
                          if now - datetime.fromisoformat(r[0]) <= KEEP]
    return out


def digest(state: dict, now: datetime) -> Optional[str]:
    """07:00 on a weekday, once: the headlines since the last regular close."""
    if now.weekday() >= 5 or not DIGEST_AT <= now.time() <= DIGEST_LATEST:
        return None
    today = now.date().isoformat()
    if state.get("spcx_news_digest") == today:
        return None
    state["spcx_news_digest"] = today
    since = last_close(now)
    rows = [r for r in state.get("spcx_news", [])
            if datetime.fromisoformat(r[0]) >= since]
    head = f"SPCX news since {since:%a} 4 PM"
    if not rows:
        return f"{head}: no new headlines."
    rows.sort(key=lambda r: (not r[4], r[0]), reverse=False)
    big = [r for r in rows if r[4]]
    rest = [r for r in rows if not r[4]][::-1]          # newest first
    lines = [f"! {r[1]} ({r[3]})" if r[3] else f"! {r[1]}" for r in big]
    lines += [f"- {r[1]} ({r[3]})" if r[3] else f"- {r[1]}" for r in rest]
    more = len(lines) - DIGEST_LINES
    body = "\n".join(lines[:DIGEST_LINES]) + (f"\n...and {more} more" if more > 0 else "")
    return f"{head} ({len(rows)}, ! = can move the stock):\n{body}"


# ---------------------------------------------------------------- self-test


def self_test() -> int:
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    failures = []

    def ok(cond, msg):
        if not cond:
            failures.append(msg)

    google = b"""<rss><channel>
<item><title>SpaceX seeks 80,000 cell sites - Reuters</title><link>https://g/1</link>
<pubDate>Fri, 23 Oct 2026 06:00:00 GMT</pubDate><source>Reuters</source></item>
<item><title>SpaceX prices $5 billion share offering - Bloomberg.com</title><link>https://g/2</link>
<pubDate>Fri, 23 Oct 2026 07:00:00 GMT</pubDate><source>Bloomberg.com</source></item>
<item><title>Rocket Lab wins contract - CNBC</title><link>https://g/3</link>
<pubDate>Fri, 23 Oct 2026 07:30:00 GMT</pubDate><source>CNBC</source></item>
</channel></rss>"""
    items = parse(google)
    ok(items[0].title == "SpaceX seeks 80,000 cell sites" and items[0].source == "Reuters",
       f"the publisher comes off Google's titles: {items[0]}")
    ok(key("SpaceX seeks 80,000 cell sites!") == key("spacex  seeks 80,000 cell-sites"),
       "punctuation and case do not make a new story")
    ok(about_company("Starship flies again") and not about_company("Rocket Lab wins"),
       "only headlines about the company")
    ok(big_word("SpaceX prices share offering") == "offering", "offering is big")
    ok(big_word("Analyst downgrades SpaceX") == "downgrades", "a downgrade is big")
    ok(big_word("Why a merger with Tesla could be a disaster") is None,
       "merger talk alone is not big")
    ok(big_word("SpaceX hits new high") is None, "ordinary headlines are not big")
    for title in ("Musk's public endorsement of SpaceX-Tesla deal may throw a wrench in talks",
                  "What Is SpaceX (SPCX) Considering With A $3.5 Trillion Merger Review?",
                  "Elon Musk To Merge SpaceX, Tesla: A Look Into $3.5 Trillion Corporate Deal"):
        ok(big_word(title) == "merger talks", f"merger talks are big: {title}")
    for title in ("SpaceX CEO Elon Musk's Approval Required in Potential Merger With Tesla",
                  "SpaceX Flags Elon Musk's Sign-Off For Any Tesla Tie-Up",
                  "Exclusive / SpaceX-Tesla merger could face antitrust scrutiny",
                  "Tesla-SpaceX merger may trigger US antitrust review"):
        ok(big_word(title) == "merger talks", f"23 Oct's merger headlines are big: {title}")
    ok(big_word("'SpaceX $40B debt deal gets pricey as AI fears spread'- Semafor") == "debt deal",
       "a debt deal is big")
    ok(big_word("SpaceX prices convertible notes") == "convertible", "convertibles are big")
    ok(big_word("Bondi Beach launch party") is None, "whole words: Bondi is not a bond")
    for title in ("SpaceX-Tesla Merger: 7 Tie-Ups Musk Named",
                  "Why a Merger With Tesla Could Be a Disaster for SpaceX",
                  "3 Hurdles Could Complicate a Tesla and SpaceX Merger",
                  "SpaceX signs launch deal with Tesla supplier",
                  "Merger talks at Boeing"):
        ok(big_word(title) is None, f"not merger news for us: {title}")
    ok(big_word("Dr. Faaris on SpaceX") is None, "whole words only")

    now = datetime(2026, 10, 23, 10, 0, tzinfo=et)           # Friday, 10 AM
    state: dict = {}
    ok(check(items, state, now) == [], "the first check only learns what is out")
    newer = parse(b"""<rss><channel>
<item><title>SpaceX seeks 80,000 cell sites - Reuters</title><link>https://g/1</link>
<pubDate>Fri, 23 Oct 2026 06:00:00 GMT</pubDate><source>Reuters</source></item>
<item><title>SpaceX downgraded? No: SpaceX gets price target raise - Barron's</title>
<link>https://g/4</link><pubDate>Fri, 23 Oct 2026 08:10:00 GMT</pubDate>
<source>Barron's</source></item>
<item><title>Starlink adds airline</title><link>https://y/5</link>
<pubDate>Fri, 23 Oct 2026 08:05:00 +0000</pubDate></item>
<item><title>Old SpaceX story</title><link>https://y/6</link>
<pubDate>Wed, 21 Oct 2026 08:05:00 +0000</pubDate></item>
<item><title>Tesla deliveries beat</title><link>https://y/7</link>
<pubDate>Fri, 23 Oct 2026 08:05:00 +0000</pubDate></item>
</channel></rss>""")
    later = datetime(2026, 10, 23, 10, 20, tzinfo=et)
    said = check(newer, state, later)
    ok(len(said) == 2, f"two new SPCX stories, not the repeat, the old one or Tesla: {said}")
    ok(said[0] == (False, "SPCX news: Starlink adds airline (Fri 4:05 AM)\nhttps://y/5"),
       f"a plain headline is silent, with its time in New York: {said}")
    ok(said[1][0] and said[1][1].startswith("SPCX NEWS (price target): ")
       and "(Barron's, Fri 4:10 AM)" in said[1][1], f"a big one says why: {said}")
    ok(check(newer, state, later + timedelta(minutes=10)) == [], "nothing twice")

    flood = parse(b"<rss><channel>" + b"".join(
        b"<item><title>SpaceX story %d</title><link>l</link>"
        b"<pubDate>Fri, 23 Oct 2026 08:1%d:00 GMT</pubDate></item>" % (i, i)
        for i in range(9)) + b"</channel></rss>")
    said = check(flood, state, later + timedelta(minutes=20))
    ok(len(said) == PER_CHECK + 1 and "3 more headline(s)" in said[-1][1],
       f"at most {PER_CHECK} a check, the rest wait for the digest: {len(said)}")

    # Alpaca's JSON: a story tagged SPCX counts even if the title never says SpaceX.
    alpaca = json.dumps({"news": [
        {"headline": "Tower stocks rally on wireless report", "created_at": "2026-10-23T08:30:00Z",
         "url": "https://b/1", "source": "benzinga", "symbols": ["CCI", "SPCX"]},
        {"headline": "Tower stocks rally on wireless report", "created_at": "2026-10-23T08:30:00Z",
         "url": "https://b/1", "source": "benzinga", "symbols": ["CCI", "SPCX"]},
        {"headline": "Analyst downgrades AMT", "created_at": "2026-10-23T08:31:00Z",
         "url": "https://b/2", "source": "benzinga", "symbols": ["AMT"]},
        {"headline": "", "created_at": "bad", "symbols": ["SPCX"]}]}).encode()
    stories = parse_alpaca(alpaca)
    ok(len(stories) == 3 and stories[0].tagged and not stories[2].tagged
       and stories[0].source == "Benzinga" and stories[0].when.hour == 8,
       f"Alpaca's stories, tagged by symbol: {stories}")
    said = check(stories, state, later + timedelta(minutes=30))
    ok(said == [(False, "SPCX news: Tower stocks rally on wireless report "
                        "(Benzinga, Fri 4:30 AM)\nhttps://b/1")],
       f"a tagged story counts once; an untagged one about another stock does not: {said}")
    saved = {k: os.environ.pop(k, None) for k in
             ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_API_SECRET")}
    try:
        ok(alpaca_headers() is None, "no keys, no Alpaca")
        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_API_SECRET"] = "id", "sec"
        ok(alpaca_headers() == {"APCA-API-KEY-ID": "id", "APCA-API-SECRET-KEY": "sec"},
           "either name for the secret works")
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    # IBKR: codes, update counts and the publisher come off the title.
    from types import SimpleNamespace as NS

    rows = [NS(time=datetime(2026, 10, 23, 8, 45), providerCode="DJ-N", articleId="1",
               headline="{A:800015:L:en}SpaceX Weighs Merger Terms With Tesla -- WSJ"),
            NS(time=datetime(2026, 10, 23, 8, 46), providerCode="DJ-N", articleId="2",
               headline="{A:800015:L:en}SpaceX Weighs Merger Terms With Tesla--2nd Update"),
            NS(time=datetime(2026, 10, 23, 8, 47), providerCode="BRFUPDN", articleId="3",
               headline="SpaceX price target raised to $210 from $160 at JPMorgan"),
            NS(time=None, providerCode="XX", articleId="4", headline="{A:1}")]
    ib_items = parse_ibkr(rows)
    ok([h.title for h in ib_items] == ["SpaceX Weighs Merger Terms With Tesla"] * 2
       + ["SpaceX price target raised to $210 from $160 at JPMorgan"],
       f"IBKR titles cleaned: {[h.title for h in ib_items]}")
    ok(ib_items[0].source == "Dow Jones" and ib_items[2].source == "Briefing.com"
       and ib_items[0].when.tzinfo is not None and ib_items[0].tagged and not ib_items[0].link,
       f"IBKR rows: {ib_items}")
    said = check(ib_items, state, later + timedelta(minutes=40))
    ok(len(said) == 2 and said[0][1] == "SPCX NEWS (merger talks): SpaceX Weighs Merger "
       "Terms With Tesla (Dow Jones, Fri 4:45 AM)", f"an update is not a new story; no "
       f"empty link line: {said}")
    ok(said[1][1].startswith("SPCX NEWS (price target)") and said[1][0] is False,
       f"Briefing's analyst action is big, but a price target rang 20 minutes ago, "
       f"so it comes silently: {said}")

    # One sound per topic every 2 hours; none overnight.
    def big_item(title, mins):
        return Headline(title, "l", "", datetime(2026, 10, 23, 14, mins, tzinfo=timezone.utc),
                        tagged=True)
    from datetime import timezone

    st = {"spcx_news_primed": True}
    noon = datetime(2026, 10, 23, 12, 0, tzinfo=et)
    said = check([big_item("SpaceX-Tesla merger talks advance", 1),
                  big_item("SpaceX and Tesla boards in merger talks", 2),
                  big_item("Analyst upgrades SpaceX", 3)], st, noon)
    ok([s for s, _ in said] == [True, False, True],
       f"the second merger story in a check is silent, an upgrade still rings: {said}")
    said = check([big_item("Tesla merger talks: what we know", 4)], st,
                 noon + timedelta(hours=1, minutes=59))
    ok(said and said[0][0] is False and said[0][1].startswith("SPCX NEWS (merger talks)"),
       f"inside 2 hours: silent, still marked: {said}")
    said = check([big_item("Tesla merger talks resume", 5)], st, noon + timedelta(hours=2))
    ok(said and said[0][0] is True, f"after 2 hours the topic rings again: {said}")
    said = check([big_item("Another analyst upgrade for SpaceX", 6)], st,
                 noon + timedelta(hours=1))
    ok(said and said[0][0] is False, f"'upgrade' and 'upgrades' are one topic: {said}")
    def at(h, m):
        when = datetime(2026, 10, 24, h, m, tzinfo=et)
        item = Headline(f"SpaceX prices offering {h}{m}", "l", "", when - timedelta(minutes=10))
        return check([item], {"spcx_news_primed": True}, when)

    for h, m in ((22, 0), (3, 30), (6, 59)):
        quiet = at(h, m)
        ok(quiet and quiet[0][0] is False, f"no sound at {h}:{m:02d}: {quiet}")
    ok(at(7, 0)[0][0] is True and at(21, 59)[0][0] is True, "7:00 AM to 9:59 PM ring")
    ok(topic("upgrades") == "upgrade" and topic("bonds") == "bond" and topic("faa") == "faa",
       "topics")

    # The 07:00 digest.
    ok(digest(state, datetime(2026, 10, 23, 6, 59, tzinfo=et)) is None, "not before 7")
    text = digest(state, datetime(2026, 10, 23, 7, 0, tzinfo=et))
    ok(text is not None and text.startswith("SPCX news since Thu 4 PM (14,"),
       f"the digest counts everything since Thursday's close: {text}")
    ok(text.split("\n")[1].startswith("! SpaceX downgraded? No"), f"big ones first: {text}")
    ok(digest(state, datetime(2026, 10, 23, 7, 5, tzinfo=et)) is None, "once a day")
    ok(digest({}, datetime(2026, 10, 24, 7, 0, tzinfo=et)) is None, "no digest on Saturday")
    monday = digest({}, datetime(2026, 10, 26, 7, 0, tzinfo=et))
    ok(monday == "SPCX news since Fri 4 PM: no new headlines.", f"Monday looks back to Friday: {monday}")
    ok(last_close(datetime(2026, 10, 27, 7, 0, tzinfo=et)).day == 26, "Tuesday looks back to Monday")
    big_state = {"spcx_news": [[datetime(2026, 10, 23, 5, i, tzinfo=et).isoformat(),
                                f"SpaceX item {i}", "l", "", ""] for i in range(20)]}
    text = digest(big_state, datetime(2026, 10, 23, 7, 0, tzinfo=et))
    ok(text.endswith("...and 5 more") and "- SpaceX item 19" in text.split("\n")[1],
       f"15 lines, newest first, then a count: {text}")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("spcx_news self-test passed")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    import urllib.request
    from zoneinfo import ZoneInfo

    try:
        from feed_check import load_env
        load_env()
    except ImportError:
        pass
    now = datetime.now(ZoneInfo("America/New_York"))
    keys = alpaca_headers()
    for name, url, parser, needs_keys in SOURCES:
        if needs_keys and keys is None:
            print(f"\n{name}: skipped, no Alpaca keys in .env")
            continue
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                       **(keys if needs_keys else {})})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                items = parser(response.read(4_000_000))
        except Exception as exc:  # noqa: BLE001
            print(f"\n{name}: did not answer ({type(exc).__name__})")
            continue
        print(f"\n{name}: {len(items)} headlines")
        show(items, now)
    ibkr = IbkrNews()
    try:
        items = ibkr.headlines()
        print(f"\nIBKR (Dow Jones, Briefing.com): {len(items)} headlines")
        show(items, now)
    except Exception as exc:  # noqa: BLE001
        print(f"\nIBKR: not read ({type(exc).__name__}: {str(exc)[:80]})")
    finally:
        ibkr.close()
    return 0


def show(items: List[Headline], now: datetime) -> None:
    """The newest 15 about the company, '!' marking the ones that ring."""
    for h in sorted(items, key=lambda h: h.when or now, reverse=True)[:15]:
        if h.tagged or about_company(h.title):
            word = big_word(h.title)
            print(f"  {_clock(h.when, now.tzinfo) if h.when else '?':>12}  "
                  f"{'! ' + word + ': ' if word else ''}{h.title[:100]}")


if __name__ == "__main__":
    raise SystemExit(main())
