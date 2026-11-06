"""
patch_sec_edgar_vix.py -- Market Scanner without FMP, part 2: SEC filings
from EDGAR, VIX from IBKR (2026-10-23).

Run AFTER patch_alpaca_news_sec_float.py. What still needed FMP:

  FILINGS  fmp_intel (the Live Feed's "SEC 8-K" rows) stopped at its first
           line without an FMP key - `if not api_key: return` - and read
           FMP's market-wide 8-K / financials lists.
  DILUTION _shadow_filings (the shadow strategy's S-1 / S-3 / F-1 / F-3 /
           424B check) read FMP's per-symbol filing search.
  VIX      hot_list_refresh's header read FMP's ^VIX quote.

Analyst grades, price targets and insider trades were only read by Rebound
Watch, which is disabled and on config.yaml's retired_jobs list - left as
they are.

Five changes:

  1. NEW src/collectors/sec_edgar.py
       fetch_sec_filings()     EDGAR's own "latest filings" feed for 8-K,
                               10-Q, 10-K and 6-K, the last 24 hours (then
                               only what is newer than the last read),
                               mapped to tickers with the SEC's ticker list,
                               in FMP's row shape.
       fetch_symbol_filings()  one company's filings from EDGAR's
                               submissions list, in FMP's row shape.
  2. src/main.py  fmp_intel runs without an FMP key and reads sec_edgar;
                  the retired RSI / congressional extras run only with one.
  3. src/main.py  _shadow_filings reads sec_edgar.
  4. src/collectors/ibkr_account.py gains get_vix(): IB Gateway, read-only,
     its own client id (7708), connect - read the VIX - disconnect, like
     get_account_snapshot(). Gateway down: None, a blank header.
     src/main.py's hot_list_refresh reads it.
  5. src/collectors/sec_float.py: a company that files no whole share count
     (FCNCA) falls back to the period's average shares, so its Float % is no
     longer blank. Replaced only if it is exactly the version the first
     patch wrote.

SEC_CONTACT in .env names the reader (already added for the float).

Every edit is matched exactly or nothing is written; backups are
timestamped, line endings kept, and if any patched file fails to compile
every file is put back and the new one removed.

    cd C:\\Dev\\Market-Scanner
    python patch_sec_edgar_vix.py --check
    python patch_sec_edgar_vix.py
    python patch_sec_edgar_vix.py --self-test

Then restart Market Scanner's service. Nothing here can place an order.
"""

from __future__ import annotations

import hashlib
import py_compile
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

MARKER = "SEC FROM EDGAR (2026-10-23)"

MAIN_PATH = Path("src") / "main.py"
IBKR_PATH = Path("src") / "collectors" / "ibkr_account.py"
FLOAT_PATH = Path("src") / "collectors" / "sec_float.py"
EDGAR_PATH = Path("src") / "collectors" / "sec_edgar.py"

#: sec_float.py exactly as patch_alpaca_news_sec_float.py wrote it (LF).
FLOAT_V1_SHA256 = "717d206298fa1e0920f4f85ae7f297785aa766f49a9d89dcb1b3d083a56176fc"

# --------------------------------------------------------------- main.py

INTEL_GATE_OLD = '''def fmp_intel():
    log.info("fmp intel starting")
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        log.info("no FMP_API_KEY set - skipping")
        heartbeat.stamp("fmp_intel", "skipped", "no api key")
        return
'''
INTEL_GATE_NEW = '''def fmp_intel():
    log.info("fmp intel starting")
    # SEC FROM EDGAR (2026-10-23): FMP was dropped. SEC filings now come
    # from EDGAR itself (collectors/sec_edgar.py) and need no key; only the
    # retired RSI / congressional-trades extras still use FMP, and only
    # when a key is set.
    api_key = os.getenv("FMP_API_KEY")
'''
WANT_OLD = '    want_rsi, want_congress = _swing_extra("fmp_rsi"), _swing_extra("congressional_trades")\n'
WANT_NEW = ('    want_rsi = bool(api_key) and _swing_extra("fmp_rsi")\n'
            '    want_congress = bool(api_key) and _swing_extra("congressional_trades")\n')
FILINGS_OLD = '''    today = store.utcnow()[:10]
    try:
        filings = fmp.fetch_sec_filings(today, today, api_key)
'''
FILINGS_NEW = '''    today = store.utcnow()[:10]
    from src.collectors import sec_edgar
    try:
        filings = sec_edgar.fetch_sec_filings(today)
'''
SHADOW_OLD = '''        rows = fmp.fetch_symbol_filings(symbol, os.getenv("FMP_API_KEY"),
                                        (today - timedelta(days=lookback_days)).isoformat(), today.isoformat())
'''
SHADOW_NEW = '''        from src.collectors import sec_edgar   # SEC FROM EDGAR (2026-10-23)
        rows = sec_edgar.fetch_symbol_filings(
            symbol, (today - timedelta(days=lookback_days)).isoformat(), today.isoformat())
'''
VIX_OLD = '''    # Market-wide context, refetched at most every 5 minutes (one FMP call).
    if time.monotonic() - _HOT_VIX["ts"] > 300:
        try:
            _HOT_VIX["vix"] = fmp.fetch_vix(os.getenv("FMP_API_KEY"))
'''
VIX_NEW = '''    # Market-wide context, refetched at most every 5 minutes (one IBKR
    # request - VIX FROM IBKR, 2026-10-23, after FMP was dropped).
    if time.monotonic() - _HOT_VIX["ts"] > 300:
        try:
            _HOT_VIX["vix"] = ibkr_account.get_vix()
'''
MAIN_EDITS = ((INTEL_GATE_OLD, INTEL_GATE_NEW, "fmp_intel's FMP-key gate"),
              (WANT_OLD, WANT_NEW, "fmp_intel's RSI / congressional switches"),
              (FILINGS_OLD, FILINGS_NEW, "fmp_intel's FMP filings call"),
              (SHADOW_OLD, SHADOW_NEW, "_shadow_filings' FMP call"),
              (VIX_OLD, VIX_NEW, "hot_list_refresh's FMP VIX call"))
MAIN_NEEDS = ("ibkr_account", "def fmp_intel(", "def _shadow_filings(")

# ------------------------------------------------------- ibkr_account.py

IBKR_NEEDS = ("import asyncio\n", "import logging\n", "log = logging.getLogger(__name__)\n",
              "def get_account_snapshot(")
IBKR_ADD = '''

# ---------------------------------------------------------------------------
# VIX FROM IBKR (2026-10-23), for hot_list_refresh's market header, after FMP
# was dropped. Index data only - the CBOE VIX's latest daily bar, whose close
# is the current level while the index trades. Same shape as the account
# snapshot above: read-only, its own client id (7708), connect - read -
# disconnect, None whenever Gateway is not there.
async def _vix(host: str, port: int, client_id: int) -> float | None:
    from ib_async import IB, Index
    logging.getLogger("ib_async").setLevel(logging.WARNING)
    ib = IB()
    try:
        await ib.connectAsync(host, port, clientId=client_id, timeout=8, readonly=True)
        vix = Index("VIX", "CBOE", "USD")
        await ib.qualifyContractsAsync(vix)
        bars = await ib.reqHistoricalDataAsync(
            vix, endDateTime="", durationStr="2 D", barSizeSetting="1 day",
            whatToShow="TRADES", useRTH=True, formatDate=1)
    except Exception as e:  # noqa: BLE001
        log.info("ibkr vix: not available (%s)", type(e).__name__)
        return None
    finally:
        if ib.isConnected():
            ib.disconnect()
    if not bars or not bars[-1].close or bars[-1].close <= 0:
        return None
    return float(bars[-1].close)


def get_vix(host: str = "127.0.0.1", port: int = 4001, client_id: int = 7708) -> float | None:
    """The VIX's current level, or None if Gateway is not running / logged in."""
    try:
        return asyncio.run(_vix(host, port, client_id))
    except Exception as e:  # noqa: BLE001
        log.info("ibkr vix: lookup failed (%s)", type(e).__name__)
        return None
'''

# ---------------------------------------------------------- sec_edgar.py

SEC_EDGAR = '''"""
sec_edgar.py - SEC filings straight from EDGAR (2026-10-23).

Replaces FMP's sec-filings-8k / sec-filings-financials (main.py's
fmp_intel) and sec-filings-search/symbol (main.py's _shadow_filings) when
FMP was dropped. Rows keep FMP's shape, so signals/intel.py, shadow.py and
focus_score.py read them unchanged:

    {'symbol':, 'cik':, 'formType':, 'filingDate':, 'acceptedDate':,
     'link':, 'finalLink':, 'hasFinancials':}

fetch_sec_filings()     EDGAR's "latest filings" feed (the getcurrent Atom
                        feed) for 8-K, 10-Q, 10-K and 6-K - the forms FMP's
                        two lists carried, amendments included. The first
                        call reads the last 24 hours; later calls stop at
                        the newest filing already read, so a 20-minute
                        cadence costs a page or two per form.
fetch_symbol_filings()  one company's filings, every form type, from
                        EDGAR's submissions list (its last ~1,000).

A filing is mapped to the company's first ticker in the SEC's own ticker
list; a filer with no ticker (a private company, a fund, an individual) is
left out, as FMP's lists left it out.

SEC_CONTACT in .env names the reader, as the SEC asks every automated
client to; without it both functions raise and the caller logs it. Calls
are spaced to stay well inside the SEC's 10-a-second limit.
"""

import logging
import os
import time
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)

CURRENT_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{accn}-index.htm"
FORMS = ("8-K", "10-Q", "10-K", "6-K")
FINANCIAL_FORMS = ("10-Q", "10-K")
LOOKBACK = timedelta(hours=24)
PAGE, MAX_PAGES, PAUSE, TIMEOUT = 100, 15, 0.15, 15
ATOM = "{http://www.w3.org/2005/Atom}"
NY = ZoneInfo("America/New_York")

_tickers = {"day": None, "by_cik": {}, "by_ticker": {}}
_newest = {}                       # form -> newest acceptance time already read


def _headers() -> dict:
    contact = os.getenv("SEC_CONTACT", "").strip()
    if not contact:
        raise RuntimeError("SEC_CONTACT missing from .env - the SEC asks every reader to name itself")
    return {"User-Agent": f"Market Scanner {contact}"}


def _get(url, headers, params=None):
    resp = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    time.sleep(PAUSE)
    return resp


def _load_tickers(headers) -> None:
    today = datetime.now(NY).date()
    if _tickers["day"] == today:
        return
    by_cik, by_ticker = {}, {}
    for v in _get(TICKERS_URL, headers).json().values():
        try:
            cik, ticker = int(v["cik_str"]), str(v["ticker"]).upper()
        except (KeyError, TypeError, ValueError):
            continue
        by_cik.setdefault(cik, ticker)          # the first listed is the main class
        by_ticker[ticker] = cik
    _tickers.update(day=today, by_cik=by_cik, by_ticker=by_ticker)


def parse_current(xml_text: str) -> list[dict]:
    """The getcurrent Atom feed: [{'form':, 'cik':, 'link':, 'accepted':}]."""
    out = []
    for e in ElementTree.fromstring(xml_text).iter(f"{ATOM}entry"):
        link_el, cat_el = e.find(f"{ATOM}link"), e.find(f"{ATOM}category")
        link = link_el.get("href") if link_el is not None else ""
        form = cat_el.get("term") if cat_el is not None else ""
        try:
            cik = int(link.split("/edgar/data/")[1].split("/")[0])
            accepted = datetime.fromisoformat(e.findtext(f"{ATOM}updated") or "")
        except (IndexError, ValueError):
            continue
        out.append({"form": form, "cik": cik, "link": link, "accepted": accepted})
    return out


def _row(symbol, cik, form, accepted, link) -> dict:
    ny = accepted.astimezone(NY)
    return {"symbol": symbol, "cik": cik, "formType": form,
            "filingDate": ny.strftime("%Y-%m-%d"),
            "acceptedDate": ny.strftime("%Y-%m-%d %H:%M:%S"),
            "link": link, "finalLink": link,
            "hasFinancials": form.split("/")[0] in FINANCIAL_FORMS}


def fetch_sec_filings(day: str | None = None) -> list[dict]:
    """Filings accepted in the last 24 hours (or since the newest already
    read), market-wide, for the forms in FORMS. `day` is the old FMP
    signature's date and is not needed: the window is by time, so evening
    filings are not lost to a UTC date rolling over."""
    headers = _headers()
    _load_tickers(headers)
    now = datetime.now(timezone.utc)
    out, seen = [], set()
    for form in FORMS:
        stop_at = max(now - LOOKBACK, _newest.get(form, now - LOOKBACK))
        newest, done = None, False
        for page in range(MAX_PAGES):
            entries = parse_current(_get(CURRENT_URL, headers, params={
                "action": "getcurrent", "type": form, "owner": "include",
                "start": page * PAGE, "count": PAGE, "output": "atom"}).text)
            for e in entries:
                newest = max(newest, e["accepted"]) if newest else e["accepted"]
                if e["accepted"] <= stop_at:
                    done = True
                    break
                symbol = _tickers["by_cik"].get(e["cik"])
                if not symbol or e["link"] in seen:
                    continue
                seen.add(e["link"])
                out.append(_row(symbol, e["cik"], e["form"], e["accepted"], e["link"]))
            if done or len(entries) < PAGE:
                break
        if newest:
            _newest[form] = newest
    return out


def fetch_symbol_filings(symbol: str, from_date: str, to_date: str) -> list[dict]:
    """Every filing for ONE symbol, all form types, filed from_date..to_date
    (YYYY-MM-DD, both included). [] for a ticker the SEC does not list."""
    headers = _headers()
    _load_tickers(headers)
    cik = _tickers["by_ticker"].get(symbol.upper().replace(".", "-"))
    if not cik:
        return []
    recent = (_get(SUBMISSIONS_URL.format(cik=cik), headers).json()
              .get("filings", {}).get("recent", {}))
    out = []
    for form, filed, accn, accepted in zip(recent.get("form", []), recent.get("filingDate", []),
                                           recent.get("accessionNumber", []),
                                           recent.get("acceptanceDateTime", [])):
        if not (from_date <= filed <= to_date):
            continue
        link = INDEX_URL.format(cik=cik, folder=accn.replace("-", ""), accn=accn)
        try:
            when = datetime.fromisoformat(accepted.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            when = None
        row = _row(symbol, cik, form, when, link) if when else {
            "symbol": symbol, "cik": cik, "formType": form, "filingDate": filed,
            "acceptedDate": None, "link": link, "finalLink": link,
            "hasFinancials": form.split("/")[0] in FINANCIAL_FORMS}
        row["filingDate"] = filed               # EDGAR's own filing date wins
        out.append(row)
    return out
'''

SEC_FLOAT_V2 = '''"""
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
per class, which the SEC's API does not return (PLTR, FCNCA); then the
balance sheet's us-gaap:CommonStockSharesOutstanding dated nearest the
float date, within 120 days, is used instead - or, failing that, the
period's average, us-gaap:WeightedAverageNumberOfSharesOutstandingBasic
(added the same day for FCNCA, which files neither count whole). The price on the float date comes from Alpaca's daily bars,
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
#: Whole-company share counts to fall back on, in order of preference.
FALLBACK_TAGS = ("us-gaap/CommonStockSharesOutstanding",
                 "us-gaap/WeightedAverageNumberOfSharesOutstandingBasic")
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
    for tag in FALLBACK_TAGS:
        bs = _get(CONCEPT_URL.format(cik=cik, tag=tag), headers)
        near = [s for s in ((bs or {}).get("units") or {}).get("shares", [])
                if s.get("val") and s.get("end")
                and abs((date.fromisoformat(s["end"]) - on_float).days) <= NEAREST_DAYS]
        if near:
            best = min(near, key=lambda s: abs((date.fromisoformat(s["end"]) - on_float).days))
            return float(latest["val"]), on_float, float(best["val"])
    return None


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


# ------------------------------------------------------------- the patching

def _read(path: Path) -> Tuple[str, bool]:
    raw = path.read_bytes()
    return raw.decode("utf-8").replace("\r\n", "\n"), b"\r\n" in raw


def plan(root: Path) -> Tuple[Dict[Path, str], List[str], bool]:
    """({path: new text}, problems, already done)."""
    problems: List[str] = []
    main_p, ibkr_p, float_p, edgar_p = (root / MAIN_PATH, root / IBKR_PATH,
                                        root / FLOAT_PATH, root / EDGAR_PATH)
    for p in (main_p, ibkr_p):
        if not p.is_file():
            problems.append(f"{p} not found - run this from C:\\Dev\\Market-Scanner")
    if not float_p.is_file():
        problems.append(f"{float_p} not found - run patch_alpaca_news_sec_float.py first")
    if problems:
        return {}, problems, False
    main, _ = _read(main_p)
    ibkr, _ = _read(ibkr_p)
    float_now, _ = _read(float_p)
    if MARKER in main and "VIX FROM IBKR" in ibkr and edgar_p.is_file():
        return {}, [], True
    if MARKER in main or "VIX FROM IBKR" in ibkr:
        problems.append("half patched (one file has the change, another not) - "
                        "put the backups back first")
    if edgar_p.is_file():
        problems.append(f"{edgar_p} already exists")
    if "NEWS FROM ALPACA" not in main:
        problems.append("run patch_alpaca_news_sec_float.py first")
    float_v1 = hashlib.sha256(float_now.encode("utf-8")).hexdigest() == FLOAT_V1_SHA256
    if not float_v1 and float_now != SEC_FLOAT_V2:
        problems.append("sec_float.py is not the version patch_alpaca_news_sec_float.py wrote")
    for needed in MAIN_NEEDS:
        if needed not in main:
            problems.append(f"main.py is not the expected version (missing: {needed})")
    for old, _, what in MAIN_EDITS:
        if main.count(old) != 1:
            problems.append(f"main.py: {what} is not the expected version")
    for needed in IBKR_NEEDS:
        if ibkr.count(needed) < 1:
            problems.append(f"ibkr_account.py is not the expected version (missing: {needed.strip()})")
    if problems:
        return {}, problems, False
    for old, new, _ in MAIN_EDITS:
        main = main.replace(old, new)
    out = {main_p: main, ibkr_p: ibkr.rstrip("\n") + "\n" + IBKR_ADD, edgar_p: SEC_EDGAR}
    if float_v1:
        out[float_p] = SEC_FLOAT_V2
    return out, [], False


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
    print("Now restart Market Scanner.")
    return 0


# ---------------------------------------------------------------- self-test

FIXTURE_MAIN = '''import os
import logging
import time
from datetime import date, timedelta
log = logging.getLogger("t")
from src.collectors import (
    alpaca_news, fmp, ibkr_account,
)
# NEWS FROM ALPACA (2026-10-23) - the first patch's marker
CALLS = []


class heartbeat:
    @staticmethod
    def stamp(*a):
        CALLS.append(("stamp",) + a)


class store:
    @staticmethod
    def utcnow():
        return "2026-10-23T20:00:00"


def _swing_extra(name):
    return True


def fmp_intel():
    log.info("fmp intel starting")
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        log.info("no FMP_API_KEY set - skipping")
        heartbeat.stamp("fmp_intel", "skipped", "no api key")
        return

    today = store.utcnow()[:10]
    try:
        filings = fmp.fetch_sec_filings(today, today, api_key)
    except Exception as e:
        log.error("fmp intel: sec filings fetch failed: %s", e)
        filings = []

    want_rsi, want_congress = _swing_extra("fmp_rsi"), _swing_extra("congressional_trades")
    return filings, want_rsi, want_congress


def _shadow_filings(symbol, today, lookback_days):
    try:
        rows = fmp.fetch_symbol_filings(symbol, os.getenv("FMP_API_KEY"),
                                        (today - timedelta(days=lookback_days)).isoformat(), today.isoformat())
    except Exception as e:  # noqa: BLE001
        log.warning("shadow: filings lookup failed for %s: %s", symbol, e)
        rows = None
    return rows


_HOT_VIX = {"ts": -1e9, "vix": None}


def hot_list_refresh():
    # Market-wide context, refetched at most every 5 minutes (one FMP call).
    if time.monotonic() - _HOT_VIX["ts"] > 300:
        try:
            _HOT_VIX["vix"] = fmp.fetch_vix(os.getenv("FMP_API_KEY"))
            _HOT_VIX["ts"] = time.monotonic()
        except Exception as e:
            log.warning("hot list: VIX fetch failed: %s", e)
    vix = _HOT_VIX["vix"]
    return vix
'''

FIXTURE_IBKR = '''"""ibkr_account.py - read-only."""

import asyncio
import logging

log = logging.getLogger(__name__)


def get_account_snapshot(host: str = "127.0.0.1", port: int = 4001, client_id: int = 7702):
    return None
'''

ATOM_PAGE = '''<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Latest Filings</title>
{entries}
</feed>'''
ATOM_ENTRY = '''<entry><title>{form} - CO ({cik:010d}) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{accn}-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-10-23</summary>
<updated>{when}</updated><category scheme="https://www.sec.gov/" label="form type" term="{form}"/>
<id>urn:tag:sec.gov,2008:accession-number={accn}</id></entry>'''


class _Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise ConnectionError(f"HTTP {self.status_code}")


def self_test() -> int:
    import asyncio
    import importlib
    import os
    import tempfile
    import types
    from datetime import date, timedelta, timezone

    failures = []

    def ok(cond, msg):
        if not cond:
            failures.append(msg)

    here = Path(__file__).resolve().parent
    try:
        sys.path.insert(0, str(here))
        import patch_alpaca_news_sec_float as first
        float_v1 = first.SEC_FLOAT
    except ImportError:
        print("(patch_alpaca_news_sec_float.py is not beside this file - "
              "the sec_float upgrade check is skipped)")
        float_v1 = None
    finally:
        sys.path.remove(str(here))
    if float_v1 is not None:
        ok(hashlib.sha256(float_v1.encode()).hexdigest() == FLOAT_V1_SHA256,
           "the fingerprint matches the first patch's sec_float.py")

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        col = root / "src" / "collectors"
        col.mkdir(parents=True)
        (root / "src" / "__init__.py").write_text("")
        (col / "__init__.py").write_text("")
        (col / "alpaca_news.py").write_text("")
        (col / "fmp.py").write_text(
            "def fetch_sec_filings(*a):\n    raise AssertionError('FMP called')\n"
            "def fetch_symbol_filings(*a):\n    raise AssertionError('FMP called')\n"
            "def fetch_vix(*a):\n    raise AssertionError('FMP called')\n")
        (root / IBKR_PATH).write_bytes(FIXTURE_IBKR.replace("\n", "\r\n").encode())
        (root / MAIN_PATH).write_text(FIXTURE_MAIN)
        (root / FLOAT_PATH).write_text(float_v1 if float_v1 is not None else SEC_FLOAT_V2)

        ok(patch(root, check=True) == 0 and not (root / EDGAR_PATH).exists(), "--check writes nothing")
        ok(patch(root, check=False) == 0, "the fixture patches")
        ok(b"\r\n" in (root / IBKR_PATH).read_bytes(), "ibkr_account.py keeps CRLF")
        ok(_read(root / FLOAT_PATH)[0] == SEC_FLOAT_V2, "sec_float.py is upgraded")
        ok(patch(root, check=False) == 0
           and len(list((root / "src").glob("main.py.bak-*"))) == 1, "a second run does nothing")

        sys.path.insert(0, folder)
        saved = {k: os.environ.get(k) for k in ("FMP_API_KEY", "SEC_CONTACT")}
        try:
            for k in saved:
                os.environ.pop(k, None)
            for name in [m for m in sys.modules if m == "src" or m.startswith("src.")]:
                del sys.modules[name]
            edgar = importlib.import_module("src.collectors.sec_edgar")
            edgar.PAUSE = 0
            ibkr = importlib.import_module("src.collectors.ibkr_account")
            main = importlib.import_module("src.main")

            # EDGAR, through a fake web.
            now = datetime.now(timezone.utc)
            mk = lambda form, cik, n, mins: ATOM_ENTRY.format(  # noqa: E731
                form=form, cik=cik, folder=f"0000{n:014d}", accn=f"0000-26-{n:06d}",
                when=(now - timedelta(minutes=mins)).astimezone(edgar.NY).isoformat(timespec="seconds"))
            pages = {"8-K": [[mk("8-K", 320193, 1, 5), mk("8-K", 999999, 2, 6),
                              mk("8-K/A", 1818874, 3, 7)] + [mk("8-K", 320193, 100 + i, 10 + i)
                                                             for i in range(97)],
                             [mk("8-K", 320193, 500, 200), mk("8-K", 320193, 501, 2000)]],
                     "10-Q": [[mk("10-Q", 1818874, 600, 30)]], "10-K": [[]], "6-K": [[]]}
            asked = []

            def web(url, headers=None, params=None, timeout=None):
                asked.append((url, dict(params or {})))
                ok("Market Scanner " in headers.get("User-Agent", ""), "the SEC is told who asks")
                if "company_tickers" in url:
                    return _Resp(200, {"0": {"cik_str": 320193, "ticker": "AAPL"},
                                       "1": {"cik_str": 1818874, "ticker": "SOFI"},
                                       "2": {"cik_str": 798941, "ticker": "FCNCA"},
                                       "3": {"cik_str": 798941, "ticker": "FCNCB"}})
                if "getcurrent" == (params or {}).get("action"):
                    form, page = params["type"], params["start"] // 100
                    entries = pages[form][page] if page < len(pages[form]) else []
                    return _Resp(200, text=ATOM_PAGE.format(entries="\n".join(entries)))
                if "submissions" in url:
                    return _Resp(200, {"filings": {"recent": {
                        "form": ["S-8", "10-Q", "424B4", "8-K"],
                        "filingDate": ["2026-10-22", "2026-10-22", "2026-09-01", "2026-08-01"],
                        "accessionNumber": ["0001628280-26-066903", "0001628280-26-066880",
                                            "0001628280-26-050000", "0001628280-26-040000"],
                        "acceptanceDateTime": ["2026-10-23T00:24:30.000Z", "2026-10-23T00:03:05.000Z",
                                               "2026-09-01T20:00:00.000Z", None]}}})
                raise AssertionError(url)

            edgar.requests = types.SimpleNamespace(get=web)
            try:
                edgar.fetch_sec_filings()
                ok(False, "no SEC_CONTACT must raise, so fmp_intel logs it")
            except RuntimeError:
                pass
            os.environ["SEC_CONTACT"] = "someone@example.org"
            rows = edgar.fetch_sec_filings("2026-10-23")
            syms = [(r["symbol"], r["formType"]) for r in rows]
            ok(len(rows) == 101 and syms[0] == ("AAPL", "8-K") and ("SOFI", "8-K/A") in syms
               and ("SOFI", "10-Q") in syms, f"24 hours of four forms, tickers mapped "
               f"(99 + 1 from page two + 1 10-Q): {len(rows)} {syms[:4]}")
            ok(any("000000000000500" in r["link"] for r in rows),
               "the second page is read while it is still inside 24 hours")
            ok(not any(r["cik"] == 999999 for r in rows), "a filer with no ticker is left out")
            ok(edgar._tickers["by_cik"][798941] == "FCNCA",
               "a company with two tickers files under its first-listed (main) one")
            ok(not any("000000000000501" in r["link"] for r in rows),
               "nothing older than 24 hours")
            ok(rows[0]["link"].endswith("-index.htm") and rows[0]["link"] == rows[0]["finalLink"]
               and rows[0]["hasFinancials"] is False
               and [r for r in rows if r["formType"] == "10-Q"][0]["hasFinancials"] is True
               and len(rows[0]["acceptedDate"]) == 19, f"FMP's row shape: {rows[0]}")
            asked.clear()
            again = edgar.fetch_sec_filings()
            ok(again == [] and len([a for a in asked if a[1].get("action") == "getcurrent"]) == 4,
               f"the next run reads one page a form and stops at what it already read: {asked}")

            got = edgar.fetch_symbol_filings("FCNCB", "2026-09-01", "2026-10-23")
            ok([(r["formType"], r["filingDate"]) for r in got] ==
               [("S-8", "2026-10-22"), ("10-Q", "2026-10-22"), ("424B4", "2026-09-01")],
               f"one company's filings in the window, any class's ticker: {got}")
            ok(got[0]["link"] == "https://www.sec.gov/Archives/edgar/data/798941/"
               "000162828026066903/0001628280-26-066903-index.htm", f"index link: {got[0]['link']}")
            ok(edgar.fetch_symbol_filings("ZZZZ", "2026-01-01", "2026-12-31") == [],
               "a ticker the SEC does not list has no filings")

            # main.py: no FMP key needed, no FMP called.
            edgar._newest.clear()
            filings, want_rsi, want_congress = main.fmp_intel()
            ok(len(filings) == 101 and not want_rsi and not want_congress,
               "fmp_intel runs without FMP; the retired FMP extras stay off without a key")
            os.environ["FMP_API_KEY"] = "k"
            _, want_rsi, _ = main.fmp_intel()
            ok(want_rsi, "with a key the extras follow their own switches, as before")
            os.environ.pop("FMP_API_KEY")
            rows = main._shadow_filings("FCNCA", date(2026, 10, 23), 60)
            ok([r["formType"] for r in rows] == ["S-8", "10-Q", "424B4"],
               f"the dilution check reads EDGAR: {rows}")

            # VIX through a fake ib_async.
            class FakeIB:
                connected = False
                fail = False

                def isConnected(self):
                    return FakeIB.connected

                async def connectAsync(self, host, port, clientId, timeout, readonly):
                    if FakeIB.fail:
                        raise ConnectionRefusedError
                    ok(readonly is True and clientId == 7708, "read-only, client id 7708")
                    FakeIB.connected = True

                async def qualifyContractsAsync(self, c):
                    return [c]

                async def reqHistoricalDataAsync(self, *a, **k):
                    return [types.SimpleNamespace(close=19.1), types.SimpleNamespace(close=19.43)]

                def disconnect(self):
                    FakeIB.connected = False

            fake = types.ModuleType("ib_async")
            fake.IB, fake.Index = FakeIB, lambda *a: a
            real = sys.modules.get("ib_async")
            sys.modules["ib_async"] = fake
            try:
                ok(main.hot_list_refresh() == 19.43 and not FakeIB.connected,
                   "the Hot List's VIX comes from IBKR, and the link is closed")
                FakeIB.fail = True
                ok(ibkr.get_vix() is None, "Gateway down: None, not a crash")
            finally:
                if real is not None:
                    sys.modules["ib_async"] = real
                else:
                    sys.modules.pop("ib_async", None)

            # sec_float v2: the average-shares fallback.
            sec = importlib.import_module("src.collectors.sec_float")

            def float_web(url, headers=None, params=None, timeout=None):
                if "company_tickers" in url:
                    return _Resp(200, {"0": {"cik_str": 798941, "ticker": "FCNCA"}})
                if "EntityPublicFloat" in url:
                    return _Resp(200, {"units": {"USD": [
                        {"end": "2025-06-30", "val": 10e9, "accn": "A2", "filed": "2026-02-17"}]}})
                if "WeightedAverage" in url:
                    return _Resp(200, {"units": {"shares": [
                        {"start": "2025-04-01", "end": "2025-06-30", "val": 12.5e6}]}})
                if "companyconcept" in url:
                    return _Resp(404)
                if "/v2/stocks/" in url:
                    return _Resp(200, {"bars": [{"c": 1600.0}]})
                raise AssertionError(url)

            sec.requests = types.SimpleNamespace(get=float_web, RequestException=ConnectionError)
            os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"] = "id", "sec"
            got = sec.free_float_pct("FCNCA", date(2026, 10, 23))
            ok(got == 50.0, f"FCNCA: 10bn / (1,600 x 12.5M average shares) = 50%: {got}")
        finally:
            sys.path.remove(folder)
            for name in [m for m in sys.modules if m == "src" or m.startswith("src.")]:
                del sys.modules[name]
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v
            for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
                os.environ.pop(k, None)

        # A different main.py is refused and nothing is written anywhere.
        other = Path(folder) / "other"
        (other / "src" / "collectors").mkdir(parents=True)
        (other / IBKR_PATH).write_text(FIXTURE_IBKR)
        (other / FLOAT_PATH).write_text(SEC_FLOAT_V2)
        changed = FIXTURE_MAIN.replace("(one FMP call)", "(one FMP request)")
        (other / MAIN_PATH).write_text(changed)
        ok(patch(other, check=False) == 1 and (other / MAIN_PATH).read_text() == changed
           and (other / IBKR_PATH).read_text() == FIXTURE_IBKR
           and not (other / EDGAR_PATH).exists(), "one edit not matching: nothing written")
        (other / MAIN_PATH).write_text(FIXTURE_MAIN.replace("# NEWS FROM ALPACA", "#"))
        ok(patch(other, check=False) == 1, "refused until the first patch has run")
        (other / MAIN_PATH).write_text(FIXTURE_MAIN)
        (other / FLOAT_PATH).write_text("something else")
        ok(patch(other, check=False) == 1, "a sec_float.py edited by hand is not overwritten")

    del asyncio
    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("patch_sec_edgar_vix self-test passed")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    return patch(Path("."), "--check" in sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
