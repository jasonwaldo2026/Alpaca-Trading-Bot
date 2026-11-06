"""
ibkr_probe.py -- what does IB Gateway actually send for one stock, right now?

A one-minute, READ-ONLY look, run before building the overnight alarms on
it. For SPCX (or --symbol) it asks IB Gateway for the same things the
alarms would need -- the last trade, bid and ask, every trade as it
prints, 5-second bars, and the last two hours of 1-minute bars -- on two
routes: SMART (what Scanner Studio's ladder uses) and OVERNIGHT (IBKR's
8 PM - 3:50 AM venue). Then it says, in plain words, what arrived.

The connection is opened read-only, with its own client id so it never
bumps Scanner Studio's. It only requests and cancels market data: no
order object exists in this file, and no account number is read.

    python ibkr_probe.py                  # SPCX, 60 seconds
    python ibkr_probe.py --seconds 120
    python ibkr_probe.py --symbol SPY

Needs IB Gateway running and logged in on port 4001, and the ib_async
package: python -m pip install ib_async
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime

HOST = os.environ.get("IBKR_HOST", "127.0.0.1")
PORT = int(os.environ.get("IBKR_PORT", "4001"))
CLIENT_ID = int(os.environ.get("IBKR_PROBE_CLIENT_ID", "7702"))   # Studio uses 7701
QUIET = {300, 2104, 2106, 2107, 2108, 2119, 2158}


def ok(x) -> bool:
    try:
        return x is not None and float(x) == float(x) and float(x) > 0
    except (TypeError, ValueError):
        return False


async def probe(symbol: str, seconds: int) -> int:
    try:
        from ib_async import IB, Stock
    except ImportError:
        print("ib_async is not installed for this Python. Install it with:\n"
              "    python -m pip install ib_async\nthen run this again.")
        return 1

    ib = IB()
    notes: list[str] = []
    ib.errorEvent += lambda reqId, code, msg, contract: (
        None if code in QUIET else notes.append(f"{code}: {msg}"[:160]))
    try:
        await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, timeout=10, readonly=True)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not reach IB Gateway on port {PORT} ({type(exc).__name__}).\n"
              f"Is it running and logged in?")
        return 1
    ib.reqMarketDataType(1)
    print(f"Connected read-only to IB Gateway ({HOST}:{PORT}, client {CLIENT_ID}) "
          f"at {datetime.now():%H:%M:%S}. Watching {symbol} for {seconds}s...\n")

    routes = {}
    for exchange in ("SMART", "OVERNIGHT"):
        c = Stock(symbol, exchange, "USD")
        try:
            q = await ib.qualifyContractsAsync(c)
        except Exception as exc:  # noqa: BLE001
            q = None
            notes.append(f"{exchange}: qualify failed ({type(exc).__name__})")
        if not q or not getattr(c, "conId", 0):
            routes[exchange] = None
            continue
        r = {"contract": c, "prints": 0, "bars5": 0, "hist": None, "last_print": None}
        r["top"] = ib.reqMktData(c, "", False, False)
        r["tbt"] = ib.reqTickByTickData(c, "AllLast")

        def count_prints(t, r=r):
            for tick in t.tickByTicks:
                if ok(getattr(tick, "price", None)):
                    r["prints"] += 1
                    r["last_print"] = (getattr(tick, "time", None), tick.price, tick.size)
        r["tbt"].updateEvent += count_prints
        r["rt"] = ib.reqRealTimeBars(c, 5, "TRADES", False)

        def count_bars(bars, has_new, r=r):
            if has_new:
                r["bars5"] += 1
        r["rt"].updateEvent += count_bars
        try:
            hist = await asyncio.wait_for(ib.reqHistoricalDataAsync(
                c, "", "7200 S", "1 min", "TRADES", useRTH=False), 20)
            r["hist"] = (len(hist), hist[-1].date if hist else None)
        except Exception as exc:  # noqa: BLE001
            r["hist"] = (0, f"failed ({type(exc).__name__})")
        routes[exchange] = r

    await asyncio.sleep(seconds)

    print(f"Results at {datetime.now():%H:%M:%S}\n")
    for exchange, r in routes.items():
        if r is None:
            print(f"  {exchange:<10} IBKR does not know {symbol} on this route\n")
            continue
        top = r["top"]
        print(f"  {exchange:<10} last {top.last if ok(top.last) else '-'}   "
              f"bid {top.bid if ok(top.bid) else '-'}   ask {top.ask if ok(top.ask) else '-'}")
        print(f"  {'':<10} trades printed while watching: {r['prints']}"
              + (f"   (latest {r['last_print'][1]} x {r['last_print'][2]} at "
                 f"{r['last_print'][0]})" if r["last_print"] else ""))
        print(f"  {'':<10} 5-second bars received: {r['bars5']}")
        n, last = r["hist"]
        print(f"  {'':<10} 1-minute history, last 2 hours: {n} bars"
              + (f", newest {last}" if last else ""))
        print()
        try:
            ib.cancelMktData(r["contract"])
            ib.cancelTickByTickData(r["contract"], "AllLast")
            ib.cancelRealTimeBars(r["rt"])
        except Exception:  # noqa: BLE001
            pass
    if notes:
        print("  Messages from IBKR:")
        for n in notes[-8:]:
            print(f"    {n}")
    ib.disconnect()
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="What IB Gateway sends for one stock.")
    parser.add_argument("--symbol", default="SPCX")
    parser.add_argument("--seconds", type=int, default=60)
    args = parser.parse_args()
    return asyncio.run(probe(args.symbol.upper(), max(10, args.seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
