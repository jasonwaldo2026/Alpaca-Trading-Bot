"""
SPCX swing study: is this worth day trading, and with what bracket?

Pulls 30 trading days of 1-minute bars from Alpaca's full SIP tape (free
historically, as long as the request ends 15+ minutes ago) and answers
four questions, in the order they matter:

  1. SWINGS    How often does it move, and how far? A stock that drifts
               0.2% a day cannot pay for a 1% target, however good the
               signal is.

  2. CLOCK     When do the moves happen? If everything is over by 10:30,
               the afternoon is not worth watching.

  3. SIGNALS   After each buy signal, what did price actually do? How far
               it went the right way before going the wrong way (MFE) and
               vice versa (MAE) is what sizes a stop and a target.

  4. BRACKETS  Every stop/target pair scored on real bars -- including
               the 1%/2% you planned -- against a baseline of entering at
               random. A bracket that wins on signals but wins just as
               often on random entries means the signal added nothing.

READ-ONLY. Market-data client only, inherited from feed_check. There is
no trading client anywhere in these files.

Why it imports from feed_check
------------------------------
The MACD, VWAP, volume and buy-condition code lives in feed_check.py and
is imported, never copied. If this study measured signals computed one
way and the live alert fired on signals computed another, the numbers
here would not describe the thing you actually trade -- and nothing would
catch the drift. Keep both files in the same folder.

Usage
-----
    python swing_study.py                  # 30 days, MACD 9,17,6
    python swing_study.py --days 60
    python swing_study.py --macd 12,26,9
    python swing_study.py --symbol AAPL
    python swing_study.py --self-test      # verify the math, no network

Setup
-----
    pip install alpaca-py pandas python-dotenv
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Sequence, Tuple

import pandas as pd

import lockups
from feed_check import (
    CONDITIONS,
    DEFAULT_MACD,
    ET,
    SESSION_OPEN,
    Macd,
    add_conditions,
    fetch_extended,
    parse_clock,
    prepare,
    trading_days,
)

# Swing sizes to report. One threshold would hide the answer: a 0.25%
# zigzag finds noise on a trending day, a 1% zigzag finds nothing on a
# quiet one. Three tells you which scale this stock actually moves at.
SWING_THRESHOLDS_PCT = (0.25, 0.5, 1.0)

# Stop and target grids for the bracket search, in percent.
STOP_GRID = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0)
TARGET_GRID = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)

# Minutes after entry at which to record where price got to.
HORIZONS_MIN = (15, 30, 60)

# An edge smaller than this is noise, not a strategy. A round trip on a
# $155 stock costs roughly a cent or two of spread even commission-free,
# so an edge of a few thousandths of a percent per trade buys nothing.
MIN_EDGE_PCT = 0.05

# The baseline samples every Nth bar rather than all 390. Entering at
# every minute of 30 days is ~11,700 trades per grid cell, which is slow
# and no more informative than a fifth of them.
BASELINE_STRIDE = 5

# Candidate trading windows, for the time-of-day section. SPCX moves
# roughly three times as much per bar at the open as it does at 14:00,
# while the signal fires at a flat rate all day -- so most alerts arrive
# when there is least to act on. These ask whether that is worth gating.
GATE_WINDOWS = (
    ("09:40-11:00", ((time(9, 40), time(11, 0)),)),
    ("09:40-10:30", ((time(9, 40), time(10, 30)),)),
    ("15:00-16:00", ((time(15, 0), time(16, 0)),)),
    ("09:40-11:00 + 15:00-16:00", ((time(9, 40), time(11, 0)),
                                   (time(15, 0), time(16, 0)))),
    ("all day (now)", ((time(0, 0), time(23, 59)),)),
)

# --------------------------------------------------------------------------
# The pre-registered VWAP hypothesis
# --------------------------------------------------------------------------
# Written down on 24 September 2026, BEFORE being tested on anything but
# the sample that produced it. In the 12 Jun - 22 Sep window, morning
# signals firing above VWAP showed a best-case/worst-case ratio of 1.67
# and finished higher an hour later 58% of the time, against 0.79 and 48%
# for those below. That is one of four cells examined, on 108
# observations, about 1.7 standard errors from a coin flip -- the same
# shape as a 54% figure that had already fooled this project once.
#
# So the rule is fixed here and not adjusted afterwards. A day on or
# after HYPOTHESIS_FROM is in-sample: the idea was found there and it is
# expected to look good, which proves nothing. A day before it has never
# been examined, and is the only place the question can actually be
# answered. Run with --days large enough to reach back past the cutoff.
HYPOTHESIS_FROM = date(2026, 6, 12)
HYPOTHESIS_WINDOW = (time(9, 40), time(11, 0))
HYPOTHESIS_BRACKET = (0.75, 2.00)

# The gate sections score ONE bracket, fixed in advance, rather than
# searching the grid again inside each window. Searching 36 cells per
# window would hand back the best of 180 tries and call it a finding --
# and with this many windows something always looks good. This is the
# bracket Jason planned to trade, scored the same way every time.
GATE_BRACKET = (1.0, 2.0)


# --------------------------------------------------------------------------
# Swings
# --------------------------------------------------------------------------

def find_swings(high: Sequence[float], low: Sequence[float], pct: float):
    """Percentage zigzag: the alternating highs and lows of a session.

    A high is only confirmed once price has fallen `pct` from it, so a
    pivot is never declared from information the moment itself did not
    have. Returns [(index, price, "H" | "L")], oldest first.
    """
    n = len(high)
    if n < 2:
        return []
    threshold = pct / 100.0
    pivots: List[Tuple[int, float, str]] = []
    trend = 0                      # +1 = seeking a high, -1 = seeking a low
    hi_i, hi_p = 0, high[0]
    lo_i, lo_p = 0, low[0]

    for i in range(1, n):
        if high[i] > hi_p:
            hi_i, hi_p = i, high[i]
        if low[i] < lo_p:
            lo_i, lo_p = i, low[i]

        if trend >= 0 and low[i] <= hi_p * (1 - threshold):
            pivots.append((hi_i, hi_p, "H"))
            trend = -1
            lo_i, lo_p = i, low[i]
        elif trend <= 0 and high[i] >= lo_p * (1 + threshold):
            pivots.append((lo_i, lo_p, "L"))
            trend = 1
            hi_i, hi_p = i, high[i]

    return pivots


def swing_legs(pivots) -> Tuple[List[float], List[float]]:
    """Run-ups (low to high) and pullbacks (high to low), each in percent."""
    run_ups, pullbacks = [], []
    for (_, p_from, kind), (_, p_to, _) in zip(pivots, pivots[1:]):
        move = 100.0 * (p_to - p_from) / p_from
        (run_ups if kind == "L" else pullbacks).append(abs(move))
    return run_ups, pullbacks


# --------------------------------------------------------------------------
# Bracket simulation
# --------------------------------------------------------------------------

@dataclass
class Trade:
    entry_index: int
    entry_price: float
    outcome: str        # "target", "stop" or "time"
    return_pct: float
    bars_held: int


def simulate(session: pd.DataFrame, entries: Sequence[int],
             stop_pct: float, target_pct: float) -> List[Trade]:
    """Run one bracket over one session.

    A signal on bar i fills at bar i+1's OPEN -- never at the close of the
    bar that produced it, which would be trading on information the moment
    did not have.

    When a single bar touches both the stop and the target, the stop is
    taken. A 1-minute bar records a high and a low but not their order, so
    the outcome is genuinely unknown; assuming the worse of the two keeps
    the result pessimistic, and a backtest that flatters itself is worse
    than useless.

    Anything still open at 16:00 is closed there. This is day trading; no
    position is carried overnight.
    """
    opens = session["open"].to_numpy()
    highs = session["high"].to_numpy()
    lows = session["low"].to_numpy()
    closes = session["close"].to_numpy()
    n = len(session)
    trades: List[Trade] = []

    for i in entries:
        fill = i + 1
        if fill >= n:
            continue
        entry = float(opens[fill])
        stop = entry * (1 - stop_pct / 100.0)
        target = entry * (1 + target_pct / 100.0)

        outcome, exit_price, held = "time", float(closes[n - 1]), n - 1 - fill
        for j in range(fill, n):
            if lows[j] <= stop:
                outcome, exit_price, held = "stop", stop, j - fill
                break
            if highs[j] >= target:
                outcome, exit_price, held = "target", target, j - fill
                break

        trades.append(Trade(
            entry_index=i,
            entry_price=entry,
            outcome=outcome,
            return_pct=100.0 * (exit_price - entry) / entry,
            bars_held=held,
        ))
    return trades


def score(trades: Sequence[Trade]) -> Dict[str, float]:
    """Win rate and average return per trade -- the number that matters."""
    if not trades:
        return {"n": 0, "win_pct": 0.0, "avg_return": 0.0,
                "target_pct": 0.0, "stop_pct": 0.0, "median_bars": 0.0}
    returns = [t.return_pct for t in trades]
    return {
        "n": len(trades),
        "win_pct": 100.0 * sum(r > 0 for r in returns) / len(returns),
        "avg_return": sum(returns) / len(returns),
        "target_pct": 100.0 * sum(t.outcome == "target" for t in trades) / len(trades),
        "stop_pct": 100.0 * sum(t.outcome == "stop" for t in trades) / len(trades),
        "median_bars": float(pd.Series([t.bars_held for t in trades]).median()),
    }


# --------------------------------------------------------------------------
# Signal outcomes
# --------------------------------------------------------------------------

def signal_outcomes(session: pd.DataFrame, entries: Sequence[int]) -> List[dict]:
    """For each signal: how far price went each way, and where it ended up.

    MFE is the best price reached before the trade would have been closed;
    MAE the worst. They are what a stop and a target should be sized
    from -- a target beyond the typical MFE rarely fills, and a stop
    inside the typical MAE is hit on trades that would have worked.
    """
    opens = session["open"].to_numpy()
    highs = session["high"].to_numpy()
    lows = session["low"].to_numpy()
    closes = session["close"].to_numpy()
    index = session.index
    n = len(session)
    rows = []

    for i in entries:
        fill = i + 1
        if fill >= n:
            continue
        entry = float(opens[fill])
        def at(column, default=float("nan")):
            """Context for the CSV. Absent on a bare OHLCV frame, which the
            outcome maths below does not need."""
            return float(session[column].iloc[i]) if column in session else default

        row = {
            "time": index[i],
            "entry_time": index[fill],
            "entry": entry,
            "macd": at("macd"),
            "volume_ratio": at("volume_ratio"),
            "above_vwap": (bool(session["close"].iloc[i] > session["vwap"].iloc[i])
                           if "vwap" in session else None),
        }
        for minutes in HORIZONS_MIN:
            end = min(fill + minutes, n - 1)
            window_hi = float(highs[fill:end + 1].max())
            window_lo = float(lows[fill:end + 1].min())
            row[f"mfe_{minutes}"] = 100.0 * (window_hi - entry) / entry
            row[f"mae_{minutes}"] = 100.0 * (window_lo - entry) / entry
            row[f"ret_{minutes}"] = 100.0 * (float(closes[end]) - entry) / entry
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def pct(values, q: float) -> float:
    return float(pd.Series(values).quantile(q)) if len(values) else float("nan")


def report(symbol: str, macd: Macd, sessions: Dict[date, pd.DataFrame],
           setup: str = "") -> pd.DataFrame:
    rule = "=" * 76
    days = sorted(sessions)
    total_bars = sum(len(s) for s in sessions.values())
    print(f"\n{rule}\n  {symbol}  swing study  --  {len(days)} sessions, "
          f"{days[0]:%Y-%m-%d} to {days[-1]:%Y-%m-%d}, MACD {macd}\n{rule}")
    if setup:
        print(f"  {setup}")
    print(f"\n  {total_bars:,} one-minute bars "
          f"({total_bars / len(days):.0f} per session out of 390)")

    # ---- 1. swings ------------------------------------------------------
    print("\n1. SWINGS -- how often it moves, and how far\n")
    print(f"   {'Threshold':<12}{'Swings/day':>12}{'Run-up: med':>14}{'90th':>9}"
          f"{'Pullback: med':>16}{'90th':>9}")
    for threshold in SWING_THRESHOLDS_PCT:
        ups, downs, per_day = [], [], []
        for session in sessions.values():
            pivots = find_swings(session["high"].to_numpy(),
                                 session["low"].to_numpy(), threshold)
            u, d = swing_legs(pivots)
            ups += u
            downs += d
            per_day.append(len(pivots))
        print(f"   {threshold:>5.2f}%     {sum(per_day) / len(per_day):>11.1f}"
              f"{pct(ups, 0.5):>13.2f}%{pct(ups, 0.9):>8.2f}%"
              f"{pct(downs, 0.5):>15.2f}%{pct(downs, 0.9):>8.2f}%")
    print("\n   A swing is only counted once price has retraced the threshold,")
    print("   so nothing here is measured with hindsight the moment lacked.")

    # ---- 2. clock -------------------------------------------------------
    print("\n2. CLOCK -- when the moves and the signals happen\n")
    buckets: Dict[str, Dict[str, float]] = {}
    for session in sessions.values():
        pivots = find_swings(session["high"].to_numpy(), session["low"].to_numpy(), 0.5)
        for i, _, kind in pivots:
            key = f"{session.index[i]:%H}:{'00' if session.index[i].minute < 30 else '30'}"
            buckets.setdefault(key, {"swings": 0, "signals": 0, "range": []})["swings"] += 1
        for i in range(len(session)):
            ts = session.index[i]
            key = f"{ts:%H}:{'00' if ts.minute < 30 else '30'}"
            slot = buckets.setdefault(key, {"swings": 0, "signals": 0, "range": []})
            slot["range"].append(100.0 * (session["high"].iloc[i] - session["low"].iloc[i])
                                 / session["close"].iloc[i])
            if bool(session["alert"].iloc[i]):
                slot["signals"] += 1

    print(f"   {'Half hour':<12}{'Swings':>9}{'Signals':>10}{'Avg bar range':>16}")
    for key in sorted(buckets):
        slot = buckets[key]
        avg_range = sum(slot["range"]) / len(slot["range"]) if slot["range"] else 0.0
        bar = "#" * int(avg_range * 200)
        print(f"   {key:<12}{slot['swings']:>9}{slot['signals']:>10}"
              f"{avg_range:>15.3f}%  {bar}")

    # ---- 3. signals -----------------------------------------------------
    all_rows: List[dict] = []
    signals_per_day = []
    for day, session in sessions.items():
        entries = [i for i in range(len(session)) if bool(session["alert"].iloc[i])]
        signals_per_day.append(len(entries))
        for row in signal_outcomes(session, entries):
            row["date"] = day
            all_rows.append(row)

    outcomes = pd.DataFrame(all_rows)
    print(f"\n3. SIGNALS -- {len(outcomes)} of them, "
          f"{sum(signals_per_day) / len(sessions):.1f} per session\n")

    if outcomes.empty:
        print("   No signals fired. Nothing to measure.")
    else:
        print(f"   {'Horizon':<10}{'Median MFE':>13}{'Median MAE':>13}"
              f"{'Median move':>14}{'Higher after':>15}")
        for minutes in HORIZONS_MIN:
            higher = 100.0 * (outcomes[f"ret_{minutes}"] > 0).mean()
            print(f"   +{minutes:<9}{outcomes[f'mfe_{minutes}'].median():>12.2f}%"
                  f"{outcomes[f'mae_{minutes}'].median():>12.2f}%"
                  f"{outcomes[f'ret_{minutes}'].median():>13.2f}%{higher:>14.0f}%")
        print("\n   MFE is how far it went your way before you would have been out;")
        print("   MAE how far against. A target past the median MFE rarely fills;")
        print("   a stop inside the median MAE is hit on trades that would work.")
        print("\n   'Higher after' at 50% is a coin flip. Meaningfully above 50%")
        print("   is the signal earning its keep.")

    # ---- 4. brackets ----------------------------------------------------
    print("\n4. BRACKETS -- every stop/target pair on real bars\n")

    def grid(entry_picker, label: str):
        print(f"   {label}")
        header = "stop vs target"
        print(f"   {header:<14}" + "".join(f"{t:>10.2f}%" for t in TARGET_GRID))
        best = None
        for stop_pct in STOP_GRID:
            cells = []
            for target_pct in TARGET_GRID:
                trades = []
                for session in sessions.values():
                    trades += simulate(session, entry_picker(session), stop_pct, target_pct)
                s = score(trades)
                cells.append(s["avg_return"])
                if s["n"] >= 20 and (best is None or s["avg_return"] > best[0]):
                    best = (s["avg_return"], stop_pct, target_pct, s)
            print(f"   {stop_pct:>6.2f}%       " + "".join(f"{c:>10.3f}" for c in cells))
        return best

    def signal_entries(session):
        return [i for i in range(len(session)) if bool(session["alert"].iloc[i])]

    def baseline_entries(session):
        return list(range(0, len(session) - 1, BASELINE_STRIDE))

    print("   Average % return per trade. Entry fills at the NEXT bar's open;")
    print("   when one bar touches both levels the stop is taken; anything")
    print("   still open at 16:00 is closed there.\n")

    best_signal = grid(signal_entries, "ON SIGNALS")
    print()
    best_base = grid(baseline_entries,
                     f"BASELINE -- entering every {BASELINE_STRIDE}th bar regardless")

    print(f"\n{rule}\n  VERDICT")
    if best_signal is None:
        print("  Too few signals to score a bracket. Try more days.")
    else:
        avg, stop_pct_best, target_pct_best, s = best_signal
        timed_out = 100.0 - s["target_pct"] - s["stop_pct"]
        print(f"  Best bracket on signals : {stop_pct_best:.2f}% stop / "
              f"{target_pct_best:.2f}% target")
        print(f"    {s['n']} trades, {s['win_pct']:.0f}% winners, "
              f"{avg:+.3f}% average, {s['median_bars']:.0f} bars held")
        print(f"    {s['target_pct']:.0f}% hit the target, {s['stop_pct']:.0f}% stopped, "
              f"{timed_out:.0f}% closed at 16:00")

        if timed_out > 50:
            print("\n  WARNING: most trades never reached either level, so this grid")
            print("  is mostly measuring how the stock drifted over the day rather")
            print("  than how the bracket performed. Either the levels are too wide")
            print("  for this stock's daily range, or it does not move enough to")
            print("  trade this way. Look at section 1 before trusting any cell.")

        if best_base:
            edge = avg - best_base[0]
            print(f"\n  Best bracket on random entries: {best_base[0]:+.3f}% average")
            print(f"  Signal edge: {edge:+.3f}% per trade")
            if edge <= 0:
                print("  -> The signal did NOT beat entering at random. Do not build")
                print("     the alert on these conditions; they are not finding")
                print("     anything a coin flip would miss.")
            elif edge < MIN_EDGE_PCT:
                print(f"  -> The edge is under {MIN_EDGE_PCT}% a trade, which is inside")
                print("     the bid-ask spread. That is noise, not a strategy: the")
                print("     signal is picking entries no better than a coin flip.")
            elif avg <= 0:
                print("  -> The signal beats random but still loses money. The")
                print("     conditions have some information; the bracket does not")
                print("     harvest it. Costs would make this worse.")
            else:
                print("  -> The signal beats random AND makes money before costs.")
                print("     Worth building. Check the edge survives commissions and")
                print("     the bid-ask spread before trading it.")
    print(rule)

    # Your planned bracket, scored explicitly, whatever the grid best was.
    if not outcomes.empty:
        trades = []
        for session in sessions.values():
            trades += simulate(session, signal_entries(session), 1.0, 2.0)
        s = score(trades)
        print("\n  Your planned 1% stop / 2% target, on signals:")
        print(f"    {s['n']} trades, {s['win_pct']:.0f}% winners, "
              f"{s['avg_return']:+.3f}% average per trade")
        print(f"    {s['target_pct']:.0f}% hit the target, {s['stop_pct']:.0f}% were "
              f"stopped, {100 - s['target_pct'] - s['stop_pct']:.0f}% closed at 16:00\n")

    # ---- 5. is a signal worth more at some times than others? ----------
    if not outcomes.empty:
        print("\n5. SIGNAL QUALITY BY TIME OF DAY\n")
        print("   Section 2 counts signals. This one asks whether they were any")
        print("   good -- the same measurements as section 3, cut by half hour.\n")
        print(f"   {'Half hour':<12}{'Signals':>9}{'Med MFE':>10}{'Med MAE':>10}"
              f"{'MFE/MAE':>10}{'Higher 60m':>13}")
        stamped = outcomes.copy()
        stamped["slot"] = [f"{t:%H}:{'00' if t.minute < 30 else '30'}"
                           for t in stamped["time"]]
        for slot in sorted(stamped["slot"].unique()):
            part = stamped[stamped["slot"] == slot]
            mfe = part["mfe_60"].median()
            mae = part["mae_60"].median()
            ratio = abs(mfe / mae) if mae else float("nan")
            higher = 100.0 * (part["ret_60"] > 0).mean()
            print(f"   {slot:<12}{len(part):>9}{mfe:>9.2f}%{mae:>9.2f}%"
                  f"{ratio:>10.2f}{higher:>12.0f}%")
        print("\n   MFE/MAE above 1.00 means the typical signal went further your")
        print("   way than against it. At 1.00 the two are the same size, which")
        print("   is what a random walk looks like and what no bracket can fix.")

    # ---- 6. what a time-of-day gate would actually buy -------------------
    print("\n6. A TIME-OF-DAY GATE\n")
    stop_gate, target_gate = GATE_BRACKET
    print(f"   One bracket, {stop_gate:.2f}% stop / {target_gate:.2f}% target, fixed")
    print("   in advance and scored identically in every window -- not a fresh")
    print("   search per window, which would find a winner by trying enough.\n")
    print("   The control matters most here. Restricting to the busiest hours")
    print("   raises returns on its own, because there is more movement to")
    print("   catch; so random entry is restricted to the SAME hours. The last")
    print("   column is the only one that says whether the SIGNAL improved.\n")

    def inside(stamp, spans) -> bool:
        return any(lo <= stamp.time() < hi for lo, hi in spans)

    total_swings = 0
    total_range = 0.0
    swing_slots: List[datetime] = []
    range_rows: List[Tuple[datetime, float]] = []
    for session in sessions.values():
        pivots = find_swings(session["high"].to_numpy(),
                             session["low"].to_numpy(), 0.5)
        for i, _, _ in pivots:
            swing_slots.append(session.index[i])
        for i in range(len(session)):
            span = 100.0 * (session["high"].iloc[i] - session["low"].iloc[i]) \
                / session["close"].iloc[i]
            range_rows.append((session.index[i], span))
            total_range += span
    total_swings = len(swing_slots)

    print(f"   {'Window':<28}{'Signals':>9}{'Swings':>9}{'Movement':>11}"
          f"{'Signal':>9}{'Random':>9}{'Edge':>9}")
    for label, spans in GATE_WINDOWS:
        signal_trades, random_trades = [], []
        for session in sessions.values():
            picks = [i for i in range(len(session))
                     if bool(session["alert"].iloc[i])
                     and inside(session.index[i], spans)]
            signal_trades += simulate(session, picks, stop_gate, target_gate)
            rolls = [i for i in range(0, len(session) - 1, BASELINE_STRIDE)
                     if inside(session.index[i], spans)]
            random_trades += simulate(session, rolls, stop_gate, target_gate)

        sig, rnd = score(signal_trades), score(random_trades)
        kept_swings = sum(1 for stamp in swing_slots if inside(stamp, spans))
        kept_range = sum(span for stamp, span in range_rows if inside(stamp, spans))
        swing_share = (f"{100.0 * kept_swings / total_swings:>7.0f}%"
                       if total_swings else f"{'--':>8}")
        range_share = (f"{100.0 * kept_range / total_range:>9.0f}%"
                       if total_range else f"{'--':>10}")
        # A handful of trades is not a measurement. Say so rather than
        # printing three decimals that invite reading a pattern into six
        # coin flips -- the whole point of this section is to resist that.
        if sig["n"] < 20 or rnd["n"] < 20:
            numbers = f"{'--':>9}{'--':>9}{'too few':>9}"
        else:
            numbers = (f"{sig['avg_return']:>9.3f}{rnd['avg_return']:>9.3f}"
                       f"{sig['avg_return'] - rnd['avg_return']:>9.3f}")
        print(f"   {label:<28}{sig['n']:>9}{swing_share}{range_share}{numbers}")

    print("\n   Signals / Swings / Movement are shares of the whole session kept")
    print("   by the gate. Signal and Random are average % per trade inside the")
    print("   window; Edge is the difference. An edge that is still near zero")
    print("   in every row means the gate cut the noise without finding an")
    print("   edge underneath -- fewer interruptions, not a better entry.")

    # ---- 7. the pre-registered VWAP test --------------------------------
    print("\n7. THE VWAP HYPOTHESIS, TESTED THE ONLY WAY THAT COUNTS\n")
    lo_h, hi_h = HYPOTHESIS_WINDOW
    stop_h, target_h = HYPOTHESIS_BRACKET
    print(f"   Rule, fixed before this ran: a signal between {lo_h:%H:%M} and")
    print(f"   {hi_h:%H:%M} with price above VWAP. {stop_h:.2f}% stop, "
          f"{target_h:.2f}% target.")
    print(f"   Days from {HYPOTHESIS_FROM:%d %b %Y} are where the idea came "
          f"from, so they")
    print("   are expected to look good and prove nothing. Days before it have")
    print("   never been examined. Only that row is evidence.\n")

    def vwap_picks(session, signals_only: bool):
        if "vwap" not in session:
            return []
        picks = []
        for i in range(len(session)):
            stamp = session.index[i]
            if not (lo_h <= stamp.time() < hi_h):
                continue
            close, vwap = session["close"].iloc[i], session["vwap"].iloc[i]
            if not (vwap == vwap and close > vwap):     # NaN-safe
                continue
            if signals_only:
                if bool(session["alert"].iloc[i]):
                    picks.append(i)
            elif i % BASELINE_STRIDE == 0:
                picks.append(i)
        return picks

    groups = {
        "before the sample (never looked at)": [d for d in days if d < HYPOTHESIS_FROM],
        f"from {HYPOTHESIS_FROM:%d %b} (where it came from)":
            [d for d in days if d >= HYPOTHESIS_FROM],
    }
    print(f"   {'Days':<38}{'n':>6}{'Signal':>9}{'Random':>9}{'Edge':>9}")
    for label, group in groups.items():
        sig_trades, rnd_trades = [], []
        for day in group:
            session = sessions[day]
            sig_trades += simulate(session, vwap_picks(session, True),
                                   stop_h, target_h)
            rnd_trades += simulate(session, vwap_picks(session, False),
                                   stop_h, target_h)
        sig, rnd = score(sig_trades), score(rnd_trades)
        if sig["n"] < 20 or rnd["n"] < 20:
            body = f"{sig['n']:>6}{'--':>9}{'--':>9}{'too few':>9}"
        else:
            body = (f"{sig['n']:>6}{sig['avg_return']:>9.3f}"
                    f"{rnd['avg_return']:>9.3f}"
                    f"{sig['avg_return'] - rnd['avg_return']:>9.3f}")
        print(f"   {label:<38}{body}")

    out_of_sample = [d for d in days if d < HYPOTHESIS_FROM]
    if not out_of_sample:
        print(f"\n   No days before {HYPOTHESIS_FROM:%d %b %Y} were fetched, so the")
        print("   test has not actually been run. Increase --days until the first")
        print("   session listed above is earlier than the cutoff; anything else")
        print("   is the idea grading its own homework.")
    else:
        print(f"\n   {len(out_of_sample)} unexamined sessions. An edge under "
              f"{MIN_EDGE_PCT}% is inside")
        print("   the spread and counts as zero however it is signed.")

    # ---- 8. what actually happened on unlock days -----------------------
    dated = [u for u in lockups.for_symbol(symbol) if u.day and u.day in sessions]
    if dated:
        print("\n8. UNLOCK DAYS\n")
        print(f"   {len(dated)} dated unlock(s) fall inside this window. This is what")
        print("   happened on them. It is not what happens on them: a handful of")
        print("   events is an anecdote, and the honest use of this table is to")
        print("   see whether the days were remarkable at all, not to forecast")
        print("   the next one.\n")

        def day_stats(session):
            first, last = session.iloc[0], session.iloc[-1]
            move = 100.0 * (last["close"] - first["open"]) / first["open"]
            span = 100.0 * (session["high"].max() - session["low"].min()) / first["open"]
            return move, span, float(session["volume"].sum())

        stats = {day: day_stats(session) for day, session in sessions.items()}
        volumes = sorted(v for _, _, v in stats.values())
        typical = volumes[len(volumes) // 2] if volumes else 0.0

        print(f"   {'Date':<12}{'Shares':>10}{'Day move':>11}{'Range':>9}"
              f"{'Volume':>11}{'x typical':>11}")
        for unlock in dated:
            move, span, volume = stats[unlock.day]
            ratio = volume / typical if typical else float("nan")
            print(f"   {unlock.day:%d %b %Y}{unlock.size().replace(' shares', ''):>10}"
                  f"{move:>10.2f}%{span:>8.2f}%"
                  f"{volume / 1_000_000:>10.1f}M{ratio:>11.2f}")

        others = [(m, r, v) for day, (m, r, v) in stats.items()
                  if day not in {u.day for u in dated}]
        if others:
            moves = sorted(m for m, _, _ in others)
            spans = sorted(r for _, r, _ in others)
            mid = len(moves) // 2
            print(f"\n   Every other session here, for comparison ({len(others)} days):")
            print(f"   {'median':<12}{'':>10}{moves[mid]:>10.2f}%{spans[mid]:>8.2f}%")
            down = sum(1 for m in moves if m < 0)
            print(f"   {down} of {len(moves)} were down days "
                  f"({100.0 * down / len(moves):.0f}%), so a fall on any given")
            print("   day is not itself evidence of anything.")
        print("\n   Dates are UNCONFIRMED unless lockups.json says otherwise.")

    return outcomes


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------

def _session(closes, highs=None, lows=None, volumes=None, first=SESSION_OPEN):
    n = len(closes)
    start = datetime.combine(date(2026, 9, 18), first, tzinfo=ET)
    idx = pd.DatetimeIndex([start + timedelta(minutes=i) for i in range(n)])
    return pd.DataFrame(
        {"open": closes,
         "high": highs if highs is not None else [c + 0.02 for c in closes],
         "low": lows if lows is not None else [c - 0.02 for c in closes],
         "close": closes,
         "volume": volumes if volumes is not None else [1000] * n},
        index=idx,
    )


def self_test() -> int:
    print("Self-test: checking the swing, bracket and outcome logic...\n")
    failures = []

    # A clean triangle: up 10%, down 10%. One high, one low.
    closes = [100 + i for i in range(11)] + [110 - i for i in range(1, 11)]
    pivots = find_swings([c + 0.01 for c in closes], [c - 0.01 for c in closes], 1.0)
    kinds = [k for _, _, k in pivots]
    # The path starts at 100 and rises, so 100 is itself a swing low -- the
    # anchor the run-up is measured from -- and the peak follows it.
    if kinds != ["L", "H"]:
        failures.append(f"expected a low then a high on an up-then-down path, got {kinds}")
    highs_found = [p for _, p, k in pivots if k == "H"]
    if not highs_found or abs(highs_found[0] - 110.01) > 0.05:
        failures.append(f"the high should be ~110, got {highs_found}")
    run_ups, pullbacks = swing_legs(pivots)
    if not run_ups or abs(run_ups[0] - 10.0) > 0.2:
        failures.append(f"the run-up should measure ~10%, got {run_ups}")

    # Noise below the threshold must produce no pivots at all.
    flat = [100 + (0.05 if i % 2 else -0.05) for i in range(60)]
    if find_swings([c + 0.01 for c in flat], [c - 0.01 for c in flat], 1.0):
        failures.append("0.1% noise should not register as a 1% swing")

    # Bracket: a bar that touches both levels must be recorded as a stop.
    both = _session([100.0, 100.0, 100.0], highs=[100.0, 100.0, 103.0],
                    lows=[100.0, 100.0, 98.0])
    trades = simulate(both, [0], stop_pct=1.0, target_pct=2.0)
    if not trades or trades[0].outcome != "stop":
        failures.append(f"a bar touching both levels must be a stop, got "
                        f"{trades[0].outcome if trades else 'nothing'}")

    # Entry fills at the NEXT bar's open, never the signal bar's close.
    gap = _session([100.0, 105.0, 105.0])
    t = simulate(gap, [0], stop_pct=50.0, target_pct=50.0)[0]
    if abs(t.entry_price - 105.0) > 1e-9:
        failures.append(f"entry should fill at the next open (105), got {t.entry_price}")

    # A clean winner and a clean loser.
    up = _session([100.0] + [100.0 + i for i in range(1, 10)],
                  highs=[100.0] + [100.4 + i for i in range(1, 10)])
    t = simulate(up, [0], stop_pct=1.0, target_pct=2.0)[0]
    if t.outcome != "target" or abs(t.return_pct - 2.0) > 1e-6:
        failures.append(f"a clean rally should hit the target for +2%, got "
                        f"{t.outcome} {t.return_pct:.3f}%")

    down = _session([100.0] + [100.0 - i for i in range(1, 10)],
                    lows=[100.0] + [99.6 - i for i in range(1, 10)])
    t = simulate(down, [0], stop_pct=1.0, target_pct=2.0)[0]
    if t.outcome != "stop" or abs(t.return_pct + 1.0) > 1e-6:
        failures.append(f"a clean decline should stop out for -1%, got "
                        f"{t.outcome} {t.return_pct:.3f}%")

    # Never exiting means closing at 16:00, not running forever.
    quiet = _session([100.0] * 30)
    t = simulate(quiet, [0], stop_pct=5.0, target_pct=5.0)[0]
    if t.outcome != "time":
        failures.append(f"an unresolved trade should close at the bell, got {t.outcome}")

    # MFE and MAE must bracket the realised move.
    rows = signal_outcomes(up, [0])
    if rows:
        r = rows[0]
        if not (r["mae_15"] <= r["ret_15"] <= r["mfe_15"]):
            failures.append("MAE <= return <= MFE was violated")

    # score() on nothing must not divide by zero.
    if score([])["n"] != 0:
        failures.append("score([]) should report zero trades")

    print(f"  Triangle pivots found          : {kinds}")
    print("  Sub-threshold noise pivots     : 0 (expected)")
    print("  Bar touching both levels       : stop (pessimistic, as specified)")
    print("  Entry fill price               : next bar's open")
    print("  Unresolved trade               : closed at 16:00")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll checks passed. The math is sound; now run it against real data.")
    return 0


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Measure SPCX's swings and brackets.")
    parser.add_argument("--symbol", default="SPCX")
    parser.add_argument("--days", type=int, default=30, help="Trading days (default 30)")
    parser.add_argument("--date", help="End on this day, YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--macd", help="fast,slow,signal (default 9,17,6)")
    parser.add_argument("--csv", default=None)
    parser.add_argument("--no-volume", action="store_true",
                        help="Drop condition (d), the volume test")
    parser.add_argument("--from", dest="earliest", default="09:45",
                        help="Earliest signal time, ET (default 09:45; 09:30 is the open)")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    macd = Macd.parse(args.macd) if args.macd else DEFAULT_MACD
    end = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
           else date.today() - timedelta(days=1))
    days = trading_days(end, max(1, args.days))
    symbol = args.symbol.upper()
    require_volume = not args.no_volume
    earliest = parse_clock(args.earliest)
    active = [CONDITIONS[c] for c in CONDITIONS if c != "cond_d_volume" or require_volume]
    print("Conditions: " + ", ".join(active).replace("(e) after 09:45",
                                                     f"(e) after {earliest:%H:%M}"))

    print(f"Fetching {symbol} 1-minute bars for {len(days)} trading days from SIP...")
    sessions: Dict[date, pd.DataFrame] = {}
    for n, day in enumerate(days, 1):
        try:
            raw = fetch_extended(symbol, day, "sip")
        except Exception as exc:  # noqa: BLE001 -- the message is the point
            print(f"  {day:%Y-%m-%d}  failed: {type(exc).__name__}: {exc}")
            continue
        if raw.empty:
            print(f"  {day:%Y-%m-%d}  no data (holiday?)")
            continue
        session = add_conditions(prepare(raw, macd), require_volume, earliest)
        if session.empty:
            print(f"  {day:%Y-%m-%d}  no regular-hours bars")
            continue
        sessions[day] = session
        if n % 5 == 0 or n == len(days):
            print(f"  {n}/{len(days)} days fetched...")

    if not sessions:
        print(f"\nNo usable data for {symbol}. Try --symbol AAPL to check the setup.")
        return 1

    setup = ("volume condition OFF" if not require_volume else "volume condition on")
    setup += f" · signals from {earliest:%H:%M}"
    outcomes = report(symbol, macd, sessions, setup)

    if not outcomes.empty:
        path = args.csv or f"{symbol}_signals_{days[0]:%Y%m%d}_{days[-1]:%Y%m%d}.csv"
        outcomes.to_csv(path, index=False)
        print(f"Per-signal outcomes written to {path}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
