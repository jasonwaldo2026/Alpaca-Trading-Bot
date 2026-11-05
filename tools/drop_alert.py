"""
drop_alert.py -- one alarm when a position's stock falls a set percent (5 Nov 2026).

Jason: "I just got in and I want to know when the stock price goes down
.625%". Every few seconds it reads the latest trade from Alpaca (the full
market; Blue Ocean overnight) and, the first time the price is at or under
the line, sends ONE Pushover alarm that repeats until you tap Acknowledge.
Then it stops.

  --from entry   the line is fixed: entry x (1 - pct)            (default)
  --from high    the line trails: highest price since start x (1 - pct)

Prices only. It reads no account and places no order: what to do at the
line is your call.

    python drop_alert.py --entry 138.65                 # SPCX, 0.625% under the entry
    python drop_alert.py --entry 138.65 --from high     # 0.625% under the high since now
    python drop_alert.py --entry 133.64 --from high --dollars 0.50   # 50 cents under the high
    python drop_alert.py --symbol TSLA --entry 250 --pct 1
    python drop_alert.py --entry 138.65 --dry-run       # print, do not send
    python drop_alert.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, time as dtime
from typing import Optional

from feed_check import ET, load_credentials, load_env

LATEST = "https://data.alpaca.markets/v2/stocks/trades/latest?symbols={symbol}&feed={feed}"


def feed_for(now: datetime) -> str:
    """Blue Ocean from 20:00 to 04:00 (the overnight session), else the full market."""
    t = now.astimezone(ET).time()
    return "boats" if t >= dtime(20, 0) or t < dtime(4, 0) else "sip"


def alarm_line(entry: float, high: float, pct: float, mode: str,
               dollars: Optional[float] = None) -> float:
    """The price that rings: under the entry, or under the highest price seen,
    by `dollars` if given (5 Nov: "Lets do 50 cents"), else by `pct` percent."""
    base = max(entry, high) if mode == "high" else entry
    return round(base - dollars, 2) if dollars else round(base * (1 - pct / 100.0), 2)


def message(symbol: str, price: float, entry: float, high: float, pct: float,
            mode: str, line: float, dollars: Optional[float] = None) -> str:
    base = f"the high ${high:,.2f}" if mode == "high" else f"your entry ${entry:,.2f}"
    size = f"${dollars:.2f}" if dollars else f"{pct:g}%"
    return (f"{symbol} DOWN {size}: now ${price:,.2f}, from {base} "
            f"(alarm line ${line:,.2f}). Prices only - your call.")


def latest_price(symbol: str, now: datetime) -> float:
    key, secret = load_credentials()
    request = urllib.request.Request(
        LATEST.format(symbol=symbol, feed=feed_for(now)),
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    with urllib.request.urlopen(request, timeout=10) as response:
        return float(json.loads(response.read().decode())["trades"][symbol]["p"])


def watch(symbol: str, entry: float, pct: float, mode: str, every: float,
          dry_run: bool, read=latest_price, send=None, clock=None, sleep=time.sleep,
          dollars: Optional[float] = None) -> int:
    clock = clock or (lambda: datetime.now(ET))
    if send is None:
        from spcx_alert import PRIORITY_EMERGENCY, send_pushover

        def send(text):
            return send_pushover(text, title=f"{symbol} drop alarm", priority=PRIORITY_EMERGENCY)
    high = entry
    size = f"${dollars:.2f}" if dollars else f"{pct:g}%"
    print(f"Watching {symbol}: alarm {size} under "
          f"{'the highest price from now' if mode == 'high' else f'your entry ${entry:,.2f}'}"
          f" -> ${alarm_line(entry, high, pct, mode, dollars):,.2f} now. Ctrl+C stops it.")
    shown: Optional[str] = None
    while True:
        now = clock()
        try:
            price = read(symbol, now)
        except Exception as exc:  # noqa: BLE001 -- one bad read is not the end
            print(f"  {now:%H:%M:%S}  could not read the price ({type(exc).__name__}); retrying")
            sleep(every)
            continue
        high = max(high, price)
        line = alarm_line(entry, high, pct, mode, dollars)
        row = f"  {now:%H:%M:%S}  {symbol} ${price:,.2f}   alarm at ${line:,.2f}"
        if row != shown:
            print(row)
            shown = row
        if price <= line:
            text = message(symbol, price, entry, high, pct, mode, line, dollars)
            if dry_run:
                print(f"  [dry run] would send: {text}")
            else:
                error = send(text)
                print(f"  ALARM SENT: {text}" if not error else f"  NOT SENT ({error}): {text}")
                if error:
                    sleep(every)
                    continue          # try again next read
            return 0
        sleep(every)


def self_test() -> int:
    failures = []

    def ok(cond, msg):
        if not cond:
            failures.append(msg)

    ok(alarm_line(138.65, 138.65, 0.625, "entry") == 137.78, "0.625% under 138.65 is 137.78")
    ok(alarm_line(138.65, 140.00, 0.625, "entry") == 137.78, "from the entry, the line never moves")
    ok(alarm_line(138.65, 140.00, 0.625, "high") == 139.12, "from the high, the line trails it up")
    ok(alarm_line(138.65, 137.00, 0.625, "high") == 137.78, "from the high, it starts at the entry")
    ok(alarm_line(133.64, 133.64, 0.625, "high", 0.50) == 133.14
       and alarm_line(133.64, 136.00, 0.625, "high", 0.50) == 135.50
       and alarm_line(133.64, 136.00, 0.625, "entry", 0.50) == 133.14, "50 cents under the high, or the entry")
    ok(feed_for(datetime(2026, 11, 5, 21, 0, tzinfo=ET)) == "boats"
       and feed_for(datetime(2026, 11, 5, 3, 59, tzinfo=ET)) == "boats"
       and feed_for(datetime(2026, 11, 5, 4, 0, tzinfo=ET)) == "sip", "overnight reads Blue Ocean")

    def run(prices, mode, fail_first=False, dollars=None):
        sent, it = [], iter(prices)
        state = {"fail": fail_first}

        def read(symbol, now):
            return next(it)

        def send(text):
            sent.append(text)
            if state["fail"]:
                state["fail"] = False
                return "down"
            return None
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            watch("SPCX", 138.65, 0.625, mode, 0, False, read=read, send=send,
                  clock=lambda: datetime(2026, 11, 5, 10, 0, tzinfo=ET), sleep=lambda s: None,
                  dollars=dollars)
        return sent

    sent = run([138.9, 138.2, 137.79, 137.78, 137.0], "entry")
    ok(len(sent) == 1 and "now $137.78" in sent[0] and "your entry $138.65" in sent[0]
       and "Prices only" in sent[0], f"one alarm, at the line, not before: {sent}")
    sent = run([139.5, 140.0, 139.2, 139.12, 138.0], "high")
    ok(len(sent) == 1 and "now $139.12" in sent[0] and "the high $140.00" in sent[0],
       f"trailing: 0.625% off the $140 high rings at $139.12: {sent}")
    sent = run([139.0, 140.0, 139.60, 139.50, 139.0], "high", dollars=0.50)
    ok(len(sent) == 1 and "DOWN $0.50: now $139.50" in sent[0] and "the high $140.00" in sent[0],
       f"50 cents off the $140 high rings at $139.50, not $139.60: {sent}")
    sent = run([137.5, 137.4], "entry", fail_first=True)
    ok(len(sent) == 2, f"a failed send is retried on the next read: {sent}")

    source = open(__file__, encoding="utf-8").read()
    for forbidden in ("TradingClient", "submit_order", "place_order"):
        ok(forbidden not in source.replace(f'"{forbidden}"', ""), f"no {forbidden} here")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("drop_alert self-test passed")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--symbol", default="SPCX")
    parser.add_argument("--entry", type=float, help="your entry price")
    parser.add_argument("--pct", type=float, default=0.625, help="percent drop that rings (0.625)")
    parser.add_argument("--dollars", type=float, help="ring this many dollars under, instead of --pct")
    parser.add_argument("--from", dest="mode", choices=("entry", "high"), default="entry")
    parser.add_argument("--every", type=float, default=5.0, help="seconds between reads (5)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not args.entry:
        parser.error("--entry is required, e.g. --entry 138.65")
    load_env()
    return watch(args.symbol.upper(), args.entry, args.pct, args.mode, args.every, args.dry_run,
                 dollars=args.dollars)


if __name__ == "__main__":
    raise SystemExit(main())
