"""
spcx_news.py -- SPCX headlines to the phone, day and night (2026-10-23).

Market Scanner reads news from about 8:00 AM to 4:35 PM, so anything that
broke overnight waited until 8 AM to show up. The merger watch runs 24/7,
so it now also reads, every 10 minutes:

  Google News   "SpaceX OR SPCX", the last day
  Yahoo Finance the SPCX headline feed

and sends:

  SILENT   each new headline, at most 6 a check (the rest wait for the
           digest) -- it sits in the tray to be read when you look
  NORMAL   a headline with a word that can move the stock at the open:
           offering, downgrade, halt, lawsuit, unlock, guidance...
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
             "buyback", "stock split", "resigns", "steps down", "recall")
MERGER_WORDS = ("merger", "merge", "merging", "combine", "combination")
MERGER_MOVES = ("talks", "deal", "agreement", "agree", "agreed", "announce",
                "announces", "announced", "review", "board", "vote", "approve",
                "approves", "approved", "special committee", "terms", "endorse",
                "endorsement", "endorses")
PER_CHECK = 6
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


def check(items: List[Headline], state: dict, now: datetime) -> List[Tuple[bool, str]]:
    """New headlines as (big, message). Remembers every one it sees, and
    keeps the recent ones for the morning digest."""
    seen: List[str] = state.setdefault("spcx_news_seen", [])
    recent: list = state.setdefault("spcx_news", [])
    first = not state.get("spcx_news_primed", False)
    seen_set = set(seen)
    out: List[Tuple[bool, str]] = []
    held = 0
    for h in sorted(items, key=lambda h: h.when or now):
        k = key(h.title)
        if not k or k in seen_set:
            continue
        seen.append(k)
        seen_set.add(k)
        if first or not about_company(h.title):
            continue
        if h.when is None or now - h.when > FRESH:
            continue
        word = big_word(h.title)
        recent.append([now.isoformat(), h.title, h.link, h.source, word or ""])
        stamp = _clock(h.when, now.tzinfo)
        by = f"{h.source}, " if h.source else ""
        if word:
            out.append((True, f"SPCX NEWS ({word}): {h.title} ({by}{stamp})\n{h.link}"))
        elif len([1 for big, _ in out if not big]) < PER_CHECK:
            out.append((False, f"SPCX news: {h.title} ({by}{stamp})\n{h.link}"))
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
    for title in ("SpaceX-Tesla Merger: 7 Tie-Ups Musk Named",
                  "Why a Merger With Tesla Could Be a Disaster for SpaceX",
                  "3 Hurdles Could Complicate a Tesla and SpaceX Merger",
                  "SpaceX signs launch deal with Tesla supplier",
                  "Merger talks at Boeing"):
        ok(big_word(title) is None, f"not merger news for us: {title}")
    ok(big_word("Dr. Faaris on SpaceX") is None, "whole words only")

    now = datetime(2026, 10, 23, 4, 0, tzinfo=et)            # Friday, 4 AM
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
    later = datetime(2026, 10, 23, 4, 20, tzinfo=et)
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

    # The 07:00 digest.
    ok(digest(state, datetime(2026, 10, 23, 6, 59, tzinfo=et)) is None, "not before 7")
    text = digest(state, datetime(2026, 10, 23, 7, 0, tzinfo=et))
    ok(text is not None and text.startswith("SPCX news since Thu 4 PM (11,"),
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

    now = datetime.now(ZoneInfo("America/New_York"))
    for url in FEEDS:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            items = parse(response.read(4_000_000))
        print(f"\n{url[:60]}... {len(items)} headlines")
        for h in sorted(items, key=lambda h: h.when or now, reverse=True)[:15]:
            if about_company(h.title):
                word = big_word(h.title)
                print(f"  {_clock(h.when, now.tzinfo) if h.when else '?':>12}  "
                      f"{'! ' + word + ': ' if word else ''}{h.title[:100]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
