"""
day_plan.py -- the SPCX day plan: set-ups, the flush alert, the debrief
(2026-10-25).

The rules tested on 59 sessions (31 Jul - 23 Oct 2026) turned into texts,
and into one sheet per day:

  9:20 AM    PASS 1. A box and two lines from yesterday's volume zone,
             this morning's pre-market range and the trap levels, and the
             verdict: dip buy allowed when the S&P is up before the open
             and it is not an unlock or earnings day. (Tested: allowed
             days 9 of 12 right, avg +1.50%; the others 6 of 17, -0.47%.)
  9:31-10:15 FLUSH. SPCX drops 2-3% below its open in the first 15
             minutes, then a one-minute bar closes up without a new low:
             buy zone, stop (1% under the flush low), sell at the open or
             by 11:00. Deeper than 3%: skip. Not on unlock / earnings days.
             (Tested: 8 of 12 bounced, avg +0.52% to 11:00.)
  9:45-10:30 PASS 2, read on 15-minute candles like Jason's chart, when the
             first big move has finished (a 1.5% move, then one 15-minute
             candle without a new extreme, or a one-third pull-back): the
             final box,
             SELL / BUY / STOP lines, most shares for a $5,000 stop, and
             the verdict again -- it can only be switched OFF here
             (S&P turned down, or a quiet open), never on.
  4:05 PM    DEBRIEF. What the lines did, what the flush did, the day.

Everything is also written to daily/SPCX YYYY-MM-DD.json beside this file
(the day's sheet, with the 9:00 to-do list), and one row per day to
daily/spcx_plan_log.csv. All of it stays on this PC. The day's PDF
(daily_report.py, rebuilt by the SPCX alarms every 15 minutes) reads that
JSON: the zones drawn on the chart, and a "Day plan" page with every step.

Prices only. Nothing here reads an account or places an order: the texts
say where the lines are, and Jason decides.

    python day_plan.py --self-test
    python day_plan.py --replay 2026-10-23   # the whole day, from the tape
"""

from __future__ import annotations

import csv
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SYMBOL, MARKET = "SPCX", "SPY"
HERE = os.path.dirname(os.path.abspath(__file__))
DAILY_DIR = os.path.join(HERE, "daily")
LOG_PATH = os.path.join(DAILY_DIR, "spcx_plan_log.csv")

PASS1_AT, PASS1_LATEST = time(9, 20), time(9, 40)
FLUSH_FROM, FLUSH_UNTIL = time(9, 31), time(10, 15)
FLUSH_WINDOW = timedelta(minutes=15)
FLUSH_MIN, FLUSH_MAX, FLUSH_STOP = 0.02, 0.03, 0.01
FLUSH_SELL_BY = time(11, 0)
#: The plan reads 15-minute candles (Jason's chart; re-tested 25 Oct: as good
#: as 5-minute ones, 17 of 28 right, +0.46%); the flush watch reads 1-minute.
PLAN_MINUTES = 15
PASS2_EARLIEST, PASS2_LATEST = time(9, 45), time(10, 30)
BIG_MOVE, PAUSE_BARS, RETRACE = 0.015, 1, 1 / 3
DEBRIEF_AT, DEBRIEF_LATEST = time(16, 5), time(19, 0)
STOP_PAST, EDGE, TRAP_ZONE, MIN_SPREAD = 0.025, 0.0025, 0.008, 0.01
MAX_LOSS = 5000.0
#: Pushover priorities (25 Oct: sound only at 9:20 and for a major change).
#: QUIET arrives with no sound; SOUND rings; LOUD rings through a focus mode.
QUIET, SOUND, LOUD = -1, 0, 1
OPEN, CLOSE = time(9, 30), time(16, 0)


@dataclass
class Bar:
    t: datetime          # bar start, Eastern, naive
    o: float
    h: float
    l: float
    c: float
    v: float


def money(x: float) -> str:
    return f"${x:,.2f}"


# --------------------------------------------------------------- the rules

def rth(bars: List[Bar]) -> List[Bar]:
    return [b for b in bars if OPEN <= b.t.time() < CLOSE]


def roll(bars: List[Bar], minutes: int) -> List[Bar]:
    """One-minute bars rolled into `minutes` bars stamped at their start."""
    out: Dict[datetime, Bar] = {}
    for b in bars:
        k = b.t.replace(minute=b.t.minute - b.t.minute % minutes, second=0, microsecond=0)
        if k not in out:
            out[k] = Bar(k, b.o, b.h, b.l, b.c, b.v)
        else:
            x = out[k]
            out[k] = Bar(k, x.o, max(x.h, b.h), min(x.l, b.l), b.c, x.v + b.v)
    return [out[k] for k in sorted(out)]


def value_area(bars: List[Bar], share: float = 0.70) -> Tuple[float, float, float]:
    """(busiest price, bottom, top) of the prices where `share` of the
    volume traded: each bar's volume spread over its range in 0.1% bins."""
    lo = min(b.l for b in bars)
    step = lo * 0.001
    prof: Dict[int, float] = {}
    for b in bars:
        a, z = int((b.l - lo) / step), int((b.h - lo) / step)
        for k in range(a, z + 1):
            prof[k] = prof.get(k, 0.0) + b.v / (z - a + 1)
    poc = max(prof, key=prof.get)
    total, inside, a, z = sum(prof.values()), prof[poc], poc, poc
    while inside < share * total:
        up, dn = prof.get(z + 1, 0.0), prof.get(a - 1, 0.0)
        if up == 0 and dn == 0:
            break
        if up >= dn:
            z += 1
            inside += up
        else:
            a -= 1
            inside += dn
    at = lambda k: lo + (k + 0.5) * step  # noqa: E731
    return at(poc), at(a), at(z)


def rounds(p: float, n: int = 3) -> List[float]:
    base = 5 * math.floor(p / 5)
    return [float(base + 5 * k) for k in range(-n, n + 1)]


def clear_of_traps(stop: float, levels: List[float]) -> float:
    """Move a stop below any level whose trap zone (0.8%: where 90% of the
    tested pokes reached) it would sit in."""
    moved = True
    while moved:
        moved = False
        for lv in levels:
            if lv * (1 - TRAP_ZONE) <= stop <= lv * (1 + TRAP_ZONE):
                stop = lv * (1 - TRAP_ZONE - 0.001)
                moved = True
    return stop


def stop_below(support: float, levels: List[float]) -> float:
    """2.5% under the support, and out of every level's trap zone."""
    return clear_of_traps(support * (1 - STOP_PAST), levels)


def pass1(prev: List[Bar], pre: List[Bar]) -> dict:
    """The 9:20 box and lines. `prev` is yesterday's regular session,
    `pre` this morning's pre-market so far."""
    pdh, pdl, pclose = max(b.h for b in prev), min(b.l for b in prev), prev[-1].c
    poc, val, vah = value_area(roll(prev, 5))
    pmh = max((b.h for b in pre), default=None)
    pml = min((b.l for b in pre), default=None)
    lo = min(x for x in (val, pml) if x is not None)
    hi = max(x for x in (vah, pmh) if x is not None)
    ups = [x for x in [pdh, pmh] + rounds(hi) if x is not None and x > hi]
    top = min(ups) if ups else hi * 1.02
    sups = [x for x in [pdl, pml] + rounds(lo) if x is not None and x <= lo]
    support = max(sups) if sups else lo
    levels = [x for x in [pdh, pdl, pmh, pml, poc, val, vah] + rounds(pclose) if x is not None]
    bottom = stop_below(support, levels)
    return dict(box_hi=hi, box_lo=lo, top=top, bottom=bottom, pdh=pdh, pdl=pdl, pclose=pclose,
                poc=poc, val=val, vah=vah, pmh=pmh, pml=pml, levels=levels)


def verdict_920(spy_pre: Optional[float], event: str) -> Tuple[bool, str]:
    if event:
        return False, f"{event} day: no dip buy today. Lines are for reference only."
    if spy_pre is None:
        return False, "S&P not readable before the open: no dip buy today. Lines are for reference only."
    if spy_pre < 0:
        return False, "S&P down before the open: no dip buy today. Lines are for reference only."
    return True, "Good morning: dip buy allowed."


def pass2_due(bars5: List[Bar], open_price: float, now: datetime) -> Optional[str]:
    """'move' when the first big move has finished, 'quiet' at 10:30 without
    one, else None. Reads complete plan candles (15-minute) only."""
    if now.time() < PASS2_EARLIEST:
        return None
    if not bars5:
        return "quiet" if now.time() >= PASS2_LATEST else None
    hi, lo = max(b.h for b in bars5), min(b.l for b in bars5)
    up, dn = hi / open_price - 1, 1 - lo / open_price
    if max(up, dn) >= BIG_MOVE:
        rising = up >= dn
        extreme = hi if rising else lo
        at = max(i for i, b in enumerate(bars5) if (b.h if rising else b.l) == extreme)
        paused = len(bars5) - 1 - at >= PAUSE_BARS
        pulled = abs(bars5[-1].c - extreme) >= RETRACE * abs(extreme - open_price)
        if paused or pulled:
            return "move"
    if now.time() >= PASS2_LATEST:
        return "move" if max(up, dn) >= BIG_MOVE else "quiet"
    return None


def pass2(p1: dict, morning: List[Bar]) -> dict:
    """The final box and SELL / BUY / STOP lines from the morning so far."""
    mhigh, mlow, price = max(b.h for b in morning), min(b.l for b in morning), morning[-1].c
    sup = [x for x in [mlow, p1["val"], p1["pdl"], p1["pml"]] + rounds(price)
           if x is not None and x < price * 0.998]
    buy_lvl = max(sup) if sup else price * 0.97
    buy = buy_lvl * (1 + EDGE)
    res = [x for x in [mhigh, p1["pdh"], p1["pmh"]] + rounds(price)
           if x is not None and x * (1 - EDGE) >= buy * (1 + MIN_SPREAD)]
    sell = (min(res) if res else buy * 1.03) * (1 - EDGE)
    stop = stop_below(buy_lvl, p1["levels"] + [mhigh, mlow])
    return dict(box_hi=max(mhigh, p1["vah"]), box_lo=min(mlow, p1["val"]), sell=sell, buy=buy,
                stop=stop, shares=int(MAX_LOSS / (buy - stop)), mhigh=mhigh, mlow=mlow, price=price)


def verdict_pass2(go_920: bool, reason_920: str, kind: str, spy_now: Optional[float]) -> Tuple[bool, str]:
    """Pass 2 can switch the dip buy off, never on."""
    if not go_920:
        return False, reason_920
    if spy_now is None or spy_now < 0:
        return False, "S&P turned down: no dip buy today. Lines are for reference only."
    if kind == "quiet":
        return False, "Quiet open: no dip buy today. Lines are for reference only."
    return True, "Still good: dip buy allowed."


def flush_signal(bars1: List[Bar], open_price: float) -> Optional[dict]:
    """The flush and its first up-bar, from complete one-minute bars since
    9:30. None until there is one; {'skip': reason} for a drop too deep."""
    window = [b for b in bars1 if b.t < bars1[0].t.replace(hour=9, minute=30) + FLUSH_WINDOW] \
        if bars1 else []
    if not window:
        return None
    low_bar = min(window, key=lambda b: b.l)
    depth = 1 - low_bar.l / open_price
    if depth < FLUSH_MIN:
        return None
    running = min(b.l for b in bars1 if b.t <= low_bar.t)
    for b in bars1:
        if b.t <= low_bar.t:
            continue
        if b.l < running:
            running = b.l
            continue
        if b.c > b.o:
            deepest = 1 - running / open_price
            if deepest > FLUSH_MAX:
                return dict(skip=f"SPCX dropped {deepest * 100:.1f}% from the open - too deep, "
                                 f"skip the flush buy.", depth=deepest * 100, t=b.t)
            return dict(depth=deepest * 100, low=running, buy=b.c, t=b.t,
                        stop=running * (1 - FLUSH_STOP), sell=open_price,
                        shares=int(MAX_LOSS / max(b.c - running * (1 - FLUSH_STOP), 0.01)))
    return None


def walk(bars: List[Bar], buy: float, sell: float, stop: float,
         until: Optional[time] = None) -> dict:
    """What price did to the lines after they were sent: BUY first, then
    SELL or STOP, else the close (or `until`)."""
    got = None
    for b in bars:
        if until and b.t.time() >= until and got:
            return dict(bought=got, out=b.o, why=f"by {until:%H:%M}", t=b.t)
        if got is None:
            if b.l <= buy:
                got = (b.t, min(buy, b.o))
            continue
        if b.l <= stop:
            return dict(bought=got, out=min(stop, b.o), why="STOP", t=b.t)
        if b.h >= sell:
            return dict(bought=got, out=max(sell, b.o), why="SELL", t=b.t)
    if got:
        return dict(bought=got, out=bars[-1].c, why="the close", t=bars[-1].t)
    return dict(bought=None)


# ------------------------------------------------------------------ texts

def text_pass1(p: dict, go: bool, reason: str) -> str:
    mark = "✅" if go else "⛔"
    return (f"SPCX zones for today\n"
            f"1. Draw a box from {money(p['box_hi'])} to {money(p['box_lo'])}. "
            f"Extend it through the whole day.\n"
            f"2. Draw a line at {money(p['top'])}.\n"
            f"3. Draw a line at {money(p['bottom'])}.\n"
            f"{mark} {reason}\n"
            f"This is your entry set-up. Do not trade against it until the next update.")


def text_flush(f: dict) -> str:
    if "skip" in f:
        return f["skip"]
    return (f"SPCX FLUSH: down {f['depth']:.1f}% from the open, now poking back up.\n"
            f"Buy zone about {money(f['buy'])}. Stop {money(f['stop'])}. "
            f"Sell at {money(f['sell'])} (the open) or by 11:00.\n"
            f"Most shares for this stop: {f['shares']:,}.")


def text_pass2(p: dict, go: bool, reason: str) -> str:
    mark = "✅" if go else "⛔"
    return (f"SPCX final zones for today\n"
            f"1. Draw a box from {money(p['box_hi'])} to {money(p['box_lo'])}. "
            f"Extend it through the whole day.\n"
            f"2. SELL line at {money(p['sell'])}.\n"
            f"3. BUY line at {money(p['buy'])}.\n"
            f"4. STOP line at {money(p['stop'])}.\n"
            f"Most shares today: {p['shares']:,}.\n"
            f"{mark} {reason}\n"
            f"These replace the 9:20 set-up.")


def describe_walk(w: dict, label: str) -> str:
    if not w.get("bought"):
        return f"{label}: BUY line never reached."
    t, price = w["bought"]
    pct = (w["out"] / price - 1) * 100
    return (f"{label}: bought {t:%H:%M} at {money(price)}, out at {w['why']} "
            f"{w['t']:%H:%M} {money(w['out'])}: {pct:+.1f}% per share.")


# ------------------------------------------------------------- the day

class Day:
    """One day's plan, kept in daily/SPCX YYYY-MM-DD.json."""

    def __init__(self, day: date, folder: str = DAILY_DIR):
        self.day, self.folder = day, folder
        self.path = os.path.join(folder, f"{SYMBOL} {day.isoformat()}.json")
        try:
            with open(self.path, encoding="utf-8") as handle:
                self.data = json.load(handle)
        except (OSError, ValueError):
            self.data = {"day": day.isoformat()}

    def save(self) -> None:
        os.makedirs(self.folder, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=1, default=str)
        os.replace(tmp, self.path)


LOG_FIELDS = ["day", "gap_pct", "spy_pre_pct", "spy_pass2_pct", "event", "go_920", "go_final",
              "pass2_time", "pass2_kind", "first_move_pct", "buy", "sell", "stop", "plan_result",
              "plan_pct", "flush", "flush_pct", "open", "high", "low", "close"]


def append_log(row: dict, path: str = LOG_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as handle:
        w = csv.DictWriter(handle, fieldnames=LOG_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


# --------------------------------------------------------------- the tape

def tape(symbol: str, start: datetime, end: datetime, now: datetime) -> List[Bar]:
    """One-minute bars from Alpaca: the full tape (SIP) for whatever is more
    than 16 minutes old -- the free plan's limit -- and IEX for the rest.
    Complete minutes only: the minute still forming is left out."""
    import open_candles

    edge = now.replace(second=0, microsecond=0)
    end = min(end, edge)
    split = min(end, now - timedelta(minutes=16))
    frames = []
    if split > start:
        frames.append(open_candles.fetch_minutes(symbol, start.replace(tzinfo=ET),
                                                 split.replace(tzinfo=ET), force_sip=True))
    if end > max(start, split):
        frames.append(open_candles.fetch_minutes(symbol, max(start, split).replace(tzinfo=ET),
                                                 end.replace(tzinfo=ET)))
    seen: Dict[datetime, Bar] = {}
    for f in frames:
        if f is None or f.empty:
            continue
        for t, r in f.iterrows():
            k = t.tz_convert(ET).to_pydatetime().replace(tzinfo=None)
            if k < end:
                seen[k] = Bar(k, float(r["open"]), float(r["high"]), float(r["low"]),
                              float(r["close"]), float(r.get("volume", 0) or 0))
    return [seen[k] for k in sorted(seen)]


def previous_day(day: date) -> date:
    d = day - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def change_since_close(bars_today: List[Bar], prev_close: Optional[float]) -> Optional[float]:
    if not bars_today or not prev_close:
        return None
    return (bars_today[-1].c / prev_close - 1) * 100


def event_today(events: list, day: date) -> str:
    for e in events:
        if getattr(e, "day", None) == day and getattr(e, "kind", "") in ("unlock", "earnings"):
            return "Unlock" if e.kind == "unlock" else "Earnings"
    return ""


# ----------------------------------------------------------------- tick

Fetch = Callable[[str, datetime, datetime, datetime], List[Bar]]


def tick(now: datetime, events: list, todo_text: Optional[str] = None,
         fetch: Fetch = tape, folder: str = DAILY_DIR,
         log_path: str = LOG_PATH) -> List[Tuple[int, str]]:
    """Called once a minute (Eastern `now`, naive or aware). Returns the
    texts to send now as (priority, text). Each step runs once a day."""
    now = now.astimezone(ET).replace(tzinfo=None) if now.tzinfo else now
    if now.weekday() >= 5:
        return []
    day = Day(now.date(), folder)
    d, out, t = day.data, [], now.time()
    changed = False
    if todo_text and not d.get("todo"):
        d["todo"], changed = todo_text, True

    def bars(symbol, start, end):
        return fetch(symbol, start, end, now)

    today = now.date()
    if "prev" not in d and PASS1_AT <= t <= PASS1_LATEST:
        pday = previous_day(today)
        prev = rth(bars(SYMBOL, datetime.combine(pday, OPEN), datetime.combine(pday, CLOSE)))
        sprev = rth(bars(MARKET, datetime.combine(pday, OPEN), datetime.combine(pday, CLOSE)))
        if len(prev) < 60:
            return out
        d["prev"] = dict(day=pday.isoformat(), close=prev[-1].c, spy_close=sprev[-1].c if sprev else None)
        day._prev = prev
        changed = True

    # ---- pass 1
    if "pass1" not in d and PASS1_AT <= t <= PASS1_LATEST:
        pday = date.fromisoformat(d["prev"]["day"])
        prev = getattr(day, "_prev", None) or rth(bars(SYMBOL, datetime.combine(pday, OPEN),
                                                       datetime.combine(pday, CLOSE)))
        pre = bars(SYMBOL, datetime.combine(today, time(4, 0)), datetime.combine(today, OPEN))
        spy_pre = change_since_close(bars(MARKET, datetime.combine(today, time(4, 0)),
                                          datetime.combine(today, OPEN)), d["prev"]["spy_close"])
        event = event_today(events, today)
        p1 = pass1(prev, pre)
        go, reason = verdict_920(spy_pre, event)
        d["pass1"] = {k: v for k, v in p1.items() if k != "levels"}
        d["levels"] = p1["levels"]
        d["go_920"], d["reason_920"] = go, reason
        d["pass1_text"] = text_pass1(p1, go, reason)
        d["context"] = {"S&P before the open": f"{spy_pre:+.2f}%" if spy_pre is not None else "n/a",
                        "Event": event or "none"}
        d["spy_pre"], d["event"] = spy_pre, event
        out.append((SOUND, d["pass1_text"]))
        changed = True

    flush_pending = "flush" not in d and FLUSH_FROM <= t <= FLUSH_UNTIL and not d.get("event")
    if "pass1" in d and t >= time(9, 31) and ("open" not in d or flush_pending or "pass2" not in d):
        session = bars(SYMBOL, datetime.combine(today, OPEN), min(now, datetime.combine(today, CLOSE)))
        if session:
            opn = session[0].o
            if "open" not in d:
                d["open"] = opn
                gap = (opn / d["prev"]["close"] - 1) * 100
                d["gap"] = gap
                d["context"]["Gap at the open"] = f"{gap:+.2f}%"
                changed = True
            # ---- flush
            if flush_pending:
                f = flush_signal(session, opn)
                if f:
                    d["flush"] = {k: v for k, v in f.items()}
                    d["flush_text"], d["flush_time"] = text_flush(f), f"{f['t']:%H:%M}"
                    out.append((QUIET if "skip" in f else LOUD, d["flush_text"]))
                    changed = True
            # ---- pass 2
            if "pass2" not in d and t >= PASS2_EARLIEST and "pass1" in d:
                done5 = [b for b in roll(session, PLAN_MINUTES)
                         if b.t + timedelta(minutes=PLAN_MINUTES) <= now]
                kind = pass2_due(done5, opn, now)
                if kind:
                    p1 = dict(d["pass1"], levels=d["levels"])
                    p2 = pass2(p1, done5 or session)
                    spy_now = change_since_close(
                        bars(MARKET, datetime.combine(today, OPEN), now), d["prev"]["spy_close"])
                    go, reason = verdict_pass2(d["go_920"], d["reason_920"], kind, spy_now)
                    hi, lo = p2["mhigh"], p2["mlow"]
                    first = (hi / opn - 1) * 100 if hi / opn - 1 >= 1 - lo / opn else -(1 - lo / opn) * 100
                    d["pass2"], d["go_final"], d["reason_final"] = p2, go, reason
                    d["pass2_kind"], d["pass2_time"] = kind, f"{now:%H:%M}"
                    d["spy_pass2"], d["first_move"] = spy_now, first
                    d["pass2_text"] = text_pass2(p2, go, reason)
                    d["context"].update({"S&P at the final set-up": f"{spy_now:+.2f}%" if spy_now is not None else "n/a",
                                         "First move": f"{first:+.2f}% ({kind})",
                                         "Final set-up at": f"{now:%H:%M}"})
                    # A major change: 9:20 allowed the dip buy and this takes it away.
                    out.append((SOUND if d["go_920"] and not go else QUIET, d["pass2_text"]))
                    changed = True

    # ---- debrief
    if "debrief_text" not in d and DEBRIEF_AT <= t <= DEBRIEF_LATEST and "pass2" in d:
        session = rth(bars(SYMBOL, datetime.combine(today, OPEN), datetime.combine(today, CLOSE)))
        if session:
            p2 = d["pass2"]
            after = [b for b in session if b.t >= datetime.combine(today, time.fromisoformat(d["pass2_time"]))]
            w = walk(after, p2["buy"], p2["sell"], p2["stop"])
            lines = [f"SPCX debrief {today:%a %b %d}",
                     f"Plan: BUY {money(p2['buy'])}  SELL {money(p2['sell'])}  STOP {money(p2['stop'])} "
                     f"({'dip buy allowed' if d.get('go_final') else 'no dip buy'})",
                     describe_walk(w, "What the lines did")]
            flush_pct = None
            f = d.get("flush")
            if f and "skip" not in f:
                ft = datetime.fromisoformat(str(f["t"]))
                # Bought at the open of the bar after the up-bar, as tested.
                fw = walk([b for b in session if b.t > ft], math.inf, f["sell"], f["stop"],
                          until=FLUSH_SELL_BY)
                lines.append(describe_walk(fw, "Flush"))
                if fw.get("bought"):
                    flush_pct = (fw["out"] / fw["bought"][1] - 1) * 100
            elif f:
                lines.append("Flush: too deep, skipped.")
            elif d.get("event"):
                lines.append(f"Flush: not watched ({d['event'].lower()} day).")
            else:
                lines.append("Flush: none today.")
            hi, lo = max(b.h for b in session), min(b.l for b in session)
            lines.append(f"Day: open {money(session[0].o)}  high {money(hi)}  low {money(lo)}  "
                         f"close {money(session[-1].c)}")
            d["debrief_text"] = "\n".join(lines)
            out.append((QUIET, d["debrief_text"]))
            plan_pct = ((w["out"] / w["bought"][1] - 1) * 100) if w.get("bought") else None
            append_log(dict(day=today.isoformat(), gap_pct=round(d.get("gap", 0), 2),
                            spy_pre_pct=d.get("spy_pre"), spy_pass2_pct=d.get("spy_pass2"),
                            event=d.get("event") or "", go_920=d.get("go_920"), go_final=d.get("go_final"),
                            pass2_time=d.get("pass2_time"), pass2_kind=d.get("pass2_kind"),
                            first_move_pct=round(d.get("first_move", 0), 2), buy=round(p2["buy"], 2),
                            sell=round(p2["sell"], 2), stop=round(p2["stop"], 2),
                            plan_result=w.get("why", "no buy"),
                            plan_pct=round(plan_pct, 2) if plan_pct is not None else "",
                            flush=("skip" if f and "skip" in f else ("yes" if f else "no")),
                            flush_pct=round(flush_pct, 2) if flush_pct is not None else "",
                            open=session[0].o, high=hi, low=lo, close=session[-1].c), log_path)
            changed = True
    if changed:
        day.save()
    return out


# ---------------------------------------------------------------- self-test

def self_test() -> int:
    import tempfile

    failures = []

    def ok(cond, msg):
        if not cond:
            failures.append(msg)

    D0, D1 = date(2026, 10, 22), date(2026, 10, 23)

    def mk(day, hh, mm, o, h, l, c, v=1000.0):
        return Bar(datetime.combine(day, time(hh, mm)), o, h, l, c, v)

    # Yesterday: 9:30-15:59, drifting 150 -> 146, busiest around 148.
    prev = []
    for i in range(390):
        t = datetime.combine(D0, OPEN) + timedelta(minutes=i)
        p = 150 - 4 * i / 389
        prev.append(Bar(t, p, p + 0.2, p - 0.2, p, 3000.0 if 147.5 < p < 148.5 else 1000.0))
    pre = [Bar(datetime.combine(D1, time(8, 0)) + timedelta(minutes=i), 147.5, 148.1, 147.0, 147.6, 200)
           for i in range(60)]
    p1 = pass1(prev, pre)
    ok(p1["box_lo"] <= 147.0 + 1e-9 and p1["box_hi"] >= 148.1 - 1e-9, f"the box holds the pre-market: {p1}")
    ok(abs(p1["poc"] - 148) < 0.6, f"busiest price near 148: {p1['poc']}")
    ok(p1["top"] > p1["box_hi"] and p1["bottom"] < p1["box_lo"] * (1 - STOP_PAST) + 1e-9,
       f"lines outside the box, the bottom 2.5%+ under support: {p1}")
    ok(all(not (lv * (1 - TRAP_ZONE) <= p1["bottom"] <= lv * (1 + TRAP_ZONE)) for lv in p1["levels"]),
       "the bottom line is in no level's trap zone")
    ok(clear_of_traps(144.9, [145.0]) < 145 * (1 - TRAP_ZONE), "a stop next to $145 moves under its zone")
    ok(stop_below(153.85, [150.0]) < 150 * (1 - TRAP_ZONE) and abs(stop_below(160.0, [150.0]) - 156.0) < 1e-9,
       "2.5% under support lands on $150: moved under its trap zone; elsewhere left at 2.5%")

    ok(verdict_920(0.3, "") == (True, "Good morning: dip buy allowed."), "S&P up: go")
    ok(verdict_920(-0.46, "")[0] is False and "S&P down" in verdict_920(-0.46, "")[1], "S&P down: no")
    ok(verdict_920(0.3, "Unlock")[1].startswith("Unlock day"), "unlock day: no")
    ok(verdict_pass2(True, "", "move", 0.2)[0] and not verdict_pass2(True, "", "move", -0.1)[0]
       and not verdict_pass2(True, "", "quiet", 0.5)[0], "pass 2 switches off on S&P down or a quiet open")
    ok(verdict_pass2(False, "S&P down before the open: x", "move", 1.0) ==
       (False, "S&P down before the open: x"), "pass 2 never switches a NO on")

    # Pass 2 trigger, on 15-minute candles.
    def b5(i, o, h, l, c):
        return Bar(datetime.combine(D1, OPEN) + timedelta(minutes=15 * i), o, h, l, c, 1)
    drop = [b5(0, 148, 148, 144.1, 146.3)]
    ok(pass2_due(drop, 148, datetime.combine(D1, time(9, 40))) is None, "not before 9:45")
    ok(pass2_due(drop, 148, datetime.combine(D1, time(9, 45))) == "move",
       "a 2.6% drop whose first candle closed a third back up fires at 9:45")
    held = [b5(0, 148, 148, 144.1, 144.4)]
    ok(pass2_due(held, 148, datetime.combine(D1, time(9, 45))) is None,
       "a candle that closes at its low: not finished")
    held.append(b5(1, 144.4, 145.0, 144.3, 144.8))
    ok(pass2_due(held, 148, datetime.combine(D1, time(10, 0))) == "move",
       "one 15-minute candle without a new low: finished")
    calm = [b5(i, 148, 148.4, 147.8, 148.1) for i in range(4)]
    ok(pass2_due(calm[:3], 148, datetime.combine(D1, time(10, 25))) is None
       and pass2_due(calm, 148, datetime.combine(D1, time(10, 30))) == "quiet", "a quiet open waits for 10:30")
    rising = [b5(0, 148, 149, 147.9, 149), b5(1, 149, 150.4, 149, 150.4)]
    ok(pass2_due(rising, 148, datetime.combine(D1, time(10, 0))) is None,
       "still making highs: not finished")
    ok([b.t.minute for b in roll(prev[:45], 15)] == [30, 45, 0] and roll(prev[:45], 15)[0].v == sum(b.v for b in prev[:15]),
       "1-minute bars roll into 15-minute candles")

    p2 = pass2(dict(p1), drop)
    ok(p2["stop"] < p2["buy"] < p2["price"] < p2["sell"] and p2["sell"] >= p2["buy"] * 1.01,
       f"STOP < BUY < price < SELL, sell 1%+ above buy: {p2}")
    ok(p2["shares"] == int(MAX_LOSS / (p2["buy"] - p2["stop"])), "shares sized to $5,000 at the stop")
    tight = pass2(dict(p1, pdh=None, pmh=None),
                  [Bar(datetime.combine(D1, OPEN), 147.0, 147.6, 146.5, 147.0, 1)])
    ok(abs(tight["buy"] - 146.5 * (1 + EDGE)) < 1e-9 and tight["sell"] >= tight["buy"] * (1 + MIN_SPREAD)
       and abs(tight["sell"] - 150 * (1 - EDGE)) < 1e-9,
       f"the morning high 0.5% above BUY is skipped; SELL goes to the next level, $150: {tight}")

    # The flush, on one-minute bars: Friday 23 Oct, roughly.
    def m(i, o, h, l, c):
        return Bar(datetime.combine(D1, OPEN) + timedelta(minutes=i), o, h, l, c, 1)
    fri = [m(0, 147.93, 148.0, 146.0, 146.2), m(1, 146.2, 146.3, 144.5, 144.6), m(2, 144.6, 144.7, 144.09, 144.3),
           m(3, 144.3, 146.3, 144.2, 146.25), m(4, 146.25, 147, 146, 146.9)]
    ok(flush_signal(fri[:3], 147.93) is None, "no up-bar yet: no flush alert")
    f = flush_signal(fri, 147.93)
    ok(f and abs(f["buy"] - 146.25) < 1e-9 and f["t"].minute == 33 and abs(f["low"] - 144.09) < 1e-9
       and abs(f["stop"] - 144.09 * 0.99) < 1e-6 and f["sell"] == 147.93,
       f"buy at the 9:33 up-bar, stop 1% under the low, sell at the open: {f}")
    red = fri[:3] + [m(3, 144.3, 144.8, 144.2, 144.25)]
    ok(flush_signal(red, 147.93) is None, "a bar that closes down is not the turn")
    lower = fri[:3] + [m(3, 144.3, 145.0, 143.9, 144.9), m(4, 144.9, 145.6, 144.0, 145.5)]
    f2 = flush_signal(lower, 147.93)
    ok(f2 and f2["t"].minute == 34 and abs(f2["low"] - 143.9) < 1e-9,
       f"a green bar that made a new low does not count; the next one does, from the new low: {f2}")
    sag = fri[:3] + [m(i, 144.6, 144.7, 144.2, 144.3) for i in range(3, 18)] + \
        [m(18, 144.3, 144.6, 143.8, 144.5), m(19, 144.5, 145.2, 143.9, 145.1)]
    f3 = flush_signal(sag, 147.93)
    ok(f3 and f3["t"].minute == 49 and abs(f3["low"] - 143.8) < 1e-9,
       f"a later, lower low restarts the wait for the turn: {f3}")
    shallow = [m(0, 148, 148, 146.0, 146.5), m(1, 146.5, 147, 146.4, 146.9)]
    ok(flush_signal(shallow, 148) is None, "a 1.4% dip is not a flush")
    deep = [m(0, 148, 148, 143.0, 143.2), m(1, 143.2, 144, 143.1, 143.9)]
    ok("skip" in (flush_signal(deep, 148) or {}), "deeper than 3%: skip")
    late = [m(0, 148, 148.2, 147.8, 148)] * 16 + [m(16, 148, 148, 144.0, 144.2), m(17, 144.2, 145, 144.1, 144.9)]
    late = [Bar(datetime.combine(D1, OPEN) + timedelta(minutes=i), b.o, b.h, b.l, b.c, 1) for i, b in enumerate(late)]
    ok(flush_signal(late, 148) is None, "a drop after the first 15 minutes is not the opening flush")

    w = walk([m(10, 147, 147, 146.1, 146.5), m(11, 146.5, 149.5, 146.4, 149.4)], 146.2, 149.1, 140)
    ok(w["why"] == "SELL" and abs(w["out"] - 149.1) < 1e-9 and abs(w["bought"][1] - 146.2) < 1e-9,
       f"BUY then SELL: {w}")
    ok(walk([m(10, 147, 147.5, 146.5, 147)], 146.2, 149, 140) == {"bought": None}, "BUY never reached")

    ok(text_pass1(dict(p1, box_hi=147.6, box_lo=139.82, top=151.25, bottom=135.22), True,
                  "Good morning: dip buy allowed.") ==
       "SPCX zones for today\n1. Draw a box from $147.60 to $139.82. Extend it through the whole day.\n"
       "2. Draw a line at $151.25.\n3. Draw a line at $135.22.\n✅ Good morning: dip buy allowed.\n"
       "This is your entry set-up. Do not trade against it until the next update.",
       "the 9:20 text, word for word")

    # A whole day through tick(), with a fake tape.
    with tempfile.TemporaryDirectory() as folder:
        sess = fri + [m(5 + i, 147.0, 147.6, 146.6, 147.2) for i in range(10)] + \
            [m(15 + i, 147.2, 148.3, 147.0, 148.0) for i in range(375)]

        def fake(symbol, start, end, now):
            if symbol == MARKET:
                base = 700.0 if start.date() == D0 else 697.0          # S&P down 0.43%
                return [Bar(start + timedelta(minutes=i), base, base, base, base, 1)
                        for i in range(int((min(end, now) - start).total_seconds() // 60))]
            src = prev if start.date() == D0 else (pre + sess)
            return [b for b in src if start <= b.t < min(end, now.replace(second=0))]

        log = os.path.join(folder, "log.csv")
        said = []
        for hh, mm in ((9, 0), (9, 20), (9, 32), (9, 34), (9, 45), (10, 0), (16, 5), (16, 6)):
            said += [(hh, mm, p, s) for p, s in
                     tick(datetime.combine(D1, time(hh, mm)), [], todo_text="TO DO today" if hh == 9 else None,
                          fetch=fake, folder=folder, log_path=log)]
        texts = [s for _, _, _, s in said]
        ok(texts[0].startswith("SPCX zones for today") and "⛔ S&P down before the open" in texts[0],
           f"9:20: the set-up, and no dip buy with the S&P down: {texts[:1]}")
        fl = [s for _, _, p, s in said if s.startswith("SPCX FLUSH")]
        ok(len(fl) == 1 and "Buy zone about $146.25" in fl[0] and [p for _, _, p, s in said if s.startswith("SPCX FLUSH")] == [LOUD],
           f"one loud flush alert at the up-bar: {fl}")
        sounds = {s.split("\n")[0][:18]: p for _, _, p, s in said}
        ok(sounds == {"SPCX zones for tod": SOUND, "SPCX FLUSH: down 2": LOUD,
                      "SPCX final zones f": QUIET, "SPCX debrief Fri O": QUIET},
           f"sound at 9:20 and the flush; a final set-up that keeps the NO and the debrief are quiet: {sounds}")
        p2t = [s for s in texts if s.startswith("SPCX final zones")]
        ok(len(p2t) == 1 and "⛔ S&P down before the open" in p2t[0], f"pass 2 keeps the NO: {p2t}")
        db = [s for s in texts if s.startswith("SPCX debrief")]
        ok(len(db) == 1 and "Flush: bought" in db[0] and "Day: open $147.93" in db[0], f"one debrief: {db}")
        sheet = json.load(open(os.path.join(folder, f"SPCX {D1.isoformat()}.json"), encoding="utf-8"))
        ok(all(sheet.get(k) for k in ("todo", "pass1_text", "flush_text", "pass2_text", "debrief_text"))
           and "S&P before the open" in sheet.get("context", {}),
           f"the day sheet the PDF reads has every part: {sorted(sheet)}")
        rows = list(csv.DictReader(open(log, encoding="utf-8")))
        ok(len(rows) == 1 and rows[0]["flush"] == "yes" and rows[0]["go_920"] == "False",
           f"one log row: {rows}")
        ok(tick(datetime(2026, 10, 24, 9, 20), [], fetch=fake, folder=folder) == [], "nothing on Saturday")

        class Unlock:
            day, kind = D1, "unlock"
        quiet = os.path.join(folder, "unlockday")
        said = []
        for hh, mm in ((9, 20), (9, 34), (9, 45), (16, 5)):
            said += [s for _, s in tick(datetime.combine(D1, time(hh, mm)), [Unlock()], fetch=fake,
                                        folder=quiet, log_path=os.path.join(quiet, "l.csv"))]
        ok(not any(s.startswith("SPCX FLUSH") for s in said) and "⛔ Unlock day" in said[0]
           and any("Flush: not watched (unlock day)." in s for s in said),
           f"an unlock day: no flush alert, and the debrief says why: {said}")

        def turning(symbol, start, end, now):
            # S&P up before the open, down after it: 9:20 says GO, the final set-up NO.
            if symbol != MARKET:
                return fake(symbol, start, end, now)
            base = 700.0 if start.date() == D0 else (702.0 if start.time() < OPEN else 697.0)
            return [Bar(start + timedelta(minutes=i), base, base, base, base, 1)
                    for i in range(int((min(end, now) - start).total_seconds() // 60))]
        turned = os.path.join(folder, "turned")
        said = []
        for hh, mm in ((9, 20), (9, 34), (9, 45), (10, 0)):
            said += tick(datetime.combine(D1, time(hh, mm)), [], fetch=turning,
                         folder=turned, log_path=os.path.join(turned, "l.csv"))
        p2 = [(p, s) for p, s in said if s.startswith("SPCX final zones")]
        ok(said and said[0][1].startswith("SPCX zones") and "✅" in said[0][1]
           and len(p2) == 1 and p2[0][0] == SOUND and "S&P turned down" in p2[0][1],
           f"a final set-up that takes the 9:20 GO away makes a sound: {said}")

        def plunge(symbol, start, end, now):
            if symbol == MARKET or start.date() == D0:
                return fake(symbol, start, end, now)
            src = pre + deep + [m(2 + i, 143.9, 144.2, 143.6, 144.0) for i in range(60)]
            return [b for b in src if start <= b.t < min(end, now.replace(second=0))]
        said = []
        for hh, mm in ((9, 20), (9, 33)):
            said += tick(datetime.combine(D1, time(hh, mm)), [], fetch=plunge,
                         folder=os.path.join(folder, "deep"), log_path=os.devnull)
        ok([p for p, s in said if "too deep" in s] == [QUIET], f"a too-deep flush is quiet: {said}")

        class Ev:
            day, kind = D1, "unlock"
        ok(event_today([Ev()], D1) == "Unlock" and event_today([Ev()], D0) == "", "unlock day read")

    if failures:
        print("SELF-TEST FAILED:")
        for x in failures:
            print(f"  - {x}")
        return 1
    print("day_plan self-test passed")
    return 0


def replay(day: date) -> int:
    """Run a past day minute by minute from the tape, printing every text."""
    import calendar_watch
    from feed_check import load_env
    import tempfile

    load_env()
    events = calendar_watch.all_events(day)
    cache: Dict[tuple, List[Bar]] = {}

    def fetch(symbol, start, end, now):
        key = (symbol, start.date())
        if key not in cache:
            cache[key] = tape(symbol, datetime.combine(start.date(), time(4, 0)),
                              datetime.combine(start.date(), time(20, 0)), datetime.now())
        return [b for b in cache[key] if start <= b.t < min(end, now.replace(second=0))]

    with tempfile.TemporaryDirectory() as folder:
        t = datetime.combine(day, time(9, 0))
        while t.time() <= time(16, 10):
            for prio, text in tick(t, events, fetch=fetch, folder=folder,
                                   log_path=os.path.join(folder, "log.csv")):
                print(f"--- {t:%H:%M} ({ {QUIET: 'quiet', SOUND: 'sound', LOUD: 'loud'}[prio] })\n{text}\n")
            t += timedelta(minutes=1)
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if "--self-test" in sys.argv:
        return self_test()
    if "--replay" in sys.argv:
        return replay(date.fromisoformat(sys.argv[sys.argv.index("--replay") + 1]))
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
