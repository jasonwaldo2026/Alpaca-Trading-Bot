"""
External scanner adapter tests — temp files only, no network.

Run:  python test_external_scanner.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

from external_scanner import (
    CallableScanner,
    FileScanner,
    records_to_results,
)
from scanner import ScanResult
from trading_bot import BotConfig

PASS, FAIL = 0, 0
EMPTY = pd.DataFrame()
CFG = BotConfig(api_key="x", api_secret="y")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def write(tmp, name, content):
    p = Path(tmp) / name
    p.write_text(content)
    return p


def main():
    tmp = tempfile.mkdtemp()

    print("\n=== Field mapping ===")
    res = records_to_results([
        {"ticker": "nvda", "rating": 0.9},
        {"symbol": "AAPL", "score": 0.5, "price": 190.0, "why": "breakout"},
        {"Symbol": "BTC/USD", "Rank": 0.7},
    ])
    check("maps ticker/rating aliases", res[0].symbol == "NVDA", [r.symbol for r in res])
    check("uppercases symbols", all(r.symbol == r.symbol.upper() for r in res))
    check("sorts by score desc",
          [r.symbol for r in res] == ["NVDA", "BTC/USD", "AAPL"], [r.symbol for r in res])
    check("infers crypto from slash",
          next(r for r in res if r.symbol == "BTC/USD").asset_class == "crypto")
    check("infers stock otherwise",
          next(r for r in res if r.symbol == "AAPL").asset_class == "stock")
    check("carries reason through",
          "breakout" in next(r for r in res if r.symbol == "AAPL").reasons)
    check("carries price through",
          next(r for r in res if r.symbol == "AAPL").price == 190.0)

    print("\n=== Malformed input is skipped, not fatal ===")
    res = records_to_results([
        {"symbol": "AAPL", "score": 0.8},
        {"no_symbol_here": 1},
        None,
        "not-a-dict",
        {"symbol": "", "score": 0.5},
        {"symbol": "MSFT", "score": "not-a-number"},
    ])
    check("keeps the valid records", {r.symbol for r in res} == {"AAPL", "MSFT"},
          [r.symbol for r in res])
    check("bad score coerces to 0.0",
          next(r for r in res if r.symbol == "MSFT").score == 0.0)

    print("\n=== Ordering preserved when no scores supplied ===")
    res = records_to_results([{"symbol": "ZZZ"}, {"symbol": "AAA"}, {"symbol": "MMM"}])
    check("keeps external order when unscored",
          [r.symbol for r in res] == ["ZZZ", "AAA", "MMM"], [r.symbol for r in res])

    print("\n=== max_symbols cap ===")
    res = records_to_results([{"symbol": f"S{i}", "score": 0.5} for i in range(500)],
                             max_symbols=10)
    check("caps runaway input", len(res) == 10, len(res))

    print("\n=== FileScanner: JSON ===")
    p = write(tmp, "scan.json", json.dumps(
        [{"symbol": "NVDA", "score": 0.9}, {"symbol": "AMD", "score": 0.6}]))
    out = FileScanner(str(p)).scan(EMPTY, EMPTY, CFG)
    check("reads a bare JSON list", [r.symbol for r in out] == ["NVDA", "AMD"],
          [r.symbol for r in out])

    p = write(tmp, "wrapped.json", json.dumps(
        {"generated": "now", "results": [{"symbol": "TSLA", "score": 0.8}]}))
    out = FileScanner(str(p)).scan(EMPTY, EMPTY, CFG)
    check("reads records under a wrapper key",
          [r.symbol for r in out] == ["TSLA"], [r.symbol for r in out])

    print("\n=== FileScanner: CSV ===")
    p = write(tmp, "scan.csv", "ticker,score,why\nSPY,0.7,trend\nQQQ,0.4,volume\n")
    out = FileScanner(str(p)).scan(EMPTY, EMPTY, CFG)
    check("reads CSV with aliased headers",
          [r.symbol for r in out] == ["SPY", "QQQ"], [r.symbol for r in out])
    check("CSV score parsed as float", out[0].score == 0.7, out[0].score)

    print("\n=== Staleness guard ===")
    p = write(tmp, "stale.json", json.dumps([{"symbol": "AAPL", "score": 0.9}]))
    old = time.time() - (5 * 3600)          # 5 hours old
    os.utime(p, (old, old))

    fresh_out = FileScanner(str(p), max_age_minutes=None).scan(EMPTY, EMPTY, CFG)
    check("age check disabled by max_age_minutes=None", len(fresh_out) == 1)

    stale_out = FileScanner(str(p), max_age_minutes=120).scan(EMPTY, EMPTY, CFG)
    check("stale file yields empty shortlist (no trades)", stale_out == [], stale_out)

    print("\n=== Missing file / unreachable scanner ===")
    missing = FileScanner(str(Path(tmp) / "nope.json"))
    check("missing file yields empty shortlist", missing.scan(EMPTY, EMPTY, CFG) == [])

    print("\n=== fail_open reuses last good result ===")
    p = write(tmp, "flaky.json", json.dumps([{"symbol": "GOOD", "score": 0.9}]))
    fo = FileScanner(str(p), fail_open=True)
    first = fo.scan(EMPTY, EMPTY, CFG)
    check("first read succeeds", [r.symbol for r in first] == ["GOOD"])
    p.unlink()
    second = fo.scan(EMPTY, EMPTY, CFG)
    check("falls back to last good when file disappears",
          [r.symbol for r in second] == ["GOOD"], second)

    fc = FileScanner(str(Path(tmp) / "never.json"), fail_open=False)
    check("fail_open=False never invents a shortlist",
          fc.scan(EMPTY, EMPTY, CFG) == [])

    print("\n=== CallableScanner ===")
    out = CallableScanner(lambda: [{"symbol": "NVDA", "score": 0.9}]).scan(EMPTY, EMPTY, CFG)
    check("wraps a callable returning dicts", [r.symbol for r in out] == ["NVDA"])

    out = CallableScanner(lambda: ["AAPL", "MSFT"]).scan(EMPTY, EMPTY, CFG)
    check("accepts a plain list of tickers",
          [r.symbol for r in out] == ["AAPL", "MSFT"], [r.symbol for r in out])

    df = pd.DataFrame({"symbol": ["SPY", "IWM"], "score": [0.9, 0.3]})
    out = CallableScanner(lambda: df).scan(EMPTY, EMPTY, CFG)
    check("accepts a DataFrame", [r.symbol for r in out] == ["SPY", "IWM"],
          [r.symbol for r in out])

    def boom():
        raise RuntimeError("scanner crashed")
    check("callable raising yields empty shortlist",
          CallableScanner(boom).scan(EMPTY, EMPTY, CFG) == [])

    print("\n=== Results are ScanResult instances the bot can consume ===")
    out = CallableScanner(lambda: ["AAPL"]).scan(EMPTY, EMPTY, CFG)
    check("returns ScanResult", isinstance(out[0], ScanResult))
    check("summary() renders", isinstance(out[0].summary(), str))

    print(f"\n{'─' * 46}\n  {PASS} passed, {FAIL} failed\n")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
