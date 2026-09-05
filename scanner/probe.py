"""
Check what your FMP key can actually do.

    python -m scanner.probe

One request per endpoint the alert design would lean on, then a plain
report: which are allowed on your plan, how many float rows the bulk
endpoint returns, how far back 5-minute bars go, and whether those bars
include pre-market. Nothing is stored; the key is scrubbed from any error.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
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
    ("1-minute bars", "historical-chart/1min", {"symbol": "AAPL"}),
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
    if label in ("5-minute bars", "1-minute bars", "Daily history"):
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


def main(argv: Optional[List[str]] = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    key = os.getenv("FMP_API_KEY", "")
    if not key:
        print("FMP_API_KEY is not set in .env.", file=sys.stderr)
        return 1

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
