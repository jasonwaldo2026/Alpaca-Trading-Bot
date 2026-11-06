"""
auto_cycle.py -- sell SPCX on a drop, buy it back on the turn. PAPER ONLY.

The experiment chosen on 22 Oct 2026, run with fake money so it can be
judged against simply holding before any real share is risked:

  SELL      when the position is worth $1,200 less than it cost: the
            price falls $1,200 / shares below the LAST PURCHASE PRICE
            ($0.69 a share on 1,750). The line is fixed; it does not
            follow the price up.
  BUY BACK  when SPCX rises $600 / shares ($0.34 on 1,750) off the
            lowest price since that sale. The buy-back price becomes
            the new purchase price, and the next line is $1,200 under it.
  AT MOST   2 sells a day, and no new sell once the day's round trips
            have cost $2,000 more than holding would have
  NO SELL   after 3:50 PM; if still out at 3:55 PM it buys back, so
            the day ends holding the same shares it started with
  NOT AT    the start: if SPCX is already under the line when the
  START     program starts, it rings and does not sell. The line arms
            once the price is back above it.

The purchase price is the paper account's average cost for SPCX, read
each morning; IBKR updates it after every buy-back.

Regular hours only, 9:30 AM - 4:00 PM ET: the 59-day test was run on
those hours, and the numbers say nothing about the evening.

What the test said, so nobody is surprised: on 59 days of SPCX this
rule did worse than holding -- about -$220 a day held from 29 Jul, and
about -$405 a day if bought fresh each morning (2-cycle cap, 1,750
shares). It acts only while the price is near the purchase price, and
it does not protect a gain. This program exists to see whether that
holds up on new days, on a paper account, before deciding anything.
Every sell and buy-back goes to the phone, and a summary at 4 PM says
what the day's cycles gained or cost compared with holding.

It CANNOT trade real money. Before any order it checks, every time it
connects:
  - the port is not a live one (4001 / 7496)
  - every account IB Gateway reports is a paper account (IBKR numbers
    those DU...; a live account is U...). One live account and it stops.
There is no switch to turn either check off.

It only sells shares the paper account already holds -- never more, so
it can never go short -- and only buys back what it sold.

    python auto_cycle.py --dry-run     # watch live prices, pretend to trade
    python auto_cycle.py               # trade the IBKR PAPER account
    python auto_cycle.py --self-test   # check the rules, no network

--dry-run reads prices from the IB Gateway you already run (port 4001,
read-only) and pretends to hold 1,750 shares bought at the first price
it sees (or at --cost); nothing is sent.

The real run needs a second IB Gateway logged in to the PAPER account,
on port 4002, with "Read-Only API" unticked in its API settings, and
market data shared with the paper account (Client Portal -> Settings ->
Paper Trading Account). Keep this file beside spcx_alert.py,
feed_check.py and heartbeat.py, which it imports.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
import time as time_mod
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable, List, Optional, Tuple

SYMBOL = "SPCX"
#: Dollars on the whole position, turned into a price per share by the shares held.
SELL_DROP = 1200.0
REBUY_RISE = 600.0
MAX_CYCLES = 2
#: Dollars the day's round trips may cost against holding before it stops selling.
MAX_COST = 2000.0
DRY_RUN_SHARES = 1750

OPEN = time(9, 30)
CLOSE = time(16, 0)
LAST_SELL = time(15, 50)
BACK_IN_BY = time(15, 55)

CHECK_SECONDS = 5
FILL_WAIT_SECONDS = 30
#: Limit orders cross the spread by this much: sell at bid - 0.10, buy at ask + 0.10.
LIMIT_CUSHION = 0.10
#: A buy-back that fails this many times in a row stops the program and rings.
BUY_TRIES = 3
BLIND_AFTER = timedelta(minutes=2)

IBKR_HOST = os.environ.get("IBKR_HOST", "127.0.0.1")
PAPER_PORT = 4002
WATCH_PORT = int(os.environ.get("IBKR_PORT", "4001"))
LIVE_PORTS = (4001, 7496)
PAPER_ACCOUNT_PREFIX = "DU"
CLIENT_ID = int(os.environ.get("IBKR_AUTO_CYCLE_CLIENT_ID", "7704"))

TRADES_CSV = "auto_cycle_trades.csv"
TITLE = "AUTO CYCLE (paper)"
PRIORITY_QUIET, PRIORITY_NORMAL, PRIORITY_LOUD = -1, 0, 1


def ok(x) -> bool:
    try:
        return x is not None and math.isfinite(float(x)) and float(x) > 0
    except (TypeError, ValueError):
        return False


def money(x: float) -> str:
    return f"-${-x:,.0f}" if x < 0 else f"${x:,.0f}"


class NotPaper(RuntimeError):
    """The connection could reach real money. Nothing is sent."""


def check_paper(port: int, accounts: List[str]) -> None:
    """Raise unless this is unmistakably a paper account. Both tests, always."""
    if port in LIVE_PORTS:
        raise NotPaper(f"port {port} is IB Gateway's LIVE port. The paper "
                       f"Gateway listens on {PAPER_PORT}.")
    if not accounts:
        raise NotPaper("IB Gateway reported no account, so it cannot be "
                       "shown to be paper.")
    live = [a for a in accounts if not a.upper().startswith(PAPER_ACCOUNT_PREFIX)]
    if live:
        raise NotPaper(f"{len(live)} account(s) here are not paper accounts "
                       f"(paper accounts start {PAPER_ACCOUNT_PREFIX}).")


@dataclass
class Cycle:
    sold_at: datetime
    shares: int
    sell_price: float
    why: str
    bought: List[Tuple[datetime, int, float]] = field(default_factory=list)

    @property
    def bought_shares(self) -> int:
        return sum(s for _, s, _ in self.bought)

    @property
    def outstanding(self) -> int:
        return self.shares - self.bought_shares

    @property
    def versus_holding(self) -> float:
        """Dollars gained (+) or lost (-) against having held these shares.

        Shares not yet bought back count at nothing either way: the day's
        summary says separately that they are still out.
        """
        return sum((self.sell_price - p) * s for _, s, p in self.bought)


class Cycler:
    """The rules, and nothing else: prices in, "sell" / "buy" out.

    No broker, no clock, no network, so every rule is tested directly.
    The caller places the order and reports the fill with sold() or
    bought(); until then nothing here changes state.
    """

    def __init__(self, drop: float = SELL_DROP, rise: float = REBUY_RISE,
                 max_cycles: int = MAX_CYCLES, max_cost: float = MAX_COST):
        self.drop, self.rise = drop, rise
        self.max_cycles, self.max_cost = max_cycles, max_cost
        self.shares = 0
        self.purchase: Optional[float] = None
        self.low: Optional[float] = None
        self.armed = False
        self.cycles: List[Cycle] = []
        self.stopped: Optional[str] = None

    def start(self, shares: int, price: float, purchase: float) -> None:
        """Holding `shares` bought at `purchase`; SPCX is at `price` now.

        Already under the line is not a reason to sell this second: the
        line arms once the price has been above it.
        """
        self.shares, self.purchase = shares, purchase
        self.armed = price > self.sell_line

    @property
    def sell_line(self) -> float:
        return self.purchase - self.drop / self.shares

    def rise_step(self, shares: int) -> float:
        return self.rise / shares

    @property
    def out(self) -> Optional[Cycle]:
        """The cycle still waiting to buy back, if any."""
        if self.cycles and self.cycles[-1].outstanding > 0:
            return self.cycles[-1]
        return None

    @property
    def versus_holding(self) -> float:
        return sum(c.versus_holding for c in self.cycles)

    def why_no_sell(self, now: datetime) -> Optional[str]:
        if self.stopped:
            return self.stopped
        if len(self.cycles) >= self.max_cycles:
            return f"{self.max_cycles} sells today, the most allowed"
        if -self.versus_holding >= self.max_cost:
            return (f"today's round trips cost {money(-self.versus_holding)}, "
                    f"the limit is {money(self.max_cost)}")
        if now.time() >= LAST_SELL:
            return f"no new sells after {LAST_SELL:%H:%M}"
        return None

    def see(self, now: datetime, price: float) -> Optional[Tuple[str, str]]:
        cycle = self.out
        if cycle is not None:
            if self.stopped:
                return None
            self.low = min(self.low, price)
            if now.time() >= BACK_IN_BY:
                return "buy", f"back in before the close (it is {now:%H:%M})"
            if price >= self.low + self.rise_step(cycle.shares):
                return "buy", (f"up {price - self.low:.2f} from the low of "
                               f"{self.low:.2f}")
            return None
        if self.shares <= 0 or self.purchase is None:
            return None
        line = self.sell_line
        if not self.armed:
            self.armed = price > line
            return None
        if price <= line and not self.why_no_sell(now):
            return "sell", (f"{self.purchase - price:.2f} under the purchase "
                            f"price of {self.purchase:.2f}")
        return None

    def sold(self, at: datetime, shares: int, price: float, why: str) -> None:
        self.cycles.append(Cycle(at, shares, price, why))
        self.low = price

    def bought(self, at: datetime, shares: int, price: float) -> None:
        """Back in: once the cycle is whole, the shares bought back take
        their price into the purchase price, as IBKR's average cost does."""
        cycle = self.out
        cycle.bought.append((at, shares, price))
        if cycle.outstanding <= 0:
            kept = self.shares - cycle.shares
            paid = sum(s * p for _, s, p in cycle.bought)
            self.purchase = (kept * self.purchase + paid) / self.shares
            self.armed = True


class Session:
    """One trading day: the Cycler, a broker, the phone and the trade log."""

    def __init__(self, broker, cycler: Cycler, notify: Callable[[str, int], None],
                 record: Callable[[dict], None], dry_run: bool):
        self.broker, self.cycler = broker, cycler
        self.notify, self.record = notify, record
        self.tag = "[dry run] " if dry_run else ""
        self.buy_misses = 0
        self.last_price_at: Optional[datetime] = None
        self.blind_said = False

    def begin(self, now: datetime) -> bool:
        shares = int(self.broker.shares())
        last, _, _ = self.broker.quote()
        if shares <= 0:
            self.notify(f"{self.tag}No {SYMBOL} shares in the account, so nothing "
                        f"to sell today. Buy some in the paper account to test.",
                        PRIORITY_NORMAL)
            return False
        if not ok(last):
            return False
        cost = self.broker.cost()
        purchase = cost if ok(cost) else last
        c = self.cycler
        c.start(shares, last, purchase)
        self.last_price_at = now
        self.notify(f"{self.tag}Watching {shares:,} {SYMBOL} at {last:.2f}, bought "
                    f"at {purchase:.2f}. Sells at {c.sell_line:.2f} "
                    f"({money(c.drop)} under), buys back {c.rise_step(shares):.2f} "
                    f"({money(c.rise)}) off the low. At most {c.max_cycles} "
                    f"sells today.", PRIORITY_QUIET)
        if not c.armed:
            self.notify(f"{self.tag}{SYMBOL} at {last:.2f} is ALREADY under the "
                        f"sell line of {c.sell_line:.2f}. Not selling now: the "
                        f"line arms once the price is back above it. Decide "
                        f"yourself whether to hold.", PRIORITY_LOUD)
        return True

    def step(self, now: datetime) -> None:
        last, bid, ask = self.broker.quote()
        if not ok(last):
            if (self.last_price_at and now - self.last_price_at > BLIND_AFTER
                    and not self.blind_said):
                self.blind_said = True
                held = self.cycler.out
                extra = (f" It is OUT of {held.outstanding:,} shares and cannot "
                         f"buy back blind." if held else "")
                self.notify(f"{self.tag}No live {SYMBOL} price for "
                            f"{BLIND_AFTER.seconds // 60} minutes.{extra} Check "
                            f"IB Gateway.", PRIORITY_LOUD)
            return
        self.last_price_at, self.blind_said = now, False
        action = self.cycler.see(now, last)
        if action is None:
            return
        side, why = action
        if side == "sell":
            self._sell(now, last, bid, why)
        else:
            self._buy(now, last, ask, why)

    def _sell(self, now, last, bid, why) -> None:
        shares = min(int(self.broker.shares()), self.cycler.shares)
        self.cycler.shares = max(shares, 0)
        if shares <= 0:
            self.cycler.stopped = "the account no longer holds any shares"
            self.notify(f"{self.tag}Would sell ({why}) but the account holds no "
                        f"{SYMBOL}. Stopped for today.", PRIORITY_LOUD)
            return
        limit = round((bid if ok(bid) else last) - LIMIT_CUSHION, 2)
        filled, price = self.broker.order("SELL", shares, limit)
        if filled <= 0:
            self.cycler.stopped = "a sell did not fill"
            self.notify(f"{self.tag}SELL of {shares:,} at {limit:.2f} did not "
                        f"fill in {FILL_WAIT_SECONDS}s and was cancelled. Still "
                        f"holding. Stopped for today.", PRIORITY_LOUD)
            return
        self.cycler.sold(now, filled, price, why)
        n = len(self.cycler.cycles)
        self.record({"side": "SELL", "shares": filled, "price": price,
                     "cycle": n, "why": why, "at": now})
        part = f" (of {shares:,} asked)" if filled < shares else ""
        self.notify(f"{self.tag}SOLD {filled:,}{part} {SYMBOL} at {price:.2f}, "
                    f"{why}. Cycle {n} of {self.cycler.max_cycles}. Buys back "
                    f"{self.cycler.rise_step(filled):.2f} above the low.",
                    PRIORITY_NORMAL)

    def _buy(self, now, last, ask, why) -> None:
        cycle = self.cycler.out
        limit = round((ask if ok(ask) else last) + LIMIT_CUSHION, 2)
        filled, price = self.broker.order("BUY", cycle.outstanding, limit)
        if filled > 0:
            self.buy_misses = 0
            self.cycler.bought(now, filled, price)
            self.record({"side": "BUY", "shares": filled, "price": price,
                         "cycle": len(self.cycler.cycles), "why": why, "at": now})
            left = cycle.outstanding
            versus = (cycle.sell_price - price) * filled
            line = "" if left else f" Next sell line {self.cycler.sell_line:.2f}."
            self.notify(f"{self.tag}BOUGHT BACK {filled:,} {SYMBOL} at {price:.2f}, "
                        f"{why}. Sold at {cycle.sell_price:.2f}: "
                        f"{money(versus)} vs holding."
                        + (f" Still out {left:,}." if left else line),
                        PRIORITY_NORMAL)
            return
        self.buy_misses += 1
        if self.buy_misses >= BUY_TRIES:
            self.cycler.stopped = "the buy-back would not fill"
            self.notify(f"{self.tag}Could not buy back {cycle.outstanding:,} "
                        f"{SYMBOL} ({BUY_TRIES} tries, last at {limit:.2f}). "
                        f"Stopped. Buy them back by hand if you want them.",
                        PRIORITY_LOUD)

    def summary(self) -> str:
        c = self.cycler
        if not c.cycles:
            return f"{self.tag}No sells today: holding all day."
        lines = [f"{self.tag}Today {len(c.cycles)} cycle(s):"]
        for i, cy in enumerate(c.cycles, 1):
            buys = ", ".join(f"{p:.2f}" for _, _, p in cy.bought) or "not bought back"
            lines.append(f"{i}. sold {cy.shares:,} at {cy.sell_price:.2f} "
                         f"{cy.sold_at:%H:%M}, back at {buys}: "
                         f"{money(cy.versus_holding)}")
        lines.append(f"Versus just holding: {money(c.versus_holding)}.")
        if c.out:
            lines.append(f"STILL OUT {c.out.outstanding:,} shares.")
        return "\n".join(lines)


class Gateway:
    """IB Gateway through ib_async. Orders only on a verified paper account."""

    QUIET = (300, 2104, 2106, 2107, 2108, 2119, 2158)

    def __init__(self, port: int, dry_run: bool, shares: int = DRY_RUN_SHARES,
                 cost: Optional[float] = None):
        self.port, self.dry_run, self.pretend = port, dry_run, shares
        self.pretend_cost = cost
        self.ib = self.contract = self.ticker = None
        self.account = ""

    def _connect(self) -> None:
        from ib_async import IB, Stock

        ib = IB()
        ib.connect(IBKR_HOST, self.port, clientId=CLIENT_ID, timeout=10,
                   readonly=self.dry_run)
        try:
            if not self.dry_run:
                accounts = list(ib.managedAccounts())
                check_paper(self.port, accounts)
                self.account = accounts[0]
            contract = Stock(SYMBOL, "SMART", "USD")
            if not [c for c in ib.qualifyContracts(contract) if c]:
                raise RuntimeError(f"IBKR does not know {SYMBOL}")
            self.ticker = ib.reqMktData(contract, "", False, False)
        except BaseException:
            ib.disconnect()
            raise
        self.ib, self.contract = ib, contract

    def _live(self):
        if self.ib is None or not self.ib.isConnected():
            self.close()
            self._connect()
        return self.ib

    def shares(self) -> int:
        if self.dry_run:
            return self.pretend
        ib = self._live()
        return int(sum(p.position for p in self._positions(ib)))

    def _positions(self, ib):
        return [p for p in ib.positions(self.account)
                if p.contract.symbol == SYMBOL and p.contract.secType == "STK"]

    def cost(self) -> Optional[float]:
        """The average price paid per share, or None if not known."""
        if self.dry_run:
            if self.pretend_cost is None:
                last = self.quote()[0]
                self.pretend_cost = last if ok(last) else None
            return self.pretend_cost
        held = self._positions(self._live())
        return float(held[0].avgCost) if len(held) == 1 else None

    def quote(self) -> Tuple[float, float, float]:
        self._live()
        t = self.ticker
        return t.last, t.bid, t.ask

    def order(self, side: str, shares: int, limit: float) -> Tuple[int, float]:
        if self.dry_run:
            last, bid, ask = self.quote()
            price = bid if side == "SELL" else ask
            price = price if ok(price) else last
            if side == "SELL":
                self.pretend -= shares
            else:
                kept = self.pretend * (self.pretend_cost or price)
                self.pretend += shares
                self.pretend_cost = (kept + shares * price) / self.pretend
            return shares, round(price, 2)
        from ib_async import LimitOrder

        ib = self._live()
        check_paper(self.port, list(ib.managedAccounts()))
        trade = ib.placeOrder(self.contract, LimitOrder(
            side, shares, limit, tif="DAY", account=self.account))
        waited = 0.0
        while not trade.isDone() and waited < FILL_WAIT_SECONDS:
            ib.sleep(0.5)
            waited += 0.5
        if not trade.isDone():
            ib.cancelOrder(trade.order)
            ib.sleep(2)
        filled = int(trade.orderStatus.filled or 0)
        return filled, round(float(trade.orderStatus.avgFillPrice or 0), 2)

    def sleep(self, seconds: float) -> None:
        if self.ib is not None and self.ib.isConnected():
            self.ib.sleep(seconds)          # keeps the price stream flowing
        else:
            time_mod.sleep(seconds)

    def close(self) -> None:
        if self.ib is not None:
            try:
                self.ib.disconnect()
            except Exception:  # noqa: BLE001 -- closing a dead link is not news
                pass
        self.ib = None


def csv_recorder(path: str, dry_run: bool) -> Callable[[dict], None]:
    def record(row: dict) -> None:
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as handle:
            out = csv.writer(handle)
            if new:
                out.writerow(["date", "time", "mode", "side", "shares", "price",
                              "cycle", "why"])
            at = row["at"]
            out.writerow([f"{at:%Y-%m-%d}", f"{at:%H:%M:%S}",
                          "dry run" if dry_run else "paper", row["side"],
                          row["shares"], f"{row['price']:.2f}", row["cycle"],
                          row["why"]])
    return record


def phone(send: bool) -> Callable[[str, int], None]:
    from spcx_alert import send_pushover

    def notify(message: str, priority: int) -> None:
        print(f"{datetime.now():%H:%M:%S}  {message}", flush=True)
        if send:
            error = send_pushover(message, title=TITLE, priority=priority)
            if error:
                print(f"           (phone: {error})", flush=True)
    return notify


def run(args) -> int:
    import heartbeat
    from feed_check import ET

    here = os.path.dirname(os.path.abspath(__file__))
    alive = os.path.join(here, heartbeat.AUTO_CYCLE)
    busy = heartbeat.already_running(alive, datetime.now(ET).replace(tzinfo=None))
    if busy:
        print(busy)
        return 1
    port = args.port or (WATCH_PORT if args.dry_run else PAPER_PORT)
    gateway = Gateway(port, args.dry_run, args.shares, args.cost)
    if not args.dry_run:
        try:
            gateway._live()
        except NotPaper as exc:
            print(f"STOP: {exc} Nothing was sent.")
            return 1
        except Exception as exc:  # noqa: BLE001 -- checked again at the open
            print(f"IB Gateway on port {port} is not answering yet ({exc}). "
                  f"Will keep trying; the paper check runs on every connect.")
    notify = phone(not args.quiet)
    record = csv_recorder(os.path.join(here, TRADES_CSV), args.dry_run)
    done: Optional[date] = None
    print(f"auto_cycle {'DRY RUN on' if args.dry_run else 'PAPER on'} port {port}. "
          f"Ctrl+C stops it.")
    try:
        while True:
            now = datetime.now(ET).replace(tzinfo=None)
            heartbeat.write(alive, now)
            if (now.weekday() >= 5 or done == now.date()
                    or not OPEN <= now.time() < CLOSE):
                time_mod.sleep(30)
                continue
            session = Session(gateway, Cycler(args.drop, args.rise, args.cycles,
                                              args.max_cost),
                              notify, record, args.dry_run)
            try:
                started = session.begin(now)
            except NotPaper as exc:
                print(f"STOP: {exc} Nothing was sent.")
                return 1
            except Exception as exc:  # noqa: BLE001 -- Gateway not up yet
                print(f"{now:%H:%M:%S}  waiting for IB Gateway: {exc}")
                gateway.close()
                time_mod.sleep(30)
                continue
            if not started:
                done = now.date()
                continue
            failing_since = None
            while True:
                now = datetime.now(ET).replace(tzinfo=None)
                if now.time() >= CLOSE:
                    break
                heartbeat.write(alive, now)
                try:
                    session.step(now)
                    failing_since = None
                except NotPaper as exc:
                    notify(f"STOPPED: {exc} Nothing was sent.", PRIORITY_LOUD)
                    return 1
                except Exception as exc:  # noqa: BLE001 -- the link drops; retry
                    print(f"{now:%H:%M:%S}  {type(exc).__name__}: {exc}")
                    gateway.close()
                    failing_since = failing_since or now
                    if now - failing_since > BLIND_AFTER and not session.blind_said:
                        session.blind_said = True
                        notify(f"Lost IB Gateway at {failing_since:%H:%M}. "
                               f"Check it.", PRIORITY_LOUD)
                gateway.sleep(CHECK_SECONDS)
            notify(session.summary(), PRIORITY_NORMAL)
            done = now.date()
    except KeyboardInterrupt:
        out = session.cycler.out if "session" in locals() else None
        if out:
            print(f"Stopped while OUT of {out.outstanding:,} shares, sold at "
                  f"{out.sell_price:.2f}. Buy them back by hand if you want them.")
        print("Stopped.")
        return 0
    finally:
        gateway.close()


# ---------------------------------------------------------------- self-test


class FakeBroker:
    def __init__(self, quotes, shares=1000, fills=None, cost=100.0):
        self.quotes = list(quotes)
        self.held, self.paid = shares, cost
        self.fills = fills or {}            # call number -> shares filled (None = all)
        self.orders: List[Tuple[str, int, float]] = []

    def shares(self) -> int:
        return self.held

    def cost(self):
        return self.paid

    def quote(self):
        last = self.quotes.pop(0) if len(self.quotes) > 1 else self.quotes[0]
        return (last, last - 0.02, last + 0.02) if ok(last) else (last, last, last)

    def order(self, side, shares, limit):
        self.orders.append((side, shares, limit))
        want = self.fills.get(len(self.orders), shares)
        filled = shares if want is None else want
        self.held += -filled if side == "SELL" else filled
        return filled, round(limit, 2) if filled else 0.0


def self_test() -> int:
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    day = datetime(2026, 10, 26)
    t = lambda h, m=0, s=0: day.replace(hour=h, minute=m, second=s)  # noqa: E731

    # The paper guard: both tests, every way round.
    for port, accounts, safe in ((4002, ["DU123"], True), (4001, ["DU123"], False),
                                 (7496, ["DU123"], False), (4002, ["U28936042"], False),
                                 (4002, ["DU1", "U2"], False), (4002, [], False),
                                 (4002, ["du9"], True)):
        try:
            check_paper(port, accounts)
            check(safe, f"port {port} {accounts} must be refused")
        except NotPaper:
            check(not safe, f"port {port} {accounts} is paper and should pass")

    # The rules alone: 1,000 shares, so $1,200 is $1.20 a share and $600 is $0.60.
    c = Cycler(1200, 600, 2, 2000)
    c.start(1000, 100.0, 100.0)
    check(c.armed and abs(c.sell_line - 98.80) < 1e-9, f"line 98.80: {c.sell_line}")
    check(c.see(t(10), 105.0) is None, "a rise is not a sell")
    check(abs(c.sell_line - 98.80) < 1e-9, "the line does not follow the price up")
    check(c.see(t(10, 1), 98.81) is None, "a cent above the line is not a sell")
    act = c.see(t(10, 2), 98.80)
    check(act and act[0] == "sell" and "100.00" in act[1], f"at the line it sells: {act}")
    c.sold(t(10, 2), 1000, 98.78, "test")
    check(c.see(t(10, 3), 97.0) is None and c.low == 97.0, "the low follows it down")
    check(c.see(t(10, 4), 97.59) is None, "$0.59 off the low is not the turn")
    act = c.see(t(10, 5), 97.60)
    check(act and act[0] == "buy", f"$0.60 off the low buys back: {act}")
    c.bought(t(10, 5), 1000, 97.62)
    check(c.out is None and abs(c.purchase - 97.62) < 1e-9,
          f"the buy-back is the new purchase price: {c.purchase}")
    check(abs(c.sell_line - 96.42) < 1e-9, f"the next line is $1.20 under it: {c.sell_line}")
    check(abs(c.versus_holding - 1160.0) < 1e-6,
          f"sold 98.78, bought 97.62 x1000 is +$1,160: {c.versus_holding}")
    # Second cycle, then the cap.
    act = c.see(t(11), 96.40)
    check(act and act[0] == "sell", f"a second cycle is allowed: {act}")
    c.sold(t(11), 1000, 96.40, "test")
    c.see(t(11, 1), 96.0)
    c.bought(t(11, 2), 1000, 96.60)
    check(c.see(t(12), 50.0) is None, "no third sell with a cap of 2")
    check("2 sells" in (c.why_no_sell(t(12)) or ""), "the cap says why")

    # A partial cycle: the purchase price mixes, as IBKR's average cost does.
    c = Cycler(1200, 600, 2, 2000)
    c.start(1000, 100.0, 100.0)
    c.sold(t(10), 400, 98.8, "test")
    c.bought(t(10, 5), 400, 99.0)
    check(abs(c.purchase - 99.6) < 1e-9, f"600 at 100 and 400 at 99 is 99.60: {c.purchase}")
    c = Cycler(1200, 600, 2, 2000)
    c.start(1000, 100.0, 100.0)
    c.sold(t(10), 500, 98.8, "test")
    c.see(t(10, 1), 97.0)
    check(c.see(t(10, 2), 97.6) is None, "$600 on the 500 sold is $1.20 a share, not $0.60")
    act = c.see(t(10, 3), 98.2)
    check(act and act[0] == "buy", f"$1.20 off the low buys back the 500: {act}")

    # Already under the line at the start: no sale until it has been back above.
    c = Cycler(1200, 600, 2, 2000)
    c.start(1000, 98.0, 100.0)
    check(not c.armed, "starting under the line is not armed")
    check(c.see(t(10), 97.0) is None, "under the line at the start does not sell")
    check(c.see(t(10, 1), 99.0) is None and c.armed, "back above the line arms it")
    act = c.see(t(10, 2), 98.7)
    check(act and act[0] == "sell", f"armed, a fall to the line sells: {act}")

    # The cost limit.
    c = Cycler(1200, 600, 5, 2000)
    c.start(1000, 100.0, 100.0)
    c.sold(t(10), 1000, 98.8, "test")
    c.bought(t(10, 5), 1000, 101.0)                   # -$2,200 against holding
    c.see(t(11), 110.0)
    check(c.see(t(11, 1), 50.0) is None, "past the cost limit, no new sell")
    check("cost" in (c.why_no_sell(t(11)) or ""), "the cost limit says why")

    # The clock: no sell late; back in before the close.
    c = Cycler(1200, 600, 2, 2000)
    c.start(1000, 100.0, 100.0)
    check(c.see(t(15, 50), 90.0) is None, "no new sell at 15:50")
    c = Cycler(1200, 600, 2, 2000)
    c.start(1000, 100.0, 100.0)
    c.sold(t(15, 40), 1000, 98.8, "test")
    check(c.see(t(15, 54), 97.0) is None, "still waiting for the turn at 15:54")
    act = c.see(t(15, 55), 96.5)
    check(act and act[0] == "buy" and "close" in act[1], f"back in by 15:55: {act}")
    check(Cycler().see(t(10), 50.0) is None, "never started, never sells")

    # A whole session through a fake broker.
    said: List[Tuple[str, int]] = []
    rows: List[dict] = []
    note = lambda m, p: said.append((m, p))  # noqa: E731
    # 1,750 bought at 100: sell line 99.31 ($0.6857), buy back $0.3429 off the low.
    b = FakeBroker([100.0, 101.0, 99.32, 99.31, 98.5, 98.84, 98.85, 99.0],
                   shares=1750)
    s = Session(b, Cycler(1200, 600, 2, 2000), note, rows.append, dry_run=False)
    check(s.begin(t(9, 30)), "a session with shares begins")
    for i in range(7):
        s.step(t(9, 31 + i))
    check([o[0] for o in b.orders] == ["SELL", "BUY"], f"one round trip: {b.orders}")
    check(b.orders[0][1] == 1750 and b.orders[1][1] == 1750, "sells and buys back all 1,750")
    check(abs(b.orders[0][2] - (99.29 - 0.10)) < 1e-9, f"sell limit is bid - 0.10: {b.orders[0]}")
    check(abs(b.orders[1][2] - (98.87 + 0.10)) < 1e-9, f"buy limit is ask + 0.10: {b.orders[1]}")
    check(b.held == 1750, "the day ends holding what it started with")
    check([r["side"] for r in rows] == ["SELL", "BUY"], "both fills are logged")
    check(any("SOLD 1,750" in m and p == PRIORITY_NORMAL for m, p in said), "the sell reaches the phone")
    check(any("BOUGHT BACK 1,750" in m for m, _ in said), "the buy-back reaches the phone")
    check("Versus just holding: $385." in s.summary(), f"the summary compares with holding:\n{s.summary()}")
    check(any("Next sell line 98.28" in m for m, _ in said), f"the new line is announced: {said}")

    # Starting under the line rings and sells nothing.
    said.clear()
    b = FakeBroker([98.0, 97.5, 97.0], shares=1000, cost=100.0)
    s = Session(b, Cycler(), note, rows.append, dry_run=False)
    s.begin(t(9, 30))
    s.step(t(9, 31))
    s.step(t(9, 32))
    check(not b.orders, f"under the line at the start, nothing is sold: {b.orders}")
    check(any(p == PRIORITY_LOUD and "ALREADY under" in m for m, p in said),
          f"and the phone is told loudly: {said}")
    b = FakeBroker([100.0, 98.0], shares=1000, cost=None)
    s = Session(b, Cycler(), note, rows.append, dry_run=False)
    s.begin(t(9, 30))
    check(s.cycler.purchase == 100.0, "no known cost: the first price stands in")

    # Never sells more than the account holds; nothing held, nothing to do.
    b = FakeBroker([100.0, 98.0], shares=0)
    s = Session(b, Cycler(), note, rows.append, dry_run=False)
    check(not s.begin(t(9, 30)) and not b.orders, "no shares: no session, no order")
    b = FakeBroker([100.0, 100.0, 98.0], shares=1000)
    s = Session(b, Cycler(), note, rows.append, dry_run=False)
    s.begin(t(9, 30))
    b.held = 400                                       # sold by hand meanwhile
    s.step(t(9, 31))
    s.step(t(9, 32))
    check(b.orders and b.orders[0][1] == 400, f"sells only what is held: {b.orders}")

    # A partial sell buys back only what was sold.
    b = FakeBroker([100.0, 100.0, 98.0, 97.0, 98.5], shares=1000, fills={1: 600})
    s = Session(b, Cycler(), note, rows.append, dry_run=False)
    s.begin(t(9, 30))
    for i in range(4):
        s.step(t(9, 31 + i))
    check(b.orders[-1][0] == "BUY" and b.orders[-1][1] == 600,
          f"buys back the 600 sold, not 1,000: {b.orders}")

    # An unfilled sell stops the day; an unfilled buy rings after 3 tries.
    said.clear()
    b = FakeBroker([100.0, 100.0, 98.0, 90.0], shares=1000, fills={1: 0})
    s = Session(b, Cycler(), note, rows.append, dry_run=False)
    s.begin(t(9, 30))
    for i in range(3):
        s.step(t(9, 31 + i))
    check(len(b.orders) == 1 and s.cycler.stopped, "an unfilled sell is not retried")
    check(any(p == PRIORITY_LOUD and "did not fill" in m for m, p in said), "and it rings")
    said.clear()
    b = FakeBroker([100.0, 100.0, 98.0, 97.0, 99.0, 99.0, 99.0, 99.0],
                   shares=1000, fills={2: 0, 3: 0, 4: 0})
    s = Session(b, Cycler(), note, rows.append, dry_run=False)
    s.begin(t(9, 30))
    for i in range(7):
        s.step(t(9, 31 + i))
    check([o[0] for o in b.orders] == ["SELL", "BUY", "BUY", "BUY"],
          f"three buy tries then stop: {b.orders}")
    check(any(p == PRIORITY_LOUD and "Could not buy back" in m for m, p in said),
          "a failed buy-back rings loud")
    check("STILL OUT 1,000" in s.summary(), "the summary says it is still out")

    # No price for 2 minutes rings once.
    said.clear()
    b = FakeBroker([100.0, float("nan")], shares=1000)
    s = Session(b, Cycler(), note, rows.append, dry_run=False)
    s.begin(t(9, 30))
    s.step(t(9, 31))
    s.step(t(9, 33))
    s.step(t(9, 34))
    check([p for _, p in said].count(PRIORITY_LOUD) == 1, f"blind rings once: {said}")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("auto_cycle self-test passed")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        description="Sell SPCX on a drop, buy back on the turn. Paper account only.")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="read prices only and pretend to trade")
    parser.add_argument("--shares", type=int, default=DRY_RUN_SHARES,
                        help="shares to pretend to hold in a dry run")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--cost", type=float, default=None,
                        help="purchase price to pretend in a dry run "
                             "(default: the first price seen)")
    parser.add_argument("--drop", type=float, default=SELL_DROP,
                        help="dollars under the purchase price that sells (default 1200)")
    parser.add_argument("--rise", type=float, default=REBUY_RISE,
                        help="dollars off the low that buys back (default 600)")
    parser.add_argument("--cycles", type=int, default=MAX_CYCLES)
    parser.add_argument("--max-cost", type=float, default=MAX_COST)
    parser.add_argument("--quiet", action="store_true", help="print only, no phone")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    from feed_check import load_env

    load_env()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
