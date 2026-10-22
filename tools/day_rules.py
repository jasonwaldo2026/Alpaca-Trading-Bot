"""
day_rules.py -- the day-trading rules, watched. READ-ONLY.

Reads the IBKR account through the IB Gateway already running for the
overnight alarms (port 4001), with a read-only connection: it can see
the day's P&L, the fills and the margin in use, and it cannot place,
change or cancel anything. There is no order object in this file.

What it says, and when:

  09:25      the rules, once a day. After a big win day (+$3,000 or
             more) it leads with rule 1: go light, let the day develop.
             If the account is already on margin it says so (rule 6).
  RULE 2     day's P&L at -$4,000: LOUD warning.
             at -$5,000: EMERGENCY "THE DAY IS OVER", once (tap to stop
             it repeating). Any trade after that rings LOUD again.
  RULE 3     2 losing trades in a row: LOUD "stop for today". Each
             further loss in the same run rings again.
  RULE 6     margin in use, 09:30-16:00, while SPCX is NOT trending up:
             LOUD, at most every 30 minutes. "Trending up" here means
             all three: price above the day's VWAP, above the 09:30
             open, and VWAP higher than 15 minutes ago.

Rule 5 (S&P negative) rides on the 09:31 opening call in open_candles.py.

What it cannot see: Robinhood. Only the IBKR account behind IB Gateway
is read, so trades placed at Robinhood count toward none of these.
The day's P&L is IBKR's own, so a held position's move counts too: an
overnight position that opens $5,000 down has ended the day by rule 2.

    python day_rules.py --watch         # run it (04:00-20:00, weekdays)
    python day_rules.py --status        # the numbers it sees, now
    python day_rules.py --self-test     # check the rules, no network

Keep this file beside spcx_alert.py, feed_check.py and heartbeat.py,
which it imports. Needs ib_async, like the overnight alarms.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time as time_mod
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

SYMBOL = "SPCX"
LOSS_WARN = 4000.0
LOSS_LIMIT = 5000.0
BIG_WIN = 3000.0
LOSING_RUN = 2
MARGIN_EVERY = timedelta(minutes=30)
TREND_LOOKBACK = 15                     # one-minute bars: VWAP must be rising over these

WATCH_FROM, WATCH_UNTIL = time(4, 0), time(20, 0)
REMIND_AT, REMIND_LATEST = time(9, 25), time(9, 45)
REGULAR_OPEN, REGULAR_CLOSE = time(9, 30), time(16, 0)
CHECK_SECONDS = 60
BLIND_AFTER = timedelta(minutes=10)

IBKR_HOST = os.environ.get("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.environ.get("IBKR_PORT", "4001"))
CLIENT_ID = int(os.environ.get("IBKR_RULES_CLIENT_ID", "7705"))
#: IBKR's "no value" for a fill's realized P&L.
UNSET = 1e300

STATE_PATH = "day_rules_state.json"
TITLE = "DAY RULES"
PRIORITY_NORMAL, PRIORITY_LOUD, PRIORITY_EMERGENCY = 0, 1, 2

RULES_TEXT = (
    "1. After a big win day: go light; do not enter until the day develops.\n"
    "2. Down $5,000 on the day: the day is over. No exceptions.\n"
    "3. Entries keep going down: stop trading for the day.\n"
    "4. Choppy stock: stay out.\n"
    "5. S&P negative: extreme caution; best not to trade.\n"
    "6. No all-in, no margin, until the day is trending up.")


def money(x: float) -> str:
    return f"-${-x:,.0f}" if x < 0 else f"+${x:,.0f}"


def finite(x) -> bool:
    try:
        return x is not None and math.isfinite(float(x)) and abs(float(x)) < UNSET
    except (TypeError, ValueError):
        return False


@dataclass
class Snapshot:
    """What the account looks like now. None means "could not tell"."""
    at: datetime
    daily_pnl: Optional[float] = None
    #: Realized P&L of each closing order today, oldest first.
    exits: List[float] = field(default_factory=list)
    fills: int = 0
    on_margin: Optional[bool] = None
    trending_up: Optional[bool] = None


def losing_run(exits: List[float]) -> int:
    """Losing trades in a row, counted back from the latest. A win ends
    the run; a trade that broke exactly even neither adds nor ends it."""
    run = 0
    for pnl in reversed(exits):
        if pnl < 0:
            run += 1
        elif pnl > 0:
            break
    return run


def trending_up(bars: pd.DataFrame, lookback: int = TREND_LOOKBACK) -> Optional[bool]:
    """Whether the regular session is trending up, from its complete
    one-minute bars: above VWAP, above the open, VWAP rising. None until
    there are enough bars to say."""
    from feed_check import add_vwap

    if bars is None or len(bars) <= lookback:
        return None
    vwap = add_vwap(bars)["vwap"]
    last = float(bars["close"].iloc[-1])
    if not finite(vwap.iloc[-1]) or not finite(vwap.iloc[-1 - lookback]):
        return None
    return bool(last > float(vwap.iloc[-1])
                and last > float(bars["open"].iloc[0])
                and float(vwap.iloc[-1]) > float(vwap.iloc[-1 - lookback]))


class Rules:
    """One day's rules: snapshots in, phone messages out.

    No network and no clock of its own, so each rule is tested directly.
    """

    def __init__(self, day: date, yesterday_pnl: Optional[float] = None,
                 warn: float = LOSS_WARN, limit: float = LOSS_LIMIT,
                 big_win: float = BIG_WIN, run: int = LOSING_RUN):
        self.day, self.yesterday = day, yesterday_pnl
        self.warn, self.limit, self.big_win, self.run = warn, limit, big_win, run
        self.reminded = self.warned = False
        self.over_fills: Optional[int] = None
        self.run_said = 0
        self.margin_said: Optional[datetime] = None

    def reminder(self, snap: Snapshot) -> Optional[Tuple[int, str]]:
        t = snap.at.time()
        if self.reminded or not REMIND_AT <= t <= REMIND_LATEST:
            return None
        self.reminded = True
        lines = []
        if finite(self.yesterday) and self.yesterday >= self.big_win:
            lines.append(f"Yesterday {money(self.yesterday)}. RULE 1: go light "
                         f"today. Do not enter until the day develops.")
        if snap.on_margin:
            lines.append("You are ON MARGIN going into the open. RULE 6: no "
                         "margin until the day is trending up.")
        lines.append(RULES_TEXT)
        return PRIORITY_NORMAL, "\n".join(lines)

    def check(self, snap: Snapshot) -> List[Tuple[int, str]]:
        said: List[Tuple[int, str]] = []
        note = self.reminder(snap)
        if note:
            said.append(note)
        pnl = snap.daily_pnl
        if finite(pnl):
            if self.over_fills is None and pnl <= -self.limit:
                self.over_fills = snap.fills
                said.append((PRIORITY_EMERGENCY,
                             f"THE DAY IS OVER. Down {money(pnl)} today. RULE 2: "
                             f"{money(-self.limit)} ends the day. No more trades."))
            elif not self.warned and self.over_fills is None and pnl <= -self.warn:
                self.warned = True
                said.append((PRIORITY_LOUD,
                             f"Down {money(pnl)} today. {money(-self.limit)} ends "
                             f"the day (RULE 2). {money(self.limit + pnl)[1:]} left."))
        if self.over_fills is not None and snap.fills > self.over_fills:
            self.over_fills = snap.fills
            said.append((PRIORITY_LOUD, "A trade AFTER the day was over. RULE 2: "
                                        "the day is over. Stop."))
        run = losing_run(snap.exits)
        if run < self.run:
            self.run_said = 0
        elif run > self.run_said:
            self.run_said = run
            said.append((PRIORITY_LOUD,
                         f"{run} losing trades in a row. RULE 3: stop trading "
                         f"for today."))
        regular = REGULAR_OPEN <= snap.at.time() < REGULAR_CLOSE
        if regular and snap.on_margin and snap.trending_up is False:
            if self.margin_said is None or snap.at - self.margin_said >= MARGIN_EVERY:
                self.margin_said = snap.at
                said.append((PRIORITY_LOUD,
                             f"On MARGIN and {SYMBOL} is not trending up (under "
                             f"VWAP, under the open, or VWAP falling). RULE 6: "
                             f"no margin until the day trends up."))
        return said


# ------------------------------------------------------------- state file


def load_state(path: str) -> Dict[str, float]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return {k: float(v) for k, v in data.get("pnl", {}).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def save_pnl(path: str, day: date, pnl: float) -> None:
    """Keep the day's last P&L, for tomorrow's rule 1. Two weeks is plenty."""
    days = load_state(path)
    days[day.isoformat()] = round(float(pnl), 2)
    keep = dict(sorted(days.items())[-14:])
    try:
        with open(f"{path}.tmp", "w", encoding="utf-8") as handle:
            json.dump({"pnl": keep}, handle, indent=1)
        os.replace(f"{path}.tmp", path)
    except OSError:
        pass


def previous_pnl(days: Dict[str, float], today: date) -> Optional[float]:
    before = [k for k in sorted(days) if k < today.isoformat()]
    return days[before[-1]] if before else None


# ------------------------------------------------------------- IB Gateway


class Account:
    """The IBKR account, read through a READ-ONLY connection."""

    QUIET = (300, 2104, 2106, 2107, 2108, 2119, 2158)

    def __init__(self):
        self.ib = self.pnl = self.contract = None
        self.account = ""

    def _connect(self) -> None:
        from ib_async import IB, Stock

        ib = IB()
        ib.connect(IBKR_HOST, IBKR_PORT, clientId=CLIENT_ID, timeout=10,
                   readonly=True)
        try:
            accounts = list(ib.managedAccounts())
            if not accounts:
                raise RuntimeError("IB Gateway reported no account")
            self.account = accounts[0]
            self.pnl = ib.reqPnL(self.account)
            contract = Stock(SYMBOL, "SMART", "USD")
            if not [c for c in ib.qualifyContracts(contract) if c]:
                raise RuntimeError(f"IBKR does not know {SYMBOL}")
            ib.sleep(2)                     # let the P&L and account values arrive
        except BaseException:
            ib.disconnect()
            raise
        self.ib, self.contract = ib, contract

    def _live(self):
        if self.ib is None or not self.ib.isConnected():
            self.close()
            self._connect()
        return self.ib

    def snapshot(self, now: datetime, day: date) -> Snapshot:
        ib = self._live()
        snap = Snapshot(at=now)
        if self.pnl is not None and finite(self.pnl.dailyPnL):
            snap.daily_pnl = float(self.pnl.dailyPnL)
        today = []
        from feed_check import ET

        for f in ib.fills():
            when = f.time.astimezone(ET) if f.time.tzinfo else f.time
            if when.date() == day:
                today.append((when, f))
        snap.fills = len(today)
        closing: Dict[int, Tuple[datetime, float]] = {}
        for when, f in sorted(today, key=lambda x: x[0]):
            realized = getattr(f.commissionReport, "realizedPNL", None)
            if finite(realized) and realized != 0:
                first, total = closing.get(f.execution.orderId, (when, 0.0))
                closing[f.execution.orderId] = (first, total + float(realized))
        snap.exits = [pnl for _, pnl in sorted(closing.values(), key=lambda x: x[0])]
        values = {v.tag: v.value for v in ib.accountValues(self.account)
                  if v.currency in ("USD", "BASE")}
        try:
            gross = float(values["GrossPositionValue"])
            net = float(values["NetLiquidation"])
            snap.on_margin = gross > net + 1.0
        except (KeyError, ValueError):
            pass
        if REGULAR_OPEN <= now.time() < REGULAR_CLOSE:
            snap.trending_up = trending_up(self._session_bars(now))
        return snap

    def _session_bars(self, now: datetime) -> Optional[pd.DataFrame]:
        start = now.replace(hour=REGULAR_OPEN.hour, minute=REGULAR_OPEN.minute,
                            second=0, microsecond=0)
        seconds = int((now - start).total_seconds()) + 120
        bars = self.ib.reqHistoricalData(
            self.contract, endDateTime="", durationStr=f"{seconds} S",
            barSizeSetting="1 min", whatToShow="TRADES", useRTH=True,
            formatDate=2, timeout=30)
        if not bars:
            return None
        from feed_check import ET

        rows = [{"at": b.date.astimezone(ET).replace(tzinfo=None), "open": b.open,
                 "high": b.high, "low": b.low, "close": b.close,
                 "volume": b.volume} for b in bars]
        frame = pd.DataFrame(rows).set_index("at")
        # Only complete minutes: the bar still forming is not a fact yet.
        forming = now.replace(second=0, microsecond=0)
        return frame[(frame.index >= start) & (frame.index < forming)]

    def close(self) -> None:
        if self.ib is not None:
            try:
                self.ib.disconnect()
            except Exception:  # noqa: BLE001 -- closing a dead link is not news
                pass
        self.ib = None


# ------------------------------------------------------------- running it


def watching(now: datetime) -> bool:
    return now.weekday() < 5 and WATCH_FROM <= now.time() < WATCH_UNTIL


def run(args) -> int:
    import heartbeat
    from feed_check import ET
    from spcx_alert import send_pushover

    here = os.path.dirname(os.path.abspath(__file__))
    alive = os.path.join(here, heartbeat.DAY_RULES)
    state = os.path.join(here, STATE_PATH)
    now = datetime.now(ET).replace(tzinfo=None)
    busy = heartbeat.already_running(alive, now)
    if busy:
        print(busy)
        return 1

    def phone(priority: int, text: str) -> None:
        print(f"{datetime.now(ET):%H:%M:%S}  {text}", flush=True)
        if not args.dry_run:
            error = send_pushover(text, title=TITLE, priority=priority)
            if error:
                print(f"           (phone: {error})", flush=True)

    account = Account()
    rules: Optional[Rules] = None
    last_seen: Optional[datetime] = None
    blind_said = False
    print("day_rules: read-only, watching weekdays 04:00-20:00 ET. Ctrl+C stops it.")
    try:
        while True:
            now = datetime.now(ET).replace(tzinfo=None)
            heartbeat.write(alive, now)
            if rules is not None and rules.day != now.date():
                rules = None
            if not watching(now):
                time_mod.sleep(CHECK_SECONDS)
                continue
            if rules is None:
                rules = Rules(now.date(), previous_pnl(load_state(state), now.date()),
                              args.loss_warn, args.loss_limit, args.big_win,
                              args.losing_run)
                last_seen = now
            try:
                snap = account.snapshot(now, now.date())
                last_seen, blind_said = now, False
                for priority, text in rules.check(snap):
                    phone(priority, text)
                if finite(snap.daily_pnl):
                    save_pnl(state, now.date(), snap.daily_pnl)
            except Exception as exc:  # noqa: BLE001 -- Gateway down: keep trying
                print(f"{now:%H:%M:%S}  {type(exc).__name__}: {exc}", flush=True)
                account.close()
                regular = REGULAR_OPEN <= now.time() < REGULAR_CLOSE
                if regular and not blind_said and now - last_seen > BLIND_AFTER:
                    blind_said = True
                    phone(PRIORITY_LOUD, "The rule alarms cannot reach IB Gateway "
                                         "and are BLIND. Rules 2, 3 and 6 are not "
                                         "being watched.")
            if account.ib is not None and account.ib.isConnected():
                account.ib.sleep(CHECK_SECONDS)
            else:
                time_mod.sleep(CHECK_SECONDS)
    except KeyboardInterrupt:
        print("Stopped.")
        return 0
    finally:
        account.close()


def status() -> int:
    from feed_check import ET

    now = datetime.now(ET).replace(tzinfo=None)
    account = Account()
    try:
        snap = account.snapshot(now, now.date())
    except Exception as exc:  # noqa: BLE001
        print(f"Could not read IB Gateway on port {IBKR_PORT}: {exc}")
        return 1
    finally:
        account.close()
    pnl = money(snap.daily_pnl) if finite(snap.daily_pnl) else "not reported"
    print(f"Day's P&L:        {pnl}")
    print(f"Trades today:     {snap.fills} fills, {len(snap.exits)} closing orders, "
          f"{losing_run(snap.exits)} losses in a row")
    print(f"On margin:        {'unknown' if snap.on_margin is None else snap.on_margin}")
    trend = {None: "not measured (outside 09:30-16:00 or too early)",
             True: "yes", False: "no"}[snap.trending_up]
    print(f"{SYMBOL} trending up: {trend}")
    return 0


# ---------------------------------------------------------------- self-test


def self_test() -> int:
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    day = date(2026, 10, 26)
    t = lambda h, m=0: datetime(2026, 10, 26, h, m)  # noqa: E731
    snap = lambda h, m=0, **k: Snapshot(at=t(h, m), **k)  # noqa: E731

    # Rule 2: a warning, then the day is over, once; trades after it ring.
    r = Rules(day)
    check(r.check(snap(10, daily_pnl=-3999)) == [], "-$3,999 says nothing")
    said = r.check(snap(10, 1, daily_pnl=-4000))
    check(len(said) == 1 and said[0][0] == PRIORITY_LOUD and "$1,000 left" in said[0][1],
          f"-$4,000 warns loudly with what is left: {said}")
    check(r.check(snap(10, 2, daily_pnl=-4500)) == [], "the warning is said once")
    said = r.check(snap(10, 3, daily_pnl=-5000, fills=6))
    check(len(said) == 1 and said[0][0] == PRIORITY_EMERGENCY
          and "THE DAY IS OVER" in said[0][1], f"-$5,000 ends the day: {said}")
    check(r.check(snap(10, 4, daily_pnl=-5600, fills=6)) == [],
          "the day is over once, not every minute")
    check(r.check(snap(10, 5, daily_pnl=-4000, fills=6)) == [],
          "climbing back does not reopen the day")
    said = r.check(snap(10, 6, daily_pnl=-4000, fills=7))
    check(len(said) == 1 and said[0][0] == PRIORITY_LOUD and "AFTER" in said[0][1],
          f"a trade after the day is over rings: {said}")
    check(r.check(snap(10, 7, daily_pnl=-4000, fills=7)) == [],
          "the same trade after the day is over rings once")
    r = Rules(day)
    said = r.check(snap(10, daily_pnl=-5200))
    check([p for p, _ in said] == [PRIORITY_EMERGENCY],
          f"straight past both lines: only the day-over: {said}")
    check(Rules(day).check(snap(10, daily_pnl=float("nan"))) == [], "no P&L, no alarm")
    check(Rules(day, limit=3000, warn=2000).check(snap(10, daily_pnl=-3000))[0][0]
          == PRIORITY_EMERGENCY, "the limit is a setting")

    # Rule 3: losing trades in a row.
    check(losing_run([]) == 0 and losing_run([-5, -3]) == 2, "two losses are a run of 2")
    check(losing_run([-5, 10, -3]) == 1, "a win ends the run")
    check(losing_run([-5, 0, -3]) == 2, "an even trade neither adds nor ends")
    r = Rules(day)
    check(r.check(snap(10, exits=[-100])) == [], "one loss says nothing")
    said = r.check(snap(10, 5, exits=[-100, -50]))
    check(len(said) == 1 and said[0][0] == PRIORITY_LOUD and "RULE 3" in said[0][1],
          f"two losses in a row ring: {said}")
    check(r.check(snap(10, 6, exits=[-100, -50])) == [], "the same run rings once")
    check(len(r.check(snap(10, 7, exits=[-100, -50, -20]))) == 1, "a third loss rings again")
    r.check(snap(11, exits=[-100, -50, -20, 300]))
    check(len(r.check(snap(11, 5, exits=[-100, -50, -20, 300, -10, -10]))) == 1,
          "after a win, a new run of 2 rings")
    check(Rules(day, run=3).check(snap(10, exits=[-1, -1])) == [], "the run is a setting")

    # Rule 6: margin before the day trends up.
    r = Rules(day)
    said = r.check(snap(10, on_margin=True, trending_up=False))
    check(len(said) == 1 and "RULE 6" in said[0][1], f"margin, not trending: rings: {said}")
    check(r.check(snap(10, 20, on_margin=True, trending_up=False)) == [],
          "not again within 30 minutes")
    check(len(r.check(snap(10, 30, on_margin=True, trending_up=False))) == 1,
          "again after 30 minutes")
    check(Rules(day).check(snap(10, on_margin=True, trending_up=True)) == [],
          "margin while trending up is allowed")
    check(Rules(day).check(snap(10, on_margin=False, trending_up=False)) == [],
          "no margin, nothing to say")
    check(Rules(day).check(snap(10, on_margin=True, trending_up=None)) == [],
          "trend not measured yet: no alarm")
    check(Rules(day).check(snap(7, on_margin=True, trending_up=False)) == [],
          "rule 6 is watched in regular hours only")

    # The 09:25 reminder, rule 1 and margin at the open.
    r = Rules(day, yesterday_pnl=3500)
    check(r.check(snap(9, 24)) == [], "nothing before 09:25")
    said = r.check(snap(9, 25, on_margin=True))
    check(len(said) == 1 and said[0][1].startswith("Yesterday +$3,500. RULE 1"),
          f"after a big win, rule 1 leads: {said}")
    check("ON MARGIN" in said[0][1] and "6. No all-in" in said[0][1],
          "margin at the open is named, and the rules follow")
    check(r.check(snap(9, 26)) == [], "the reminder comes once")
    said = Rules(day, yesterday_pnl=2999).check(snap(9, 30))
    check(said and "RULE 1" not in said[0][1].split("\n")[0], "a small win is not a big win")
    check(Rules(day).check(snap(9, 50)) == [], "too late for the morning reminder")

    # Trending up from minute bars.
    idx = pd.date_range("2026-10-26 09:30", periods=30, freq="1min")
    up = pd.DataFrame({"open": [100 + i * 0.1 for i in range(30)],
                       "high": [100.2 + i * 0.1 for i in range(30)],
                       "low": [99.9 + i * 0.1 for i in range(30)],
                       "close": [100.1 + i * 0.1 for i in range(30)],
                       "volume": [1000] * 30}, index=idx)
    check(trending_up(up) is True, "a steady climb is trending up")
    down = up.copy()
    for col in ("open", "high", "low", "close"):
        down[col] = 200 - up[col]
    check(trending_up(down) is False, "a steady fall is not")
    check(trending_up(up.iloc[:10]) is None, "too few bars: cannot say")
    fade = up.copy()
    fade.iloc[-1, fade.columns.get_loc("close")] = 99.0
    check(trending_up(fade) is False, "under the open is not trending up")
    def frame(rows):
        return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"],
                            index=idx[:len(rows)])
    # Above VWAP and VWAP rising, but under a 110 open: not trending up.
    rows = [(110, 110, 100, 100, 1)] + [(100 + i * 0.1,) * 4 + (1000,) for i in range(1, 30)]
    check(trending_up(frame(rows)) is False, "under the open, even above a rising VWAP")
    # Above VWAP and the open, but VWAP falling over the last 15 minutes.
    rows = ([(100, 110, 100, 110, 10000)] + [(110,) * 4 + (10000,)] * 9
            + [(101,) * 4 + (10000,)] * 19 + [(112,) * 4 + (1,)])
    check(trending_up(frame(rows)) is False, "a falling VWAP is not trending up")

    # State: yesterday's P&L for rule 1.
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, STATE_PATH)
        check(previous_pnl(load_state(path), day) is None, "no file, no yesterday")
        save_pnl(path, date(2026, 10, 22), 1200)
        save_pnl(path, date(2026, 10, 23), 4100)
        save_pnl(path, day, -300)
        check(previous_pnl(load_state(path), day) == 4100,
              "Friday is the day before Monday")
        for i in range(20):
            save_pnl(path, date(2026, 9, 1) + timedelta(days=i), 1)
        check(len(load_state(path)) == 14, "only two weeks are kept")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("day_rules self-test passed")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Watch the day-trading rules. Read-only.")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="print only, no phone")
    parser.add_argument("--loss-warn", type=float, default=LOSS_WARN)
    parser.add_argument("--loss-limit", type=float, default=LOSS_LIMIT)
    parser.add_argument("--big-win", type=float, default=BIG_WIN)
    parser.add_argument("--losing-run", type=int, default=LOSING_RUN)
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    from feed_check import load_env

    load_env()
    if args.status:
        return status()
    if args.watch:
        return run(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
