"""
patch_alpaca_news_sec_float.py -- Market Scanner without FMP: news from
Alpaca, float from the SEC (2026-10-23).

FMP is gone. Two things in Market Scanner still needed it, and both went
quiet without saying so, because each was wrapped in "only if there is an
FMP key":

  NEWS   fast_scan's stage 2 (the Live Feed's news, and catalyst
         confluence) ran only `if api_key:` and read FMP's market-wide
         news/stock-latest.
  FLOAT  top_gainers_scan's Float % column read FMP's shares-float, only
         `if api_key_fmp:`.

Four changes:

  1. src/collectors/alpaca_news.py gains fetch_market_news() and
     available(). Market-wide Benzinga news from Alpaca (the same keys
     already in .env), the last 30 minutes, up to 200 stories, turned
     into FMP's row shape -- symbol, title, publisher, site,
     publishedDate, url, text -- ONE ROW PER TAGGED SYMBOL, the link
     ending "#SYMBOL" so a story about three tracked stocks alerts all
     three (alerts are de-duplicated by link).
  2. src/main.py's news stage runs when Alpaca's keys are set, and reads
     fetch_market_news() instead of FMP.
  3. NEW src/collectors/sec_float.py: free float % APPROXIMATED from SEC
     filings -- public float ($, from the last annual report) divided by
     (that date's closing price from Alpaca x the shares outstanding in
     the same report). Once a day per symbol; SEC_CONTACT in .env names
     the reader, as the SEC asks.
  4. src/main.py's top_gainers_scan fills Float % from sec_float.

Every edited place is checked against the exact text it was written for;
a file that differs is refused and nothing at all is written. Backups are
timestamped, line endings kept, and if any patched file fails to compile
every file is put back and the new one removed. Run it again and it says
it is already done.

    cd C:\\Dev\\Market-Scanner
    python patch_alpaca_news_sec_float.py --check
    python patch_alpaca_news_sec_float.py
    python patch_alpaca_news_sec_float.py --self-test

Then restart Market Scanner's service. Nothing here can place an order.
"""

from __future__ import annotations

import py_compile
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

MARKER = "NEWS FROM ALPACA (2026-10-23)"

# ------------------------------------------------------- 1. alpaca_news.py

ALPACA_NEWS_NEEDS = (
    'BASE = "https://data.alpaca.markets/v1beta1/news"\n',
    "def _headers() -> dict:\n",
    "from datetime import datetime, timedelta, timezone\n",
    "from . import _retry\n",
)

ALPACA_NEWS_ADD = '''

# NEWS FROM ALPACA (2026-10-23) - market-wide news for fast_scan's stage 2,
# replacing FMP's news/stock-latest when FMP was dropped.
def available() -> bool:
    """Whether Alpaca's keys are set, so news can be read at all."""
    return bool(os.getenv("ALPACA_API_KEY") and os.getenv("ALPACA_SECRET_KEY"))


def fetch_market_news(minutes: int = 30, pages: int = 4) -> list[dict]:
    """
    MARKET-WIDE news (Benzinga, through Alpaca) from the last `minutes`,
    newest first, up to `pages` x 50 stories, in the row shape FMP's
    fetch_stock_news() returned - so main.py's fast_scan, signals/
    fast_scan.evaluate_news() and signals/catalyst.evaluate() read it
    unchanged:

        {'symbol':, 'title':, 'publisher':, 'site':, 'publishedDate':,
         'url':, 'text':}

    ONE ROW PER TAGGED SYMBOL. An Alpaca story names every ticker it
    touches; FMP gave one symbol per row. Alerts are de-duplicated by
    link, so each row's link ends '#SYMBOL' - the page still opens, and
    a story about three tracked stocks alerts all three instead of only
    the first. A roundup tagged with twenty tickers becomes twenty rows,
    and fast_scan keeps only those it is tracking, as it always did.
    """
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    params = {"start": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 50, "sort": "desc"}
    rows = []
    for _ in range(pages):
        resp = _retry.get(BASE, headers=_headers(), params=params, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        for a in body.get("news", []):
            url = a.get("url")
            source = a.get("source") or None
            for symbol in a.get("symbols") or []:
                rows.append({
                    "symbol": symbol,
                    "title": a.get("headline"),
                    "publisher": source.capitalize() if source else None,
                    "site": source,
                    "publishedDate": a.get("created_at"),
                    "url": f"{url}#{symbol}" if url else None,
                    "text": a.get("summary"),
                })
        token = body.get("next_page_token")
        if not token:
            break
        params = dict(params, page_token=token)
    return rows
'''

# --------------------------------------------------------------- 2. main.py

NEWS_GATE_OLD = ('    if api_key:\n'
                 '        cat_cfg = CONFIG.get("catalyst_confluence", {})\n')
NEWS_GATE_NEW = ('    # NEWS FROM ALPACA (2026-10-23): FMP was dropped. This stage used to\n'
                 '    # run only `if api_key:` (FMP\'s) and read FMP\'s news; it now runs\n'
                 '    # whenever Alpaca\'s keys are set and reads Alpaca\'s market-wide\n'
                 '    # news - see collectors/alpaca_news.fetch_market_news().\n'
                 '    if alpaca_news.available():\n'
                 '        cat_cfg = CONFIG.get("catalyst_confluence", {})\n')
NEWS_FETCH_OLD = "            articles = fmp.fetch_stock_news(api_key)\n"
NEWS_FETCH_NEW = "            articles = alpaca_news.fetch_market_news()\n"

FLOAT_OLD = '''    api_key_fmp = os.getenv("FMP_API_KEY")
    for symbol, sig in top_n:
        free_float_pct = None
        if api_key_fmp:
            try:
                free_float_pct = fmp.fetch_free_float_pct(symbol, api_key_fmp)
            except Exception as e:
                log.warning("top gainers scan: float fetch failed for %s: %s", symbol, e)
        sig["detail"]["free_float_pct"] = free_float_pct
'''
FLOAT_NEW = '''    # FLOAT FROM SEC (2026-10-23): FMP was dropped. free_float_pct is now
    # APPROXIMATED from SEC filings - see collectors/sec_float.py for what
    # that means (as of the last annual report; blank for new listings).
    from src.collectors import sec_float
    for symbol, sig in top_n:
        free_float_pct = None
        try:
            free_float_pct = sec_float.free_float_pct(symbol)
        except Exception as e:
            log.warning("top gainers scan: float fetch failed for %s: %s", symbol, e)
        sig["detail"]["free_float_pct"] = free_float_pct
'''

MAIN_EDITS = ((NEWS_GATE_OLD, NEWS_GATE_NEW, "fast_scan's news stage (`if api_key:`)"),
              (NEWS_FETCH_OLD, NEWS_FETCH_NEW, "fast_scan's FMP news call"),
              (FLOAT_OLD, FLOAT_NEW, "top_gainers_scan's FMP float lookup"))
MAIN_NEEDS = ("alpaca_news,", "def fast_scan(", "def top_gainers_scan(")

# --------------------------------------------------------- 3. sec_float.py

SEC_FLOAT = '''"""
sec_float.py - free float %, APPROXIMATED from SEC filings (2026-10-23).

Replaces FMP's shares-float when FMP was dropped. Used by main.py's
top_gainers_scan for the Top Gainers Float % column.

    float %  =  public float ($)
                / (closing price on the public float's date
                   x shares outstanding in the same annual report)

Every annual report (10-K) states, on its cover, the market value of the
shares held by non-affiliates on the last day of the company's second
quarter (dei:EntityPublicFloat), and the shares outstanding a little
later (dei:EntityCommonStockSharesOutstanding, summed over share
classes). A company with several share classes may file that count only
per class, which the SEC's API does not return (PLTR); then the balance
sheet's us-gaap:CommonStockSharesOutstanding dated nearest the float
date, within 120 days, is used instead. The price on the float date comes from Alpaca's daily bars,
unadjusted, so it matches the dollar figure the company computed.

WHAT THIS IS NOT:
  - current. The float date can be over a year old; offerings, unlocks
    and buybacks since then are not in it.
  - available for every stock. A company that has not filed its first
    annual report (a new listing - SPCX in Oct 2026), a foreign filer
    (20-F) or a fund has no public float, and gets None: a blank cell,
    never a guess.
  - more than 100%. Buybacks between the two dates can push the raw
    ratio over; it is capped.

Once a day per symbol, misses included, so a missing company costs one
lookup a day. After three SEC or Alpaca failures in a row it rests for
15 minutes, so a slow SEC never stretches a scan past its interval.

SEC_CONTACT in .env names the reader (the SEC asks every automated
client to); without it this returns None and says so once in the log.
"""

import logging
import os
import time
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/{tag}.json"
NEAREST_DAYS = 120
BARS_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"
TIMEOUT = 8
REST_AFTER, REST_SECONDS = 3, 900

_ciks = {"day": None, "map": {}}
_cache = {}                         # symbol -> (day, value or None)
_trouble = {"in_a_row": 0, "until": 0.0, "warned": False}


def _sec_headers():
    contact = os.getenv("SEC_CONTACT", "").strip()
    return {"User-Agent": f"Market Scanner {contact}"} if contact else None


def _alpaca_headers():
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret} if key and secret else None


def _get(url, headers, params=None):
    """JSON, or None for a 404 (the company has no such fact). Other
    failures raise, and count toward the rest."""
    resp = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _cik(symbol, headers, today):
    if _ciks["day"] != today:
        data = _get(TICKERS_URL, headers) or {}
        _ciks["map"] = {str(v.get("ticker", "")).upper(): int(v["cik_str"])
                        for v in data.values() if v.get("cik_str")}
        _ciks["day"] = today
    return _ciks["map"].get(symbol.upper().replace(".", "-"))


def _float_and_shares(cik, headers):
    """(public float $, its date, shares outstanding in the same report)
    or None."""
    pf = _get(CONCEPT_URL.format(cik=cik, tag="dei/EntityPublicFloat"), headers)
    floats = [f for f in ((pf or {}).get("units") or {}).get("USD", [])
              if f.get("val") and f.get("end") and f.get("accn")]
    if not floats:
        return None
    latest = max(floats, key=lambda f: (f["end"], f.get("filed", "")))
    on_float = date.fromisoformat(latest["end"])
    so = _get(CONCEPT_URL.format(cik=cik, tag="dei/EntityCommonStockSharesOutstanding"), headers)
    rows = [s for s in ((so or {}).get("units") or {}).get("shares", [])
            if s.get("accn") == latest["accn"] and s.get("val")]
    if rows:
        # Several share classes on one cover are summed (same date).
        on = max(s["end"] for s in rows)
        shares = sum(s["val"] for s in rows if s["end"] == on)
        return float(latest["val"]), on_float, float(shares)
    bs = _get(CONCEPT_URL.format(cik=cik, tag="us-gaap/CommonStockSharesOutstanding"), headers)
    near = [s for s in ((bs or {}).get("units") or {}).get("shares", [])
            if s.get("val") and s.get("end")
            and abs((date.fromisoformat(s["end"]) - on_float).days) <= NEAREST_DAYS]
    if not near:
        return None
    best = min(near, key=lambda s: abs((date.fromisoformat(s["end"]) - on_float).days))
    return float(latest["val"]), on_float, float(best["val"])


def _close_on(symbol, day, headers):
    """The last unadjusted daily close on or before `day`."""
    data = _get(BARS_URL.format(symbol=symbol), headers, params={
        "timeframe": "1Day", "adjustment": "raw", "limit": 10,
        "start": (day - timedelta(days=10)).isoformat(), "end": day.isoformat()})
    bars = (data or {}).get("bars") or []
    return float(bars[-1]["c"]) if bars and bars[-1].get("c") else None


def compute(public_float, close, shares):
    """The formula alone, rounded to a tenth and capped at 100."""
    if not public_float or not close or not shares:
        return None
    return round(min(100.0, 100.0 * public_float / (close * shares)), 1)


def free_float_pct(symbol, today=None):
    """Approximate free float % for one stock, or None (see the module
    docstring for when, and why None is the honest answer)."""
    today = today or date.today()
    hit = _cache.get(symbol)
    if hit and hit[0] == today:
        return hit[1]
    sec, alpaca = _sec_headers(), _alpaca_headers()
    if sec is None or alpaca is None:
        if not _trouble["warned"]:
            _trouble["warned"] = True
            log.warning("sec_float: %s missing from .env - Float %% stays blank",
                        "SEC_CONTACT" if sec is None else "Alpaca keys")
        return None
    if time.monotonic() < _trouble["until"]:
        return None
    try:
        value = None
        cik = _cik(symbol, sec, today)
        if cik:
            found = _float_and_shares(cik, sec)
            if found:
                public_float, on, shares = found
                value = compute(public_float, _close_on(symbol, on, alpaca), shares)
        _trouble["in_a_row"] = 0
    except (requests.RequestException, ValueError, KeyError) as e:
        _trouble["in_a_row"] += 1
        if _trouble["in_a_row"] >= REST_AFTER:
            _trouble["until"] = time.monotonic() + REST_SECONDS
            _trouble["in_a_row"] = 0
            log.warning("sec_float: %d failures in a row (last: %s) - resting 15 min",
                        REST_AFTER, e)
        return None                 # not cached: tried again on the next scan
    _cache[symbol] = (today, value)
    return value
'''

SEC_FLOAT_PATH = Path("src") / "collectors" / "sec_float.py"
ALPACA_NEWS_PATH = Path("src") / "collectors" / "alpaca_news.py"
MAIN_PATH = Path("src") / "main.py"


# ------------------------------------------------------------- the patching

def _read(path: Path) -> Tuple[str, bool]:
    raw = path.read_bytes()
    return raw.decode("utf-8").replace("\r\n", "\n"), b"\r\n" in raw


def plan(root: Path) -> Tuple[Dict[Path, str], List[str], bool]:
    """({path: new text}, problems, already done)."""
    problems: List[str] = []
    main_path, news_path = root / MAIN_PATH, root / ALPACA_NEWS_PATH
    float_path = root / SEC_FLOAT_PATH
    for p in (main_path, news_path):
        if not p.is_file():
            problems.append(f"{p} not found - run this from C:\\Dev\\Market-Scanner")
    if problems:
        return {}, problems, False
    main, _ = _read(main_path)
    news, _ = _read(news_path)
    if MARKER in main and MARKER in news and float_path.is_file():
        return {}, [], True
    if MARKER in main or MARKER in news:
        problems.append("half patched (one file has the change, another not) - "
                        "put the backups back first")
    if float_path.is_file():
        problems.append(f"{float_path} already exists")
    for needed in ALPACA_NEWS_NEEDS:
        if news.count(needed) != 1:
            problems.append(f"alpaca_news.py is not the expected version (missing: {needed.strip()})")
    for needed in MAIN_NEEDS:
        if needed not in main:
            problems.append(f"main.py is not the expected version (missing: {needed})")
    for old, _, what in MAIN_EDITS:
        if main.count(old) != 1:
            problems.append(f"main.py: {what} is not the expected version")
    if problems:
        return {}, problems, False
    for old, new, _ in MAIN_EDITS:
        main = main.replace(old, new)
    news = news.rstrip("\n") + "\n" + ALPACA_NEWS_ADD
    if "\nimport os\n" not in news:
        problems.append("alpaca_news.py does not import os")
        return {}, problems, False
    return {main_path: main, news_path: news, float_path: SEC_FLOAT}, [], False


def patch(root: Path, check: bool) -> int:
    new, problems, done = plan(root)
    if done:
        print("already patched - nothing to do")
        return 0
    if problems:
        print("STOP: nothing changed:")
        for p in problems:
            print(f"  - {p}")
        return 1
    if check:
        print("ready: every place matches; run without --check to patch")
        return 0
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    backups: Dict[Path, Path] = {}
    created: List[Path] = []
    try:
        for path, text in new.items():
            if path.is_file():
                _, crlf = _read(path)
                backups[path] = path.with_name(f"{path.name}.bak-{stamp}")
                shutil.copy2(path, backups[path])
            else:
                crlf = False
                created.append(path)
            path.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
        for path in new:
            py_compile.compile(str(path), doraise=True)
    except (OSError, py_compile.PyCompileError) as exc:
        for path, backup in backups.items():
            shutil.copy2(backup, path)
        for path in created:
            path.unlink(missing_ok=True)
        print(f"STOP: {exc} - every file put back as it was")
        return 1
    print("patched:")
    for path in new:
        print(f"  {path}" + (f"   (backup {backups[path].name})" if path in backups else "   (new)"))
    print("Now: add SEC_CONTACT to .env if it is not there, and restart Market Scanner.")
    return 0


# ---------------------------------------------------------------- self-test

FIXTURE_NEWS = '''"""alpaca_news.py - news headlines for stocks."""

import logging
import os
from datetime import datetime, timedelta, timezone

from . import _retry

log = logging.getLogger(__name__)

BASE = "https://data.alpaca.markets/v1beta1/news"


def _headers() -> dict:
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("missing")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def fetch_news(symbol: str, end: str, lookback_hours: int = 48, limit: int = 20) -> list[dict]:
    return []
'''

FIXTURE_MAIN = '''import os
import logging
log = logging.getLogger("t")
from src.collectors import (
    alpaca_bars, alpaca_news, fmp,
)


def fast_scan():
    api_key = os.getenv("FMP_API_KEY")
    volume_hits = []
    news_findings = []
    catalyst_findings = []
    if api_key:
        cat_cfg = CONFIG.get("catalyst_confluence", {})
        tracked = {"AAA", "BBB"}

        try:
            articles = fmp.fetch_stock_news(api_key)
        except Exception as e:
            log.error("fast scan: news fetch failed: %s", e)
            articles = []

        for article in articles:
            if article.get("symbol") in tracked and article.get("url"):
                news_findings.append((article["symbol"], article["url"], article["title"]))
    return news_findings


def top_gainers_scan(top_n):
    api_key_fmp = os.getenv("FMP_API_KEY")
    for symbol, sig in top_n:
        free_float_pct = None
        if api_key_fmp:
            try:
                free_float_pct = fmp.fetch_free_float_pct(symbol, api_key_fmp)
            except Exception as e:
                log.warning("top gainers scan: float fetch failed for %s: %s", symbol, e)
        sig["detail"]["free_float_pct"] = free_float_pct
    return top_n
'''


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")


def self_test() -> int:
    import importlib
    import os
    import tempfile
    import types
    from datetime import date

    failures = []

    def ok(cond, msg):
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        (root / "src" / "collectors").mkdir(parents=True)
        (root / "src" / "__init__.py").write_text("")
        (root / "src" / "collectors" / "__init__.py").write_text("")
        (root / "src" / "collectors" / "_retry.py").write_text("def get(*a, **k):\n    raise RuntimeError\n")
        (root / "src" / "collectors" / "alpaca_bars.py").write_text("")
        (root / "src" / "collectors" / "fmp.py").write_text(
            "def fetch_stock_news(k):\n    raise AssertionError('FMP called')\n"
            "def fetch_free_float_pct(s, k):\n    raise AssertionError('FMP called')\n")
        (root / ALPACA_NEWS_PATH).write_bytes(FIXTURE_NEWS.replace("\n", "\r\n").encode())
        (root / MAIN_PATH).write_text(FIXTURE_MAIN)

        ok(patch(root, check=True) == 0 and not (root / SEC_FLOAT_PATH).exists(),
           "--check writes nothing")
        ok(patch(root, check=False) == 0, "the fixture patches")
        ok(b"\r\n" in (root / ALPACA_NEWS_PATH).read_bytes(), "alpaca_news.py keeps CRLF")
        ok(len(list((root / "src" / "collectors").glob("alpaca_news.py.bak-*"))) == 1
           and len(list((root / "src").glob("main.py.bak-*"))) == 1, "backups kept")
        ok(patch(root, check=False) == 0 and len(list((root / "src").glob("main.py.bak-*"))) == 1,
           "a second run does nothing")

        sys.path.insert(0, folder)
        saved_env = {k: os.environ.get(k) for k in
                     ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "SEC_CONTACT", "FMP_API_KEY")}
        try:
            for k in saved_env:
                os.environ.pop(k, None)
            for name in [m for m in sys.modules if m == "src" or m.startswith("src.")]:
                del sys.modules[name]
            news = importlib.import_module("src.collectors.alpaca_news")
            retry = importlib.import_module("src.collectors._retry")
            ok(news.available() is False, "no Alpaca keys, no news")
            os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"] = "id", "sec"
            ok(news.available() is True, "Alpaca keys, news")

            pages = [
                {"news": [{"headline": "Tower stocks rally", "created_at": "2026-10-23T09:00:00Z",
                           "url": "https://b/1", "source": "benzinga", "summary": "s",
                           "symbols": ["AAA", "CCI", "BBB"]},
                          {"headline": "No link", "url": "", "symbols": ["AAA"]}],
                 "next_page_token": "p2"},
                {"news": [{"headline": "Second page", "created_at": "2026-10-23T08:59:00Z",
                           "url": "https://b/2", "source": "benzinga", "symbols": ["BBB"]}],
                 "next_page_token": None}]
            calls = []

            def fake_get(url, headers=None, params=None, timeout=None):
                calls.append(dict(params))
                return _Resp(200, pages[len(calls) - 1])

            retry.get = fake_get
            rows = news.fetch_market_news()
            ok(len(calls) == 2 and calls[1].get("page_token") == "p2" and "symbols" not in calls[0],
               f"market-wide, follows the page token: {calls}")
            ok([(r["symbol"], r["url"]) for r in rows] ==
               [("AAA", "https://b/1#AAA"), ("CCI", "https://b/1#CCI"), ("BBB", "https://b/1#BBB"),
                ("AAA", None), ("BBB", "https://b/2#BBB")],
               f"one row per symbol, link per symbol: {rows}")
            ok(rows[0]["title"] == "Tower stocks rally" and rows[0]["publisher"] == "Benzinga"
               and rows[0]["site"] == "benzinga" and rows[0]["publishedDate"] == "2026-10-23T09:00:00Z"
               and rows[0]["text"] == "s", f"FMP's row shape: {rows[0]}")

            main = importlib.import_module("src.main")
            main.CONFIG = {}
            calls.clear()
            found = main.fast_scan()
            ok(found == [("AAA", "https://b/1#AAA", "Tower stocks rally"),
                         ("BBB", "https://b/1#BBB", "Tower stocks rally"),
                         ("BBB", "https://b/2#BBB", "Second page")],
               f"fast_scan reads Alpaca, no FMP key needed, every tracked symbol alerts: {found}")
            os.environ.pop("ALPACA_API_KEY")
            real_fetch, asked = news.fetch_market_news, []
            news.fetch_market_news = lambda *a, **k: asked.append(1) or []
            ok(main.fast_scan() == [] and not asked,
               "no Alpaca keys: the news stage is skipped, not attempted")
            news.fetch_market_news = real_fetch
            os.environ["ALPACA_API_KEY"] = "id"

            # sec_float, through a fake web.
            sec = importlib.import_module("src.collectors.sec_float")
            ok(sec.compute(20e9, 15.0, 1.275e9) == 100.0 and sec.compute(10e9, 20.0, 1e9) == 50.0
               and sec.compute(None, 1, 1) is None and sec.compute(1, 0, 1) is None,
               "the formula, capped at 100")
            web = {"tickers": 0, "concept": 0, "bars": 0}

            def sec_get(url, headers=None, params=None, timeout=None):
                if "company_tickers" in url:
                    web["tickers"] += 1
                    return _Resp(200, {"0": {"cik_str": 1818874, "ticker": "SOFI"},
                                       "1": {"cik_str": 1181412, "ticker": "SPCX"},
                                       "2": {"cik_str": 999, "ticker": "BRK-B"},
                                       "3": {"cik_str": 1321655, "ticker": "PLTR"},
                                       "4": {"cik_str": 777, "ticker": "FAR"}})
                if "companyconcept" in url:
                    web["concept"] += 1
                    ok("Market Scanner " in headers.get("User-Agent", ""), "the SEC is told who asks")
                    if "CIK0001181412" in url:
                        return _Resp(404)
                    if "CIK0000000777" in url and "EntityPublicFloat" not in url:
                        if "dei/" in url:
                            return _Resp(404)
                        return _Resp(200, {"units": {"shares": [
                            {"end": "2024-12-31", "val": 1.0e9}, {"end": "2026-06-30", "val": 1.0e9}]}})
                    if "CIK0001321655" in url:
                        if "dei/EntityCommonStockSharesOutstanding" in url:
                            return _Resp(404)
                        if "us-gaap/CommonStockSharesOutstanding" in url:
                            return _Resp(200, {"units": {"shares": [
                                {"end": "2025-03-31", "val": 3.0e9},
                                {"end": "2025-06-30", "val": 2.5e9},
                                {"end": "2026-06-30", "val": 9.9e9}]}})
                    if url.endswith("EntityPublicFloat.json"):
                        return _Resp(200, {"units": {"USD": [
                            {"end": "2024-06-30", "val": 6.7e9, "accn": "A1", "filed": "2025-02-24"},
                            {"end": "2025-06-30", "val": 10e9, "accn": "A2", "filed": "2026-02-17"}]}})
                    return _Resp(200, {"units": {"shares": [
                        {"end": "2025-01-30", "val": 1.0e9, "accn": "A1"},
                        {"end": "2026-01-30", "val": 0.6e9, "accn": "A2"},
                        {"end": "2026-01-30", "val": 0.4e9, "accn": "A2"},
                        {"end": "2026-07-31", "val": 2.0e9, "accn": "Q2"}]}})
                if "/v2/stocks/" in url:
                    web["bars"] += 1
                    ok(params["end"] == "2025-06-30" and params["adjustment"] == "raw",
                       f"the close ON the float date, unadjusted: {params}")
                    return _Resp(200, {"bars": [{"c": 19.0}, {"c": 20.0}]})
                raise AssertionError(url)

            sec.requests = types.SimpleNamespace(get=sec_get,
                                                 RequestException=Exception)
            today = date(2026, 10, 23)
            ok(sec.free_float_pct("SOFI", today) is None, "no SEC_CONTACT: blank")
            os.environ["SEC_CONTACT"] = "someone@example.org"
            got = sec.free_float_pct("SOFI", today)
            ok(got == 50.0, f"10bn / (20 x (0.6bn + 0.4bn) from the same report) = 50%: {got}")
            before = dict(web)
            ok(sec.free_float_pct("SOFI", today) == 50.0 and web == before, "cached for the day")
            ok(sec.free_float_pct("SPCX", today) is None, "a new listing (404) is blank")
            before = dict(web)
            ok(sec.free_float_pct("SPCX", today) is None and web == before, "and the miss is cached")
            ok(sec.free_float_pct("ZZZZ", today) is None, "an unknown ticker is blank")
            ok(sec.free_float_pct("FAR", today) is None,
               "a balance-sheet count more than 120 days from the float date is not used")
            got = sec.free_float_pct("PLTR", today)
            ok(got == 20.0, f"several share classes: the balance sheet's count on the float "
                            f"date, 10bn / (20 x 2.5bn) = 20%: {got}")

            def down(url, headers=None, params=None, timeout=None):
                raise ConnectionError("SEC down")

            sec.requests = types.SimpleNamespace(get=down, RequestException=ConnectionError)
            sec._cache.clear()
            sec._ciks["day"] = None
            for _ in range(3):
                ok(sec.free_float_pct("SOFI", today) is None, "a failure is blank, not a crash")
            ok(sec._trouble["until"] > 0, "three failures in a row: rest")
            calls_before = sec._trouble["until"]
            ok(sec.free_float_pct("SOFI", today) is None and sec._trouble["until"] == calls_before,
               "while resting, nothing is asked")

            gainers = main.top_gainers_scan([("SOFI", {"detail": {}})])
            ok(gainers[0][1]["detail"]["free_float_pct"] is None,
               "top_gainers_scan asks sec_float, not FMP (which would raise)")
        finally:
            sys.path.remove(folder)
            for name in [m for m in sys.modules if m == "src" or m.startswith("src.")]:
                del sys.modules[name]
            for k, v in saved_env.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v

        # A different main.py is refused and nothing is written anywhere.
        other = Path(folder) / "other"
        (other / "src" / "collectors").mkdir(parents=True)
        (other / ALPACA_NEWS_PATH).write_text(FIXTURE_NEWS)
        changed = FIXTURE_MAIN.replace("articles = fmp.fetch_stock_news(api_key)",
                                       "articles = fmp.fetch_stock_news(api_key, limit=500)")
        (other / MAIN_PATH).write_text(changed)
        ok(patch(other, check=False) == 1 and (other / MAIN_PATH).read_text() == changed
           and (other / ALPACA_NEWS_PATH).read_text() == FIXTURE_NEWS
           and not (other / SEC_FLOAT_PATH).exists(),
           "one edit not matching: nothing is written anywhere")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("patch_alpaca_news_sec_float self-test passed")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    return patch(Path("."), "--check" in sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
