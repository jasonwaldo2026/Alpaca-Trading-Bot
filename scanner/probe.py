"""
Check what your data providers can actually do.

    python -m scanner.probe            # endpoint check, then freshness
    python -m scanner.probe --fresh    # freshness only

One request per FMP endpoint the alert design would lean on, then a plain
report: which are allowed on your plan, how many float rows the bulk
endpoint returns, how far back 5-minute bars go, and whether those bars
include pre-market.

Then the question no provider's marketing answers: **how old is the newest
bar each one will give me right now?** FMP and Alpaca are asked for the
same symbol at the same moment and their newest bar timestamps are
compared, so "real-time" is measured rather than believed. Run it during
market hours; outside them both simply report the last bar of the session.

Nothing is stored; keys are scrubbed from any error.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

BASE = "https://financialmodelingprep.com/stable/"

#: (label, path, params) — the endpoints the alert design would use.
CHECKS: List[Tuple[str, str, Dict[str, str]]] = [
    ("Company profile", "profile", {"symbol": "AAPL"}),
    ("Quote", "quote", {"symbol": "AAPL"}),
    ("Batch quote (3 symbols)", "batch-quote", {"symbols": "AAPL,MSFT,NVDA"}),
    ("Price change (1d…10y)", "stock-price-change", {"symbol": "AAPL"}),
    ("Bulk float, page 0", "shares-float-all", {"page": "0", "limit": "1000"}),
    ("Screener (price<20, vol>500k)", "company-screener",
     {"priceLowerThan": "20", "volumeMoreThan": "500000", "isActivelyTrading": "true",
      "exchange": "NASDAQ,NYSE,AMEX", "limit": "50"}),
    ("Most actives", "most-actives", {}),
    ("Biggest gainers", "biggest-gainers", {}),
    ("Daily history", "historical-price-eod/full", {"symbol": "AAPL"}),
    ("5-minute bars", "historical-chart/5min", {"symbol": "AAPL"}),
    ("5-minute bars, extended", "historical-chart/5min", {"symbol": "AAPL", "extended": "true"}),
    ("1-minute bars", "historical-chart/1min", {"symbol": "AAPL"}),
    ("1-minute bars, extended", "historical-chart/1min", {"symbol": "AAPL", "extended": "true"}),
    ("Aftermarket quote", "aftermarket-quote", {"symbol": "AAPL"}),
    ("Aftermarket trade", "aftermarket-trade", {"symbol": "AAPL"}),
    ("Stock news", "news/stock", {"symbols": "AAPL", "limit": "5"}),
    ("Earnings calendar", "earnings-calendar", {}),
]


def _scrub(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text


def fetch(path: str, params: Dict[str, str], key: str, timeout: float = 30) -> Tuple[Optional[Any], str]:
    """(json, status) where status is 'ok', 'refused (HTTP n)', or an error."""
    query = urllib.parse.urlencode({**params, "apikey": key})
    try:
        with urllib.request.urlopen(BASE + path + "?" + query, timeout=timeout) as r:
            return json.loads(r.read().decode()), "ok"
    except urllib.error.HTTPError as exc:
        why = {401: "key rejected", 402: "not in your plan", 403: "not in your plan",
               429: "quota used up"}.get(exc.code, "")
        return None, f"refused (HTTP {exc.code}{', ' + why if why else ''})"
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        return None, "error: " + _scrub(str(exc), key)


def describe(label: str, data: Any) -> str:
    """One line of what came back, for the endpoints where the shape matters."""
    if isinstance(data, dict) and "Error Message" in data:
        return "refused: " + str(data["Error Message"])[:80]
    if not isinstance(data, list):
        return "ok"
    n = len(data)
    if label == "Bulk float, page 0":
        with_float = sum(1 for r in data if r.get("floatShares"))
        return f"{n} rows, {with_float} with a float figure"
    if label.startswith(("5-minute bars", "1-minute bars")) or label == "Daily history":
        stamps = sorted(str(r.get("date", "")) for r in data if r.get("date"))
        if not stamps:
            return f"{n} rows, no dates"
        note = f"{n} bars, {stamps[0]} → {stamps[-1]}"
        if "bars" in label and label != "Daily history":
            times = [s[11:16] for s in stamps if len(s) >= 16]
            pre = sum(1 for t in times if t < "09:30")
            post = sum(1 for t in times if t >= "16:00")
            note += (f"; pre-market bars: {pre}, after-hours bars: {post}"
                     if times else "")
            note += ("  ← extended hours INCLUDED" if pre or post
                     else "  ← regular hours only")
        return note
    if label.startswith("Screener"):
        return f"{n} matches (capped at 50 for this check)"
    if label == "Price change (1d…10y)" and n:
        keys = [k for k in data[0] if k != "symbol"]
        return "windows: " + ", ".join(keys)
    return f"{n} rows"


# ── Freshness: how old is the newest bar, right now? ─────────────────────────

#: A liquid name trades every minute of every session, so a stale bar means
#: the provider is behind, not that nobody traded.
FRESH_SYMBOL = "AAPL"


def parse_fmp_stamp(text: str) -> Optional[datetime]:
    """FMP stamps intraday bars 'YYYY-MM-DD HH:MM:SS' in US Eastern."""
    from zoneinfo import ZoneInfo
    try:
        naive = datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return naive.replace(tzinfo=ZoneInfo("America/New_York"))


def age_minutes(stamp: datetime, now: Optional[datetime] = None) -> float:
    now = now or datetime.now(timezone.utc)
    return (now - stamp).total_seconds() / 60


def describe_age(stamp: Optional[datetime], bar_minutes: int,
                 now: Optional[datetime] = None) -> str:
    """
    How old the newest bar is, and what that means.

    A bar is stamped with its **start**, so a just-closed 5-minute bar is
    already 5 minutes old by construction. Anything beyond that is the
    provider being behind — that surplus is the number worth comparing.
    """
    if stamp is None:
        return "no bars returned"
    mins = age_minutes(stamp, now)
    lag = mins - bar_minutes
    if lag < 1:
        verdict = "current — no lag beyond the bar itself"
    elif lag < bar_minutes:
        verdict = f"{lag:.0f} min behind the bar close"
    else:
        verdict = f"{lag:.0f} min behind — over one whole bar late"
    local = stamp.astimezone()
    return f"newest bar {local:%H:%M}, {mins:.0f} min old ({verdict})"


def fmp_freshness(key: str) -> List[Tuple[str, str]]:
    """Newest FMP bar per interval, plain and extended."""
    out = []
    for label, path, minutes, extra in (
        ("FMP 5-minute", "historical-chart/5min", 5, {}),
        ("FMP 5-minute, extended", "historical-chart/5min", 5, {"extended": "true"}),
        ("FMP 1-minute", "historical-chart/1min", 1, {}),
        ("FMP 1-minute, extended", "historical-chart/1min", 1, {"extended": "true"}),
    ):
        data, status = fetch(path, {"symbol": FRESH_SYMBOL, **extra}, key)
        if status != "ok" or not isinstance(data, list) or not data:
            out.append((label, status if status != "ok" else "no bars returned"))
            continue
        stamps = [parse_fmp_stamp(r.get("date")) for r in data]
        stamps = [x for x in stamps if x]
        out.append((label, describe_age(max(stamps) if stamps else None, minutes)))
    return out


def alpaca_freshness() -> List[Tuple[str, str]]:
    """
    Newest Alpaca bar per interval, through the project's own fetcher — so
    this measures what the scanner would actually see, forming bar dropped
    and all.
    """
    try:
        from core.client import AlpacaClient, Credentials
        from core.data import MarketDataFetcher, feed_from_env
    except ImportError as exc:                      # pragma: no cover
        return [("Alpaca", f"cannot import the project's fetcher: {exc}")]

    creds = Credentials.from_env()
    if not creds.is_complete():
        return [("Alpaca", "ALPACA_API_KEY / ALPACA_API_SECRET not set in .env")]

    try:
        client = AlpacaClient(creds)
        feed = feed_from_env()
    except (ValueError, Exception) as exc:          # noqa: BLE001
        return [("Alpaca", f"could not connect: {exc}")]

    feed_note = f" [feed={getattr(feed, 'value', 'account default')}]"
    out = []
    for label, minutes in (("Alpaca 5-minute", 5), ("Alpaca 1-minute", 1)):
        try:
            frames = MarketDataFetcher(client, minutes, feed=feed).get_bars(
                [FRESH_SYMBOL], limit=5
            )
        except Exception as exc:                    # noqa: BLE001
            out.append((label, f"request failed: {exc}"))
            continue
        frame = frames.get(FRESH_SYMBOL)
        if frame is None or frame.empty:
            out.append((label, "no bars returned"))
            continue
        newest = frame.index.max().to_pydatetime()
        out.append((label + feed_note, describe_age(newest, minutes)))
    return out


def freshness_report(key: str) -> int:
    print(f"\nBar freshness for {FRESH_SYMBOL} at {datetime.now():%H:%M:%S} local")
    print("A bar is stamped with its start, so a just-closed 5-minute bar is "
          "5 minutes old.\nAnything past that is the provider running behind.\n")
    rows = fmp_freshness(key) + alpaca_freshness()
    width = max(len(r[0]) for r in rows) + 2
    for label, line in rows:
        print(f"{label:<{width}} {line}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    argv = sys.argv[1:] if argv is None else argv
    key = os.getenv("FMP_API_KEY", "")
    if not key:
        print("FMP_API_KEY is not set in .env.", file=sys.stderr)
        return 1

    if "--fresh" in argv:
        return freshness_report(key)

    print(f"FMP check at {datetime.now():%Y-%m-%d %H:%M} local — {len(CHECKS)} requests\n")
    width = max(len(label) for label, _, _ in CHECKS) + 2
    refused = 0
    for label, path, params in CHECKS:
        data, status = fetch(path, params, key)
        line = describe(label, data) if status == "ok" else status
        if status != "ok" or line.startswith("refused"):
            refused += 1
        print(f"{label:<{width}} {line}")

    print()
    if refused:
        print(f"{refused} endpoint(s) unavailable on this key. The rest are usable as-is.")
    else:
        print("Every endpoint the alert design needs is available on this key.")
    return freshness_report(key)


if __name__ == "__main__":
    sys.exit(main())
