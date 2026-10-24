"""
ibkr_scanner_probe.py -- can IBKR replace FMP for Market Scanner? (2026-10-23)

A one-time, READ-ONLY look at what this IBKR account's API offers for the
jobs Market Scanner used FMP for. It asks IB Gateway five things and says
in plain words what came back:

  1. Scanners   which scan types and filters exist -- above all, is there
                a FLOAT filter, a MARKET CAP filter, and pre-market gainers?
                The full list is saved to ibkr_scanner_params.xml.
  2. Fundamentals  shares outstanding, float and market cap for SPCX
  3. News       which news providers this account can read, and the
                latest SPCX headlines from them (last 3 days)
  4. VIX        whether the VIX index can be read
  5. Samples    a top % gainers scan (under $2bn market cap, over $1) and
                a most-active scan, ten rows each

The connection is opened read-only with its own client id (7706), so it
never bumps Scanner Studio (7701), the alarms (7703) or the rule watcher
(7705). It requests data and nothing else: no order exists in this file,
and no account number is read.

    python ibkr_scanner_probe.py
    python ibkr_scanner_probe.py --self-test

Needs IB Gateway logged in on port 4001, and ib_async.
"""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ElementTree
from typing import Dict, List, Optional, Tuple

HOST = os.environ.get("IBKR_HOST", "127.0.0.1")
PORT = int(os.environ.get("IBKR_PORT", "4001"))
CLIENT_ID = int(os.environ.get("IBKR_SCANNER_PROBE_CLIENT_ID", "7706"))
QUIET = {300, 2104, 2106, 2107, 2108, 2119, 2158}
PARAMS_FILE = "ibkr_scanner_params.xml"

#: Scan types Market Scanner would want, if this account has them.
WANTED_SCANS = ("TOP_PERC_GAIN", "TOP_PERC_LOSE", "MOST_ACTIVE", "HOT_BY_VOLUME",
                "TOP_OPEN_PERC_GAIN", "HIGH_OPEN_GAP", "TOP_AFTER_HOURS_PERC_GAIN",
                "HOT_BY_PRICE", "TOP_VOLUME_RATE")
#: Words that mark a filter worth reporting.
FILTER_WORDS = ("float", "marketcap", "sharesoutstanding", "shares out", "avgvolume",
                "average volume", "changeperc", "premarket", "pre-market", "gap")


def parse_scanner_params(xml: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """({scanCode: displayName}, {filter code: displayName}) from IBKR's
    scanner-parameters XML."""
    root = ElementTree.fromstring(xml)
    scans = {}
    for st in root.iter("ScanType"):
        code = (st.findtext("scanCode") or "").strip()
        if code:
            scans[code] = (st.findtext("displayName") or "").strip()
    filters = {}
    for field in root.iter("AbstractField"):
        code = (field.findtext("code") or "").strip()
        if code:
            filters[code] = (field.findtext("displayName") or "").strip()
    return scans, filters


def interesting_filters(filters: Dict[str, str]) -> Dict[str, str]:
    def hit(code: str, name: str) -> bool:
        text = f"{code} {name}".lower().replace("_", "")
        return any(w.replace(" ", "") in text.replace(" ", "") for w in FILTER_WORDS)
    return {c: n for c, n in sorted(filters.items()) if hit(c, n)}


def parse_snapshot(xml: str) -> Dict[str, Optional[float]]:
    """Shares outstanding, float and market cap ($ millions) from a
    ReportSnapshot, each None if absent."""
    root = ElementTree.fromstring(xml)
    out: Dict[str, Optional[float]] = {"shares_out": None, "float": None, "mktcap_m": None}
    shares = root.find(".//SharesOut")
    if shares is not None:
        try:
            out["shares_out"] = float((shares.text or "").strip())
        except ValueError:
            pass
        try:
            out["float"] = float(shares.get("TotalFloat", ""))
        except ValueError:
            pass
    for ratio in root.iter("Ratio"):
        if ratio.get("FieldName") == "MKTCAP":
            try:
                out["mktcap_m"] = float((ratio.text or "").strip())
            except ValueError:
                pass
    return out


def millions(x: Optional[float]) -> str:
    if x is None:
        return "not given"
    return f"{x / 1e6:,.0f}M" if x >= 1e6 else f"{x:,.0f}"


def probe() -> int:
    try:
        from ib_async import IB, Index, ScannerSubscription, Stock, TagValue
    except ImportError:
        print("ib_async is not installed: python -m pip install ib_async")
        return 1

    errors: List[str] = []
    ib = IB()
    ib.errorEvent += lambda req, code, msg, contract=None: (
        None if code in QUIET else errors.append(f"{code}: {msg}"[:200]))
    try:
        ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=15, readonly=True)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not reach IB Gateway on port {PORT}: {type(exc).__name__}: {exc}")
        print("Is it running and logged in?")
        return 1

    def section(title: str) -> None:
        errors.clear()
        print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))

    def said() -> str:
        return ("; IBKR said: " + " | ".join(errors[-2:])) if errors else ""

    found_float = found_cap = False
    try:
        section("1. Scanners")
        try:
            xml = ib.reqScannerParameters()
            here = os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(here, PARAMS_FILE), "w", encoding="utf-8") as handle:
                handle.write(xml)
            scans, filters = parse_scanner_params(xml)
            print(f"{len(scans)} scan types and {len(filters)} filters "
                  f"(all saved to {PARAMS_FILE}).")
            print("Scan types Market Scanner would use:")
            for code in WANTED_SCANS:
                print(f"  {'yes' if code in scans else 'NO '}  {code:<28} {scans.get(code, '')}")
            picked = interesting_filters(filters)
            found_float = any("float" in f"{c} {n}".lower() for c, n in picked.items())
            found_cap = any("marketcap" in c.lower().replace("_", "") for c in picked)
            print(f"Filters about float, market cap, volume, change and gaps ({len(picked)}):")
            for code, name in picked.items():
                print(f"  {code:<34} {name}")
            print(f"FLOAT filter:      {'YES' if found_float else 'no'}")
            print(f"MARKET CAP filter: {'YES' if found_cap else 'no'}")
        except Exception as exc:  # noqa: BLE001
            print(f"Could not read scanner parameters: {type(exc).__name__}{said()}")

        section("2. Fundamentals for SPCX")
        try:
            stock = Stock("SPCX", "SMART", "USD")
            ib.qualifyContracts(stock)
            xml = ib.reqFundamentalData(stock, "ReportSnapshot")
            if xml:
                f = parse_snapshot(xml)
                print(f"Shares outstanding: {millions(f['shares_out'])}")
                print(f"Float:              {millions(f['float'])}")
                cap = f["mktcap_m"]
                print(f"Market cap:         {'$' + format(cap, ',.0f') + 'M' if cap else 'not given'}")
            else:
                print(f"No fundamentals returned{said()}")
        except Exception as exc:  # noqa: BLE001
            print(f"Fundamentals not available: {type(exc).__name__}{said()}")

        section("3. News providers")
        providers = []
        try:
            providers = ib.reqNewsProviders()
            if providers:
                for p in providers:
                    print(f"  {p.code:<10} {p.name}")
            else:
                print(f"None available to this account{said()}")
        except Exception as exc:  # noqa: BLE001
            print(f"Could not list news providers: {type(exc).__name__}{said()}")
        if providers:
            section("3b. Latest SPCX headlines from IBKR news (3 days)")
            try:
                from datetime import datetime, timedelta, timezone

                stock = Stock("SPCX", "SMART", "USD")
                ib.qualifyContracts(stock)
                end = datetime.now(timezone.utc)
                news = ib.reqHistoricalNews(
                    stock.conId, "+".join(p.code for p in providers),
                    (end - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S.0"),
                    end.strftime("%Y-%m-%d %H:%M:%S.0"), 15)
                news = news if isinstance(news, list) else ([news] if news else [])
                if news:
                    for n in news:
                        print(f"  {str(n.time)[:16]}  {n.providerCode:<8} {n.headline[:90]}")
                else:
                    print(f"  No SPCX headlines from these providers{said()}")
            except Exception as exc:  # noqa: BLE001
                print(f"  Could not read IBKR news: {type(exc).__name__}{said()}")

        section("4. VIX")
        try:
            vix = Index("VIX", "CBOE", "USD")
            ib.qualifyContracts(vix)
            bars = ib.reqHistoricalData(vix, endDateTime="", durationStr="5 D",
                                        barSizeSetting="1 day", whatToShow="TRADES",
                                        useRTH=True, formatDate=1, timeout=20)
            if bars:
                print(f"VIX last close {bars[-1].close:.2f} on {bars[-1].date}")
            else:
                print(f"No VIX data{said()}")
        except Exception as exc:  # noqa: BLE001
            print(f"VIX not available: {type(exc).__name__}{said()}")

        for title, code, tags in (
                ("5a. Sample: top % gainers, $1+, under $2bn", "TOP_PERC_GAIN",
                 [TagValue("priceAbove", "1"), TagValue("marketCapBelow1e6", "2000")]),
                ("5b. Sample: most active, $1+", "MOST_ACTIVE",
                 [TagValue("priceAbove", "1")])):
            section(title)
            try:
                sub = ScannerSubscription(instrument="STK", locationCode="STK.US.MAJOR",
                                          scanCode=code, numberOfRows=10)
                rows = ib.reqScannerData(sub, [], tags)
                if rows:
                    print("  " + ", ".join(f"{r.rank + 1}. {r.contractDetails.contract.symbol}"
                                           for r in rows))
                else:
                    print(f"  No rows (outside market hours some scans are empty){said()}")
            except Exception as exc:  # noqa: BLE001
                print(f"  Scan failed: {type(exc).__name__}{said()}")
    finally:
        ib.disconnect()

    print("\n=== What it means " + "=" * 42)
    print(f"Float from IBKR:      {'possible (scanner filter)' if found_float else 'see section 2'}")
    print(f"Market cap from IBKR: {'possible (scanner filter)' if found_cap else 'see section 2'}")
    print("Paste everything above back into the chat.")
    return 0


def self_test() -> int:
    failures = []

    def ok(cond, msg):
        if not cond:
            failures.append(msg)

    params = """<ScanParameterResponse><ScanTypeList>
<ScanType><displayName>Top % Gainers</displayName><scanCode>TOP_PERC_GAIN</scanCode></ScanType>
<ScanType><displayName>Most Active</displayName><scanCode>MOST_ACTIVE</scanCode></ScanType>
</ScanTypeList><FilterList>
<RangeFilter><id>mc</id><AbstractField><code>marketCapAbove1e6</code><displayName>Market Cap Above</displayName></AbstractField>
<AbstractField><code>marketCapBelow1e6</code><displayName>Market Cap Below</displayName></AbstractField></RangeFilter>
<RangeFilter><id>fl</id><AbstractField><code>floatSharesAbove</code><displayName>Float Shares Above</displayName></AbstractField></RangeFilter>
<RangeFilter><id>dv</id><AbstractField><code>dividendYieldAbove</code><displayName>Dividend Yield Above</displayName></AbstractField></RangeFilter>
</FilterList></ScanParameterResponse>"""
    scans, filters = parse_scanner_params(params)
    ok(scans == {"TOP_PERC_GAIN": "Top % Gainers", "MOST_ACTIVE": "Most Active"}, f"scans: {scans}")
    ok(len(filters) == 4, f"filters: {filters}")
    picked = interesting_filters(filters)
    ok(set(picked) == {"marketCapAbove1e6", "marketCapBelow1e6", "floatSharesAbove"},
       f"float and market cap are picked, dividends are not: {picked}")

    snap = """<ReportSnapshot><CoGeneralInfo>
<SharesOut Date="2026-09-30" TotalFloat="1250000000.0">4600000000.0</SharesOut>
</CoGeneralInfo><Ratios><Group ID="Price and volume">
<Ratio FieldName="MKTCAP" Type="N">712345.6</Ratio></Group></Ratios></ReportSnapshot>"""
    f = parse_snapshot(snap)
    ok(f == {"shares_out": 4.6e9, "float": 1.25e9, "mktcap_m": 712345.6}, f"snapshot: {f}")
    ok(parse_snapshot("<ReportSnapshot/>") == {"shares_out": None, "float": None,
                                               "mktcap_m": None}, "missing fields are None")
    ok(millions(4.6e9) == "4,600M" and millions(None) == "not given", "millions()")

    source = open(__file__, encoding="utf-8").read()
    for forbidden in ("placeOrder", "LimitOrder", "MarketOrder", "accountValues",
                      "managedAccounts"):
        if forbidden in source.replace(f'"{forbidden}"', ""):
            failures.append(f"this probe must not mention {forbidden}")

    if failures:
        print("SELF-TEST FAILED:")
        for x in failures:
            print(f"  - {x}")
        return 1
    print("ibkr_scanner_probe self-test passed")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(self_test() if "--self-test" in sys.argv else probe())
